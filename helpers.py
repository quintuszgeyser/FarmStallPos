"""
Shared utilities — imported by app.py and (eventually) blueprints.
Import order: helpers → models → db. Never import from app.py here.
"""

import os
import uuid
import random
from decimal import Decimal
from datetime import datetime, timedelta

from flask import session
from werkzeug.security import generate_password_hash

from models import (
    db,
    User, UserSession, Setting,
    Product, ProductImage, RecipeLine,
    StockBatch, StockConsumption,
    Sale,
    SESSION_TIMEOUT_MINUTES, SESSION_LOGOUT_HOURS,
)


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    s = Setting.query.filter_by(key=key).first()
    return s.value if s else default


def set_setting(key, value):
    s = Setting.query.filter_by(key=key).first()
    if s:
        s.value = str(value)
    else:
        s = Setting(key=key, value=str(value))
        db.session.add(s)
    db.session.commit()


# ---------------------------------------------------------------------------
# Auth helpers — no dependency on the Flask app object
# ---------------------------------------------------------------------------

def current_user():
    if 'user_id' not in session:
        return None
    return db.session.get(User, session.get('user_id'))


def require_login():
    if 'user_id' not in session:
        return False
    user = db.session.get(User, session['user_id'])
    if not user or not user.active:
        session.clear()
        return False
    sid = session.get('session_id')
    if sid:
        sess = db.session.get(UserSession, sid)
        if sess and sess.logged_out is None:
            last = sess.last_active or sess.logged_in
            if last < datetime.utcnow() - timedelta(hours=SESSION_LOGOUT_HOURS):
                sess.logged_out = last
                db.session.commit()
                session.clear()
                return False
    return True


def require_role(*roles):
    u = current_user()
    return bool(u and u.has_role(*roles))


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def seed_first_admin():
    if User.query.count() == 0:
        admin_user = os.getenv('ADMIN_USER', 'admin')
        admin_pass = os.getenv('ADMIN_PASS', 'admin123')
        hashed = generate_password_hash(admin_pass)
        db.session.add(User(username=admin_user, password_hash=hashed, role='admin', active=True))
        db.session.commit()
        default_markup = os.getenv('DEFAULT_MARKUP_PERCENT')
        if default_markup:
            try:
                set_setting('markup_percent', float(default_markup))
            except Exception:
                pass
    if not User.query.filter_by(username='Online Shop').first():
        db.session.add(User(
            username='Online Shop',
            password_hash='!',
            role='teller',
            active=False,
        ))
        db.session.commit()


def get_online_user_id():
    u = User.query.filter_by(username='Online Shop').first()
    return u.id if u else None


# ---------------------------------------------------------------------------
# FIFO inventory helpers
# ---------------------------------------------------------------------------

def consume_fifo(ingredient_id, qty_needed_base, sale_id, now, _depth=0):
    """
    Consume qty_needed_base units of ingredient_id from FIFO batches.
    Recursive for compound ingredients (recipe within recipe).
    Returns total COGS as Decimal. Never raises — consumes what's available.
    """
    if _depth > 10:
        return Decimal('0')

    qty_needed = Decimal(str(qty_needed_base))

    sub_lines = RecipeLine.query.filter_by(product_id=ingredient_id).all()
    if sub_lines:
        total_cost = Decimal('0')
        for sub in sub_lines:
            sub_qty = sub.qty_base * qty_needed
            total_cost += consume_fifo(sub.ingredient_id, sub_qty, sale_id, now, _depth + 1)
        return total_cost

    qty_to_consume = qty_needed
    total_cost = Decimal('0')

    batch_q = (StockBatch.query
               .filter_by(product_id=ingredient_id)
               .filter(StockBatch.qty_remaining_base > 0)
               .with_for_update()
               .order_by(StockBatch.purchased_at.asc(), StockBatch.id.asc()))

    batches = batch_q.filter(StockBatch.purchased_at <= now).all()
    if not batches:
        batches = batch_q.all()

    for batch in batches:
        if qty_to_consume <= 0:
            break
        take = min(Decimal(str(batch.qty_remaining_base)), qty_to_consume)
        batch.qty_remaining_base = Decimal(str(batch.qty_remaining_base)) - take
        cost = take * Decimal(str(batch.cost_per_base_unit))
        total_cost += cost
        db.session.add(StockConsumption(
            sale_id=sale_id,
            ingredient_id=ingredient_id,
            batch_id=batch.id,
            qty_consumed_base=take,
            cost_per_base_unit=batch.cost_per_base_unit,
            consumed_at=now
        ))
        qty_to_consume -= take

    return total_cost


def reverse_fifo(sale_id):
    """Restore all batch quantities consumed by this sale_id. Delete consumption records."""
    records = StockConsumption.query.filter_by(sale_id=sale_id).all()
    for r in records:
        batch = db.session.get(StockBatch, r.batch_id, with_for_update=True)
        if batch:
            batch.qty_remaining_base = (
                Decimal(str(batch.qty_remaining_base)) + Decimal(str(r.qty_consumed_base))
            )
        db.session.delete(r)


def get_stock_level(product_id):
    from sqlalchemy import func
    result = db.session.query(
        func.sum(StockBatch.qty_remaining_base)
    ).filter_by(product_id=product_id).scalar()
    return float(result or 0)


def get_fifo_cost_per_unit(product_id):
    batch = (StockBatch.query
             .filter_by(product_id=product_id)
             .filter(StockBatch.qty_remaining_base > 0)
             .order_by(StockBatch.purchased_at.asc(), StockBatch.id.asc())
             .first())
    return float(batch.cost_per_base_unit) if batch else 0.0


# ---------------------------------------------------------------------------
# Product helpers
# ---------------------------------------------------------------------------

def sync_sell_packages(product_id, packages):
    """Create/update/delete auto-managed package products for a stock_item."""
    existing = Product.query.filter_by(parent_stock_item_id=product_id).all()
    existing_by_name = {p.name: p for p in existing}
    submitted_names = {pkg['name'] for pkg in packages}

    for name, prod in existing_by_name.items():
        if name not in submitted_names:
            RecipeLine.query.filter_by(product_id=prod.id).delete()
            if Sale.query.filter_by(product_id=prod.id).count() == 0:
                db.session.delete(prod)

    parent = db.session.get(Product, product_id)  # noqa: F841 — kept for future use

    for pkg in packages:
        pkg_name = pkg.get('name', '').strip()
        qty_base = Decimal(str(pkg.get('qty_base', 0)))
        price    = Decimal(str(pkg.get('price', 0)))
        barcode  = pkg.get('barcode', '').strip() or None

        if not pkg_name or qty_base <= 0:
            continue

        if pkg_name in existing_by_name:
            prod = existing_by_name[pkg_name]
            prod.price = price
            if barcode:
                clash = Product.query.filter(Product.barcode == barcode, Product.id != prod.id).first()
                if not clash:
                    prod.barcode = barcode
            RecipeLine.query.filter_by(product_id=prod.id).delete()
            db.session.add(RecipeLine(
                product_id=prod.id,
                ingredient_id=product_id,
                qty_base=qty_base
            ))
        else:
            if not barcode:
                barcode = _gen_barcode(product_id)
            if Product.query.filter_by(barcode=barcode).first():
                barcode = _gen_barcode(product_id)
            prod = Product(
                name=pkg_name,
                price=price,
                barcode=barcode,
                product_type='recipe',
                is_for_sale=True,
                sold_by_weight=False,
                parent_stock_item_id=product_id
            )
            db.session.add(prod)
            db.session.flush()
            db.session.add(RecipeLine(
                product_id=prod.id,
                ingredient_id=product_id,
                qty_base=qty_base
            ))


def _gen_barcode(seed_id):
    for _ in range(30):
        rnd = str(random.randint(0, 99999)).zfill(5)
        core = f"200{str(seed_id).zfill(5)}{rnd}"[:12]
        check = _ean13_check(core)
        candidate = core + check
        if not Product.query.filter_by(barcode=candidate).first():
            return candidate
    return str(uuid.uuid4().int)[:13]


def _ean13_check(code12):
    s = sum(int(code12[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
    return str((10 - s % 10) % 10)


# ---------------------------------------------------------------------------
# Date parsing helper (used by kitchen and stats routes)
# ---------------------------------------------------------------------------

def _parse_dt(value: str, is_end: bool = False):
    if not value:
        return None
    v = value.strip()
    try:
        if len(v) == 10 and v[4] == '-' and v[7] == '-':
            d = datetime.strptime(v, "%Y-%m-%d")
            return d.replace(hour=23, minute=59, second=59, microsecond=999999) if is_end else d
        v2 = v.replace('Z', '')
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(v2, fmt)
            except ValueError:
                pass
        d = datetime.strptime(v[:10], "%Y-%m-%d")
        return d.replace(hour=23, minute=59, second=59, microsecond=999999) if is_end else d
    except Exception:
        return None


def _serialize_product(p, include_recipe=False, include_packages=False):
    d = {
        'id':           p.id,
        'name':         p.name,
        'price':        float(p.price) if p.price is not None else None,
        'barcode':      p.barcode,
        'stock_qty':    p.stock_qty,
        'product_type': p.product_type,
        'unit_type':    p.unit_type,
        'base_unit':    p.base_unit,
        'sold_by_weight':       p.sold_by_weight,
        'is_for_sale':          p.is_for_sale,
        'price_per_unit':       float(p.price_per_unit) if p.price_per_unit is not None else None,
        'low_stock_threshold':  float(p.low_stock_threshold) if p.low_stock_threshold is not None else None,
        'package_size':         float(p.package_size) if p.package_size is not None else None,
        'package_size_unit':    p.package_size_unit,
        'package_unit':         p.package_unit,
        'parent_stock_item_id': p.parent_stock_item_id,
        'margin_pct':      float(p.margin_pct) if p.margin_pct is not None else None,
        'is_prepared':          p.is_prepared,
        'is_available_online':  p.is_available_online,
        'image_url':            p.image_url,
        'description':          p.description,
        'is_archived':          p.is_archived,
        'archived_reason':      p.archived_reason,
        'images': [{
            'id':            img.id,
            'filename':      img.filename,
            'is_primary':    img.is_primary,
            'display_order': img.display_order,
        } for img in ProductImage.query.filter_by(product_id=p.id).order_by(ProductImage.display_order).all()],
    }
    if p.product_type == 'stock_item':
        d['stock_level'] = get_stock_level(p.id)
        d['low_stock']   = (
            p.low_stock_threshold is not None and
            d['stock_level'] < float(p.low_stock_threshold)
        )
    if include_recipe and p.product_type in ('recipe',):
        lines = RecipeLine.query.filter_by(product_id=p.id).all()
        d['recipe_lines'] = []
        for ln in lines:
            ing = db.session.get(Product, ln.ingredient_id)
            d['recipe_lines'].append({
                'ingredient_id':   ln.ingredient_id,
                'ingredient_name': ing.name if ing else None,
                'unit_type':       ing.unit_type if ing else None,
                'base_unit':       ing.base_unit if ing else None,
                'qty_base':        float(ln.qty_base),
            })
    if include_packages and p.product_type == 'stock_item':
        pkgs = Product.query.filter_by(parent_stock_item_id=p.id).all()
        d['sell_packages'] = []
        for pkg in pkgs:
            rl = RecipeLine.query.filter_by(product_id=pkg.id).first()
            d['sell_packages'].append({
                'id':       pkg.id,
                'name':     pkg.name,
                'price':    float(pkg.price) if pkg.price is not None else None,
                'barcode':  pkg.barcode,
                'qty_base': float(rl.qty_base) if rl else None,
            })
    return d

"""
Shared utilities - imported by app.py and (eventually) blueprints.
Import order: helpers → models → db. Never import from app.py here.
"""

import os
import re
import uuid
import random
from decimal import Decimal
from datetime import datetime, timedelta

from flask import session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from decimal import ROUND_HALF_UP

from models import (
    db,
    User, UserSession, Setting,
    Product, ProductImage, RecipeLine, Category,
    StockBatch, StockConsumption,
    Sale, Purchase,
    ConsignmentLiability,
    SESSION_TIMEOUT_MINUTES, SESSION_LOGOUT_HOURS,
)


# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------

def normalize_category_name(name):
    """Trim and collapse internal whitespace. Returns '' for None/blank."""
    return re.sub(r'\s+', ' ', (name or '').strip())


def get_or_create_category(name):
    """Resolve a category by case-insensitive normalized name, creating it if
    it does not yet exist. Returns the Category row, or None when name is blank.
    Does NOT commit - caller's transaction owns the flush/commit."""
    clean = normalize_category_name(name)
    if not clean:
        return None
    norm = clean.lower()
    cat = Category.query.filter_by(name_norm=norm).first()
    if cat:
        return cat
    cat = Category(name=clean, name_norm=norm)
    db.session.add(cat)
    db.session.flush()
    return cat


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
# Auth helpers - no dependency on the Flask app object
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
            now  = datetime.utcnow()
            # Hard logout after SESSION_LOGOUT_HOURS total
            if last < now - timedelta(hours=SESSION_LOGOUT_HOURS):
                sess.logged_out = last
                db.session.commit()
                session.clear()
                return False
            # Idle logout after SESSION_TIMEOUT_MINUTES of inactivity
            if last < now - timedelta(minutes=SESSION_TIMEOUT_MINUTES):
                sess.logged_out = last
                db.session.commit()
                session.clear()
                return False
    return True


def require_role(*roles):
    u = current_user()
    if not u or not u.active:
        session.clear()
        return False
    return bool(u.has_role(*roles))


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def seed_first_admin():
    # NOTE: this runs in EVERY gunicorn worker at startup. On a fresh (empty) DB all
    # workers race - several see count()==0 and try to INSERT the same admin. The loser
    # hits a UniqueViolation, so each insert is guarded: attempt, and on IntegrityError
    # roll back and treat it as "another worker already seeded it" (same philosophy as
    # db.create_all() skip-on-conflict in strong_migrate()).
    if User.query.count() == 0:
        admin_user = os.getenv('ADMIN_USER', 'admin')
        admin_pass = os.getenv('ADMIN_PASS', 'admin123')
        # On a provisioned appliance box, refuse to seed the well-known default -
        # register-store.sh always supplies a unique ADMIN_PASS. Gated on STORE_ID so
        # the original Lady Coleen box (which seeds admin/admin123) is unchanged.
        if os.getenv('STORE_ID', '').strip() and admin_pass == 'admin123':
            raise RuntimeError(
                "ADMIN_PASS is unset (still 'admin123') on a provisioned store box. "
                "register-store.sh must generate a unique admin password per store."
            )
        hashed = generate_password_hash(admin_pass)
        db.session.add(User(username=admin_user, password_hash=hashed, role='admin', active=True))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()  # another worker won the race - fine
        else:
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
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()  # another worker won the race - fine


def get_online_user_id():
    u = User.query.filter_by(username='Online Shop').first()
    return u.id if u else None


# ---------------------------------------------------------------------------
# FIFO inventory helpers
# ---------------------------------------------------------------------------

def consume_fifo(ingredient_id, qty_needed_base, sale_id, now, _depth=0, sale_unit_price=None):
    """
    Consume qty_needed_base units of ingredient_id from FIFO batches.
    Recursive for compound ingredients (recipe within recipe).
    Returns total COGS as Decimal. Never raises - consumes what's available.

    sale_unit_price: selling price per base unit — required for PCT_OF_SALE consignment products.
    """
    if _depth > 10:
        return Decimal('0')

    qty_needed = Decimal(str(qty_needed_base))

    sub_lines = RecipeLine.query.filter_by(product_id=ingredient_id).all()
    if sub_lines:
        prod = db.session.get(Product, ingredient_id)
        if not (prod and prod.is_produced):
            # Made-to-order recipe: consume raw ingredients recursively.
            total_cost = Decimal('0')
            for sub in sub_lines:
                sub_qty = sub.qty_base * qty_needed
                total_cost += consume_fifo(sub.ingredient_id, sub_qty, sale_id, now, _depth + 1)
            return total_cost
        # Batch-produced recipe: fall through to consume from its own finished-goods batch.

    qty_to_consume = qty_needed
    total_cost = Decimal('0')

    batch_q = (StockBatch.query
               .filter_by(product_id=ingredient_id)
               .filter(StockBatch.qty_remaining_base > 0)
               .with_for_update()
               .order_by(StockBatch.sort_order.asc().nulls_last(),
                         StockBatch.purchased_at.asc(), StockBatch.id.asc()))

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

        # Consignment liability: generate on every FIFO consumption of a consignment batch.
        # Write-offs pass sale_id='wo-{uuid}' — still owed (shrinkage is supplier's risk too).
        if getattr(batch, 'ownership_type', 'NORMAL') == 'CONSIGNMENT' and batch.supplier_id:
            _prod = db.session.get(Product, ingredient_id)
            _basis = getattr(_prod, 'settlement_basis', 'FIXED_COST') if _prod else 'FIXED_COST'
            _sale_price_snap = None
            _pct_snap = None
            if _basis == 'PCT_OF_SALE' and sale_unit_price is not None and _prod:
                _pct = Decimal(str(_prod.consignment_pct or 0)) / Decimal('100')
                _unit_cost = (Decimal(str(sale_unit_price)) * _pct).quantize(Decimal('0.000001'))
                _sale_price_snap = float(sale_unit_price)
                _pct_snap = float(_prod.consignment_pct or 0)
            else:
                _cuc = getattr(batch, 'consignment_unit_cost', None)
                _unit_cost = Decimal(str(_cuc)) if _cuc is not None else Decimal(str(batch.cost_per_base_unit))
            _amount = (take * _unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            db.session.add(ConsignmentLiability(
                supplier_id=batch.supplier_id,
                product_id=ingredient_id,
                batch_id=batch.id,
                sale_id=sale_id,
                qty_consumed=float(take),
                unit_cost=float(_unit_cost),
                amount_owed=float(_amount),
                sale_price_at_time=_sale_price_snap,
                settlement_percent_at_time=_pct_snap,
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


def reverse_consignment_liabilities(sale_id):
    """Mark all outstanding consignment liabilities for this sale as voided.
    Already-settled liabilities are left intact (financial audit trail)."""
    from datetime import datetime as _dt
    liabilities = ConsignmentLiability.query.filter_by(
        sale_id=sale_id, status='outstanding'
    ).all()
    now = _dt.utcnow()
    for lib in liabilities:
        lib.status = 'voided'
        lib.settled_at = now


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
             .order_by(StockBatch.sort_order.asc().nulls_last(),
                       StockBatch.purchased_at.asc(), StockBatch.id.asc())
             .first())
    return float(batch.cost_per_base_unit) if batch else 0.0


def _auto_price_products(product_ids, min_drift_pct=0):
    """Calculate auto-price for products with auto_price=True and store as pending_price.
    The pending price must be explicitly applied by the user before the till uses it.
    min_drift_pct: only flag when actual markup has drifted more than this many pct
    points from the target markup. 0 = flag any price change (original behaviour)."""
    from decimal import Decimal as _D
    import logging as _logging
    _log = _logging.getLogger('pos')
    if not product_ids:
        return
    global_markup = _D(str(get_setting('markup_percent', 20) or 20))
    changed = False
    for pid in product_ids:
        try:
            p = db.session.get(Product, pid)
            if not p or not getattr(p, 'auto_price', True):
                continue
            batches = (StockBatch.query
                       .filter_by(product_id=pid)
                       .filter(StockBatch.qty_remaining_base > 0)
                       .all())
            if not batches:
                continue
            total_qty  = sum(_D(str(b.qty_remaining_base)) for b in batches)
            total_cost = sum(_D(str(b.qty_remaining_base)) * _D(str(b.cost_per_base_unit)) for b in batches)
            if total_qty <= 0:
                continue
            cost = total_cost / total_qty  # WAC — full Decimal precision
            markup = _D(str(p.margin_pct)) if p.margin_pct is not None else global_markup
            new_price = (cost * (1 + markup / 100)).quantize(_D('0.0001'))
            if p.sold_by_weight and p.unit_type in ('weight', 'volume'):
                current = _D(str(p.price_per_unit or 0))
                if min_drift_pct > 0 and cost > 0 and current > 0:
                    actual_markup = (current / cost - 1) * 100
                    if abs(actual_markup - markup) <= _D(str(min_drift_pct)):
                        if p.pending_price_per_unit is not None:
                            p.pending_price_per_unit = None
                            changed = True
                        continue
                if abs(new_price - current) > _D('0.0001'):
                    p.pending_price_per_unit = new_price
                    changed = True
            else:
                new_price_r = new_price.quantize(_D('0.01'))
                current = _D(str(p.price or 0))
                if min_drift_pct > 0 and cost > 0 and current > 0:
                    actual_markup = (current / cost - 1) * 100
                    if abs(actual_markup - markup) <= _D(str(min_drift_pct)):
                        if p.pending_price is not None:
                            p.pending_price = None
                            changed = True
                        continue
                if abs(new_price_r - current) > _D('0.005'):
                    p.pending_price = new_price_r
                    changed = True
        except Exception as e:
            _log.warning(f'[auto_price] product {pid}: {e}')
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def _run_markup_drift_check(min_drift_pct=None):
    """Scan all active auto_price=True products and flag those whose actual markup
    has drifted more than min_drift_pct points from their target markup.
    min_drift_pct defaults to the 'markup_drift_pct' setting (fallback 5)."""
    import logging as _logging
    _log = _logging.getLogger('pos')
    try:
        if min_drift_pct is None:
            min_drift_pct = float(get_setting('markup_drift_pct', 5) or 5)
        ids = [
            p.id for p in Product.query.filter(
                Product.is_archived == False,
                Product.auto_price == True,
            ).all()
        ]
        if ids:
            _auto_price_products(ids, min_drift_pct=min_drift_pct)
            _log.info(f'[markup_drift] scanned {len(ids)} products (threshold {min_drift_pct}%)')
    except Exception as e:
        _log.warning(f'[markup_drift] scan failed: {e}')


_markup_scheduler_started = False


def _start_markup_drift_scheduler(app):
    """Start hourly background markup-drift check. Call once from create_app()."""
    global _markup_scheduler_started
    if _markup_scheduler_started:
        return
    _markup_scheduler_started = True

    import threading
    import time

    def _loop():
        time.sleep(300)  # 5-min warm-up delay
        while True:
            try:
                with app.app_context():
                    _run_markup_drift_check()
            except Exception as e:
                app.logger.warning(f'[markup_drift] scheduler error: {e}')
            time.sleep(3600)  # check every hour

    t = threading.Thread(target=_loop, daemon=True, name='markup-drift-check')
    t.start()


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

    parent = db.session.get(Product, product_id)  # noqa: F841 - kept for future use

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


def _ean13_check(code12):
    s = sum(int(code12[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
    return str((10 - s % 10) % 10)


def _gen_barcode_from_code(product_code):
    """Generate deterministic EAN-13 for fixed-price products from product_code.
    Format: 1 + PPPPP (5-digit code) + 000000 (6 zeros) + check digit.
    Weight/volume products don't get a stored barcode - scale generates dynamically.
    """
    core = f"1{str(product_code).zfill(5)}000000"
    return core + _ean13_check(core)


def _plu_range(sold_by_weight, unit_type, product_type):
    """Return (lo, hi) range for product_code based on type."""
    if sold_by_weight and unit_type == 'volume':
        return 30000, 39999
    elif sold_by_weight:
        return 1, 19999
    elif product_type in ('simple', 'stock_item'):
        return 20000, 29999
    else:
        return 40000, 49999


def _assign_product_code(sold_by_weight, unit_type, product_type):
    """Assign the smallest available product_code gap for the given product type.
    For fixed-price products also skips codes whose auto-generated barcode is
    already taken by another product (guards against barcode/product_code mismatch
    in existing data).
    Uses table lock - caller must be inside a transaction.
    """
    lo, hi = _plu_range(sold_by_weight, unit_type, product_type)
    from sqlalchemy import text as _text
    db.session.execute(_text("LOCK TABLE products IN SHARE ROW EXCLUSIVE MODE"))

    used_codes = {r[0] for r in db.session.execute(_text(
        "SELECT product_code FROM products "
        "WHERE product_code >= :lo AND product_code <= :hi"
    ), {'lo': lo, 'hi': hi}).fetchall()}

    # For fixed-price products the barcode is derived from the product_code.
    # Pre-load all barcodes so we can skip any code whose barcode is already taken.
    check_barcode = not sold_by_weight and unit_type != 'volume'
    used_barcodes = set()
    if check_barcode:
        used_barcodes = {r[0] for r in db.session.execute(_text(
            "SELECT barcode FROM products WHERE barcode IS NOT NULL"
        )).fetchall()}

    for code in range(lo, hi + 1):
        if code in used_codes:
            continue
        if check_barcode and _gen_barcode_from_code(code) in used_barcodes:
            continue
        return code

    raise ValueError(f"Product code range {lo}-{hi} exhausted")


def validate_product_code(new_code, product_id=None):
    """Check product_code is available. Returns (ok, conflict_product_name or None)."""
    if not new_code or new_code <= 0 or new_code > 99999:
        return False, "Product code must be between 1 and 99999"
    conflict = Product.query.filter(
        Product.product_code == new_code,
        Product.id != product_id if product_id else True
    ).first()
    if conflict:
        return False, f"PLU {new_code} already used by '{conflict.name}'"
    return True, None


def _gen_barcode(seed_id):
    """Legacy fallback - generates random EAN-13 with prefix 100."""
    for _ in range(30):
        rnd = str(random.randint(0, 99999)).zfill(5)
        core = f"100{str(seed_id).zfill(5)}{rnd}"[:12]
        check = _ean13_check(core)
        candidate = core + check
        if not Product.query.filter_by(barcode=candidate).first():
            return candidate
    return str(uuid.uuid4().int)[:13]


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


def _serialize_product(p, include_recipe=False, include_packages=False, image_cache=None):
    d = {
        'id':           p.id,
        'name':         p.name,
        'price':        float(p.price) if p.price is not None else None,
        'barcode':      p.barcode,
        'product_code': p.product_code,
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
        'category_id':          p.category_id,
        'category_name':        p.category.name if p.category else None,
        'sub_category_id':      p.sub_category_id,
        'sub_category_name':    p.sub_category.name if getattr(p, 'sub_category', None) else None,
        'product_family_id':    p.product_family_id,
        'family_name':          p.family.name if getattr(p, 'family', None) else None,
        'is_default_variant':   p.is_default_variant,
        # Scale sync fields
        'sync_to_scale':           p.sync_to_scale,
        'scale_tare':              float(p.scale_tare) if p.scale_tare is not None else 0,
        'scale_shelf_life':        p.scale_shelf_life or 0,
        'scale_pack_qty':          p.scale_pack_qty or 0,
        'scale_open_price':        p.scale_open_price,
        'scale_msg1':              p.scale_msg1 or '',
        'scale_msg2':              p.scale_msg2 or '',
        # Barcode config - used by POS scanner to decode scale labels
        'scale_barcode_prefix':    20,    # scale always uses prefix 20
        'scale_barcode_format':    'price_cents',  # VVVVVV = total price in cents
        'scale_last_synced_at':    p.scale_last_synced_at.isoformat() if p.scale_last_synced_at else None,
        'scale_last_sync_status':  p.scale_last_sync_status,
        'scale_last_sync_error':   p.scale_last_sync_error,
        'stat_unit_size':          float(p.stat_unit_size) if p.stat_unit_size is not None else None,
        'is_produced':             p.is_produced,
        'batch_size':              float(p.batch_size) if p.batch_size is not None else 1.0,
        'stock_unit':              p.stock_unit,
        'last_overhead_costs':     p.last_overhead_costs,
        # Consignment fields
        'is_consignment':    p.is_consignment,
        'settlement_basis':  p.settlement_basis,
        'consignment_pct':   float(p.consignment_pct) if p.consignment_pct is not None else None,
        'auto_price':        p.auto_price if getattr(p, 'auto_price', None) is not None else True,
        'pending_price':          float(p.pending_price) if getattr(p, 'pending_price', None) is not None else None,
        'pending_price_per_unit': float(p.pending_price_per_unit) if getattr(p, 'pending_price_per_unit', None) is not None else None,
        'cost_per_base_unit':     (lambda _b: float(_b.cost_per_base_unit) if _b and _b.cost_per_base_unit else None)(
            StockBatch.query.filter_by(product_id=p.id)
            .order_by(StockBatch.purchased_at.desc(), StockBatch.id.desc()).first()
        ),
        'images': image_cache[p.id] if image_cache is not None else [{
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
    if p.product_type == 'recipe' and p.is_produced:
        d['stock_level'] = get_stock_level(p.id)
    if p.product_type == 'simple':
        # Weighted-average purchase cost per unit - lets the product page show
        # margin/markup for resale goods (no stock batches, costed from purchases).
        total_value, total_qty = db.session.query(
            func.coalesce(func.sum(Purchase.qty_added * Purchase.purchase_price), 0),
            func.coalesce(func.sum(Purchase.qty_added), 0),
        ).filter(Purchase.product_id == p.id).one()
        d['unit_cost'] = float(total_value) / float(total_qty) if total_qty else None
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

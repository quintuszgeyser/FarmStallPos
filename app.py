
# -*- coding: utf-8 -*-
"""
Farm Stall POS — v1.5.0
- Recipe-based stock system with FIFO costing
- Product types: simple, stock_item, recipe
- Variable weight selling (sold_by_weight flag)
- Package products auto-created from stock items
- Backward compatible: existing simple products unchanged
"""

import os, uuid, logging, traceback
from datetime import datetime, date, timedelta
from collections import defaultdict
from io import StringIO, BytesIO
from decimal import Decimal

from flask import Flask, jsonify, request, session, send_file, render_template, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, func, Numeric
from werkzeug.security import generate_password_hash, check_password_hash

APP_VERSION = '1.6.0'

# -----------------------------
# Logging
# -----------------------------
LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'pos.log')
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(),           # also print to console
    ]
)
logger = logging.getLogger('pos')

app = Flask(__name__)

# Decimal → float for JSON (Numeric columns return Decimal)
from flask.json.provider import DefaultJSONProvider
class _JSONProvider(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)
app.json_provider_class = _JSONProvider
app.json = _JSONProvider(app)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key')

# ---- Request/response logging ----
import time as _time

@app.before_request
def _log_request():
    g._req_start = _time.monotonic()
    if request.path.startswith('/static'):
        return
    user_id = session.get('user_id', '-')
    logger.info('REQ  %s %s  user=%s', request.method, request.path, user_id)

    uid = session.get('user_id')
    if not uid:
        return
    # /api/me is a passive presence-check — don't create sessions from it
    if request.path == '/api/me':
        return
    now = datetime.utcnow()
    sid = session.get('session_id')
    if sid:
        sess = db.session.get(UserSession, sid)
        if sess and sess.logged_out is None:
            cutoff = now - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
            last   = sess.last_active or sess.logged_in
            if last < cutoff:
                sess.logged_out = last + timedelta(minutes=SESSION_TIMEOUT_MINUTES)
                db.session.commit()
                session.pop('session_id', None)
                sid = None
            else:
                sess.last_active = now
                db.session.commit()
        elif sess is None:
            # Cookie points at a deleted/missing session record — clear it
            session.pop('session_id', None)
            sid = None
    if not sid:
        # Only open a new session if no other open session exists for this user
        # (prevents duplicates from parallel browser requests)
        existing = UserSession.query.filter_by(
            user_id=uid, logged_out=None
        ).order_by(UserSession.logged_in.desc()).first()
        if existing:
            session['session_id'] = existing.id
            existing.last_active  = now
            db.session.commit()
        else:
            new_sess = UserSession(user_id=uid, logged_in=now, last_active=now)
            db.session.add(new_sess)
            db.session.commit()
            session['session_id'] = new_sess.id

@app.after_request
def _log_response(response):
    if request.path.startswith('/static'):
        return response
    elapsed_ms = round((_time.monotonic() - getattr(g, '_req_start', 0)) * 1000)
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(level, 'RESP %s %s  status=%s  %dms',
               request.method, request.path, response.status_code, elapsed_ms)
    return response

@app.errorhandler(Exception)
def _handle_exception(e):
    logger.error('UNHANDLED EXCEPTION  %s %s\n%s',
                 request.method, request.path, traceback.format_exc())
    return jsonify({'error': 'Internal server error', 'detail': str(e)}), 500

# ---- DB URL rewrite to psycopg driver ----
db_url = os.getenv('DATABASE_URL', 'sqlite:///pos.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif db_url.startswith('postgresql://') and '+psycopg://' not in db_url:
    db_url = 'postgresql+psycopg://' + db_url.split('://', 1)[1]

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# -----------------------------
# Models
# -----------------------------
class User(db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(20), nullable=False, default='teller')
    active        = db.Column(db.Boolean, nullable=False, default=True)


class Product(db.Model):
    __tablename__ = 'products'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), unique=True, nullable=False)
    price         = db.Column(Numeric(10, 2), nullable=True)   # null for stock_items (cost from batches)
    barcode       = db.Column(db.String(32), unique=True, nullable=True)
    stock_qty     = db.Column(db.Integer, nullable=False, default=0, server_default='0')  # simple only

    # Product type system
    product_type  = db.Column(db.String(20), nullable=False, default='simple', server_default='simple')
    # 'simple'     — fixed unit product, integer stock_qty (legacy behaviour)
    # 'stock_item' — raw ingredient/bulk stock tracked in FIFO batches by unit (g/ml/unit)
    # 'recipe'     — composed of other products via recipe_lines

    # Unit system (stock_item only)
    unit_type     = db.Column(db.String(10), nullable=True)   # 'weight' | 'volume' | 'count'
    base_unit     = db.Column(db.String(10), nullable=True)   # 'g' | 'ml' | 'unit'

    # Selling config
    sold_by_weight      = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    # True → teller enters qty at till (biltong, cheese); qty in Sale = base units consumed
    is_for_sale         = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    # False → internal ingredient only, never shown at teller
    price_per_unit      = db.Column(Numeric(10, 4), nullable=True)
    # For sold_by_weight products: R per base unit (e.g. R0.50 per g)

    # Stock management
    low_stock_threshold = db.Column(Numeric(10, 4), nullable=True)
    package_size        = db.Column(Numeric(10, 4), nullable=True)   # always stored in base unit (e.g. 1000 ml per carton)
    package_size_unit   = db.Column(db.String(10), nullable=True)    # display unit the admin entered (e.g. 'L', 'kg')
    package_unit        = db.Column(db.String(30), nullable=True)    # name of the package (e.g. 'carton', 'bag')

    # Package / child product link
    parent_stock_item_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    margin_pct           = db.Column(Numeric(5, 2), nullable=True)   # stored margin % for this product
    is_prepared          = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    # TRUE = product requires kitchen preparation; triggers a KitchenOrder on checkout
    is_archived          = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    # TRUE = discontinued product, hidden everywhere (not the same as is_for_sale=false which means internal ingredient)
    archived_reason      = db.Column(db.String(200), nullable=True)
    # 'cascade' = auto-archived because an ingredient was archived


class KitchenOrder(db.Model):
    __tablename__ = 'kitchen_orders'
    id           = db.Column(db.Integer, primary_key=True)
    sale_id      = db.Column(db.String(64), nullable=False, index=True)
    product_id   = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    product_name = db.Column(db.String(120), nullable=False)        # snapshot — survives product rename
    qty          = db.Column(Numeric(10, 4), nullable=False)
    ingredients  = db.Column(db.Text, nullable=True)                # JSON snapshot of recipe lines
    status       = db.Column(db.String(20), nullable=False, default='pending')  # pending | completed | cancelled
    sort_order   = db.Column(db.Integer, nullable=False, default=0)  # lower = higher priority in queue
    queued_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    teller_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    notes        = db.Column(db.String(500), nullable=True)


class Supplier(db.Model):
    __tablename__ = 'suppliers'
    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(120), unique=True, nullable=False)
    phone   = db.Column(db.String(50),  nullable=True)
    email   = db.Column(db.String(120), nullable=True)
    website = db.Column(db.String(200), nullable=True)
    notes   = db.Column(db.String(500), nullable=True)


class RecipeLine(db.Model):
    __tablename__ = 'recipe_lines'
    id            = db.Column(db.Integer, primary_key=True)
    product_id    = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_base      = db.Column(Numeric(10, 4), nullable=False)   # in ingredient's base unit per 1 sale


class StockBatch(db.Model):
    __tablename__ = 'stock_batches'
    id                  = db.Column(db.Integer, primary_key=True)
    product_id          = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_purchased_base  = db.Column(Numeric(10, 4), nullable=False)
    qty_remaining_base  = db.Column(Numeric(10, 4), nullable=False)
    cost_per_base_unit  = db.Column(Numeric(10, 6), nullable=False)   # R per g / ml / unit
    purchased_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    supplier_id         = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    user_id             = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class StockConsumption(db.Model):
    __tablename__ = 'stock_consumption'
    id                  = db.Column(db.Integer, primary_key=True)
    sale_id             = db.Column(db.String(64), nullable=False, index=True)
    ingredient_id       = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    batch_id            = db.Column(db.Integer, db.ForeignKey('stock_batches.id'), nullable=False)
    qty_consumed_base   = db.Column(Numeric(10, 4), nullable=False)
    cost_per_base_unit  = db.Column(Numeric(10, 6), nullable=False)   # snapshot at time of sale
    consumed_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class StockAdjustment(db.Model):
    __tablename__ = 'stock_adjustments'
    id                = db.Column(db.Integer, primary_key=True)
    product_id        = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    adjustment_type   = db.Column(db.String(20), nullable=False)  # 'stocktake' | 'writeoff'
    qty_change_base   = db.Column(Numeric(10, 4), nullable=False)  # negative = reduction
    system_qty_before = db.Column(Numeric(10, 4), nullable=False)  # stock level before adjustment
    cost_written_off  = db.Column(Numeric(10, 4), nullable=True)   # writeoff only: COGS value lost
    reason            = db.Column(db.String(200), nullable=False)
    adjusted_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user_id           = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class Purchase(db.Model):
    __tablename__ = 'purchases'
    id             = db.Column(db.Integer, primary_key=True)
    product_id     = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_added      = db.Column(db.Integer, nullable=False)
    purchase_price = db.Column(Numeric(10, 2), nullable=False)
    date_time      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class Setting(db.Model):
    __tablename__ = 'settings'
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(200), nullable=False)


class UserSession(db.Model):
    __tablename__ = 'user_sessions'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    logged_in   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    logged_out  = db.Column(db.DateTime, nullable=True)
    last_active = db.Column(db.DateTime, nullable=True)

SESSION_TIMEOUT_MINUTES = 10    # idle window for time-tracking purposes
SESSION_LOGOUT_HOURS    = 2     # hard logout after this much total inactivity


class Sale(db.Model):
    __tablename__ = 'sales'
    id          = db.Column(db.Integer, primary_key=True)
    sale_id     = db.Column(db.String(64), index=True, nullable=False)
    date_time   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty         = db.Column(Numeric(10, 4), nullable=False)   # Numeric for variable weight support
    unit_price  = db.Column(Numeric(10, 2), nullable=False)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    voided      = db.Column(db.Boolean, nullable=False, default=False)
    voided_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    voided_at   = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.String(200), nullable=True)
    flagged     = db.Column(db.Boolean, nullable=False, default=False)
    flag_note   = db.Column(db.String(500), nullable=True)
    flag_resolved = db.Column(db.Boolean, nullable=False, default=False)
    sub_log     = db.Column(db.Text, nullable=True)  # JSON {ingredient_id: replacement_id} for recipe subs


class Special(db.Model):
    __tablename__ = 'specials'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    special_price = db.Column(Numeric(10, 2), nullable=False)
    active        = db.Column(db.Boolean, nullable=False, default=True, server_default='true')


class SpecialLine(db.Model):
    __tablename__ = 'special_lines'
    id         = db.Column(db.Integer, primary_key=True)
    special_id = db.Column(db.Integer, db.ForeignKey('specials.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty        = db.Column(db.Integer, nullable=False, default=1)


# -----------------------------
# Utilities
# -----------------------------
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

def require_login():
    if 'user_id' not in session:
        return False
    user = db.session.get(User, session['user_id'])
    if not user or not user.active:
        session.clear()
        return False
    # Hard logout after 2 hours of total inactivity
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

def current_user():
    if 'user_id' not in session:
        return None
    return db.session.get(User, session.get('user_id'))

def require_role(role):
    u = current_user()
    return bool(u and u.role == role)

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


# -----------------------------
# FIFO Stock Functions
# -----------------------------
def consume_fifo(ingredient_id, qty_needed_base, sale_id, now, _depth=0):
    """
    Consume qty_needed_base units of ingredient_id from FIFO batches.
    Recursive: if the ingredient itself has recipe_lines (compound ingredient),
    recurse into each sub-ingredient instead.
    Returns total COGS as Decimal. Never raises — consumes what's available.
    """
    if _depth > 10:
        return Decimal('0')

    qty_needed = Decimal(str(qty_needed_base))

    # Check if this ingredient is itself a recipe (compound ingredient e.g. Cheese Blend)
    sub_lines = RecipeLine.query.filter_by(product_id=ingredient_id).all()
    if sub_lines:
        total_cost = Decimal('0')
        for sub in sub_lines:
            sub_qty = sub.qty_base * qty_needed
            total_cost += consume_fifo(sub.ingredient_id, sub_qty, sale_id, now, _depth + 1)
        return total_cost

    # Direct ingredient: consume from FIFO batches oldest-first.
    # Only use batches purchased ON OR BEFORE the sale date so that future
    # stock (bought later at a different price) never distorts historical COGS.
    # Fall back to all available batches if none exist before the sale date
    # (e.g. backdated sales or opening stock).
    qty_to_consume = qty_needed
    total_cost = Decimal('0')

    batch_q = (StockBatch.query
               .filter_by(product_id=ingredient_id)
               .filter(StockBatch.qty_remaining_base > 0)
               .with_for_update()
               .order_by(StockBatch.purchased_at.asc(), StockBatch.id.asc()))

    batches = batch_q.filter(StockBatch.purchased_at <= now).all()
    if not batches:
        # Fallback: use any available batch (covers opening stock with no purchase date)
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
    """Current stock level for a stock_item: sum of remaining batch quantities."""
    result = db.session.query(
        func.sum(StockBatch.qty_remaining_base)
    ).filter_by(product_id=product_id).scalar()
    return float(result or 0)


def get_fifo_cost_per_unit(product_id):
    """Current FIFO cost: cost_per_base_unit from oldest non-empty batch."""
    batch = (StockBatch.query
             .filter_by(product_id=product_id)
             .filter(StockBatch.qty_remaining_base > 0)
             .order_by(StockBatch.purchased_at.asc(), StockBatch.id.asc())
             .first())
    return float(batch.cost_per_base_unit) if batch else 0.0


def sync_sell_packages(product_id, packages):
    """
    Create/update/delete auto-managed package products for a stock_item.
    packages = [{name, qty_base, price, barcode}]
    """
    # Get existing auto-created packages for this stock item
    existing = Product.query.filter_by(parent_stock_item_id=product_id).all()
    existing_by_name = {p.name: p for p in existing}
    submitted_names = {pkg['name'] for pkg in packages}

    # Delete packages that were removed
    for name, prod in existing_by_name.items():
        if name not in submitted_names:
            # Remove recipe lines first
            RecipeLine.query.filter_by(product_id=prod.id).delete()
            # Only delete if no sales history
            if Sale.query.filter_by(product_id=prod.id).count() == 0:
                db.session.delete(prod)

    parent = db.session.get(Product, product_id)

    for pkg in packages:
        pkg_name = pkg.get('name', '').strip()
        qty_base = Decimal(str(pkg.get('qty_base', 0)))
        price    = Decimal(str(pkg.get('price', 0)))
        barcode  = pkg.get('barcode', '').strip() or None

        if not pkg_name or qty_base <= 0:
            continue

        if pkg_name in existing_by_name:
            # Update existing
            prod = existing_by_name[pkg_name]
            prod.price = price
            if barcode:
                # Only update if not taken by another product
                clash = Product.query.filter(Product.barcode == barcode, Product.id != prod.id).first()
                if not clash:
                    prod.barcode = barcode
            # Update recipe line
            RecipeLine.query.filter_by(product_id=prod.id).delete()
            db.session.add(RecipeLine(
                product_id=prod.id,
                ingredient_id=product_id,
                qty_base=qty_base
            ))
        else:
            # Create new package product
            # Auto-generate barcode if not provided
            if not barcode:
                barcode = _gen_barcode(product_id)
            # Ensure barcode unique
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
            db.session.flush()   # get prod.id
            db.session.add(RecipeLine(
                product_id=prod.id,
                ingredient_id=product_id,
                qty_base=qty_base
            ))


def _gen_barcode(seed_id):
    """Generate a unique EAN-13-style internal barcode."""
    import random
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
        'is_prepared':     p.is_prepared,
        'is_archived':     p.is_archived,
        'archived_reason': p.archived_reason,
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


# -----------------------------
# Strong startup migration
# -----------------------------
def strong_migrate():
    db.create_all()
    engine = db.engine
    engine_name = engine.dialect.name

    with engine.begin() as conn:

        if engine_name == 'sqlite':
            # ---- sales table ----
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS sales (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sale_id TEXT NOT NULL,
              date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              product_id INTEGER NOT NULL,
              qty REAL NOT NULL,
              unit_price REAL NOT NULL
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_sale_id ON sales (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_date_time ON sales (date_time)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_product_dt ON sales (product_id, date_time)")
            existing_sales = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(sales)").fetchall()]
            for col, defn in [('voided','INTEGER NOT NULL DEFAULT 0'),('voided_by','INTEGER'),
                               ('voided_at','TIMESTAMP'),('void_reason','TEXT'),('user_id','INTEGER'),
                               ('flagged','INTEGER NOT NULL DEFAULT 0'),('flag_note','TEXT'),
                               ('flag_resolved','INTEGER NOT NULL DEFAULT 0')]:
                if col not in existing_sales:
                    conn.exec_driver_sql(f"ALTER TABLE sales ADD COLUMN {col} {defn}")

            # ---- products new columns ----
            existing_prod = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(products)").fetchall()]
            for col, defn in [
                ('product_type',         "TEXT NOT NULL DEFAULT 'simple'"),
                ('unit_type',            'TEXT'),
                ('base_unit',            'TEXT'),
                ('sold_by_weight',       'INTEGER NOT NULL DEFAULT 0'),
                ('is_for_sale',          'INTEGER NOT NULL DEFAULT 1'),
                ('price_per_unit',       'REAL'),
                ('low_stock_threshold',  'REAL'),
                ('package_size',         'REAL'),
                ('package_size_unit',    'TEXT'),
                ('package_unit',         'TEXT'),
                ('parent_stock_item_id', 'INTEGER'),
                ('margin_pct',  'REAL'),
                ('is_prepared', 'INTEGER NOT NULL DEFAULT 0'),
            ]:
                if col not in existing_prod:
                    conn.exec_driver_sql(f"ALTER TABLE products ADD COLUMN {col} {defn}")

            # kitchen_orders table (SQLite)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS kitchen_orders (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              sale_id      TEXT NOT NULL,
              product_id   INTEGER,
              product_name TEXT NOT NULL,
              qty          REAL NOT NULL,
              ingredients  TEXT,
              status       TEXT NOT NULL DEFAULT 'pending',
              sort_order   INTEGER NOT NULL DEFAULT 0,
              queued_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              completed_at TIMESTAMP,
              teller_id    INTEGER,
              notes        TEXT
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_sale_id   ON kitchen_orders (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_status    ON kitchen_orders (status)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_queued_at ON kitchen_orders (queued_at)")

            # ---- new tables ----
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS suppliers (
              id      INTEGER PRIMARY KEY AUTOINCREMENT,
              name    TEXT NOT NULL UNIQUE,
              contact TEXT,
              notes   TEXT
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS recipe_lines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id INTEGER NOT NULL,
              ingredient_id INTEGER NOT NULL,
              qty_base REAL NOT NULL
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_recipe_lines_product ON recipe_lines (product_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id INTEGER NOT NULL,
              qty_purchased_base REAL NOT NULL,
              qty_remaining_base REAL NOT NULL,
              cost_per_base_unit REAL NOT NULL,
              purchased_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              supplier_id INTEGER,
              user_id INTEGER
            )""")
            # Add supplier_id if missing on existing table (SQLite)
            existing_sb = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(stock_batches)").fetchall()]
            if 'supplier_id' not in existing_sb:
                conn.exec_driver_sql("ALTER TABLE stock_batches ADD COLUMN supplier_id INTEGER")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_batches_product ON stock_batches (product_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_consumption (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sale_id TEXT NOT NULL,
              ingredient_id INTEGER NOT NULL,
              batch_id INTEGER NOT NULL,
              qty_consumed_base REAL NOT NULL,
              cost_per_base_unit REAL NOT NULL,
              consumed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_consumption_sale ON stock_consumption (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_consumption_ingredient ON stock_consumption (ingredient_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_adjustments (
              id                INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id        INTEGER NOT NULL,
              adjustment_type   TEXT NOT NULL,
              qty_change_base   REAL NOT NULL,
              system_qty_before REAL NOT NULL,
              cost_written_off  REAL,
              reason            TEXT NOT NULL,
              adjusted_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              user_id           INTEGER
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_adj_product ON stock_adjustments (product_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_adj_date ON stock_adjustments (adjusted_at)")

        else:
            # ---- PostgreSQL ----
            def pg_try(sql):
                try:
                    conn.exec_driver_sql("SAVEPOINT sp")
                    conn.exec_driver_sql(sql)
                    conn.exec_driver_sql("RELEASE SAVEPOINT sp")
                except Exception:
                    conn.exec_driver_sql("ROLLBACK TO SAVEPOINT sp")

            # sales table
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS sales (
              id SERIAL PRIMARY KEY,
              sale_id TEXT NOT NULL,
              date_time TIMESTAMP NOT NULL DEFAULT NOW(),
              product_id INTEGER NOT NULL REFERENCES products(id),
              qty NUMERIC(10,4) NOT NULL,
              unit_price NUMERIC(10,2) NOT NULL,
              user_id INTEGER REFERENCES users(id),
              voided BOOLEAN NOT NULL DEFAULT FALSE,
              voided_by INTEGER REFERENCES users(id),
              voided_at TIMESTAMP,
              void_reason TEXT
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_sale_id ON sales (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_date_time ON sales (date_time)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_product_dt ON sales (product_id, date_time)")

            for col, defn in [
                ('voided',        'BOOLEAN NOT NULL DEFAULT FALSE'),
                ('voided_by',     'INTEGER'),
                ('voided_at',     'TIMESTAMP'),
                ('void_reason',   'TEXT'),
                ('user_id',       'INTEGER'),
                ('flagged',       'BOOLEAN NOT NULL DEFAULT FALSE'),
                ('flag_note',     'TEXT'),
                ('flag_resolved', 'BOOLEAN NOT NULL DEFAULT FALSE'),
            ]:
                pg_try(f"ALTER TABLE sales ADD COLUMN {col} {defn}")

            # Fix money + qty columns
            for tbl, col, typ in [
                ('products',  'price',          'NUMERIC(10,2)'),
                ('purchases', 'purchase_price', 'NUMERIC(10,2)'),
                ('sales',     'unit_price',     'NUMERIC(10,2)'),
                ('sales',     'qty',            'NUMERIC(10,4)'),
            ]:
                pg_try(f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE {typ} USING {col}::{typ}")

            # products new columns
            for col, defn in [
                ('product_type',         "VARCHAR(20) NOT NULL DEFAULT 'simple'"),
                ('unit_type',            'VARCHAR(10)'),
                ('base_unit',            'VARCHAR(10)'),
                ('sold_by_weight',       'BOOLEAN NOT NULL DEFAULT FALSE'),
                ('is_for_sale',          'BOOLEAN NOT NULL DEFAULT TRUE'),
                ('price_per_unit',       'NUMERIC(10,4)'),
                ('low_stock_threshold',  'NUMERIC(10,4)'),
                ('package_size',         'NUMERIC(10,4)'),
                ('package_size_unit',    'VARCHAR(10)'),
                ('package_unit',         'VARCHAR(30)'),
                ('parent_stock_item_id', 'INTEGER'),
                ('margin_pct',  'NUMERIC(5,2)'),
                ('is_prepared', 'BOOLEAN NOT NULL DEFAULT FALSE'),
            ]:
                pg_try(f"ALTER TABLE products ADD COLUMN {col} {defn}")

            # kitchen_orders table (PostgreSQL)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS kitchen_orders (
              id           SERIAL PRIMARY KEY,
              sale_id      TEXT NOT NULL,
              product_id   INTEGER REFERENCES products(id),
              product_name VARCHAR(120) NOT NULL,
              qty          NUMERIC(10,4) NOT NULL,
              ingredients  TEXT,
              status       VARCHAR(20) NOT NULL DEFAULT 'pending',
              sort_order   INTEGER NOT NULL DEFAULT 0,
              queued_at    TIMESTAMP NOT NULL DEFAULT NOW(),
              completed_at TIMESTAMP,
              teller_id    INTEGER REFERENCES users(id),
              notes        TEXT
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_sale_id   ON kitchen_orders (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_status    ON kitchen_orders (status)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_queued_at ON kitchen_orders (queued_at)")

            # Allow price and barcode to be null (stock_items may not have a selling price)
            pg_try("ALTER TABLE products ALTER COLUMN price DROP NOT NULL")
            pg_try("ALTER TABLE products ALTER COLUMN barcode DROP NOT NULL")

            # FK constraints
            for constraint, tbl, col, ref_tbl, ref_col in [
                ('fk_sales_user',        'sales',    'user_id',             'users',    'id'),
                ('fk_sales_voided_by',   'sales',    'voided_by',           'users',    'id'),
                ('fk_purchases_user',    'purchases','user_id',             'users',    'id'),
                ('fk_products_parent',   'products', 'parent_stock_item_id','products', 'id'),
            ]:
                pg_try(f"ALTER TABLE {tbl} ADD CONSTRAINT {constraint} FOREIGN KEY ({col}) REFERENCES {ref_tbl}({ref_col})")

            # purchases user_id
            pg_try("ALTER TABLE purchases ADD COLUMN user_id INTEGER")
            pg_try("ALTER TABLE purchases ADD CONSTRAINT fk_purchases_user2 FOREIGN KEY (user_id) REFERENCES users(id)")

            # ---- new tables ----
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS suppliers (
              id      SERIAL PRIMARY KEY,
              name    VARCHAR(120) NOT NULL UNIQUE,
              contact VARCHAR(200),
              notes   VARCHAR(500)
            )""")

            # Add supplier_id to stock_batches
            pg_try("ALTER TABLE stock_batches ADD COLUMN supplier_id INTEGER")
            pg_try("ALTER TABLE stock_batches ADD CONSTRAINT fk_batches_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers(id)")
            # Split contact into phone/email/website
            pg_try("ALTER TABLE suppliers ADD COLUMN phone   VARCHAR(50)")
            pg_try("ALTER TABLE suppliers ADD COLUMN email   VARCHAR(120)")
            pg_try("ALTER TABLE suppliers ADD COLUMN website VARCHAR(200)")
            pg_try("UPDATE suppliers SET phone = contact WHERE contact IS NOT NULL AND email IS NULL AND website IS NULL")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS recipe_lines (
              id            SERIAL PRIMARY KEY,
              product_id    INTEGER NOT NULL REFERENCES products(id),
              ingredient_id INTEGER NOT NULL REFERENCES products(id),
              qty_base      NUMERIC(10,4) NOT NULL
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_recipe_lines_product ON recipe_lines (product_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_batches (
              id                  SERIAL PRIMARY KEY,
              product_id          INTEGER NOT NULL REFERENCES products(id),
              qty_purchased_base  NUMERIC(10,4) NOT NULL,
              qty_remaining_base  NUMERIC(10,4) NOT NULL,
              cost_per_base_unit  NUMERIC(10,6) NOT NULL,
              purchased_at        TIMESTAMP NOT NULL DEFAULT NOW(),
              user_id             INTEGER REFERENCES users(id)
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_batches_product ON stock_batches (product_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_batches_remaining ON stock_batches (product_id, qty_remaining_base)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_consumption (
              id                  SERIAL PRIMARY KEY,
              sale_id             TEXT NOT NULL,
              ingredient_id       INTEGER NOT NULL REFERENCES products(id),
              batch_id            INTEGER NOT NULL REFERENCES stock_batches(id),
              qty_consumed_base   NUMERIC(10,4) NOT NULL,
              cost_per_base_unit  NUMERIC(10,6) NOT NULL,
              consumed_at         TIMESTAMP NOT NULL DEFAULT NOW()
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_consumption_sale ON stock_consumption (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_consumption_ingredient ON stock_consumption (ingredient_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_adjustments (
              id                SERIAL PRIMARY KEY,
              product_id        INTEGER NOT NULL REFERENCES products(id),
              adjustment_type   VARCHAR(20) NOT NULL,
              qty_change_base   NUMERIC(10,4) NOT NULL,
              system_qty_before NUMERIC(10,4) NOT NULL,
              cost_written_off  NUMERIC(10,4),
              reason            TEXT NOT NULL,
              adjusted_at       TIMESTAMP NOT NULL DEFAULT NOW(),
              user_id           INTEGER REFERENCES users(id)
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_adj_product ON stock_adjustments (product_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_adj_date ON stock_adjustments (adjusted_at)")

            pg_try("ALTER TABLE products ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT FALSE")
            pg_try("ALTER TABLE products ADD COLUMN archived_reason VARCHAR(200)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS user_sessions (
              id          SERIAL PRIMARY KEY,
              user_id     INTEGER NOT NULL REFERENCES users(id),
              logged_in   TIMESTAMP NOT NULL DEFAULT NOW(),
              logged_out  TIMESTAMP
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_user_sessions_user ON user_sessions (user_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_user_sessions_in ON user_sessions (logged_in)")
            pg_try("ALTER TABLE user_sessions ADD COLUMN last_active TIMESTAMP")

            # sub_log: JSON map {ingredient_id: replacement_id} for recipe substitutions
            pg_try("ALTER TABLE sales ADD COLUMN sub_log TEXT")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS specials (
              id            SERIAL PRIMARY KEY,
              name          VARCHAR(120) NOT NULL,
              special_price NUMERIC(10,2) NOT NULL,
              active        BOOLEAN NOT NULL DEFAULT TRUE
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS special_lines (
              id         SERIAL PRIMARY KEY,
              special_id INTEGER NOT NULL REFERENCES specials(id) ON DELETE CASCADE,
              product_id INTEGER NOT NULL REFERENCES products(id),
              qty        INTEGER NOT NULL DEFAULT 1
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_special_lines_special ON special_lines (special_id)")

        # Legacy backfill
        sales_count = conn.execute(text("SELECT COUNT(*) FROM sales")).scalar_one()
        if sales_count == 0:
            legacy_ok = False
            try:
                conn.execute(text("SELECT 1 FROM transactions LIMIT 1"))
                conn.execute(text("SELECT 1 FROM transaction_lines LIMIT 1"))
                legacy_ok = True
            except Exception:
                pass
            if legacy_ok:
                conn.exec_driver_sql("""
                INSERT INTO sales (sale_id, date_time, product_id, qty, unit_price)
                SELECT CAST(t.id AS TEXT), t.date_time, tl.product_id, tl.qty, tl.unit_price
                FROM transaction_lines tl
                JOIN transactions t ON tl.transaction_id = t.id
                """)


with app.app_context():
    strong_migrate()
    seed_first_admin()
    # Close any open sessions that are clearly stale:
    # — no last_active (created before the column existed)
    # — last_active more than SESSION_LOGOUT_HOURS ago
    _stale_cutoff = datetime.utcnow() - timedelta(hours=SESSION_LOGOUT_HOURS)
    _stale = UserSession.query.filter(
        UserSession.logged_out == None,
        db.or_(
            UserSession.last_active == None,
            UserSession.last_active < _stale_cutoff,
        )
    ).all()
    for _s in _stale:
        _s.logged_out = _s.last_active or _s.logged_in
    if _stale:
        db.session.commit()
        logger.info('Closed %d stale sessions on startup', len(_stale))


# -----------------------------
# Auth
# -----------------------------
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    user = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password_hash, password) or not user.active:
        return jsonify({'ok': False, 'error': 'Invalid credentials'}), 401
    session['user_id'] = user.id
    sess = UserSession(user_id=user.id, logged_in=datetime.utcnow())
    db.session.add(sess)
    db.session.commit()
    session['session_id'] = sess.id
    return jsonify({'ok': True, 'username': user.username, 'role': user.role})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    sid = session.get('session_id')
    if sid:
        sess = db.session.get(UserSession, sid)
        if sess and sess.logged_out is None:
            sess.logged_out = datetime.utcnow()
            db.session.commit()
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/me', methods=['GET'])
def api_me():
    u = current_user()
    if not u:
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, 'username': u.username, 'role': u.role})

@app.route('/api/db-migrate', methods=['POST'])
def api_db_migrate():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    strong_migrate()
    return jsonify({'ok': True})


# -----------------------------
# Users (admin)
# -----------------------------
@app.route('/api/users', methods=['GET'])
def api_users_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    users = User.query.order_by(User.username.asc()).all()
    return jsonify([{'username': u.username, 'role': u.role, 'active': u.active} for u in users])

@app.route('/api/users', methods=['POST'])
def api_users_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    username = data.get('username', '').strip()
    role     = data.get('role', 'teller')
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if role not in ('admin', 'teller'):
        return jsonify({'error': 'Invalid role'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username exists'}), 409
    u = User(username=username, role=role, password_hash=generate_password_hash(password), active=True)
    db.session.add(u)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/users/update', methods=['POST'])
def api_users_update():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data     = request.json or {}
    username = data.get('username')
    role     = data.get('role')
    active   = data.get('active')
    password = data.get('password')
    u = User.query.filter_by(username=username).first()
    if not u:
        return jsonify({'error': 'User not found'}), 404
    if role in ('admin', 'teller'):
        u.role = role
    if isinstance(active, bool):
        u.active = active
    if password:
        u.password_hash = generate_password_hash(password)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/users/<username>', methods=['DELETE'])
def api_users_delete(username):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    u = User.query.filter_by(username=username).first()
    if not u:
        return jsonify({'error': 'User not found'}), 404
    db.session.delete(u)
    db.session.commit()
    return jsonify({'ok': True})


# -----------------------------
# Products
# -----------------------------
@app.route('/api/products', methods=['GET'])
def api_products_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    products = Product.query.order_by(Product.name.asc()).all()
    include_recipe = request.args.get('full') == '1'
    return jsonify([_serialize_product(p, include_recipe=include_recipe, include_packages=include_recipe) for p in products])

@app.route('/api/products/<int:pid>', methods=['GET'])
def api_product_get_one(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(_serialize_product(p, include_recipe=True, include_packages=True))

@app.route('/api/products', methods=['POST'])
def api_products_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}

    name         = data.get('name', '').strip()
    product_type = data.get('product_type', 'simple')
    barcode      = (data.get('barcode') or '').strip() or None

    if not name:
        return jsonify({'error': 'name required'}), 400
    if product_type not in ('simple', 'stock_item', 'recipe'):
        return jsonify({'error': 'Invalid product_type'}), 400

    # Validate barcode uniqueness if provided
    if barcode and Product.query.filter_by(barcode=barcode).first():
        return jsonify({'error': 'Barcode exists'}), 409
    if Product.query.filter_by(name=name).first():
        return jsonify({'error': 'Product name exists'}), 409

    # Auto-generate barcode if not provided and not a pure ingredient
    if not barcode:
        next_id = (db.session.query(func.max(Product.id)).scalar() or 0) + 1
        barcode = _gen_barcode(next_id)

    price         = data.get('price')
    stock_qty     = int(data.get('stock_qty', 0) or 0)
    unit_type     = data.get('unit_type') or None
    base_unit     = data.get('base_unit') or None
    sold_by_weight      = bool(data.get('sold_by_weight', False))
    is_for_sale         = bool(data.get('is_for_sale', True))
    price_per_unit      = data.get('price_per_unit') or None
    low_stock_threshold = data.get('low_stock_threshold') or None
    package_size        = data.get('package_size') or None
    package_size_unit   = data.get('package_size_unit') or None
    package_unit        = data.get('package_unit') or None

    try:
        price         = float(price) if price is not None else None
        price_per_unit      = float(price_per_unit) if price_per_unit is not None else None
        low_stock_threshold = float(low_stock_threshold) if low_stock_threshold is not None else None
        package_size_raw    = float(package_size) if package_size is not None else None
    except Exception:
        return jsonify({'error': 'Invalid numeric field'}), 400

    # Convert package_size to base unit if a display unit was provided
    unit_conversions = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1}
    if package_size_raw is not None and package_size_unit and unit_type:
        conv = unit_conversions.get(package_size_unit, 1)
        package_size = package_size_raw * conv
    else:
        package_size = package_size_raw

    # Derive base_unit from unit_type if not provided
    if unit_type and not base_unit:
        base_unit = {'weight': 'g', 'volume': 'ml', 'count': 'unit'}.get(unit_type)

    try:
        margin_pct = float(data.get('margin_pct')) if data.get('margin_pct') is not None else None
    except Exception:
        margin_pct = None

    p = Product(
        name=name, barcode=barcode, stock_qty=stock_qty,
        price=price, product_type=product_type,
        unit_type=unit_type, base_unit=base_unit,
        sold_by_weight=sold_by_weight, is_for_sale=is_for_sale,
        is_prepared=bool(data.get('is_prepared', False)),
        price_per_unit=price_per_unit,
        low_stock_threshold=low_stock_threshold,
        package_size=package_size, package_size_unit=package_size_unit, package_unit=package_unit,
        margin_pct=margin_pct,
    )
    db.session.add(p)
    db.session.flush()

    # Recipe lines
    recipe_lines = data.get('recipe_lines', [])
    for rl in recipe_lines:
        ing_id   = int(rl.get('ingredient_id', 0))
        qty_base = Decimal(str(rl.get('qty_base', 0)))
        if ing_id and qty_base > 0:
            db.session.add(RecipeLine(product_id=p.id, ingredient_id=ing_id, qty_base=qty_base))

    # Sell packages
    sell_packages = data.get('sell_packages', [])
    if sell_packages:
        sync_sell_packages(p.id, sell_packages)

    db.session.commit()
    return jsonify({'ok': True, 'id': p.id})

@app.route('/api/products/update', methods=['POST'])
def api_products_update():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    pid  = data.get('id')
    p    = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404

    if 'name' in data:
        name = data['name'].strip()
        other = Product.query.filter(Product.id != p.id, Product.name == name).first()
        if other:
            return jsonify({'error': 'Product name exists'}), 409
        p.name = name

    if 'barcode' in data and data['barcode']:
        bc = data['barcode'].strip()
        other = Product.query.filter(Product.id != p.id, Product.barcode == bc).first()
        if other:
            return jsonify({'error': 'Barcode exists'}), 409
        p.barcode = bc

    if 'price' in data and data['price'] is not None:
        try: p.price = float(data['price'])
        except Exception: return jsonify({'error': 'Invalid price'}), 400

    if 'stock_qty' in data and data['stock_qty'] is not None:
        try: p.stock_qty = int(data['stock_qty'])
        except Exception: return jsonify({'error': 'Invalid stock_qty'}), 400

    for field in ('product_type', 'unit_type', 'base_unit', 'package_size_unit', 'package_unit'):
        if field in data:
            setattr(p, field, data[field] or None)

    # Derive base_unit from unit_type if needed
    if p.unit_type and not p.base_unit:
        p.base_unit = {'weight': 'g', 'volume': 'ml', 'count': 'unit'}.get(p.unit_type)

    for field in ('price_per_unit', 'low_stock_threshold'):
        if field in data:
            try: setattr(p, field, float(data[field]) if data[field] is not None else None)
            except Exception: return jsonify({'error': f'Invalid {field}'}), 400

    # package_size: convert from display unit to base unit on save
    if 'package_size' in data:
        try:
            raw = float(data['package_size']) if data['package_size'] is not None else None
            if raw is not None:
                pkg_unit_display = data.get('package_size_unit') or p.package_size_unit
                unit_conversions = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1}
                conv = unit_conversions.get(pkg_unit_display, 1) if pkg_unit_display else 1
                p.package_size = raw * conv
            else:
                p.package_size = None
        except Exception:
            return jsonify({'error': 'Invalid package_size'}), 400

    for field in ('sold_by_weight', 'is_for_sale', 'is_prepared', 'is_archived'):
        if field in data:
            setattr(p, field, bool(data[field]))

    if 'margin_pct' in data:
        try:
            p.margin_pct = float(data['margin_pct']) if data['margin_pct'] is not None else None
        except Exception:
            return jsonify({'error': 'Invalid margin_pct'}), 400

    if 'archived_reason' in data:
        p.archived_reason = data['archived_reason'] or None

    # Update recipe lines
    if 'recipe_lines' in data:
        RecipeLine.query.filter_by(product_id=p.id).delete()
        for rl in data['recipe_lines']:
            ing_id   = int(rl.get('ingredient_id', 0))
            qty_base = Decimal(str(rl.get('qty_base', 0)))
            if ing_id and qty_base > 0:
                db.session.add(RecipeLine(product_id=p.id, ingredient_id=ing_id, qty_base=qty_base))

    # Update sell packages
    if 'sell_packages' in data:
        sync_sell_packages(p.id, data['sell_packages'])

    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/products/<int:pid>/archive', methods=['POST'])
def api_product_archive(pid):
    """Archive a product. For ingredients used in active recipes, caller must provide replacements or cascade."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404

    # replacements = {
    #   recipe_product_id: 'remove'                               — delete the line
    #                    | { ingredient_id: int, qty_base: float } — swap ingredient + optionally change qty
    # }
    replacements = data.get('replacements', {})

    # Find active recipes using this ingredient
    affected_recipes = []
    recipe_lines_for_p = RecipeLine.query.filter_by(ingredient_id=pid).all()
    for rl in recipe_lines_for_p:
        recipe = db.session.get(Product, rl.product_id)
        if recipe and not recipe.is_archived:
            affected_recipes.append(recipe)

    for recipe in affected_recipes:
        rep = replacements.get(str(recipe.id))
        if rep == 'remove':
            # Remove this ingredient line from the recipe entirely
            rl = RecipeLine.query.filter_by(product_id=recipe.id, ingredient_id=pid).first()
            if rl:
                db.session.delete(rl)
        elif rep:
            # Swap the ingredient (and optionally the qty) in the recipe
            rl = RecipeLine.query.filter_by(product_id=recipe.id, ingredient_id=pid).first()
            if rl:
                if isinstance(rep, dict):
                    rl.ingredient_id = int(rep['ingredient_id'])
                    if rep.get('qty_base') is not None:
                        rl.qty_base = Decimal(str(rep['qty_base']))
                else:
                    rl.ingredient_id = int(rep)
        else:
            # Cascade-archive the recipe
            recipe.is_archived     = True
            recipe.archived_reason = 'cascade'

    p.is_archived     = True
    p.archived_reason = data.get('reason') or None
    db.session.commit()

    cascaded = [r.id for r in affected_recipes if r.is_archived and r.archived_reason == 'cascade']
    return jsonify({'ok': True, 'cascaded_recipe_ids': cascaded})


@app.route('/api/products/<int:pid>/restore', methods=['POST'])
def api_product_restore(pid):
    """Restore an archived product. Offers to restore cascade-archived recipes if all their ingredients are active."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404

    p.is_archived     = False
    p.archived_reason = None

    # Auto-restore cascade-archived recipes that depended only on this ingredient
    restore_recipe_ids = data.get('restore_recipes', [])
    for rid in restore_recipe_ids:
        recipe = db.session.get(Product, int(rid))
        if not recipe or recipe.archived_reason != 'cascade':
            continue
        # Only restore if ALL ingredients are now active
        lines = RecipeLine.query.filter_by(product_id=recipe.id).all()
        all_active = all(
            (db.session.get(Product, rl.ingredient_id) or Product(is_archived=True)).is_archived == False
            for rl in lines
        )
        if all_active:
            recipe.is_archived     = False
            recipe.archived_reason = None

    # Also check what cascade recipes COULD be restored (for the frontend to ask)
    restorable = []
    for rl in RecipeLine.query.filter_by(ingredient_id=pid).all():
        recipe = db.session.get(Product, rl.product_id)
        if recipe and recipe.is_archived and recipe.archived_reason == 'cascade':
            lines = RecipeLine.query.filter_by(product_id=recipe.id).all()
            # Simulate this product being restored
            all_active = all(
                (db.session.get(Product, l.ingredient_id) or Product(is_archived=True)).is_archived == False
                or l.ingredient_id == pid
                for l in lines
            )
            if all_active:
                restorable.append({'id': recipe.id, 'name': recipe.name})

    db.session.commit()
    return jsonify({'ok': True, 'restorable_recipes': restorable})


@app.route('/api/products/<int:pid>/archive/preview', methods=['GET'])
def api_product_archive_preview(pid):
    """Return recipes affected by archiving this product, with available replacement ingredients."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404

    affected = []
    for rl in RecipeLine.query.filter_by(ingredient_id=pid).all():
        recipe = db.session.get(Product, rl.product_id)
        if recipe and not recipe.is_archived:
            # All active stock_items (both for-sale and internal ingredients) are valid replacements
            candidates = Product.query.filter(
                Product.product_type == 'stock_item',
                Product.is_archived == False,
                Product.id != pid
            ).order_by(Product.name.asc()).all()
            affected.append({
                'recipe_id':       recipe.id,
                'recipe_name':     recipe.name,
                'current_qty_base': float(rl.qty_base),
                'current_base_unit': p.base_unit or 'g',
                'current_unit_type': p.unit_type or 'weight',
                'replacements': [
                    {
                        'id':        c.id,
                        'name':      c.name,
                        'unit_type': c.unit_type,
                        'base_unit': c.base_unit,
                        'package_size': float(c.package_size) if c.package_size else None,
                        'package_unit': c.package_unit,
                    }
                    for c in candidates
                ],
            })

    return jsonify({'affected_recipes': affected})


@app.route('/api/products/<name>', methods=['DELETE'])
def api_products_delete(name):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = Product.query.filter_by(name=name).first()
    if not p:
        return jsonify({'error': 'Product not found'}), 404

    ref_sales = Sale.query.filter_by(product_id=p.id).count()
    ref_pur   = Purchase.query.filter_by(product_id=p.id).count()
    ref_batch = StockBatch.query.filter_by(product_id=p.id).count()
    if ref_sales or ref_pur or ref_batch:
        return jsonify({
            'error': 'Product has historical references — disable instead of deleting.',
            'hint':  'Set is_for_sale=false to hide from teller without losing history.'
        }), 409

    RecipeLine.query.filter_by(product_id=p.id).delete()
    RecipeLine.query.filter_by(ingredient_id=p.id).delete()
    # Delete child packages (if no sales)
    for child in Product.query.filter_by(parent_stock_item_id=p.id).all():
        if Sale.query.filter_by(product_id=child.id).count() == 0:
            RecipeLine.query.filter_by(product_id=child.id).delete()
            db.session.delete(child)
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/products/<int:pid>/recipe_cost')
def api_recipe_cost(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    lines = RecipeLine.query.filter_by(product_id=pid).all()
    if not lines:
        return jsonify({'product_id': pid, 'recipe_cost': 0, 'lines': [], 'suggested_prices': {}})

    def _recipe_cost_recursive(product_id, multiplier=1.0, _depth=0):
        """Return (total_cost, lines) expanding sub-recipes recursively."""
        if _depth > 10:
            return 0.0, []
        sub_lines = RecipeLine.query.filter_by(product_id=product_id).all()
        total = 0.0
        lines_out = []
        for ln in sub_lines:
            ing = db.session.get(Product, ln.ingredient_id)
            scaled_qty = float(ln.qty_base) * multiplier
            if ing and ing.product_type == 'recipe':
                # Sub-recipe: recurse
                sub_cost, sub_lines_out = _recipe_cost_recursive(ing.id, scaled_qty, _depth + 1)
                total += sub_cost
                lines_out.append({
                    'ingredient_id':   ing.id,
                    'ingredient_name': ing.name,
                    'base_unit':       None,
                    'qty_base':        scaled_qty,
                    'cost_per_unit':   0,
                    'line_cost':       round(sub_cost, 4),
                    'is_sub_recipe':   True,
                    'sub_lines':       sub_lines_out,
                })
            else:
                cost_per = get_fifo_cost_per_unit(ln.ingredient_id) if ing else 0
                line_cost = scaled_qty * cost_per
                total += line_cost
                lines_out.append({
                    'ingredient_id':   ln.ingredient_id,
                    'ingredient_name': ing.name if ing else None,
                    'base_unit':       ing.base_unit if ing else None,
                    'qty_base':        scaled_qty,
                    'cost_per_unit':   cost_per,
                    'line_cost':       round(line_cost, 4),
                    'is_sub_recipe':   False,
                })
        return total, lines_out

    total_cost, result_lines = _recipe_cost_recursive(pid)
    markup = float(get_setting('markup_percent', 40) or 40)
    suggested = {}
    for pct in [30, 40, 50, 60]:
        if pct < 100:
            suggested[f'{pct}%'] = round(total_cost / (1 - pct / 100), 2)

    return jsonify({
        'product_id':       pid,
        'recipe_cost':      round(total_cost, 4),
        'lines':            result_lines,
        'suggested_prices': suggested,
        'default_markup':   markup,
    })

@app.route('/api/products/<int:pid>/fifo_price')
def api_fifo_price(pid):
    """
    Calculate the weighted average cost across ALL available stock for this product,
    then suggest a selling price at the requested markup.

    Works for all product types:
      - simple     : weighted average of all purchase records
      - stock_item : weighted average across all remaining FIFO batches
      - recipe     : weighted average cost per ingredient × qty_base per sale
    """
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404

    try:
        markup_param = request.args.get('markup')
        markup = Decimal(str(markup_param)) if markup_param else Decimal(str(get_setting('markup_percent', 40) or 40))
    except Exception:
        return jsonify({'error': 'Invalid markup'}), 400

    lines_detail = []
    avg_cost     = Decimal('0')

    def batch_weighted_avg(product_id):
        """Weighted average cost per base unit across all remaining batches."""
        batches = StockBatch.query.filter_by(product_id=product_id)\
            .filter(StockBatch.qty_remaining_base > 0).all()
        total_qty  = sum(Decimal(str(b.qty_remaining_base)) for b in batches)
        if total_qty <= 0:
            return Decimal('0'), Decimal('0')
        total_cost = sum(Decimal(str(b.qty_remaining_base)) * Decimal(str(b.cost_per_base_unit)) for b in batches)
        return total_cost / total_qty, total_qty

    def recipe_total_cost(product_id, qty=Decimal('1'), _depth=0):
        """Recursively calculate cost of qty portions of a recipe product."""
        if _depth > 10:
            return Decimal('0'), []
        sub_lines = RecipeLine.query.filter_by(product_id=product_id).all()
        total = Decimal('0')
        detail = []
        for rl in sub_lines:
            ing = db.session.get(Product, rl.ingredient_id)
            scaled = Decimal(str(rl.qty_base)) * qty
            if not ing:
                continue
            if ing.product_type == 'recipe':
                sub_cost, sub_detail = recipe_total_cost(ing.id, scaled, _depth + 1)
                total += sub_cost
                detail.append({
                    'label':       ing.name,
                    'qty_per_sale': float(scaled),
                    'base_unit':   None,
                    'avg_cost_per_unit': 0,
                    'line_cost':   float(sub_cost),
                    'is_sub_recipe': True,
                })
            else:
                avg_per_unit, _ = batch_weighted_avg(ing.id)
                line_cost = avg_per_unit * scaled
                total += line_cost
                detail.append({
                    'label':             ing.name,
                    'qty_per_sale':      float(scaled),
                    'base_unit':         ing.base_unit,
                    'avg_cost_per_unit': float(avg_per_unit),
                    'line_cost':         float(line_cost),
                })
        return total, detail

    if p.product_type == 'simple':
        rows = Purchase.query.filter_by(product_id=pid).all()
        total_qty  = sum(Decimal(str(r.qty_added)) for r in rows)
        if total_qty > 0:
            total_cost = sum(Decimal(str(r.qty_added)) * Decimal(str(r.purchase_price)) for r in rows)
            avg_cost   = total_cost / total_qty
            lines_detail.append({'label': p.name, 'avg_cost_per_unit': float(avg_cost), 'total_qty': float(total_qty)})

    elif p.product_type == 'stock_item':
        avg_per_unit, total_qty = batch_weighted_avg(pid)
        avg_cost = avg_per_unit
        lines_detail.append({
            'label':            p.name,
            'avg_cost_per_unit': float(avg_per_unit),
            'base_unit':        p.base_unit,
            'total_qty':        float(total_qty),
        })

    elif p.product_type == 'recipe':
        avg_cost, lines_detail = recipe_total_cost(pid)

    if avg_cost <= 0:
        return jsonify({
            'product_id':      pid,
            'avg_cost':        0,
            'suggested_price': 0,
            'markup_pct':      float(markup),
            'lines':           lines_detail,
            'warning':         'No stock found — receive stock first',
        })

    suggested = avg_cost / (1 - markup / 100) if markup < 100 else avg_cost * 2

    suggestions = {}
    for pct in [20, 30, 40, 50, 60, 70]:
        suggestions[f'{pct}%'] = round(float(avg_cost) / (1 - pct / 100), 2)

    return jsonify({
        'product_id':      pid,
        'avg_cost':        round(float(avg_cost), 4),
        'suggested_price': round(float(suggested), 2),
        'markup_pct':      float(markup),
        'lines':           lines_detail,
        'suggestions':     suggestions,
    })


@app.route('/api/products/<int:pid>/suggested_price')
def api_suggested_price(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    rows = Purchase.query.filter_by(product_id=pid).all()
    total_qty = sum(r.qty_added for r in rows)
    wac = (sum(r.qty_added * r.purchase_price for r in rows) / float(total_qty)) if total_qty > 0 else float(p.price or 0)
    markup_param = request.args.get('markup')
    try:
        markup = float(markup_param) if markup_param else float(get_setting('markup_percent', 20) or 20)
    except Exception:
        markup = 20.0
    suggested = round(wac * (1 + markup / 100.0), 2)
    return jsonify({'product_id': pid, 'wac': round(wac, 4), 'markup_percent': markup, 'suggested_price': suggested})


# -----------------------------
# Suppliers (admin)
# -----------------------------
@app.route('/api/suppliers', methods=['GET'])
def api_suppliers_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    suppliers = Supplier.query.order_by(Supplier.name.asc()).all()
    return jsonify([{'id': s.id, 'name': s.name, 'phone': s.phone, 'email': s.email, 'website': s.website, 'notes': s.notes} for s in suppliers])

@app.route('/api/suppliers', methods=['POST'])
def api_suppliers_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    name    = data.get('name', '').strip()
    phone   = data.get('phone',   '').strip() or None
    email   = data.get('email',   '').strip() or None
    website = data.get('website', '').strip() or None
    notes   = data.get('notes',   '').strip() or None
    if not name:
        return jsonify({'error': 'name required'}), 400
    if Supplier.query.filter_by(name=name).first():
        return jsonify({'error': 'Supplier already exists'}), 409
    s = Supplier(name=name, phone=phone, email=email, website=website, notes=notes)
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id})

@app.route('/api/suppliers/<int:sid>', methods=['POST'])
def api_suppliers_update(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'name' in data:
        name = data['name'].strip()
        clash = Supplier.query.filter(Supplier.id != sid, Supplier.name == name).first()
        if clash:
            return jsonify({'error': 'Supplier name already exists'}), 409
        s.name = name
    if 'phone'   in data: s.phone   = data['phone'].strip()   or None
    if 'email'   in data: s.email   = data['email'].strip()   or None
    if 'website' in data: s.website = data['website'].strip() or None
    if 'notes'   in data: s.notes   = data['notes'].strip()   or None
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/suppliers/<int:sid>', methods=['DELETE'])
def api_suppliers_delete(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    # Nullify references rather than blocking
    StockBatch.query.filter_by(supplier_id=sid).update({'supplier_id': None})
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/suppliers/<int:sid>/products', methods=['GET'])
def api_suppliers_products(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404

    # Get distinct products from batches for this supplier
    batches = (db.session.query(StockBatch.product_id, func.max(StockBatch.purchased_at).label('last_received'))
               .filter_by(supplier_id=sid)
               .group_by(StockBatch.product_id)
               .all())

    result = []
    for prod_id, last_received in batches:
        p = db.session.get(Product, prod_id)
        if p:
            result.append({
                'id': p.id,
                'name': p.name,
                'product_type': p.product_type,
                'last_received': last_received.date().isoformat() if last_received else None,
            })

    result.sort(key=lambda x: x['name'])
    return jsonify(result)

@app.route('/api/suppliers/<int:sid>/purchase_run', methods=['POST'])
def api_suppliers_purchase_run(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404

    data = request.json or {}
    lines = data.get('lines', [])
    date_str = data.get('date')

    if not lines:
        return jsonify({'error': 'No lines provided'}), 400

    # Parse date if provided
    purchase_date = datetime.now()
    if date_str:
        try:
            parts = date_str.split('-')
            purchase_date = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            return jsonify({'error': 'Invalid date format'}), 400

    u = current_user()
    created_products = []
    batches_created = 0

    unit_conversions = {
        'g': 1, 'kg': 1000,
        'ml': 1, 'L': 1000,
        'unit': 1, 'dozen': 12,
    }

    for line in lines:
        pid = line.get('product_id')
        new_prod = line.get('new_product')
        qty = line.get('qty')
        unit = line.get('unit', 'unit')
        total_price = line.get('total_price')

        try:
            qty = float(qty)
            total_price = float(total_price)
        except Exception:
            return jsonify({'error': 'Invalid qty or total_price'}), 400

        # Create new product if requested
        if new_prod:
            name = new_prod.get('name', '').strip()
            if not name:
                return jsonify({'error': 'new_product.name required'}), 400

            # Check for duplicate name
            if Product.query.filter_by(name=name).first():
                return jsonify({'error': f'Product name "{name}" already exists'}), 409

            # Auto-generate barcode
            next_id = (db.session.query(func.max(Product.id)).scalar() or 0) + 1
            barcode = _gen_barcode(next_id)

            price = new_prod.get('price')
            product_type = new_prod.get('product_type', 'simple')
            base_unit = new_prod.get('base_unit') or None
            unit_type = new_prod.get('unit_type') or None

            if product_type not in ('simple', 'stock_item'):
                return jsonify({'error': 'Invalid product_type'}), 400

            try:
                price = float(price) if price is not None else None
            except Exception:
                return jsonify({'error': 'Invalid price'}), 400

            p = Product(
                name=name, barcode=barcode, stock_qty=0,
                price=price, product_type=product_type,
                unit_type=unit_type, base_unit=base_unit,
            )
            db.session.add(p)
            db.session.flush()
            pid = p.id
            created_products.append({'id': p.id, 'name': p.name})
        else:
            # Use existing product
            try:
                pid = int(pid)
            except Exception:
                return jsonify({'error': 'product_id required'}), 400

        p = db.session.get(Product, pid)
        if not p:
            return jsonify({'error': f'Product id {pid} not found'}), 404

        # Handle stock based on product type
        if p.product_type == 'stock_item':
            # Create FIFO batch
            conversion = unit_conversions.get(unit, 1)
            qty_base = qty * conversion
            cost_per_base = total_price / qty_base

            batch = StockBatch(
                product_id=pid,
                qty_purchased_base=qty_base,
                qty_remaining_base=qty_base,
                cost_per_base_unit=cost_per_base,
                supplier_id=sid,
                user_id=u.id if u else None,
                purchased_at=purchase_date
            )
            db.session.add(batch)
            batches_created += 1
        elif p.product_type == 'simple':
            # Add to stock_qty
            p.stock_qty = (p.stock_qty or 0) + int(qty)
            batches_created += 1

    db.session.commit()

    return jsonify({
        'ok': True,
        'created_products': created_products,
        'batches_created': batches_created
    })


# -----------------------------
# Stock — FIFO batch management
# -----------------------------
@app.route('/api/stock/ingredients', methods=['GET'])
def api_stock_ingredients():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    products = Product.query.filter(
        Product.product_type == 'stock_item'
    ).order_by(Product.name.asc()).all()

    result = []
    for p in products:
        batches = (StockBatch.query
                   .filter_by(product_id=p.id)
                   .filter(StockBatch.qty_remaining_base > 0)
                   .order_by(StockBatch.purchased_at.desc())
                   .all())
        stock_level = sum(float(b.qty_remaining_base) for b in batches)
        result.append({
            'id':           p.id,
            'name':         p.name,
            'unit_type':    p.unit_type,
            'base_unit':    p.base_unit,
            'package_size':      float(p.package_size) if p.package_size else None,
            'package_size_unit': p.package_size_unit,
            'package_unit':      p.package_unit,
            'stock_level':       stock_level,
            'low_stock':    p.low_stock_threshold is not None and stock_level < float(p.low_stock_threshold),
            'low_stock_threshold': float(p.low_stock_threshold) if p.low_stock_threshold else None,
            'sold_by_weight': p.sold_by_weight,
            'is_for_sale':    p.is_for_sale,
            'price_per_unit': float(p.price_per_unit) if p.price_per_unit else None,
            'batches': [{
                'id':                 b.id,
                'qty_purchased_base': float(b.qty_purchased_base),
                'qty_remaining_base': float(b.qty_remaining_base),
                'cost_per_base_unit': float(b.cost_per_base_unit),
                'purchased_at':       b.purchased_at.isoformat(),
                'supplier_id':        b.supplier_id,
                'supplier_name':      db.session.get(Supplier, b.supplier_id).name if b.supplier_id else None,
            } for b in batches],
            'sell_packages': [{
                'id':       pkg.id,
                'name':     pkg.name,
                'price':    float(pkg.price) if pkg.price else None,
                'barcode':  pkg.barcode,
                'qty_base': float(RecipeLine.query.filter_by(product_id=pkg.id).first().qty_base)
                            if RecipeLine.query.filter_by(product_id=pkg.id).first() else None,
            } for pkg in Product.query.filter_by(parent_stock_item_id=p.id).all()],
        })
    return jsonify(result)


@app.route('/api/stock/receive', methods=['POST'])
def api_stock_receive():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}

    pid          = data.get('product_id')
    qty          = data.get('qty')
    unit         = data.get('unit', '')
    total_price  = data.get('total_price')       # total amount paid
    price_per_unit = data.get('price_per_unit')  # alternative: per-unit price
    supplier_id  = data.get('supplier_id') or None
    if supplier_id:
        try: supplier_id = int(supplier_id)
        except Exception: supplier_id = None

    try:
        pid = int(pid)
        qty = float(qty)
    except Exception:
        return jsonify({'error': 'Invalid product_id or qty'}), 400

    p = db.session.get(Product, pid)
    if not p or p.product_type != 'stock_item':
        return jsonify({'error': 'Product not found or not a stock_item'}), 404

    # Unit conversion to base unit
    unit_conversions = {
        'g': 1, 'kg': 1000,
        'ml': 1, 'L': 1000,
        'unit': 1, 'dozen': 12,
    }
    # Package unit conversion
    if p.package_size and p.package_unit and unit == p.package_unit:
        conversion = float(p.package_size)
    else:
        conversion = unit_conversions.get(unit, 1)

    qty_base = qty * conversion

    # Calculate cost per base unit
    if total_price is not None:
        try:
            cost_per_base = float(total_price) / qty_base
        except Exception:
            return jsonify({'error': 'Invalid total_price'}), 400
    elif price_per_unit is not None:
        try:
            cpu = float(price_per_unit)
            # price_per_unit is per the entered unit, convert to per base unit
            cost_per_base = cpu / conversion
        except Exception:
            return jsonify({'error': 'Invalid price_per_unit'}), 400
    else:
        return jsonify({'error': 'total_price or price_per_unit required'}), 400

    u = current_user()
    batch = StockBatch(
        product_id=pid,
        qty_purchased_base=qty_base,
        qty_remaining_base=qty_base,
        cost_per_base_unit=cost_per_base,
        supplier_id=supplier_id,
        user_id=u.id if u else None
    )
    db.session.add(batch)
    db.session.commit()

    return jsonify({
        'ok': True,
        'batch_id':           batch.id,
        'qty_base':           qty_base,
        'base_unit':          p.base_unit,
        'cost_per_base_unit': round(cost_per_base, 6),
    })


@app.route('/api/stock/adjust', methods=['POST'])
def api_stock_adjust():
    """Stocktake: set actual physical count. System calculates diff and adjusts batches."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}

    pid         = data.get('product_id')
    actual_qty  = data.get('actual_qty')    # what you physically counted
    unit        = data.get('unit', '')
    reason      = data.get('reason', '').strip()

    try:
        pid        = int(pid)
        actual_qty = float(actual_qty)
    except Exception:
        return jsonify({'error': 'Invalid product_id or actual_qty'}), 400

    if not reason:
        return jsonify({'error': 'reason required'}), 400

    p = db.session.get(Product, pid, with_for_update=True)
    if not p or p.product_type != 'stock_item':
        return jsonify({'error': 'Product not found or not a stock_item'}), 404

    # Convert to base unit
    unit_conversions = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1}
    if p.package_size and p.package_unit and unit == p.package_unit:
        conversion = float(p.package_size)
    else:
        conversion = unit_conversions.get(unit, 1)

    actual_base = Decimal(str(actual_qty * conversion))
    system_base = Decimal(str(get_stock_level(pid)))
    diff        = actual_base - system_base   # + means more than expected, - means less

    u   = current_user()
    now = datetime.utcnow()

    if diff < 0:
        # Less stock than expected — consume from oldest batches (unexplained loss)
        consume_fifo(pid, abs(diff), f'adj-{uuid.uuid4()}', now)
    elif diff > 0:
        # More stock than expected — add to the most recent batch at its cost
        latest_batch = (StockBatch.query
                        .filter_by(product_id=pid)
                        .filter(StockBatch.qty_remaining_base > 0)
                        .order_by(StockBatch.purchased_at.desc(), StockBatch.id.desc())
                        .first())
        if latest_batch:
            latest_batch.qty_remaining_base = (
                Decimal(str(latest_batch.qty_remaining_base)) + diff
            )
        else:
            # No existing batch — create one at zero cost
            db.session.add(StockBatch(
                product_id=pid,
                qty_purchased_base=diff,
                qty_remaining_base=diff,
                cost_per_base_unit=Decimal('0'),
                purchased_at=now,
                user_id=u.id if u else None
            ))

    db.session.add(StockAdjustment(
        product_id=pid,
        adjustment_type='stocktake',
        qty_change_base=diff,
        system_qty_before=system_base,
        reason=reason,
        adjusted_at=now,
        user_id=u.id if u else None
    ))
    db.session.commit()

    return jsonify({
        'ok':           True,
        'system_before': float(system_base),
        'actual':        float(actual_base),
        'difference':    float(diff),
        'base_unit':     p.base_unit,
    })


@app.route('/api/stock/batches/<int:batch_id>', methods=['PATCH'])
def api_stock_batch_edit(batch_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    batch = db.session.get(StockBatch, batch_id)
    if not batch:
        return jsonify({'error': 'Batch not found'}), 404

    if 'supplier_id' in data:
        sid = data['supplier_id']
        batch.supplier_id = int(sid) if sid else None

    if 'purchased_at' in data:
        try:
            batch.purchased_at = datetime.fromisoformat(data['purchased_at'])
        except Exception:
            return jsonify({'error': 'Invalid purchased_at date'}), 400

    if 'total_price' in data:
        try:
            total = float(data['total_price'])
            batch.cost_per_base_unit = total / float(batch.qty_purchased_base)
        except Exception:
            return jsonify({'error': 'Invalid total_price'}), 400

    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/stock/writeoff', methods=['POST'])
def api_stock_writeoff():
    """Write off spoiled/damaged stock — deducts from oldest FIFO batch."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}

    pid    = data.get('product_id')
    qty    = data.get('qty')
    unit   = data.get('unit', '')
    reason = data.get('reason', '').strip()

    try:
        pid = int(pid)
        qty = float(qty)
    except Exception:
        return jsonify({'error': 'Invalid product_id or qty'}), 400

    if not reason:
        return jsonify({'error': 'reason required'}), 400
    if qty <= 0:
        return jsonify({'error': 'qty must be positive'}), 400

    p = db.session.get(Product, pid, with_for_update=True)
    if not p or p.product_type != 'stock_item':
        return jsonify({'error': 'Product not found or not a stock_item'}), 404

    # Convert to base unit
    unit_conversions = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1}
    if p.package_size and p.package_unit and unit == p.package_unit:
        conversion = float(p.package_size)
    else:
        conversion = unit_conversions.get(unit, 1)

    qty_base        = Decimal(str(qty * conversion))
    system_before   = Decimal(str(get_stock_level(pid)))

    if qty_base > system_before:
        return jsonify({
            'error': f'Cannot write off {float(qty_base)}{p.base_unit} — only {float(system_before)}{p.base_unit} in stock'
        }), 400

    u   = current_user()
    now = datetime.utcnow()

    # Consume from oldest batches and capture cost
    cost_written_off = consume_fifo(pid, qty_base, f'wo-{uuid.uuid4()}', now)

    db.session.add(StockAdjustment(
        product_id=pid,
        adjustment_type='writeoff',
        qty_change_base=-qty_base,
        system_qty_before=system_before,
        cost_written_off=cost_written_off,
        reason=reason,
        adjusted_at=now,
        user_id=u.id if u else None
    ))
    db.session.commit()

    return jsonify({
        'ok':              True,
        'qty_written_off': float(qty_base),
        'base_unit':       p.base_unit,
        'cost_written_off': float(cost_written_off),
    })


@app.route('/api/stock/adjustments/<int:adj_id>', methods=['PATCH'])
def api_stock_adjustment_edit(adj_id):
    """Edit a write-off: reverse the original qty, apply the corrected qty."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}

    adj = db.session.get(StockAdjustment, adj_id)
    if not adj:
        return jsonify({'error': 'Adjustment not found'}), 404
    if adj.adjustment_type != 'writeoff':
        return jsonify({'error': 'Only write-offs can be edited'}), 400

    new_qty  = data.get('qty')
    new_unit = data.get('unit', '')
    new_reason = data.get('reason', '').strip() or adj.reason

    try:
        new_qty = float(new_qty)
    except Exception:
        return jsonify({'error': 'Invalid qty'}), 400
    if new_qty <= 0:
        return jsonify({'error': 'qty must be positive'}), 400

    p = db.session.get(Product, adj.product_id)
    if not p:
        return jsonify({'error': 'Product not found'}), 404

    # Convert new qty to base unit
    unit_conversions = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1}
    if p.package_size and p.package_unit and new_unit == p.package_unit:
        conversion = float(p.package_size)
    else:
        conversion = unit_conversions.get(new_unit, 1)
    new_qty_base = Decimal(str(new_qty * conversion))

    old_qty_base = abs(Decimal(str(adj.qty_change_base)))
    diff = new_qty_base - old_qty_base  # positive = writing off more, negative = restoring

    u   = current_user()
    now = datetime.utcnow()

    if diff > 0:
        # Writing off MORE — consume additional qty from FIFO batches
        current_stock = Decimal(str(get_stock_level(p.id)))
        if diff > current_stock:
            return jsonify({'error': f'Cannot write off additional {float(diff)}{p.base_unit} — only {float(current_stock)}{p.base_unit} in stock'}), 400
        extra_cost = consume_fifo(p.id, diff, f'wo-edit-{uuid.uuid4()}', now)
        adj.cost_written_off = (Decimal(str(adj.cost_written_off or 0)) + Decimal(str(extra_cost)))
    elif diff < 0:
        # Writing off LESS — restore the over-written qty back to a batch
        restore_qty = abs(diff)
        # Add back to the most recent batch for this product
        batch = StockBatch.query.filter_by(product_id=p.id).order_by(StockBatch.purchased_at.desc()).first()
        if batch:
            batch.qty_remaining_base = float(Decimal(str(batch.qty_remaining_base)) + restore_qty)
        # Recalculate cost: proportional to new qty
        if old_qty_base > 0:
            adj.cost_written_off = Decimal(str(adj.cost_written_off or 0)) * (new_qty_base / old_qty_base)

    adj.qty_change_base = -new_qty_base
    adj.reason = new_reason
    db.session.commit()

    return jsonify({'ok': True, 'new_qty_base': float(new_qty_base), 'cost_written_off': float(adj.cost_written_off or 0)})


@app.route('/api/stock/adjustments', methods=['GET'])
def api_stock_adjustments():
    """Return adjustment history for a product or today's writeoffs."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    pid        = request.args.get('product_id')
    adj_type   = request.args.get('type')   # 'stocktake' | 'writeoff'
    start_param = request.args.get('start')
    end_param   = request.args.get('end')

    q = StockAdjustment.query
    if pid:
        q = q.filter_by(product_id=int(pid))
    if adj_type:
        q = q.filter_by(adjustment_type=adj_type)
    if start_param:
        start_dt = _parse_dt(start_param)
        if start_dt: q = q.filter(StockAdjustment.adjusted_at >= start_dt)
    if end_param:
        end_dt = _parse_dt(end_param, is_end=True)
        if end_dt: q = q.filter(StockAdjustment.adjusted_at <= end_dt)

    rows = q.order_by(StockAdjustment.adjusted_at.desc()).limit(500).all()

    user_names = {}
    uids = {r.user_id for r in rows if r.user_id}
    if uids:
        for usr in User.query.filter(User.id.in_(uids)).all():
            user_names[usr.id] = usr.username

    prod_names = {}
    pids = {r.product_id for r in rows}
    if pids:
        for prod in Product.query.filter(Product.id.in_(pids)).all():
            prod_names[prod.id] = (prod.name, prod.base_unit)

    result = []
    for r in rows:
        pname, bunit = prod_names.get(r.product_id, ('?', '?'))
        result.append({
            'id':               r.id,
            'product_id':       r.product_id,
            'product_name':     pname,
            'base_unit':        bunit,
            'adjustment_type':  r.adjustment_type,
            'qty_change_base':  float(r.qty_change_base),
            'system_qty_before': float(r.system_qty_before),
            'cost_written_off': float(r.cost_written_off) if r.cost_written_off else None,
            'reason':           r.reason,
            'adjusted_at':      r.adjusted_at.isoformat(),
            'adjusted_by':      user_names.get(r.user_id, ''),
        })
    return jsonify(result)


# -----------------------------
# Purchases (simple products — legacy path)
# -----------------------------
@app.route('/api/purchases', methods=['GET'])
def api_purchases_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    rows = Purchase.query.order_by(Purchase.date_time.desc()).all()
    result = []
    for r in rows:
        prod = db.session.get(Product, r.product_id)
        result.append({
            'id':            r.id,
            'product_id':    r.product_id,
            'product_name':  prod.name if prod else None,
            'qty_added':     r.qty_added,
            'purchase_price': float(r.purchase_price),
            'date_time':     r.date_time.isoformat()
        })
    return jsonify(result)

@app.route('/api/purchases', methods=['POST'])
def api_purchases_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data  = request.json or {}
    pid   = data.get('product_id')
    qty   = data.get('qty_added')
    price = data.get('purchase_price')
    try:
        pid = int(pid); qty = int(qty); price = float(price)
    except Exception:
        return jsonify({'error': 'Invalid product_id/qty/price'}), 400
    p = db.session.get(Product, pid, with_for_update=True)
    if not p:
        return jsonify({'error': 'Product not found'}), 404
    if qty <= 0 or price < 0:
        return jsonify({'error': 'Invalid values'}), 400
    u = current_user()
    purch = Purchase(product_id=pid, qty_added=qty, purchase_price=price, user_id=u.id if u else None)
    p.stock_qty = (p.stock_qty or 0) + qty
    db.session.add(purch)
    db.session.commit()
    return jsonify({'ok': True})


# -----------------------------
# Specials
# -----------------------------
def _serialize_special(s):
    lines = SpecialLine.query.filter_by(special_id=s.id).all()
    return {
        'id':            s.id,
        'name':          s.name,
        'special_price': float(s.special_price),
        'active':        s.active,
        'lines': [
            {
                'product_id':   l.product_id,
                'product_name': db.session.get(Product, l.product_id).name if db.session.get(Product, l.product_id) else None,
                'qty':          l.qty,
            }
            for l in lines
        ],
    }

@app.route('/api/specials', methods=['GET'])
def api_specials_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    specials = Special.query.order_by(Special.name.asc()).all()
    return jsonify([_serialize_special(s) for s in specials])

@app.route('/api/specials', methods=['POST'])
def api_specials_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data  = request.json or {}
    name  = data.get('name', '').strip()
    price = data.get('special_price')
    lines = data.get('lines', [])
    if not name:
        return jsonify({'error': 'Name required'}), 400
    if price is None:
        return jsonify({'error': 'special_price required'}), 400
    s = Special(name=name, special_price=Decimal(str(price)), active=data.get('active', True))
    db.session.add(s)
    db.session.flush()
    for l in lines:
        db.session.add(SpecialLine(special_id=s.id, product_id=int(l['product_id']), qty=int(l.get('qty', 1))))
    db.session.commit()
    return jsonify(_serialize_special(s)), 201

@app.route('/api/specials/<int:sid>', methods=['POST'])
def api_specials_update(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Special, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'name'          in data: s.name          = data['name'].strip()
    if 'special_price' in data: s.special_price = Decimal(str(data['special_price']))
    if 'active'        in data: s.active        = bool(data['active'])
    if 'lines' in data:
        SpecialLine.query.filter_by(special_id=sid).delete()
        for l in data['lines']:
            db.session.add(SpecialLine(special_id=sid, product_id=int(l['product_id']), qty=int(l.get('qty', 1))))
    db.session.commit()
    return jsonify(_serialize_special(s))

@app.route('/api/specials/<int:sid>', methods=['DELETE'])
def api_specials_delete(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Special, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    SpecialLine.query.filter_by(special_id=sid).delete()
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})

# -----------------------------
# Ingredient substitution suggestions
# For a given recipe product, return its default ingredients plus
# a ranked list of alternative stock items per ingredient based on
# past substitution history stored in sale notes (via sub_log).
# -----------------------------
@app.route('/api/products/<int:pid>/substitutions', methods=['GET'])
def api_product_substitutions(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p or p.product_type != 'recipe':
        return jsonify({'error': 'Not a recipe product'}), 404

    lines = RecipeLine.query.filter_by(product_id=pid).all()
    default_ingredients = []
    for rl in lines:
        ing = db.session.get(Product, rl.ingredient_id)
        if not ing:
            continue
        default_ingredients.append({
            'ingredient_id':   rl.ingredient_id,
            'ingredient_name': ing.name,
            'qty_base':        float(rl.qty_base),
            'unit_type':       ing.unit_type,
            'base_unit':       ing.base_unit,
        })

    # Possible alternatives: all non-archived stock items
    alternatives = [
        {'id': a.id, 'name': a.name, 'unit_type': a.unit_type, 'base_unit': a.base_unit}
        for a in Product.query.filter_by(product_type='stock_item', is_archived=False)
                               .order_by(Product.name.asc()).all()
    ]

    # Past substitution history for this product — stored as JSON in sale's note col (sub_log key)
    import json as _json
    history = {}  # ingredient_id -> Counter of {replacement_id: count}
    try:
        rows = db.session.execute(
            text("SELECT sub_log FROM sales WHERE product_id = :pid AND sub_log IS NOT NULL LIMIT 500"),
            {'pid': pid}
        ).fetchall()
        for row in rows:
            try:
                log = _json.loads(row[0])
                for ing_id_str, rep_id in log.items():
                    ing_id = int(ing_id_str)
                    history.setdefault(ing_id, {})
                    history[ing_id][rep_id] = history[ing_id].get(rep_id, 0) + 1
            except Exception:
                pass
    except Exception:
        pass  # sub_log column may not exist yet — safe to skip

    # Rank alternatives per default ingredient by frequency
    ranked = {}
    for ing in default_ingredients:
        ing_id  = ing['ingredient_id']
        counts  = history.get(ing_id, {})
        ordered = sorted(counts.keys(), key=lambda k: counts[k], reverse=True)
        ranked[ing_id] = ordered  # list of product_ids, most-used first

    return jsonify({
        'default_ingredients': default_ingredients,
        'alternatives':        alternatives,
        'history_ranked':      ranked,
    })


# -----------------------------
# Sales / Transactions
# -----------------------------
def _parse_dt(value: str, is_end=False):
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


@app.route('/api/transactions', methods=['GET'])
def api_transactions_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    u           = current_user()
    limit_param = request.args.get('limit')
    start_param = request.args.get('start')
    end_param   = request.args.get('end')

    q = db.session.query(Sale).filter(Sale.voided == False)

    if u.role == 'admin':
        today = date.today()
        if start_param or end_param:
            start_dt = _parse_dt(start_param) or datetime(today.year, today.month, today.day)
            end_dt   = _parse_dt(end_param, is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)
        else:
            start_dt = datetime(today.year, today.month, today.day)
            end_dt   = datetime(today.year, today.month, today.day, 23, 59, 59)
        q = q.filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt)

    rows = q.order_by(Sale.id.desc()).limit(2000).all()

    product_names = {}
    if rows:
        pids = {r.product_id for r in rows}
        for prod in Product.query.filter(Product.id.in_(pids)).all():
            product_names[prod.id] = prod.name

    user_names = {}
    uids = {r.user_id for r in rows if r.user_id}
    if uids:
        for usr in User.query.filter(User.id.in_(uids)).all():
            user_names[usr.id] = usr.username

    grouped       = defaultdict(list)
    dates         = {}
    users_by_sale = {}
    flags_by_sale = {}
    for r in rows:
        grouped[r.sale_id].append(r)
        dates.setdefault(r.sale_id, r.date_time)
        if r.user_id:
            users_by_sale[r.sale_id] = user_names.get(r.user_id, '')
        if r.flagged:
            flags_by_sale[r.sale_id] = {
                'flagged':       True,
                'flag_note':     r.flag_note,
                'flag_resolved': r.flag_resolved,
            }

    # Preload COGS for all sale_ids in one query
    sale_ids = list(grouped.keys())
    cogs_by_sale = defaultdict(Decimal)
    if sale_ids:
        consumptions = StockConsumption.query.filter(
            StockConsumption.sale_id.in_(sale_ids)
        ).all()
        for c in consumptions:
            cogs_by_sale[c.sale_id] += Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit))

    result = []
    for sid in sorted(grouped.keys(), key=lambda k: max(x.id for x in grouped[k]), reverse=True):
        items = []
        total = Decimal('0')
        for ln in grouped[sid]:
            name     = product_names.get(ln.product_id, f"Product {ln.product_id}")
            subtotal = Decimal(str(ln.qty)) * ln.unit_price
            total   += subtotal
            items.append({
                'product_id': ln.product_id,
                'name':       name,
                'qty':        float(ln.qty),
                'unit_price': float(ln.unit_price),
                'subtotal':   float(subtotal),
            })
        cogs   = float(round(cogs_by_sale.get(sid, Decimal('0')), 4))
        total_f = float(round(total, 2))
        margin  = round((total_f - cogs) / total_f * 100, 1) if total_f > 0 and cogs > 0 else None
        result.append({
            'id':         sid,
            'date_time':  dates[sid].isoformat(),
            'total':      total_f,
            'lines':      items,
            'teller':        users_by_sale.get(sid, ''),
            'cogs':          cogs if cogs > 0 else None,
            'margin_pct':    margin,
            'flagged':       flags_by_sale.get(sid, {}).get('flagged', False),
            'flag_note':     flags_by_sale.get(sid, {}).get('flag_note'),
            'flag_resolved': flags_by_sale.get(sid, {}).get('flag_resolved', False),
        })

    if u.role != 'admin':
        result = result[:5]
    elif limit_param:
        try: result = result[:int(limit_param)]
        except Exception: pass

    return jsonify(result)


@app.route('/api/transactions', methods=['POST'])
def api_transactions_post():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    cart = data.get('cart', [])
    if not cart:
        return jsonify({'error': 'Empty cart'}), 400

    sale_uuid = str(uuid.uuid4())
    now       = datetime.utcnow()
    u         = current_user()

    import json as _json

    for item in cart:
        pid        = int(item['product_id'])
        qty        = Decimal(str(item.get('qty', 1)))
        unit_price = Decimal(str(item.get('unit_price')))
        # subs: {ingredient_id: replacement_product_id} — sent from teller modal
        subs_raw   = item.get('subs', {})
        subs       = {int(k): int(v) for k, v in subs_raw.items()} if subs_raw else {}
        # extras: [{ingredient_id, qty_base}] — additional ingredients added by teller
        extras     = item.get('extras', [])

        sub_log_val = _json.dumps(subs) if subs else None

        db.session.add(Sale(
            sale_id=sale_uuid, date_time=now,
            product_id=pid, qty=qty, unit_price=unit_price,
            user_id=u.id if u else None,
            sub_log=sub_log_val,
        ))

        p = db.session.get(Product, pid, with_for_update=True)
        if not p:
            continue

        if p.product_type == 'simple':
            p.stock_qty = max(0, (p.stock_qty or 0) - int(qty))

        elif p.product_type == 'stock_item':
            # Sold directly by weight (e.g. loose biltong, loose cheese)
            consume_fifo(pid, qty, sale_uuid, now)

        elif p.product_type == 'recipe':
            lines = RecipeLine.query.filter_by(product_id=pid).all()
            for rl in lines:
                actual_id = subs.get(rl.ingredient_id, rl.ingredient_id)
                if actual_id == -1:
                    continue  # ingredient was removed by teller
                consume_fifo(actual_id, Decimal(str(rl.qty_base)) * qty, sale_uuid, now)
            # Extra ingredients added by teller
            for ex in extras:
                ex_id  = int(ex.get('ingredient_id', 0))
                ex_qty = Decimal(str(ex.get('qty_base', 0)))
                if ex_id and ex_qty > 0:
                    consume_fifo(ex_id, ex_qty * qty, sale_uuid, now)

    # Create kitchen orders for products that require preparation
    kitchen_count = 0
    max_sort = db.session.query(func.max(KitchenOrder.sort_order))\
        .filter_by(status='pending').scalar() or 0

    def _collect_kitchen_orders(product_id, qty, depth=0, subs=None, extras=None):
        """
        Recursively collect kitchen orders needed for a product × qty.
        subs:   {ingredient_id: replacement_product_id} from teller substitutions.
        extras: [{ingredient_id, qty_base}] teller-added ingredients.
        Returns list of (product, qty, ingredients_snapshot) tuples.
        """
        if depth > 10:
            return []
        p = db.session.get(Product, product_id)
        if not p:
            return []
        subs   = subs or {}
        extras = extras or []

        if p.is_prepared:
            ingredients = []
            for rl in RecipeLine.query.filter_by(product_id=product_id).all():
                actual_id  = subs.get(rl.ingredient_id, rl.ingredient_id)
                if actual_id == -1:
                    orig = db.session.get(Product, rl.ingredient_id)
                    ingredients.append({
                        'name':        orig.name if orig else str(rl.ingredient_id),
                        'qty':         0,
                        'base_unit':   '',
                        'substituted': True,
                        'removed':     True,
                    })
                    continue
                ing        = db.session.get(Product, actual_id)
                orig_ing   = db.session.get(Product, rl.ingredient_id) if actual_id != rl.ingredient_id else ing
                if not ing:
                    continue
                substituted = actual_id != rl.ingredient_id
                if ing.product_type == 'stock_item':
                    entry = {
                        'name':        ing.name,
                        'qty':         float(rl.qty_base) * float(qty),
                        'base_unit':   ing.base_unit or 'unit',
                        'substituted': substituted,
                    }
                    if substituted and orig_ing:
                        entry['original_name'] = orig_ing.name
                    ingredients.append(entry)
                elif ing.product_type == 'recipe':
                    ingredients.append({
                        'name':        ing.name,
                        'qty':         float(qty),
                        'base_unit':   'portion',
                        'substituted': substituted,
                    })

            # Append teller-added extras to the ingredient snapshot
            for ex in extras:
                ex_id  = int(ex.get('ingredient_id', 0))
                ex_qty = float(ex.get('qty_base', 0)) * float(qty)
                if ex_id and ex_qty > 0:
                    ex_ing = db.session.get(Product, ex_id)
                    if ex_ing:
                        ingredients.append({
                            'name':      ex_ing.name,
                            'qty':       ex_qty,
                            'base_unit': ex_ing.base_unit or 'unit',
                            'extra':     True,
                        })

            return [(p, qty, ingredients)]

        elif p.product_type == 'recipe':
            results = []
            for rl in RecipeLine.query.filter_by(product_id=product_id).all():
                sub_qty = Decimal(str(rl.qty_base)) * qty
                results.extend(_collect_kitchen_orders(rl.ingredient_id, sub_qty, depth + 1, subs))
            return results

        return []

    all_kitchen = []
    for pos, item in enumerate(cart):
        pid        = int(item['product_id'])
        qty        = Decimal(str(item.get('qty', 1)))
        item_subs  = {int(k): int(v) for k, v in item.get('subs', {}).items()}
        item_extras = item.get('extras', [])
        all_kitchen.extend(_collect_kitchen_orders(pid, qty, subs=item_subs, extras=item_extras))

    for ko_pos, (ko_product, ko_qty, ko_ingredients) in enumerate(all_kitchen):
        db.session.add(KitchenOrder(
            sale_id=sale_uuid,
            product_id=ko_product.id,
            product_name=ko_product.name,
            qty=ko_qty,
            ingredients=_json.dumps(ko_ingredients),
            status='pending',
            sort_order=max_sort + ko_pos + 1,
            queued_at=now,
            teller_id=u.id if u else None,
        ))
    kitchen_count = len(all_kitchen)

    db.session.commit()
    return jsonify({'ok': True, 'transaction_id': sale_uuid, 'kitchen_orders': kitchen_count})


@app.route('/api/transactions/<sale_id>/flag', methods=['POST'])
def api_transaction_flag(sale_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data    = request.json or {}
    note    = data.get('note', '').strip()
    resolve = data.get('resolve', False)   # admin resolving the flag

    rows = Sale.query.filter_by(sale_id=sale_id).all()
    if not rows:
        return jsonify({'error': 'Transaction not found'}), 404

    if resolve:
        if not require_role('admin'):
            return jsonify({'error': 'Forbidden'}), 403
        for row in rows:
            row.flag_resolved = True
    else:
        if not note:
            return jsonify({'error': 'note required'}), 400
        for row in rows:
            row.flagged   = True
            row.flag_note = note
            row.flag_resolved = False

    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/transactions/<sale_id>/void', methods=['POST'])
def api_transaction_void(sale_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data   = request.json or {}
    reason = data.get('reason', '').strip()
    rows   = Sale.query.filter_by(sale_id=sale_id, voided=False).with_for_update().all()
    if not rows:
        return jsonify({'error': 'Transaction not found or already voided'}), 404

    u   = current_user()
    now = datetime.utcnow()
    for row in rows:
        row.voided     = True
        row.voided_by  = u.id if u else None
        row.voided_at  = now
        row.void_reason = reason
        p = db.session.get(Product, row.product_id, with_for_update=True)
        if p and p.product_type == 'simple':
            p.stock_qty = (p.stock_qty or 0) + int(row.qty)

    # Restore FIFO batch quantities
    reverse_fifo(sale_id)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/transactions/<sale_id>/edit', methods=['POST'])
def api_transaction_edit(sale_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data  = request.json or {}
    lines = data.get('lines', [])
    if not lines:
        return jsonify({'error': 'lines required'}), 400

    rows = Sale.query.filter_by(sale_id=sale_id, voided=False).with_for_update().all()
    if not rows:
        return jsonify({'error': 'Transaction not found or voided'}), 404

    orig_date = rows[0].date_time

    # Restore original stock
    for row in rows:
        p = db.session.get(Product, row.product_id, with_for_update=True)
        if p and p.product_type == 'simple':
            p.stock_qty = (p.stock_qty or 0) + int(row.qty)
        db.session.delete(row)

    reverse_fifo(sale_id)

    # Insert updated lines
    u   = current_user()
    now = orig_date
    for item in lines:
        pid        = int(item['product_id'])
        qty        = Decimal(str(item.get('qty', 1)))
        unit_price = Decimal(str(item.get('unit_price')))
        if qty <= 0:
            continue
        db.session.add(Sale(
            sale_id=sale_id, date_time=now,
            product_id=pid, qty=qty, unit_price=unit_price,
            user_id=u.id if u else None
        ))
        p = db.session.get(Product, pid, with_for_update=True)
        if not p:
            continue
        if p.product_type == 'simple':
            p.stock_qty = max(0, (p.stock_qty or 0) - int(qty))
        elif p.product_type == 'stock_item':
            consume_fifo(pid, qty, sale_id, now)
        elif p.product_type == 'recipe':
            for rl in RecipeLine.query.filter_by(product_id=pid).all():
                consume_fifo(rl.ingredient_id, Decimal(str(rl.qty_base)) * qty, sale_id, now)

    db.session.commit()
    return jsonify({'ok': True})


# -----------------------------
# Kitchen Order Queue
# -----------------------------
def _serialize_kitchen_order(ko):
    import json as _json
    u = db.session.get(User, ko.teller_id) if ko.teller_id else None
    wait = None
    if ko.queued_at:
        end = ko.completed_at or datetime.utcnow()
        wait = int((end - ko.queued_at).total_seconds())
    try:
        ingredients = _json.loads(ko.ingredients) if ko.ingredients else []
    except Exception:
        ingredients = []
    return {
        'id':           ko.id,
        'sale_id':      ko.sale_id,
        'product_id':   ko.product_id,
        'product_name': ko.product_name,
        'qty':          float(ko.qty),
        'ingredients':  ingredients,
        'status':       ko.status,
        'sort_order':   ko.sort_order,
        'queued_at':    ko.queued_at.isoformat() if ko.queued_at else None,
        'completed_at': ko.completed_at.isoformat() if ko.completed_at else None,
        'wait_seconds': wait,
        'teller':       u.username if u else '',
        'notes':        ko.notes,
    }


@app.route('/api/kitchen/orders', methods=['GET'])
def api_kitchen_orders():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    include_completed = request.args.get('include_completed') == '1'
    date_param        = request.args.get('date')

    if include_completed and date_param:
        dt = _parse_dt(date_param)
        end_dt = _parse_dt(date_param, is_end=True)
        orders = (KitchenOrder.query
                  .filter(KitchenOrder.queued_at >= dt, KitchenOrder.queued_at <= end_dt)
                  .order_by(KitchenOrder.queued_at.asc()).all())
    else:
        orders = (KitchenOrder.query
                  .filter(KitchenOrder.status == 'pending')
                  .order_by(KitchenOrder.sort_order.asc(), KitchenOrder.queued_at.asc())
                  .all())

    return jsonify([_serialize_kitchen_order(o) for o in orders])


@app.route('/api/kitchen/orders/count', methods=['GET'])
def api_kitchen_orders_count():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    count = KitchenOrder.query.filter_by(status='pending').count()
    return jsonify({'count': count})


@app.route('/api/kitchen/orders/<int:order_id>/status', methods=['POST'])
def api_kitchen_order_status(order_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data   = request.json or {}
    status = data.get('status', '').strip()
    if status not in ('completed', 'cancelled'):
        return jsonify({'error': 'status must be completed or cancelled'}), 400

    ko = db.session.get(KitchenOrder, order_id)
    if not ko:
        return jsonify({'error': 'Order not found'}), 404
    if ko.status != 'pending':
        return jsonify({'error': 'Order already resolved'}), 400

    ko.status = status
    if status == 'completed':
        ko.completed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'wait_seconds': _serialize_kitchen_order(ko)['wait_seconds']})


@app.route('/api/kitchen/orders/<int:order_id>/move', methods=['POST'])
def api_kitchen_order_move(order_id):
    """Move an order up or down in the queue by swapping sort_order values."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data      = request.json or {}
    direction = data.get('direction')   # 'up' or 'down'
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be up or down'}), 400

    ko = db.session.get(KitchenOrder, order_id)
    if not ko or ko.status != 'pending':
        return jsonify({'error': 'Order not found or not pending'}), 404

    # Get all pending orders sorted by current sort_order
    all_pending = (KitchenOrder.query
                   .filter_by(status='pending')
                   .order_by(KitchenOrder.sort_order.asc(), KitchenOrder.queued_at.asc())
                   .all())

    idx = next((i for i, o in enumerate(all_pending) if o.id == order_id), None)
    if idx is None:
        return jsonify({'error': 'Order not found in pending queue'}), 404

    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if swap_idx < 0 or swap_idx >= len(all_pending):
        return jsonify({'ok': True, 'note': 'Already at boundary'})

    # Perform the swap then renumber entire queue cleanly 0,1,2,...
    all_pending[idx], all_pending[swap_idx] = all_pending[swap_idx], all_pending[idx]
    for i, order in enumerate(all_pending):
        order.sort_order = i
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/kitchen/orders/sale/<sale_id>/status', methods=['POST'])
def api_kitchen_sale_status(sale_id):
    """Mark all pending kitchen orders for a sale as completed or cancelled."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data   = request.json or {}
    status = data.get('status', '').strip()
    if status not in ('completed', 'cancelled'):
        return jsonify({'error': 'status must be completed or cancelled'}), 400

    orders = KitchenOrder.query.filter_by(sale_id=sale_id, status='pending').all()
    if not orders:
        return jsonify({'error': 'No pending orders found for this sale'}), 404

    now = datetime.utcnow()
    for ko in orders:
        ko.status = status
        if status == 'completed':
            ko.completed_at = now
    db.session.commit()

    wait = int((now - orders[0].queued_at).total_seconds()) if orders[0].queued_at else None
    return jsonify({'ok': True, 'wait_seconds': wait})


@app.route('/api/kitchen/orders/sale/<sale_id>/move', methods=['POST'])
def api_kitchen_sale_move(sale_id):
    """Move all orders for a sale up or down in the queue as a group."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data      = request.json or {}
    direction = data.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be up or down'}), 400

    # Build the current queue grouped by sale_id, ordered by the minimum sort_order in each group
    all_pending = (KitchenOrder.query
                   .filter_by(status='pending')
                   .order_by(KitchenOrder.sort_order.asc(), KitchenOrder.queued_at.asc())
                   .all())

    # Group into list-of-groups preserving encounter order
    seen = {}
    groups = []  # list of (sale_id, [orders])
    for o in all_pending:
        if o.sale_id not in seen:
            seen[o.sale_id] = len(groups)
            groups.append((o.sale_id, []))
        groups[seen[o.sale_id]][1].append(o)

    idx = next((i for i, (sid, _) in enumerate(groups) if sid == sale_id), None)
    if idx is None:
        return jsonify({'error': 'Sale not found in pending queue'}), 404

    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if swap_idx < 0 or swap_idx >= len(groups):
        return jsonify({'ok': True, 'note': 'Already at boundary'})

    groups[idx], groups[swap_idx] = groups[swap_idx], groups[idx]

    # Renumber all orders sequentially according to new group order
    sort_counter = 0
    for _, grp_orders in groups:
        for o in grp_orders:
            o.sort_order = sort_counter
            sort_counter += 1
    db.session.commit()
    return jsonify({'ok': True})


# -----------------------------
# CSV Exports (admin)
# -----------------------------
@app.route('/admin/export/products')
def export_products_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    sio = StringIO()
    sio.write('id,name,price,barcode,product_type,stock_qty,unit_type,base_unit\n')
    for p in Product.query.order_by(Product.id.asc()).all():
        sio.write(f"{p.id},{p.name},{p.price},{p.barcode},{p.product_type},{p.stock_qty},{p.unit_type},{p.base_unit}\n")
    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name='products.csv')

@app.route('/admin/export/transactions')
def export_transactions_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    start_param = request.args.get('start')
    end_param   = request.args.get('end')
    today       = date.today()
    start_dt    = _parse_dt(start_param) or datetime(today.year, today.month, today.day)
    end_dt      = _parse_dt(end_param, is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    q = (db.session.query(
            Sale.sale_id,
            func.min(Sale.date_time).label('dt'),
            func.sum(Sale.qty * Sale.unit_price).label('total'))
         .filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
         .group_by(Sale.sale_id)
         .order_by(func.max(Sale.id).desc()))

    sio = StringIO()
    sio.write('id,date_time,total\n')
    for row in q.all():
        iso   = row.dt.isoformat() if row.dt else ''
        total = round(float(row.total or 0), 2)
        sio.write(f"{row.sale_id},{iso},{total}\n")

    buf   = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    fname = f"sales_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv"
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=fname)


# -----------------------------
# Settings (admin)
# -----------------------------
@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if request.method == 'GET':
        return jsonify({'markup_percent': float(get_setting('markup_percent', 20) or 20)})
    data = request.json or {}
    mp   = data.get('markup_percent')
    try:
        mp = float(mp)
    except Exception:
        return jsonify({'error': 'Invalid markup_percent'}), 400
    set_setting('markup_percent', mp)
    return jsonify({'ok': True})


# -----------------------------
# Stats (admin)
# -----------------------------
@app.route('/api/stats/today')
def api_stats_today():
    # Legacy alias — redirects to /api/stats with today's date
    today = date.today().isoformat()
    request.args = request.args.copy()
    from werkzeug.datastructures import ImmutableMultiDict
    request.args = ImmutableMultiDict([('start', today), ('end', today)])
    return api_stats()

@app.route('/api/stats')
def api_stats():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    today = date.today()
    try:
        start_dt = datetime.fromisoformat(request.args.get('start', today.isoformat()))
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(request.args.get('end', today.isoformat()))
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    rows = (db.session.query(Sale)
            .filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
            .all())

    # ── Core totals ──
    transactions_count = len({r.sale_id for r in rows})
    total_sales_value  = float(sum(Decimal(str(r.qty)) * r.unit_price for r in rows))
    total_items_sold   = float(sum(r.qty for r in rows))

    basket_value_map = defaultdict(float)
    basket_qty_map   = defaultdict(float)
    for r in rows:
        val = float(Decimal(str(r.qty)) * r.unit_price)
        basket_value_map[r.sale_id] += val
        basket_qty_map[r.sale_id]   += float(r.qty)
    avg_basket_value = (sum(basket_value_map.values()) / len(basket_value_map)) if basket_value_map else 0.0
    avg_basket_qty   = (sum(basket_qty_map.values())   / len(basket_qty_map))   if basket_qty_map   else 0.0

    # ── COGS & profit ──
    sale_ids = list({r.sale_id for r in rows})
    total_cogs = 0.0
    if sale_ids:
        consumptions = StockConsumption.query.filter(StockConsumption.sale_id.in_(sale_ids)).all()
        total_cogs   = float(sum(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)) for c in consumptions))
    gross_profit = total_sales_value - total_cogs
    gross_margin = round(gross_profit / total_sales_value * 100, 1) if total_sales_value > 0 else None

    # ── Write-offs ──
    writeoffs = StockAdjustment.query.filter(
        StockAdjustment.adjustment_type == 'writeoff',
        StockAdjustment.adjusted_at >= start_dt,
        StockAdjustment.adjusted_at <= end_dt
    ).all()
    total_writeoff_cost  = float(sum(float(w.cost_written_off or 0) for w in writeoffs))
    total_writeoff_count = len(writeoffs)

    # ── Kitchen ──
    kitchen_in_range = KitchenOrder.query.filter(
        KitchenOrder.queued_at >= start_dt, KitchenOrder.queued_at <= end_dt
    ).all()
    kitchen_completed_list = [k for k in kitchen_in_range if k.status == 'completed']
    kitchen_count = len(kitchen_completed_list)
    now_dt = datetime.utcnow()
    pending_orders = KitchenOrder.query.filter_by(status='pending').order_by(KitchenOrder.queued_at.asc()).all()
    max_wait_seconds = None
    if pending_orders:
        max_wait_seconds = round((now_dt - pending_orders[0].queued_at).total_seconds(), 0)
    # Average completed wait time in the period
    completed_waits = [
        (k.completed_at - k.queued_at).total_seconds()
        for k in kitchen_completed_list if k.completed_at and k.queued_at
    ]
    avg_completed_wait = round(sum(completed_waits) / len(completed_waits)) if completed_waits else None

    # ── Top products by qty AND revenue ──
    top_qty_map     = defaultdict(float)
    top_revenue_map = defaultdict(float)
    for r in rows:
        top_qty_map[r.product_id]     += float(r.qty)
        top_revenue_map[r.product_id] += float(Decimal(str(r.qty)) * r.unit_price)

    all_pids = set(top_qty_map.keys()) | set(top_revenue_map.keys())
    name_map = {p.id: p.name for p in Product.query.filter(Product.id.in_(all_pids)).all()} if all_pids else {}

    top_by_qty = [
        {'product_id': pid, 'name': name_map.get(pid, str(pid)), 'qty_sold': qty,
         'revenue': round(top_revenue_map.get(pid, 0), 2)}
        for pid, qty in sorted(top_qty_map.items(), key=lambda x: x[1], reverse=True)[:10]
    ]
    top_by_revenue = [
        {'product_id': pid, 'name': name_map.get(pid, str(pid)),
         'revenue': round(rev, 2), 'qty_sold': round(top_qty_map.get(pid, 0), 2)}
        for pid, rev in sorted(top_revenue_map.items(), key=lambda x: x[1], reverse=True)[:10]
    ]

    # ── Revenue by hour (today view) ──
    revenue_per_hour = defaultdict(float)
    for r in rows:
        revenue_per_hour[r.date_time.hour] += float(Decimal(str(r.qty)) * r.unit_price)
    hourly = [{'hour': h, 'revenue': round(v, 2)} for h, v in sorted(revenue_per_hour.items())]

    # ── Revenue by day (multi-day view) ──
    revenue_per_day  = defaultdict(float)
    tx_per_day       = defaultdict(set)
    profit_per_day   = defaultdict(float)
    for r in rows:
        d = r.date_time.date().isoformat()
        revenue_per_day[d] += float(Decimal(str(r.qty)) * r.unit_price)
        tx_per_day[d].add(r.sale_id)
    # Attach COGS per day via consumptions
    if sale_ids:
        sale_date_map = {r.sale_id: r.date_time.date().isoformat() for r in rows}
        for c in consumptions:
            d = sale_date_map.get(c.sale_id)
            if d:
                profit_per_day[d] += float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)))
    daily = [
        {
            'date':        d,
            'revenue':     round(revenue_per_day[d], 2),
            'profit':      round(revenue_per_day[d] - profit_per_day.get(d, 0), 2),
            'tx_count':    len(tx_per_day[d]),
        }
        for d in sorted(revenue_per_day.keys())
    ]

    # ── Best / worst day ──
    best_day  = max(daily, key=lambda x: x['revenue'], default=None)
    worst_day = min(daily, key=lambda x: x['revenue'], default=None) if len(daily) > 1 else None

    # ── Revenue by minute (only useful for single-day ranges) ──
    revenue_per_minute = defaultdict(float)
    for r in rows:
        minute_key = r.date_time.strftime('%H:%M')
        revenue_per_minute[minute_key] += float(Decimal(str(r.qty)) * r.unit_price)
    minutely = [{'minute': m, 'revenue': round(v, 2)} for m, v in sorted(revenue_per_minute.items())]

    # ── Employee stats ──
    emp_revenue   = defaultdict(float)
    emp_tx        = defaultdict(set)
    emp_items     = defaultdict(float)
    emp_first     = {}
    emp_last      = {}
    for r in rows:
        uid = r.user_id or 0
        val = float(Decimal(str(r.qty)) * r.unit_price)
        emp_revenue[uid] += val
        emp_tx[uid].add(r.sale_id)
        emp_items[uid]   += float(r.qty)
        dt = r.date_time
        if uid not in emp_first or dt < emp_first[uid]: emp_first[uid] = dt
        if uid not in emp_last  or dt > emp_last[uid]:  emp_last[uid]  = dt

    # Session durations — only sessions that started within the range, capped at range end.
    # Excludes zombie sessions (started before range, never logged out) which would
    # otherwise inflate time by 24h per unclosed session.
    sessions_in_range = UserSession.query.filter(
        UserSession.logged_in >= start_dt,
        UserSession.logged_in <= end_dt
    ).all()
    emp_session_minutes = defaultdict(float)
    emp_session_count   = defaultdict(int)
    emp_sessions        = defaultdict(list)
    emp_first_login     = {}   # earliest login in range
    emp_last_activity   = {}   # latest last_active / logged_out in range
    now_utc = datetime.utcnow()
    for s in sessions_in_range:
        natural_end  = s.logged_out or now_utc
        clamped_end  = min(natural_end, end_dt, now_utc)
        duration_min = (clamped_end - s.logged_in).total_seconds() / 60.0
        if duration_min <= 0:
            continue
        emp_session_minutes[s.user_id] += duration_min
        emp_session_count[s.user_id]   += 1
        emp_sessions[s.user_id].append({
            'login':       s.logged_in.isoformat(),
            'logout':      s.logged_out.isoformat() if s.logged_out else None,
            'last_active': s.last_active.isoformat() if s.last_active else None,
            'duration_min': round(duration_min, 1),
            'open':        s.logged_out is None,
        })
        # Track span for rate calculation
        uid = s.user_id
        if uid not in emp_first_login or s.logged_in < emp_first_login[uid]:
            emp_first_login[uid] = s.logged_in
        activity_end = s.last_active or clamped_end
        if uid not in emp_last_activity or activity_end > emp_last_activity[uid]:
            emp_last_activity[uid] = activity_end

    # Build name map from ALL user IDs that appear in sales or sessions
    all_user_ids = list(
        {r.user_id for r in rows if r.user_id} | set(emp_session_minutes.keys())
    )
    user_name_map = {u.id: u.username for u in User.query.filter(User.id.in_(all_user_ids)).all()} if all_user_ids else {}

    employee_stats = []
    for uid in set(list(emp_revenue.keys()) + list(emp_session_minutes.keys())):
        if uid == 0:
            continue
        name         = user_name_map.get(uid, f'User {uid}')
        tx_count     = len(emp_tx.get(uid, set()))
        rev          = emp_revenue.get(uid, 0)
        items        = emp_items.get(uid, 0)
        sess_mins    = emp_session_minutes.get(uid, 0)
        sess_count   = emp_session_count.get(uid, 0)
        # Work span = first login to last activity — more realistic denominator for rates
        first_login    = emp_first_login.get(uid)
        last_activity  = emp_last_activity.get(uid)
        if first_login and last_activity and last_activity > first_login:
            work_span_mins = (last_activity - first_login).total_seconds() / 60.0
        else:
            work_span_mins = sess_mins
        rev_per_hour = (rev / (work_span_mins / 60)) if work_span_mins > 0 else None
        tx_per_hour  = (tx_count / (work_span_mins / 60)) if work_span_mins > 0 else None
        avg_tx_val   = (rev / tx_count) if tx_count > 0 else 0
        first_sale   = emp_first.get(uid)
        last_sale    = emp_last.get(uid)
        employee_stats.append({
            'user_id':         uid,
            'name':            name,
            'transactions':    tx_count,
            'revenue':         round(rev, 2),
            'items_sold':      round(items, 2),
            'avg_tx_value':    round(avg_tx_val, 2),
            'session_count':   sess_count,
            'session_minutes': round(sess_mins, 1),
            'revenue_per_hour': round(rev_per_hour, 2) if rev_per_hour is not None else None,
            'tx_per_hour':     round(tx_per_hour, 2) if tx_per_hour is not None else None,
            'first_sale':      first_sale.isoformat() if first_sale else None,
            'last_sale':       last_sale.isoformat() if last_sale else None,
            'sessions':        sorted(emp_sessions.get(uid, []), key=lambda x: x['login']),
        })
    employee_stats.sort(key=lambda x: x['revenue'], reverse=True)

    # ── Supplier cost breakdown ──
    supplier_costs = defaultdict(float)
    batches_in_range = StockBatch.query.filter(
        StockBatch.purchased_at >= start_dt, StockBatch.purchased_at <= end_dt
    ).all()
    for b in batches_in_range:
        total_cost = float(b.qty_purchased_base) * float(b.cost_per_base_unit)
        sup_name = db.session.get(Supplier, b.supplier_id).name if b.supplier_id else 'Unknown'
        supplier_costs[sup_name] += total_cost
    supplier_breakdown = [
        {'supplier': k, 'total_cost': round(v, 2)}
        for k, v in sorted(supplier_costs.items(), key=lambda x: x[1], reverse=True)
    ]

    return jsonify({
        'transactions_count':    transactions_count,
        'total_sales_value':     round(total_sales_value, 2),
        'total_items_sold':      round(total_items_sold, 2),
        'avg_basket_value':      round(avg_basket_value, 2),
        'avg_basket_qty':        round(avg_basket_qty, 2),
        'total_cogs':            round(total_cogs, 2),
        'gross_profit':          round(gross_profit, 2),
        'gross_margin':          gross_margin,
        'total_writeoff_cost':   round(total_writeoff_cost, 2),
        'writeoff_count':        total_writeoff_count,
        'kitchen_orders_today':   kitchen_count,
        'avg_wait_seconds':       max_wait_seconds,
        'avg_completed_wait':     avg_completed_wait,
        'top_products':          top_by_qty,
        'top_by_revenue':        top_by_revenue,
        'revenue_per_hour':      hourly,
        'revenue_per_day':       daily,
        'best_day':              best_day,
        'worst_day':             worst_day,
        'supplier_breakdown':    supplier_breakdown,
        'revenue_per_minute':    minutely,
        'employee_stats':        employee_stats,
    })


@app.route('/api/stats/drilldown')
def api_stats_drilldown():
    """Return all transactions for a specific slice (day, hour, minute, product, user)."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    slice_type = request.args.get('type')   # day | hour | minute | product | user | range
    slice_val  = request.args.get('value')  # ISO date, hour int, HH:MM, product_id, user_id
    start_arg  = request.args.get('start')
    end_arg    = request.args.get('end')

    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)

    if slice_type == 'day' and slice_val:
        try:
            d = date.fromisoformat(slice_val)
            q = q.filter(Sale.date_time >= datetime(d.year, d.month, d.day),
                         Sale.date_time <= datetime(d.year, d.month, d.day, 23, 59, 59))
        except Exception:
            pass
    elif slice_type == 'hour' and slice_val is not None:
        q = q.filter(db.func.extract('hour', Sale.date_time) == int(slice_val))
    elif slice_type == 'minute' and slice_val:
        try:
            hh, mm = slice_val.split(':')
            q = q.filter(db.func.extract('hour',   Sale.date_time) == int(hh),
                         db.func.extract('minute', Sale.date_time) == int(mm))
        except Exception:
            pass
    elif slice_type == 'product' and slice_val:
        q = q.filter(Sale.product_id == int(slice_val))
    elif slice_type == 'user' and slice_val:
        q = q.filter(Sale.user_id == int(slice_val))

    rows = q.order_by(Sale.date_time.desc()).all()

    # Group by sale_id
    sale_map = defaultdict(list)
    for r in rows:
        sale_map[r.sale_id].append(r)

    pids = {r.product_id for r in rows}
    uids = {r.user_id for r in rows if r.user_id}
    prod_names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    user_names = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    transactions = []
    for sid, sale_rows in sale_map.items():
        sale_rows_sorted = sorted(sale_rows, key=lambda r: r.date_time)
        total = float(sum(Decimal(str(r.qty)) * r.unit_price for r in sale_rows))
        transactions.append({
            'sale_id':   sid[:8],
            'date_time': sale_rows_sorted[0].date_time.isoformat(),
            'teller':    user_names.get(sale_rows_sorted[0].user_id, '—'),
            'total':     round(total, 2),
            'item_count': sum(float(r.qty) for r in sale_rows),
            'lines': [
                {
                    'product':    prod_names.get(r.product_id, str(r.product_id)),
                    'qty':        float(r.qty),
                    'unit_price': float(r.unit_price),
                    'line_total': round(float(Decimal(str(r.qty)) * r.unit_price), 2),
                }
                for r in sorted(sale_rows, key=lambda x: x.product_id)
            ],
        })
    transactions.sort(key=lambda x: x['date_time'], reverse=True)

    # ── Summary block ──
    total_revenue  = sum(t['total'] for t in transactions)
    total_tx       = len(transactions)
    avg_tx_value   = total_revenue / total_tx if total_tx else 0
    largest_sale   = max(transactions, key=lambda x: x['total'], default=None)
    smallest_sale  = min(transactions, key=lambda x: x['total'], default=None) if total_tx > 1 else None

    # Top products in this slice
    prod_rev   = defaultdict(float)
    prod_qty   = defaultdict(float)
    for t in transactions:
        for l in t['lines']:
            prod_rev[l['product']]  += l['line_total']
            prod_qty[l['product']]  += l['qty']
    top_products = sorted(
        [{'product': p, 'revenue': round(v, 2), 'qty': round(prod_qty[p], 2)} for p, v in prod_rev.items()],
        key=lambda x: x['revenue'], reverse=True
    )[:5]

    # Peak hour in this slice
    hour_rev = defaultdict(float)
    for t in transactions:
        h = int(t['date_time'][11:13])
        hour_rev[h] += t['total']
    peak_hour = max(hour_rev, key=hour_rev.get) if hour_rev else None

    # Teller breakdown
    teller_rev = defaultdict(float)
    teller_tx  = defaultdict(int)
    for t in transactions:
        teller_rev[t['teller']] += t['total']
        teller_tx[t['teller']]  += 1
    teller_breakdown = sorted(
        [{'teller': k, 'revenue': round(v, 2), 'tx_count': teller_tx[k]} for k, v in teller_rev.items()],
        key=lambda x: x['revenue'], reverse=True
    )

    summary = {
        'total_revenue':  round(total_revenue, 2),
        'tx_count':       total_tx,
        'avg_tx_value':   round(avg_tx_value, 2),
        'largest_sale':   largest_sale,
        'smallest_sale':  smallest_sale,
        'top_products':   top_products,
        'peak_hour':      peak_hour,
        'teller_breakdown': teller_breakdown,
    }

    return jsonify({'summary': summary, 'transactions': transactions})


@app.route('/api/stats/drilldown/supplier')
def api_stats_drilldown_supplier():
    """Return stock batches purchased from a supplier in the date range."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    supplier_name = request.args.get('supplier', '')
    start_arg     = request.args.get('start')
    end_arg       = request.args.get('end')
    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    if supplier_name and supplier_name != 'Unknown':
        sup = Supplier.query.filter_by(name=supplier_name).first()
        if sup:
            batches = StockBatch.query.filter(
                StockBatch.supplier_id == sup.id,
                StockBatch.purchased_at >= start_dt,
                StockBatch.purchased_at <= end_dt
            ).order_by(StockBatch.purchased_at.desc()).all()
        else:
            batches = []
    else:
        batches = StockBatch.query.filter(
            StockBatch.supplier_id == None,
            StockBatch.purchased_at >= start_dt,
            StockBatch.purchased_at <= end_dt
        ).order_by(StockBatch.purchased_at.desc()).all()

    pids = {b.product_id for b in batches}
    prod_names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}

    result = []
    for b in batches:
        total = float(b.qty_purchased_base) * float(b.cost_per_base_unit)
        result.append({
            'date':        b.purchased_at.isoformat(),
            'product':     prod_names.get(b.product_id, str(b.product_id)),
            'qty_base':    float(b.qty_purchased_base),
            'cost_per_unit': float(b.cost_per_base_unit),
            'total_cost':  round(total, 2),
            'remaining':   float(b.qty_remaining_base),
        })
    return jsonify(result)


@app.route('/api/stats/drilldown/kitchen')
def api_stats_drilldown_kitchen():
    """Return kitchen orders in the date range."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    start_arg = request.args.get('start')
    end_arg   = request.args.get('end')
    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    orders = KitchenOrder.query.filter(
        KitchenOrder.queued_at >= start_dt,
        KitchenOrder.queued_at <= end_dt
    ).order_by(KitchenOrder.queued_at.desc()).all()

    uids = {o.teller_id for o in orders if o.teller_id}
    user_names = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    result = []
    for o in orders:
        wait = None
        if o.completed_at and o.queued_at:
            wait = round((o.completed_at - o.queued_at).total_seconds())
        result.append({
            'id':           o.id,
            'sale_id':      o.sale_id[:8],
            'product':      o.product_name,
            'qty':          float(o.qty),
            'status':       o.status,
            'teller':       user_names.get(o.teller_id, '—'),
            'queued_at':    o.queued_at.isoformat() if o.queued_at else None,
            'completed_at': o.completed_at.isoformat() if o.completed_at else None,
            'wait_seconds': wait,
            'notes':        o.notes or '',
        })
    return jsonify(result)


@app.route('/api/stats/drilldown/writeoffs')
def api_stats_drilldown_writeoffs():
    """Return write-off adjustments in the date range."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    start_arg = request.args.get('start')
    end_arg   = request.args.get('end')
    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    writeoffs = StockAdjustment.query.filter(
        StockAdjustment.adjustment_type == 'writeoff',
        StockAdjustment.adjusted_at >= start_dt,
        StockAdjustment.adjusted_at <= end_dt
    ).order_by(StockAdjustment.adjusted_at.desc()).all()

    pids  = {w.product_id for w in writeoffs}
    uids  = {w.user_id for w in writeoffs if w.user_id}
    prods = {p.id: p for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    users = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    result = []
    for w in writeoffs:
        p = prods.get(w.product_id)
        result.append({
            'date':       w.adjusted_at.isoformat() if w.adjusted_at else None,
            'product':    p.name if p else str(w.product_id),
            'qty_change': float(w.qty_change_base),
            'base_unit':  p.base_unit if p else '',
            'cost':       float(w.cost_written_off) if w.cost_written_off else 0,
            'by':         users.get(w.user_id, '—'),
        })
    return jsonify(result)


@app.route('/api/stats/drilldown/profit')
def api_stats_drilldown_profit():
    """Return per-product profit breakdown for the date range."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    start_arg = request.args.get('start')
    end_arg   = request.args.get('end')
    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    rows = db.session.query(Sale).filter(
        Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False
    ).all()

    sale_ids = list({r.sale_id for r in rows})
    consumptions = StockConsumption.query.filter(
        StockConsumption.sale_id.in_(sale_ids)
    ).all() if sale_ids else []

    # Revenue per product
    rev_map = defaultdict(float)
    qty_map = defaultdict(float)
    for r in rows:
        rev_map[r.product_id] += float(Decimal(str(r.qty)) * r.unit_price)
        qty_map[r.product_id] += float(r.qty)

    # COGS: map consumption back to sale → product
    sale_product_map = {}
    for r in rows:
        if r.sale_id not in sale_product_map:
            sale_product_map[r.sale_id] = r.product_id

    cogs_map = defaultdict(float)
    for c in consumptions:
        pid = sale_product_map.get(c.sale_id)
        if pid:
            cogs_map[pid] += float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)))

    all_pids = set(rev_map.keys())
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(all_pids)).all()} if all_pids else {}

    result = []
    for pid in sorted(all_pids, key=lambda x: rev_map[x], reverse=True):
        rev  = rev_map[pid]
        cogs = cogs_map.get(pid, 0)
        profit = rev - cogs
        margin = round(profit / rev * 100, 1) if rev > 0 else None
        result.append({
            'product':  names.get(pid, str(pid)),
            'qty_sold': round(qty_map[pid], 2),
            'revenue':  round(rev, 2),
            'cogs':     round(cogs, 2),
            'profit':   round(profit, 2),
            'margin':   margin,
        })
    return jsonify(result)


# -----------------------------
# UI / Diagnostics
# -----------------------------
@app.route('/')
def index():
    return render_template('index.html', app_env=os.getenv('APP_ENV', 'qa'))

@app.route('/guide')
def user_guide():
    return render_template('user_guide.html')

@app.route('/api/logs')
def api_logs():
    """Admin-only: return last N lines of the log file."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    n = int(request.args.get('n', 200))
    try:
        with open(LOG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return jsonify({'lines': lines[-n:], 'total': len(lines), 'path': LOG_PATH})
    except FileNotFoundError:
        return jsonify({'lines': [], 'total': 0, 'path': LOG_PATH})


@app.route('/api/db-health')
def api_db_health():
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/__version')
def version():
    return jsonify({'version': APP_VERSION})

if __name__ == '__main__':
    _cert = os.path.join(os.path.dirname(__file__), 'cert.pem')
    _key  = os.path.join(os.path.dirname(__file__), 'cert.key')
    _ssl  = (_cert, _key) if os.path.exists(_cert) and os.path.exists(_key) else None
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5443' if _ssl else '5000')), ssl_context=_ssl)

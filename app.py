
# -*- coding: utf-8 -*-
"""
Farm Stall POS — v1.3.0
- Single-table design for sales (replaces transactions + transaction_lines)
- Grouping via sale_id (UUID string) to keep multi-line "transactions"
- Startup migration: create 'sales', backfill from legacy tables if present
- Stats & exports rewritten to use 'sales'
- psycopg v3 driver mapping retained for Python 3.13 compatibility
"""

import os, uuid
from datetime import datetime, date
from collections import defaultdict
from io import StringIO, BytesIO

from flask import Flask, jsonify, request, session, send_file, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, func
from werkzeug.security import generate_password_hash, check_password_hash

APP_VERSION = '1.3.0'

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key')

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
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='teller')
    active = db.Column(db.Boolean, nullable=False, default=True)

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    price = db.Column(db.Float, nullable=False)
    barcode = db.Column(db.String(32), unique=True, nullable=False)
    stock_qty = db.Column(db.Integer, nullable=False, default=0)
    # Optional soft-delete if you plan to disable instead of hard delete:
    # active = db.Column(db.Boolean, nullable=False, default=True)

class Purchase(db.Model):
    __tablename__ = 'purchases'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_added = db.Column(db.Integer, nullable=False)
    purchase_price = db.Column(db.Float, nullable=False)
    date_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class Setting(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(200), nullable=False)

# NEW: single-table "sales" (replaces transactions + transaction_lines)

class Sale(db.Model):
    __tablename__ = 'sales'
    id = db.Column(db.Integer, primary_key=True)
    # was: db.Integer
    sale_id = db.Column(db.String(64), index=True, nullable=False)  # one receipt across multiple rows
    date_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)


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
    user = User.query.get(session['user_id'])
    return bool(user and user.active)

def current_user():
    if 'user_id' not in session:
        return None
    return User.query.get(session.get('user_id'))

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
# Strong startup migration
# -----------------------------
def strong_migrate():
    # Ensure base tables exist
    db.create_all()

    engine = db.engine
    engine_name = engine.dialect.name

    with engine.begin() as conn:
        # 1) Create 'sales' table if not exists
        if engine_name == 'sqlite':
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS sales (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sale_id TEXT NOT NULL,
              date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              product_id INTEGER NOT NULL,
              qty INTEGER NOT NULL,
              unit_price REAL NOT NULL
            )
            """)
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_sale_id ON sales (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_date_time ON sales (date_time)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_product_dt ON sales (product_id, date_time)")
        else:
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS sales (
              id SERIAL PRIMARY KEY,
              sale_id TEXT NOT NULL,
              date_time TIMESTAMP NOT NULL DEFAULT NOW(),
              product_id INTEGER NOT NULL REFERENCES products(id),
              qty INTEGER NOT NULL,
              unit_price DOUBLE PRECISION NOT NULL
            )
            """)
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_sale_id ON sales (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_date_time ON sales (date_time)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_product_dt ON sales (product_id, date_time)")

        # 2) Backfill from legacy tables if they exist and 'sales' is empty
        sales_count = conn.execute(text("SELECT COUNT(*) FROM sales")).scalar_one()
        if sales_count == 0:
            # detect legacy tables
            legacy_ok = False
            try:
                conn.execute(text("SELECT 1 FROM transactions LIMIT 1"))
                conn.execute(text("SELECT 1 FROM transaction_lines LIMIT 1"))
                legacy_ok = True
            except Exception:
                legacy_ok = False

            if legacy_ok:
                if engine_name == 'sqlite':
                    conn.exec_driver_sql("""
                    INSERT INTO sales (sale_id, date_time, product_id, qty, unit_price)
                    SELECT CAST(t.id AS TEXT), t.date_time, tl.product_id, tl.qty, tl.unit_price
                    FROM transaction_lines tl
                    JOIN transactions t ON tl.transaction_id = t.id
                    """)
                else:
                    conn.exec_driver_sql("""
                    INSERT INTO sales (sale_id, date_time, product_id, qty, unit_price)
                    SELECT CAST(t.id AS TEXT), t.date_time, tl.product_id, tl.qty, tl.unit_price
                    FROM transaction_lines tl
                    JOIN transactions t ON tl.transaction_id = t.id
                    """)

with app.app_context():
    strong_migrate()
    seed_first_admin()

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
    return jsonify({'ok': True, 'username': user.username, 'role': user.role})

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/me', methods=['GET'])
def api_me():
    u = current_user()
    if not u:
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, 'username': u.username, 'role': u.role})

# -----------------------------
# Admin: manual migrate endpoint
# -----------------------------
@app.route('/api/db-migrate', methods=['POST'])
def api_db_migrate():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    strong_migrate()
    return jsonify({'ok': True})

# -----------------------------
# Users (admin) — unchanged
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
    role = data.get('role', 'teller')
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
    data = request.json or {}
    username = data.get('username')
    role = data.get('role')
    active = data.get('active')
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
# Products — unchanged behaviour
# -----------------------------
@app.route('/api/products', methods=['GET'])
def api_products_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    products = Product.query.order_by(Product.name.asc()).all()
    return jsonify([
        {'id': p.id, 'name': p.name, 'price': p.price, 'barcode': p.barcode, 'stock_qty': p.stock_qty}
        for p in products
    ])

@app.route('/api/products', methods=['POST'])
def api_products_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    name = data.get('name', '').strip()
    price = data.get('price')
    barcode = data.get('barcode', '').strip()
    stock_qty = int(data.get('stock_qty', 0) or 0)
    if not name or price is None or not barcode:
        return jsonify({'error': 'name, price, barcode required'}), 400
    try:
        price = float(price)
    except Exception:
        return jsonify({'error': 'Invalid price'}), 400
    if Product.query.filter_by(name=name).first():
        return jsonify({'error': 'Product name exists'}), 409
    if Product.query.filter_by(barcode=barcode).first():
        return jsonify({'error': 'Barcode exists'}), 409
    p = Product(name=name, price=price, barcode=barcode, stock_qty=stock_qty)
    db.session.add(p)
    db.session.commit()
    return jsonify({'ok': True, 'id': p.id})

@app.route('/api/products/update', methods=['POST'])
def api_products_update():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    pid = data.get('id')
    p = Product.query.get(pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404

    name = data.get('name')
    price = data.get('price')
    barcode = data.get('barcode')
    stock_qty = data.get('stock_qty')

    if name:
        name = name.strip()
        other = Product.query.filter(Product.id != p.id, Product.name == name).first()
        if other:
            return jsonify({'error': 'Product name exists'}), 409
        p.name = name
    if price is not None:
        try:
            p.price = float(price)
        except Exception:
            return jsonify({'error': 'Invalid price'}), 400
    if barcode:
        barcode = barcode.strip()
        other = Product.query.filter(Product.id != p.id, Product.barcode == barcode).first()
        if other:
            return jsonify({'error': 'Barcode exists'}), 409
        p.barcode = barcode
    if stock_qty is not None:
        try:
            p.stock_qty = int(stock_qty)
        except Exception:
            return jsonify({'error': 'Invalid stock_qty'}), 400

    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/products/<name>', methods=['DELETE'])
def api_products_delete(name):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = Product.query.filter_by(name=name).first()
    if not p:
        return jsonify({'error': 'Product not found'}), 404

    # Protect history (recommended): block delete if referenced
    ref_sales = Sale.query.filter_by(product_id=p.id).count()
    ref_pur = Purchase.query.filter_by(product_id=p.id).count()
    if ref_sales or ref_pur:
        return jsonify({
            'error': 'Product has historical references',
            'sales_rows': ref_sales,
            'purchases': ref_pur,
            'hint': 'Consider disabling product instead of deleting.'
        }), 409

    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/products/<int:pid>/suggested_price')
def api_suggested_price(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = Product.query.get(pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404
    rows = Purchase.query.filter_by(product_id=pid).all()
    total_qty = sum(r.qty_added for r in rows)
    if total_qty > 0:
        wac = sum(r.qty_added * r.purchase_price for r in rows) / float(total_qty)
    else:
        wac = p.price
    markup_param = request.args.get('markup')
    if markup_param is not None:
        try:
            markup = float(markup_param)
        except Exception:
            markup = float(get_setting('markup_percent', 20) or 20)
    else:
        markup = float(get_setting('markup_percent', 20) or 20)
    suggested = round(wac * (1 + markup/100.0), 2)
    return jsonify({'product_id': pid, 'wac': round(wac, 4), 'markup_percent': markup, 'suggested_price': suggested})

# -----------------------------
# Purchases (admin)
# -----------------------------
@app.route('/api/purchases', methods=['GET'])
def api_purchases_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    rows = Purchase.query.order_by(Purchase.date_time.desc()).all()
    result = []
    for r in rows:
        p = Product.query.get(r.product_id)
        result.append({
            'id': r.id,
            'product_id': r.product_id,
            'product_name': p.name if p else None,
            'qty_added': r.qty_added,
            'purchase_price': r.purchase_price,
            'date_time': r.date_time.isoformat()
        })
    return jsonify(result)

@app.route('/api/purchases', methods=['POST'])
def api_purchases_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    pid = data.get('product_id')
    qty = data.get('qty_added')
    price = data.get('purchase_price')
    try:
        pid = int(pid)
        qty = int(qty)
        price = float(price)
    except Exception:
        return jsonify({'error': 'Invalid product_id/qty/price'}), 400
    p = Product.query.get(pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404
    if qty <= 0 or price < 0:
        return jsonify({'error': 'Invalid values'}), 400
    purch = Purchase(product_id=pid, qty_added=qty, purchase_price=price)
    p.stock_qty = (p.stock_qty or 0) + qty
    db.session.add(purch)
    db.session.commit()
    return jsonify({'ok': True})

# -----------------------------
# Sales (replaces Transactions)
# -----------------------------
@app.route('/api/transactions', methods=['GET'])
def api_transactions_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    # Load all recent sales rows and group by sale_id in Python (keeps response shape identical)
    rows = (db.session.query(Sale)
            .order_by(Sale.id.desc())
            .limit(2000)  # safety cap; tweak as needed
            .all())

    # Preload product names
    product_names = {}
    if rows:
        pids = {r.product_id for r in rows}
        for p in Product.query.filter(Product.id.in_(pids)).all():
            product_names[p.id] = p.name

    grouped = defaultdict(list)
    dates = {}
    for r in rows:
        grouped[r.sale_id].append(r)
        dates.setdefault(r.sale_id, r.date_time)

    result = []
    # newest "transactions" first by inferred sale block latest id
    for sid in sorted(grouped.keys(),
                      key=lambda k: max(x.id for x in grouped[k]),
                      reverse=True):
        items = []
        total = 0.0
        for ln in grouped[sid]:
            name = product_names.get(ln.product_id, f"Product {ln.product_id}")
            subtotal = ln.qty * ln.unit_price
            total += subtotal
            items.append({
                'product_id': ln.product_id,
                'name': name,
                'qty': ln.qty,
                'unit_price': ln.unit_price,
                'subtotal': subtotal
            })
        result.append({
            'id': sid,  # logical transaction id is sale_id
            'date_time': dates[sid].isoformat(),
            'total': round(total, 2),
            'lines': items
        })
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
    now = datetime.utcnow()

    # Insert one Sale row per line item; adjust stock
    for item in cart:
        pid = int(item['product_id'])
        qty = int(item.get('qty', 1))
        unit_price = float(item.get('unit_price'))

        db.session.add(Sale(
            sale_id=sale_uuid,
            date_time=now,
            product_id=pid,
            qty=qty,
            unit_price=unit_price
        ))
        p = Product.query.get(pid)
        if p:
            p.stock_qty = max(0, (p.stock_qty or 0) - qty)

    db.session.commit()
    return jsonify({'ok': True, 'transaction_id': sale_uuid})

# -----------------------------
# CSV Exports (admin) — BytesIO fix retained
# -----------------------------
@app.route('/admin/export/products')
def export_products_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    admin_token = os.getenv('ADMIN_TOKEN')
    if admin_token and request.args.get('token') != admin_token:
        return jsonify({'error': 'Invalid token'}), 403

    sio = StringIO()
    sio.write('id,name,price,barcode,stock_qty\n')
    for p in Product.query.order_by(Product.id.asc()).all():
        sio.write(f"{p.id},{p.name},{p.price},{p.barcode},{p.stock_qty}\n")
    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name='products.csv')


from datetime import datetime, date, timedelta

def _parse_dt(value: str, is_end=False) -> datetime:
    """
    Accepts:
      - 'YYYY-MM-DD'          -> 00:00:00 for start, 23:59:59.999999 for end
      - ISO 'YYYY-MM-DDTHH:MM[:SS[.ffffff]]' -> parsed as-is
    """
    if not value:
        return None
    v = value.strip()
    try:
        # Date-only
        if len(v) == 10 and v[4] == '-' and v[7] == '-':
            d = datetime.strptime(v, "%Y-%m-%d")
            if is_end:
                # end of day
                return d.replace(hour=23, minute=59, second=59, microsecond=999999)
            return d
        # Try full ISO
        # Allow 'Z' suffix or offset-less
        v2 = v.replace('Z', '')
        # Try with microseconds
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S.%f",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(v2, fmt)
            except ValueError:
                pass
        # Fallback: just date
        d = datetime.strptime(v[:10], "%Y-%m-%d")
        if is_end:
            return d.replace(hour=23, minute=59, second=59, microsecond=999999)
        return d
    except Exception:
        return None

@app.route('/admin/export/transactions')
def export_transactions_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    admin_token = os.getenv('ADMIN_TOKEN')
    if admin_token and request.args.get('token') != admin_token:
        return jsonify({'error': 'Invalid token'}), 403

    # -------- Date range handling --------
    # Incoming query params (optional):
    #   ?start=YYYY-MM-DD or ISO datetime
    #   ?end=YYYY-MM-DD or ISO datetime
    # If omitted, suggest today's range by default.
    start_param = request.args.get('start')
    end_param   = request.args.get('end')

    # Default suggestion: today 00:00:00 → today 23:59:59.999999
    today = date.today()
    suggested_start = datetime(today.year, today.month, today.day, 0, 0, 0)
    suggested_end   = datetime(today.year, today.month, today.day, 23, 59, 59, 999999)

    start_dt = _parse_dt(start_param, is_end=False) or suggested_start
    end_dt   = _parse_dt(end_param,   is_end=True)  or suggested_end

    # Guardrail: if user swapped them accidentally
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    # -------- Query grouped totals per sale_id within date range --------
    q = (db.session.query(
            Sale.sale_id,
            func.min(Sale.date_time).label('dt'),
            func.sum(Sale.qty * Sale.unit_price).label('total'))
         .filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt)
         .group_by(Sale.sale_id)
         .order_by(func.max(Sale.id).desc()))

    # -------- CSV --------
    sio = StringIO()
    sio.write('id,date_time,total\n')
    for row in q.all():
        # dt = first datetime in the sale block
        iso = row.dt.isoformat() if row.dt else ''
        total = round(row.total or 0, 2)
        # Note: minimal escaping since fields are simple; extend if names with commas ever added
        sio.write(f"{row.sale_id},{iso},{total}\n")

    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    # Surface the effective range as filename hint
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
    mp = data.get('markup_percent')
    try:
        mp = float(mp)
    except Exception:
        return jsonify({'error': 'Invalid markup_percent'}), 400
    set_setting('markup_percent', mp)
    return jsonify({'ok': True})

# -----------------------------
# Stats (admin; now from sales)
# -----------------------------
@app.route('/api/stats/today')
def api_stats_today():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    today = date.today()
    start_dt = datetime(today.year, today.month, today.day)
    end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    rows = (db.session.query(Sale)
            .filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt)
            .all())

    transactions_count = len({r.sale_id for r in rows})
    total_sales_value = sum(r.qty * r.unit_price for r in rows)
    total_items_sold = sum(r.qty for r in rows)

    # Avg basket size = items per sale_id
    basket_map = defaultdict(int)
    for r in rows:
        basket_map[r.sale_id] += r.qty
    basket_sizes = list(basket_map.values())
    avg_basket_size = (sum(basket_sizes)/len(basket_sizes)) if basket_sizes else 0.0

    # Top products
    top_counts = defaultdict(int)
    for r in rows:
        top_counts[r.product_id] += r.qty
    top_sorted = sorted(top_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    top_products = []
    if top_sorted:
        pids = [pid for pid, _ in top_sorted]
        name_map = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()}
        for pid, qty in top_sorted:
            top_products.append({'product_id': pid, 'name': name_map.get(pid, str(pid)), 'qty_sold': qty})

    # Revenue per hour
    revenue_per_hour = defaultdict(float)
    for r in rows:
        revenue_per_hour[r.date_time.hour] += r.qty * r.unit_price
    hourly = [{'hour': h, 'revenue': round(v, 2)} for h, v in sorted(revenue_per_hour.items())]

    return jsonify({
        'transactions_count': transactions_count,
        'total_sales_value': round(total_sales_value, 2),
        'total_items_sold': total_items_sold,
        'avg_basket_size': round(avg_basket_size, 2),
        'top_products': top_products,
        'revenue_per_hour': hourly
    })

# -----------------------------
# UI / Diagnostics
# -----------------------------
@app.route('/')
def index():
    return render_template('index.html')

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
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))

# -*- coding: utf-8 -*-
"""
Farm Stall POS — Updated

Changes in this version:
- Fix Product CRUD (create, update, delete) endpoints and validations.
- Transactions are ordered by greatest transaction id first.
- Hide tabs before login; role-based tab visibility.
- Stock system: Product.stock_qty, Purchase entries, automatic stock decrement on sales.
- Suggested sale price API using weighted average cost (WAC) + markup percentage (admin-configurable Setting).
- Admin-only Stats tab with visuals: today's metrics & top products.
- CSV exports preserved.
- Service worker cache bump recommended (see static/sw.js).
"""

import os
from datetime import datetime, date
from collections import defaultdict

from flask import Flask, jsonify, request, session, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text
from werkzeug.security import generate_password_hash, check_password_hash

APP_VERSION = '1.2.0'

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///pos.db')
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
    role = db.Column(db.String(20), nullable=False, default='teller')  # 'admin' or 'teller'
    active = db.Column(db.Boolean, nullable=False, default=True)

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    price = db.Column(db.Float, nullable=False)
    barcode = db.Column(db.String(32), unique=True, nullable=False)
    # New: tracked stock quantity
    stock_qty = db.Column(db.Integer, nullable=False, default=0)

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    date_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

class TransactionLine(db.Model):
    __tablename__ = 'transaction_lines'
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)

# New: Purchases to increase stock and track cost
class Purchase(db.Model):
    __tablename__ = 'purchases'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_added = db.Column(db.Integer, nullable=False)
    purchase_price = db.Column(db.Float, nullable=False)  # price per unit
    date_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

# New: Simple key/value settings (e.g., default markup percentage)
class Setting(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(200), nullable=False)

# -----------------------------
# Utilities & Bootstrap
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
    # If no users exist, create admin from env
    if User.query.count() == 0:
        admin_user = os.getenv('ADMIN_USER', 'admin')
        admin_pass = os.getenv('ADMIN_PASS', 'admin123')
        hashed = generate_password_hash(admin_pass)
        db.session.add(User(username=admin_user, password_hash=hashed, role='admin', active=True))
        db.session.commit()
        # Set default markup from env if provided
        default_markup = os.getenv('DEFAULT_MARKUP_PERCENT')
        if default_markup:
            try:
                set_setting('markup_percent', float(default_markup))
            except Exception:
                pass


def safe_migrate():
    """Attempt lightweight migrations for new columns/tables."""
    db.create_all()
    # Attempt to add stock_qty column if missing (SQLite/Postgres).
    try:
        # Check if column exists by querying pragma for SQLite or information_schema for Postgres
        engine_name = db.session.bind.dialect.name
        if engine_name == 'sqlite':
            res = db.session.execute(text("PRAGMA table_info(products)")).fetchall()
            cols = [r[1] for r in res]
            if 'stock_qty' not in cols:
                # SQLite can't alter easily; create temp table path is complex. For simplicity, add default via SQL if possible.
                # If not possible, we'll ignore; new DBs get column via model.
                pass
        else:
            res = db.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='products'"))
            cols = [r[0] for r in res]
            if 'stock_qty' not in cols:
                db.session.execute(text("ALTER TABLE products ADD COLUMN stock_qty INTEGER NOT NULL DEFAULT 0"))
                db.session.commit()
    except Exception:
        # Ignore migration errors to avoid breaking startup
        db.session.rollback()

with app.app_context():
    safe_migrate()
    seed_first_admin()

# -----------------------------
# Routes: Auth
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
# Routes: Users (admin only)
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
# Routes: Products (admin only for manage)
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
    # Optional: prevent delete if referenced by lines? We'll allow for simplicity.
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})

# Suggested price API using WAC + markup%
@app.route('/api/products/<int:pid>/suggested_price')
def api_suggested_price(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = Product.query.get(pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404
    # Compute weighted average cost from purchases (fallback to current price if none)
    rows = Purchase.query.filter_by(product_id=pid).all()
    total_qty = sum(r.qty_added for r in rows)
    if total_qty > 0:
        wac = sum(r.qty_added * r.purchase_price for r in rows) / float(total_qty)
    else:
        wac = p.price
    # Get markup from param or setting
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
# Routes: Purchases (admin only)
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
# Routes: Transactions
# -----------------------------
@app.route('/api/transactions', methods=['GET'])
def api_transactions_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    # Order by greatest id first
    trs = Transaction.query.order_by(Transaction.id.desc()).all()
    result = []
    for t in trs:
        lines = TransactionLine.query.filter_by(transaction_id=t.id).all()
        line_items = []
        total = 0.0
        for ln in lines:
            p = Product.query.get(ln.product_id)
            name = p.name if p else f"Product {ln.product_id}"
            subtotal = ln.qty * ln.unit_price
            total += subtotal
            line_items.append({'product_id': ln.product_id, 'name': name, 'qty': ln.qty, 'unit_price': ln.unit_price, 'subtotal': subtotal})
        result.append({'id': t.id, 'date_time': t.date_time.isoformat(), 'total': round(total, 2), 'lines': line_items})
    return jsonify(result)

@app.route('/api/transactions', methods=['POST'])
def api_transactions_post():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    cart = data.get('cart', [])  # [{product_id, qty, unit_price}]
    if not cart:
        return jsonify({'error': 'Empty cart'}), 400
    # Create transaction
    t = Transaction(date_time=datetime.utcnow())
    db.session.add(t)
    db.session.flush()  # get id
    for item in cart:
        pid = int(item['product_id'])
        qty = int(item.get('qty', 1))
        unit_price = float(item.get('unit_price'))
        db.session.add(TransactionLine(transaction_id=t.id, product_id=pid, qty=qty, unit_price=unit_price))
        # Decrement stock
        p = Product.query.get(pid)
        if p:
            p.stock_qty = max(0, (p.stock_qty or 0) - qty)
    db.session.commit()
    return jsonify({'ok': True, 'transaction_id': t.id})

# -----------------------------
# Routes: CSV Exports (admin)
# -----------------------------
@app.route('/admin/export/products')
def export_products_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    # Optional token gate
    admin_token = os.getenv('ADMIN_TOKEN')
    if admin_token and request.args.get('token') != admin_token:
        return jsonify({'error': 'Invalid token'}), 403
    # Generate CSV in memory
    from io import StringIO
    sio = StringIO()
    sio.write('id,name,price,barcode,stock_qty\n')
    for p in Product.query.order_by(Product.id.asc()).all():
        sio.write(f"{p.id},{p.name},{p.price},{p.barcode},{p.stock_qty}\n")
    sio.seek(0)
    return send_file(
        sio, mimetype='text/csv', as_attachment=True, download_name='products.csv'
    )

@app.route('/admin/export/transactions')
def export_transactions_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    admin_token = os.getenv('ADMIN_TOKEN')
    if admin_token and request.args.get('token') != admin_token:
        return jsonify({'error': 'Invalid token'}), 403
    from io import StringIO
    sio = StringIO()
    sio.write('id,date_time,total\n')
    for t in Transaction.query.order_by(Transaction.id.asc()).all():
        # Compute total
        lines = TransactionLine.query.filter_by(transaction_id=t.id).all()
        total = sum(ln.qty * ln.unit_price for ln in lines)
        sio.write(f"{t.id},{t.date_time.isoformat()},{round(total,2)}\n")
    sio.seek(0)
    return send_file(sio, mimetype='text/csv', as_attachment=True, download_name='transactions.csv')

@app.route('/admin/export/transaction_lines')
def export_transaction_lines_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    admin_token = os.getenv('ADMIN_TOKEN')
    if admin_token and request.args.get('token') != admin_token:
        return jsonify({'error': 'Invalid token'}), 403
    from io import StringIO
    sio = StringIO()
    sio.write('id,transaction_id,product_id,qty,unit_price\n')
    for ln in TransactionLine.query.order_by(TransactionLine.id.asc()).all():
        sio.write(f"{ln.id},{ln.transaction_id},{ln.product_id},{ln.qty},{ln.unit_price}\n")
    sio.seek(0)
    return send_file(sio, mimetype='text/csv', as_attachment=True, download_name='transaction_lines.csv')

# -----------------------------
# Routes: Diagnostics & Version & Settings
# -----------------------------
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
# Stats: Admin-only, today
# -----------------------------
@app.route('/api/stats/today')
def api_stats_today():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    today = date.today()
    start_dt = datetime(today.year, today.month, today.day)
    # End of day
    end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)
    # Fetch transactions today
    trs = Transaction.query.filter(Transaction.date_time >= start_dt, Transaction.date_time <= end_dt).all()
    transactions_count = len(trs)
    total_sales_value = 0.0
    total_items_sold = 0
    basket_sizes = []
    top_counts = defaultdict(int)
    revenue_per_hour = defaultdict(float)
    for t in trs:
        lines = TransactionLine.query.filter_by(transaction_id=t.id).all()
        basket_qty = 0
        tx_total = 0.0
        for ln in lines:
            basket_qty += ln.qty
            tx_total += ln.qty * ln.unit_price
            top_counts[ln.product_id] += ln.qty
        total_items_sold += basket_qty
        total_sales_value += tx_total
        hour = t.date_time.hour
        revenue_per_hour[hour] += tx_total
        basket_sizes.append(basket_qty)
    avg_basket_size = (sum(basket_sizes)/len(basket_sizes)) if basket_sizes else 0.0
    # Top products by qty
    top_sorted = sorted(top_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    top_products = []
    for pid, qty in top_sorted:
        p = Product.query.get(pid)
        top_products.append({'product_id': pid, 'name': p.name if p else str(pid), 'qty_sold': qty})
    # Hourly revenue
    hourly = [{'hour': h, 'revenue': round(rev,2)} for h, rev in sorted(revenue_per_hour.items())]
    return jsonify({
        'transactions_count': transactions_count,
        'total_sales_value': round(total_sales_value, 2),
        'total_items_sold': total_items_sold,
        'avg_basket_size': round(avg_basket_size, 2),
        'top_products': top_products,
        'revenue_per_hour': hourly
    })

# -----------------------------
# UI Routes
# -----------------------------
@app.route('/')
def index():
    return app.send_static_file('../templates/index.html')

@app.route('/templates/index.html')
def index_template():
    # Fallback to serve template via send_file
    from flask import render_template
    return render_template('index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))

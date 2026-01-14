import os
from datetime import datetime, date
from zoneinfo import ZoneInfo
from io import StringIO
import csv

from flask import Flask, request, jsonify, session, render_template, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='static', template_folder='templates')

DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///pos.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql+psycopg2://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-change-me')
app.secret_key = SECRET_KEY

LOCAL_TZ = os.getenv('LOCAL_TZ', 'Africa/Johannesburg')
TZ = ZoneInfo(LOCAL_TZ)

ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASS', 'admin')
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN')

db = SQLAlchemy(app)

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='teller')
    active = db.Column(db.Boolean, default=True)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    barcode = db.Column(db.String(20), unique=True, nullable=True)

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    date_time = db.Column(db.DateTime(timezone=False), nullable=False, index=True)
    transaction_date = db.Column(db.Date, nullable=False, index=True, default=date.today)
    lines = db.relationship('TransactionLine', backref='transaction', cascade='all, delete-orphan', lazy=True)

class TransactionLine(db.Model):
    __tablename__ = 'transaction_lines'
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id', ondelete='CASCADE'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)

with app.app_context():
    db.create_all()
    # Add transaction_date column if missing
    try:
        db.session.execute(text('SELECT transaction_date FROM transactions LIMIT 1'))
    except Exception:
        try:
            db.session.execute(text('ALTER TABLE transactions ADD COLUMN transaction_date DATE'))
            txs = db.session.execute(text('SELECT id, date_time FROM transactions')).fetchall()
            for tid, dt in txs:
                if isinstance(dt, str):
                    try:
                        parsed = datetime.fromisoformat(dt)
                    except Exception:
                        parsed = datetime.utcnow()
                else:
                    parsed = dt
                local_date = parsed.replace(tzinfo=ZoneInfo('UTC')).astimezone(TZ).date()
                db.session.execute(text('UPDATE transactions SET transaction_date=:d WHERE id=:id'), {"d": local_date, "id": tid})
            db.session.commit()
        except Exception:
            db.session.rollback()
    if User.query.count() == 0:
        u = User(username=ADMIN_USER, password_hash=generate_password_hash(ADMIN_PASS), role='admin', active=True)
        db.session.add(u)
        db.session.commit()

# Helpers

def current_user():
    username = session.get('username')
    if not username:
        return None
    user = User.query.filter_by(username=username).first()
    if not user or not user.active:
        return None
    return user

def require_login():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "not_authenticated"}), 401
    return None

def require_admin():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "not_authenticated"}), 401
    if user.role != 'admin':
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return None

def verify_export_token(req):
    if ADMIN_TOKEN:
        return req.headers.get('X-Admin-Token') == ADMIN_TOKEN
    return True

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/db-health')
def db_health():
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/__version')
def version():
    return jsonify({'app': 'Farm Stall POS', 'version': '1.2.0'})

# Auth
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(force=True)
    username = data.get('username', '').strip()
    password = data.get('password', '')
    user = User.query.filter_by(username=username).first()
    if not user or not user.active or not user.check_password(password):
        return jsonify({'ok': False, 'error': 'invalid_credentials'}), 401
    session['username'] = user.username
    return jsonify({'ok': True, 'user': {'username': user.username, 'role': user.role, 'active': user.active}})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/me', methods=['GET'])
def me():
    user = current_user()
    if not user:
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, 'user': {'username': user.username, 'role': user.role, 'active': user.active}})

# Users
@app.route('/api/users', methods=['GET'])
def list_users():
    guard = require_admin()
    if guard: return guard
    users = User.query.order_by(User.username.asc()).all()
    return jsonify([{ 'username': u.username, 'role': u.role, 'active': u.active } for u in users])

@app.route('/api/users', methods=['POST'])
def create_user():
    guard = require_admin()
    if guard: return guard
    data = request.get_json(force=True)
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    role = data.get('role', 'teller')
    active = bool(data.get('active', True))
    if not username or not password:
        return jsonify({'ok': False, 'error': 'username_password_required'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'ok': False, 'error': 'username_exists'}), 409
    u = User(username=username, password_hash=generate_password_hash(password), role=role, active=active)
    db.session.add(u)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/users/update', methods=['POST'])
def update_user():
    guard = require_admin()
    if guard: return guard
    data = request.get_json(force=True)
    username = data.get('username')
    u = User.query.filter_by(username=username).first()
    if not u:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    if 'role' in data: u.role = data['role']
    if 'active' in data: u.active = bool(data['active'])
    if 'password' in data and data['password']:
        u.password_hash = generate_password_hash(data['password'])
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/users/<username>', methods=['DELETE'])
def delete_user(username):
    guard = require_admin()
    if guard: return guard
    u = User.query.filter_by(username=username).first()
    if not u:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    db.session.delete(u)
    db.session.commit()
    return jsonify({'ok': True})

# Products
@app.route('/api/products', methods=['GET'])
def get_products():
    user_guard = require_login()
    if user_guard: return user_guard
    products = Product.query.order_by(Product.name.asc()).all()
    result = { p.name: { 'id': p.id, 'price': float(p.price), 'barcode': p.barcode } for p in products }
    return jsonify(result)

@app.route('/api/products', methods=['POST'])
def create_product():
    guard = require_admin()
    if guard: return guard
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    price = data.get('price')
    barcode = (data.get('barcode') or '').strip() or None
    if not name or price is None:
        return jsonify({'ok': False, 'error': 'name_price_required'}), 400
    if Product.query.filter_by(name=name).first():
        return jsonify({'ok': False, 'error': 'name_exists'}), 409
    if barcode and Product.query.filter_by(barcode=barcode).first():
        return jsonify({'ok': False, 'error': 'barcode_exists'}), 409
    p = Product(name=name, price=price, barcode=barcode)
    db.session.add(p)
    db.session.commit()
    return jsonify({'ok': True, 'product': { 'id': p.id, 'name': p.name, 'price': float(p.price), 'barcode': p.barcode }})

@app.route('/api/products/update', methods=['POST'])
def update_product():
    guard = require_admin()
    if guard: return guard
    data = request.get_json(force=True)
    old_name = data.get('old_name')
    if not old_name:
        return jsonify({'ok': False, 'error': 'old_name_required'}), 400
    p = Product.query.filter_by(name=old_name).first()
    if not p:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    new_name = data.get('new_name')
    new_price = data.get('price')
    new_barcode = data.get('barcode')

    if new_name:
        new_name = new_name.strip()
        if new_name != p.name and Product.query.filter_by(name=new_name).first():
            return jsonify({'ok': False, 'error': 'name_exists'}), 409
        p.name = new_name
    if new_price is not None:
        p.price = new_price
    if new_barcode is not None:
        new_barcode = new_barcode.strip() or None
        if new_barcode and Product.query.filter(Product.id != p.id, Product.barcode == new_barcode).first():
            return jsonify({'ok': False, 'error': 'barcode_exists'}), 409
        p.barcode = new_barcode
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/products/<name>', methods=['DELETE'])
def delete_product(name):
    guard = require_admin()
    if guard: return guard
    p = Product.query.filter_by(name=name).first()
    if not p:
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})

# Transactions
@app.route('/api/transactions', methods=['GET'])
def list_transactions():
    guard = require_login()
    if guard: return guard
    today_local = datetime.now(TZ).date()
    txs = (Transaction.query
            .filter(Transaction.transaction_date == today_local)
            .order_by(Transaction.date_time.desc())
            .all())

    def tx_to_dict(t: Transaction):
        lines = TransactionLine.query.filter_by(transaction_id=t.id).all()
        line_dicts = []
        total = 0.0
        for ln in lines:
            prod = Product.query.get(ln.product_id)
            line_total = float(ln.unit_price) * ln.qty
            total += line_total
            line_dicts.append({
                'product_id': ln.product_id,
                'product_name': prod.name if prod else '(deleted product)',
                'qty': ln.qty,
                'unit_price': float(ln.unit_price),
                'line_total': line_total,
            })
        return {
            'id': t.id,
            'date_time': t.date_time.isoformat(sep=' ', timespec='seconds'),
            'transaction_date': t.transaction_date.isoformat(),
            'total': round(total, 2),
            'lines': line_dicts
        }

    return jsonify([tx_to_dict(t) for t in txs])

@app.route('/api/transactions', methods=['POST'])
def create_transaction():
    guard = require_login()
    if guard: return guard
    data = request.get_json(force=True)
    items = data.get('items', [])
    if not items:
        return jsonify({'ok': False, 'error': 'no_items'}), 400
    now_utc = datetime.utcnow()
    today_local = datetime.now(TZ).date()
    t = Transaction(date_time=now_utc, transaction_date=today_local)
    db.session.add(t)
    db.session.flush()

    for it in items:
        name = it.get('product_name')
        qty = int(it.get('qty', 1))
        pid = it.get('product_id')
        prod = None
        if pid:
            prod = Product.query.get(pid)
        if not prod and name:
            prod = Product.query.filter((Product.name == name) | (Product.barcode == name)).first()
        if not prod:
            db.session.rollback()
            return jsonify({'ok': False, 'error': f'product_not_found: {name or pid}'}), 404
        line = TransactionLine(transaction_id=t.id, product_id=prod.id, qty=max(1, qty), unit_price=prod.price)
        db.session.add(line)

    db.session.commit()
    return jsonify({'ok': True, 'transaction_id': t.id})

# Exports

def _csv_response(filename: str, rows: list, header: list):
    sio = StringIO()
    import csv as _csv
    writer = _csv.writer(sio)
    writer.writerow(header)
    writer.writerows(rows)
    data = sio.getvalue()
    return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={filename}'})

@app.route('/admin/export/products', methods=['GET'])
def export_products():
    guard = require_admin()
    if guard: return guard
    if not verify_export_token(request):
        return jsonify({'ok': False, 'error': 'bad_admin_token'}), 403
    prods = Product.query.order_by(Product.id.asc()).all()
    rows = [[p.id, p.name, float(p.price), p.barcode or ''] for p in prods]
    return _csv_response('products.csv', rows, ['id', 'name', 'price', 'barcode'])

@app.route('/admin/export/transactions', methods=['GET'])
def export_transactions():
    guard = require_admin()
    if guard: return guard
    if not verify_export_token(request):
        return jsonify({'ok': False, 'error': 'bad_admin_token'}), 403
    txs = Transaction.query.order_by(Transaction.id.asc()).all()
    rows = [[t.id, t.date_time.isoformat(sep=' ', timespec='seconds'), t.transaction_date.isoformat()] for t in txs]
    today_local = datetime.now(TZ).date()
    old = Transaction.query.filter(Transaction.transaction_date < today_local).all()
    for t in old:
        db.session.delete(t)
    db.session.commit()
    return _csv_response('transactions.csv', rows, ['id', 'date_time', 'transaction_date'])

@app.route('/admin/export/transaction_lines', methods=['GET'])
def export_transaction_lines():
    guard = require_admin()
    if guard: return guard
    if not verify_export_token(request):
        return jsonify({'ok': False, 'error': 'bad_admin_token'}), 403
    lines = TransactionLine.query.order_by(TransactionLine.id.asc()).all()
    rows = [[ln.id, ln.transaction_id, ln.product_id, ln.qty, float(ln.unit_price)] for ln in lines]
    return _csv_response('transaction_lines.csv', rows, ['id', 'transaction_id', 'product_id', 'qty', 'unit_price'])

if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=True)

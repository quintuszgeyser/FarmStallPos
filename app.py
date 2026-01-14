
from flask import Flask, render_template, jsonify, request, Response, send_from_directory, make_response, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import relationship
from sqlalchemy import text
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import io, csv, json, os, random

# --- Configuration ---
CURRENCY      = os.getenv("CURRENCY", "R")   # UI currency symbol
DATABASE_URL  = os.getenv("DATABASE_URL")    # Render → Service → Environment
ADMIN_TOKEN   = os.getenv("ADMIN_TOKEN")     # Optional: protects /admin/export/*
SECRET_KEY    = os.getenv("SECRET_KEY", "dev-secret-change-me")  # set in Render

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = True
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# --- Models ---
class User(db.Model):
    __tablename__ = "users"
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(20), nullable=False, default="teller")  # 'admin' | 'teller'
    active        = db.Column(db.Boolean, nullable=False, default=True)

class Product(db.Model):
    __tablename__ = "products"
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(120), unique=True, nullable=False)
    price    = db.Column(db.Numeric(10, 2), nullable=False)
    barcode  = db.Column(db.String(64), unique=True)

class Transaction(db.Model):
    __tablename__ = "transactions"
    id        = db.Column(db.Integer, primary_key=True)
    date_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    lines     = relationship("TransactionLine", back_populates="transaction", cascade="all, delete-orphan")

class TransactionLine(db.Model):
    __tablename__ = "transaction_lines"
    id             = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), nullable=False)
    product_id     = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    qty            = db.Column(db.Integer, nullable=False)
    unit_price     = db.Column(db.Numeric(10, 2), nullable=False)

    transaction = relationship("Transaction", back_populates="lines")
    product     = relationship("Product")

# --- Helpers: barcode generation & migration ---
def _ean13_checksum(digits12: str) -> int:
    odd_sum  = sum(int(d) for i, d in enumerate(digits12, start=1) if i % 2 == 1)
    even_sum = sum(int(d) for i, d in enumerate(digits12, start=1) if i % 2 == 0)
    s = odd_sum + 3 * even_sum
    return (10 - (s % 10)) % 10

def generate_ean13(prefix: str = "200") -> str:
    payload_len = 12 - len(prefix)
    mid = "".join(str(random.randint(0, 9)) for _ in range(payload_len))
    base = prefix + mid
    c = _ean13_checksum(base)
    return base + str(c)

def generate_unique_barcode() -> str:
    while True:
        code = generate_ean13("200")
        if not Product.query.filter_by(barcode=code).first():
            return code

def ensure_barcode_column_and_backfill():
    try:
        db.session.execute(text(
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS barcode VARCHAR(64) UNIQUE"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()

    rows = db.session.execute(
        text("SELECT id FROM products WHERE barcode IS NULL OR barcode = ''")
    ).fetchall()
    if rows:
        for (pid,) in rows:
            code = generate_unique_barcode()
            db.session.execute(
                text("UPDATE products SET barcode = :code WHERE id = :pid"),
                {"code": code, "pid": pid},
            )
        db.session.commit()

def seed_default_admin():
    """Create a first admin if users table is empty."""
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_pass = os.getenv("ADMIN_PASS", "admin")
    if User.query.count() == 0:
        db.session.add(User(
            username=admin_user,
            password_hash=generate_password_hash(admin_pass),
            role="admin",
            active=True
        ))
        db.session.commit()

# Create tables & run tiny migration
with app.app_context():
    db.create_all()
    ensure_barcode_column_and_backfill()
    seed_default_admin()

# --- RBAC helpers ---
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)

def require_login():
    if not session.get("user_id"):
        return jsonify({"error": "Unauthorized"}), 401
    return None

def require_role(*roles):
    """Return a Response if denied; else None."""
    guard = require_login()
    if guard: return guard
    user = current_user()
    if not user or not user.active or user.role not in roles:
        return jsonify({"error": "Forbidden"}), 403
    return None

# --- Routes (UI) ---
@app.get("/")
def index():
    """Serve the main UI; no-cache to avoid stale HTML while iterating."""
    html = render_template("index.html", currency=CURRENCY)
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp

# Serve PWA files from root paths
@app.get("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js")

@app.get("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")

# Optional: diagnostic
@app.get("/api/me")
def api_me():
    user = current_user()
    if not user:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "user": {"username": user.username, "role": user.role, "active": user.active}
    })

# --- Auth API ---
@app.post("/api/login")
def api_login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = User.query.filter_by(username=username, active=True).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid credentials"}), 401
    session["user_id"] = user.id
    session["role"]    = user.role
    return jsonify({"ok": True, "user": {"username": user.username, "role": user.role}})

@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})

# --- Users management (ADMIN ONLY) ---
@app.get("/api/users")
def list_users():
    guard = require_role("admin")
    if guard: return guard
    users = User.query.order_by(User.id).all()
    return jsonify([{
        "id": u.id, "username": u.username, "role": u.role, "active": u.active
    } for u in users])

@app.post("/api/users")
def create_user():
    guard = require_role("admin")
    if guard: return guard
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role     = (data.get("role") or "teller").strip()
    active   = bool(data.get("active", True))
    if not username or not password or role not in ("admin", "teller"):
        return jsonify({"error": "username, password, role(admin|teller) required"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "User exists"}), 409
    db.session.add(User(
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
        active=active
    ))
    db.session.commit()
    return jsonify({"ok": True})

@app.post("/api/users/update")
def update_user():
    guard = require_role("admin")
    if guard: return guard
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    role     = (data.get("role") or "").strip()
    active   = data.get("active")
    password = data.get("password")  # optional
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({"error": "User not found"}), 404
    if role:
        if role not in ("admin", "teller"):
            return jsonify({"error": "Invalid role"}), 400
        user.role = role
    if active is not None:
        user.active = bool(active)
    if password:
        user.password_hash = generate_password_hash(password)
    db.session.commit()
    return jsonify({"ok": True})

@app.delete("/api/users/<username>")
def delete_user(username):
    guard = require_role("admin")
    if guard: return guard
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({"error": "User not found"}), 404
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True})

# --- Products API (ADMIN ONLY) ---
@app.get("/api/products")
def api_get_products():
    guard = require_role("admin")
    if guard: return guard
    rows = Product.query.order_by(Product.id).all()
    return jsonify({p.name: {"id": p.id, "price": float(p.price), "barcode": p.barcode} for p in rows})

@app.post("/api/products")
def api_add_product():
    guard = require_role("admin")
    if guard: return guard
    data  = request.get_json(force=True)
    name  = (data.get("name") or "").strip()
    price = float(data.get("price") or 0)
    barcode = (data.get("barcode") or "").strip()
    if not name:
        return jsonify({"error": "Product name required"}), 400
    if Product.query.filter_by(name=name).first():
        return jsonify({"error": "Product already exists"}), 409
    if barcode:
        if Product.query.filter_by(barcode=barcode).first():
            return jsonify({"error": "Barcode already exists"}), 409
    else:
        barcode = generate_unique_barcode()
    db.session.add(Product(name=name, price=price, barcode=barcode))
    db.session.commit()
    return jsonify({"ok": True, "barcode": barcode})

@app.post("/api/products/update")
def api_update_product():
    guard = require_role("admin")
    if guard: return guard
    data      = request.get_json(force=True)
    old_name  = (data.get("old_name") or "").strip()
    new_name  = (data.get("new_name") or "").strip() or old_name
    price     = float(data.get("price") or 0)
    barcode   = (data.get("barcode") or "").strip()
    prod = Product.query.filter_by(name=old_name).first()
    if not prod:
        return jsonify({"error": "Original product not found"}), 404
    if barcode and barcode != (prod.barcode or ""):
        if Product.query.filter_by(barcode=barcode).first():
            return jsonify({"error": "Barcode already exists"}), 409
        prod.barcode = barcode
    prod.name  = new_name
    prod.price = price
    db.session.commit()
    return jsonify({"ok": True, "barcode": prod.barcode})

@app.delete("/api/products/<name>")
def api_delete_product(name):
    guard = require_role("admin")
    if guard: return guard
    prod = Product.query.filter_by(name=name).first()
    if not prod:
        return jsonify({"error": "Product not found"}), 404
    db.session.delete(prod)
    db.session.commit()
    return jsonify({"ok": True})

# --- Transactions API (ADMIN or TELLER) ---
@app.get("/api/transactions")
def api_get_transactions():
    guard = require_role("admin", "teller")
    if guard: return guard
    q = db.session.query(
        Transaction.id.label("tran_id"),
        Transaction.date_time,
        Product.name.label("product_name"),
        TransactionLine.qty,
        (TransactionLine.qty * TransactionLine.unit_price).label("amount"),
    ).join(TransactionLine, Transaction.id == TransactionLine.transaction_id) \
     .join(Product, Product.id == TransactionLine.product_id) \
     .order_by(Transaction.id, TransactionLine.id)

    payload = []
    for row in q.all():
        payload.append({
            "tran_id":     row.tran_id,
            "date_time":   row.date_time.strftime("%Y-%m-%d %H:%M:%S"),
            "product_id":  row.product_name,
            "no_of_items": int(row.qty),
            "amount":      float(row.amount)
        })
    return jsonify(payload)

@app.post("/api/transactions")
def api_add_transaction():
    guard = require_role("admin", "teller")
    if guard: return guard
    data  = request.get_json(force=True)
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "No items"}), 400
    tran = Transaction(date_time=datetime.utcnow())
    db.session.add(tran)
    db.session.flush()
    for it in items:
        name = (it.get("product_name") or "").strip()
        qty  = int(it.get("qty") or 1)
        prod = Product.query.filter_by(name=name).first()
        if not prod:
            try:
                prod = Product.query.get(int(name))
            except Exception:
                prod = None
        if not prod:
            db.session.rollback()
            return jsonify({"error": f'Product "{name}" not found'}), 404
        db.session.add(TransactionLine(
            transaction_id = tran.id,
            product_id     = prod.id,
            qty            = qty,
            unit_price     = prod.price
        ))
    db.session.commit()
    lines = [{
        "tran_id":     tran.id,
        "date_time":   tran.date_time.strftime("%Y-%m-%d %H:%M:%S"),
        "product_id":  l.product.name,
        "no_of_items": int(l.qty),
        "amount":      float(l.qty * l.unit_price)
    } for l in tran.lines]
    return jsonify({"ok": True, "tran_id": tran.id, "lines": lines})

# --- Admin: DB health & CSV exports (ADMIN ONLY) ---
def require_admin_token():
    if not ADMIN_TOKEN:
        return None
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return Response("Unauthorized", status=401)
    return None

def csv_response_eager(filename: str, header: list[str], rows: list[list]):
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(header)
    w.writerows(rows)
    out = s.getvalue()
    return Response(out, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/api/db-health")
def db_health():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/admin/export/products")
def export_products():
    guard = require_role("admin")
    if guard: return guard
    tok = require_admin_token()
    if tok: return tok
    products = db.session.query(Product).order_by(Product.id).all()
    rows = [[p.id, p.name, float(p.price), p.barcode or ""] for p in products]
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return csv_response_eager(f"products_{stamp}.csv",
                              ["id", "name", "price", "barcode"], rows)

@app.get("/admin/export/transactions")
def export_transactions():
    guard = require_role("admin");  tok = require_admin_token()
    if guard: return guard
    if tok: return tok
    txs = db.session.query(Transaction).order_by(Transaction.id).all()
    rows = [[t.id, t.date_time.isoformat()] for t in txs]
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return csv_response_eager(f"transactions_{stamp}.csv",
                              ["id", "date_time"], rows)

@app.get("/admin/export/transaction_lines")
def export_transaction_lines():
    guard = require_role("admin");  tok = require_admin_token()
    if guard: return guard
    if tok: return tok
    lines = db.session.query(TransactionLine).order_by(TransactionLine.id).all()
    rows  = [[l.id, l.transaction_id, l.product_id, l.qty, float(l.unit_price)] for l in lines]
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return csv_response_eager(f"transaction_lines_{stamp}.csv",
                              ["id", "transaction_id", "product_id", "qty", "unit_price"], rows)

# --- Local dev entrypoint ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)


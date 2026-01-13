
from flask import Flask, render_template, jsonify, request, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import relationship
from sqlalchemy import text
from datetime import datetime
import io, csv, json, os

# --- Configuration ---
CURRENCY = os.getenv("CURRENCY", "R")            # UI currency
DATABASE_URL = os.getenv("DATABASE_URL")         # Set this in Render → Service → Environment
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")           # Optional: protects /admin/export/*

app = Flask(__name__)
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = True
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# --- Models ---
class Product(db.Model):
    __tablename__ = "products"
    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(120), unique=True, nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)

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

# Create tables if they don't exist (for production, consider Alembic/Flask-Migrate)
with app.app_context():
    db.create_all()

# --- Routes (UI) ---
@app.get("/")
def index():
    return render_template("index.html", currency=CURRENCY)

# --- Products API ---
@app.get("/api/products")
def api_get_products():
    rows = Product.query.order_by(Product.id).all()
    # Return in the same shape your frontend expects: {name: {id, price}}
    return jsonify({p.name: {"id": p.id, "price": float(p.price)} for p in rows})

@app.post("/api/products")
def api_add_product():
    data  = request.get_json(force=True)
    name  = (data.get("name") or "").strip()
    price = float(data.get("price") or 0)
    if not name:
        return jsonify({"error": "Product name required"}), 400
    if Product.query.filter_by(name=name).first():
        return jsonify({"error": "Product already exists"}), 409
    db.session.add(Product(name=name, price=price))
    db.session.commit()
    return jsonify({"ok": True})

@app.post("/api/products/update")
def api_update_product():
    data      = request.get_json(force=True)
    old_name  = (data.get("old_name") or "").strip()
    new_name  = (data.get("new_name") or "").strip() or old_name
    price     = float(data.get("price") or 0)
    prod      = Product.query.filter_by(name=old_name).first()
    if not prod:
        return jsonify({"error": "Original product not found"}), 404
    prod.name  = new_name
    prod.price = price
    db.session.commit()
    return jsonify({"ok": True})

@app.delete("/api/products/<name>")
def api_delete_product(name):
    prod = Product.query.filter_by(name=name).first()
    if not prod:
        return jsonify({"error": "Product not found"}), 404
    db.session.delete(prod)
    db.session.commit()
    return jsonify({"ok": True})

# --- Transactions API ---
@app.get("/api/transactions")
def api_get_transactions():
    """
    Returns per-line rows; your frontend groups by tran_id.
    Keeps the "product_id" field compatible with the UI (product name).
    """
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
            "product_id":  row.product_name,  # keep compatible with your UI
            "no_of_items": int(row.qty),
            "amount":      float(row.amount)
        })
    return jsonify(payload)

@app.post("/api/transactions")
def api_add_transaction():
    """
    Payload: { "items": [ {"product_name":"Apple","qty":2}, ... ] }
    Creates 1 Transaction + N lines; responds with {ok, tran_id, lines:[...]}.
    """
    data  = request.get_json(force=True)
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "No items"}), 400

    tran = Transaction(date_time=datetime.utcnow())
    db.session.add(tran)
    db.session.flush()  # get tran.id before committing

    for it in items:
        name = (it.get("product_name") or "").strip()
        qty  = int(it.get("qty") or 1)

        # Lookup by name, or fallback to numeric ID string
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

# --- Admin: DB health & CSV exports ---
def require_admin():
    """Simple header-based guard. Set ADMIN_TOKEN env var to enable."""
    if not ADMIN_TOKEN:
        return None  # protection disabled
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return Response("Unauthorized", status=401)
    return None

def csv_response(filename: str, header: list[str], rows_iter):
    """Stream a CSV download with a header row and the given iterator of rows."""
    def generate():
        yield ",".join(header) + "\n"
        for row in rows_iter:
            s = io.StringIO()
            w = csv.writer(s)
            w.writerow(row)
            yield s.getvalue()
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/api/db-health")
def db_health():
    """
    Confirms the app can reach the DB (internal on Render).
    Returns {"ok": true} on success.
    """
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/admin/export/products")
def export_products():
    guard = require_admin()
    if guard: return guard

    q = db.session.query(Product).order_by(Product.id)
    header = ["id", "name", "price"]
    def rows():
        for p in q.all():
            yield [p.id, p.name, float(p.price)]
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return csv_response(f"products_{stamp}.csv", header, rows())

@app.get("/admin/export/transactions")
def export_transactions():
    guard = require_admin()
    if guard: return guard

    q = db.session.query(Transaction).order_by(Transaction.id)
    header = ["id", "date_time"]
    def rows():
        for t in q.all():
            yield [t.id, t.date_time.isoformat()]
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return csv_response(f"transactions_{stamp}.csv", header, rows())

@app.get("/admin/export/transaction_lines")
def export_transaction_lines():
    guard = require_admin()
    if guard: return guard

    q = db.session.query(TransactionLine).order_by(TransactionLine.id)
    header = ["id", "transaction_id", "product_id", "qty", "unit_price"]
    def rows():
        for l in q.all():
            yield [l.id, l.transaction_id, l.product_id, l.qty, float(l.unit_price)]
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return csv_response(f"transaction_lines_{stamp}.csv", header, rows())

# --- Local dev entrypoint ---
if __name__ == "__main__":
    # Local dev only; on Render you run with: gunicorn app:app  (Start Command)
    app.run(host="0.0.0.0", port=5000, debug=True)

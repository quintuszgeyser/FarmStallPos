
from flask import Flask, render_template, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import relationship
from datetime import datetime
import os

# --- Configuration ---
CURRENCY = os.getenv("CURRENCY", "R")                 # UI currency
DATABASE_URL = os.getenv("DATABASE_URL")             # Set this in Render → Service → Environment
# Example: postgresql://user:pass@host:5432/farmstall_pos

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

# --- Routes ---
@app.get("/")
def index():
    return render_template("index.html", currency=CURRENCY)

# Products
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

# Transactions
@app.get("/api/transactions")
def api_get_transactions():
    # Return per-line rows, grouped by tran_id on the frontend (same as before)
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
            "product_id":  row.product_name,            # keep compatible with your UI
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

if __name__ == "__main__":
    # Local dev only; on Render you run with: gunicorn app:app  (Start Command)
    app.run(host="0.0.0.0", port=5000, debug=True)

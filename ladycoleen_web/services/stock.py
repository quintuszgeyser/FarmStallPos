"""
FIFO stock service for online farmshop orders.
Ported verbatim from farm_pos_web/app.py consume_fifo().

STRICT MODE: All lines must have sufficient stock or the entire order fails.
Pre-check runs first, then deduction — both inside a single transaction with FOR UPDATE locks.
"""
import uuid
import logging
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import text

log = logging.getLogger(__name__)


# ── Public API ──────────────────────────────────────────────────────────────

def check_and_deduct_order(db, online_order_id: int, admin_user_id: int) -> str:
    """
    Validate stock and deduct for an online order.
    Returns the new pos_sale_id (UUID string) on success.
    Raises ValueError with a human-readable message on failure.
    Caller must commit db.session after this returns successfully.
    """
    from sqlalchemy import text

    # Lock the order row and verify it's still pending
    order_row = db.session.execute(
        text("SELECT id, status FROM online_orders WHERE id = :id FOR UPDATE"),
        {"id": online_order_id}
    ).fetchone()

    if not order_row:
        raise ValueError("Order not found")
    if order_row.status != "pending":
        raise ValueError(f"Order already {order_row.status} — cannot confirm again")

    # Fetch all line items
    lines = db.session.execute(
        text("""
            SELECT ol.id, ol.product_id, ol.qty,
                   p.product_type, p.stock_qty, p.name
            FROM online_order_lines ol
            JOIN products p ON p.id = ol.product_id
            WHERE ol.online_order_id = :oid
        """),
        {"oid": online_order_id}
    ).fetchall()

    if not lines:
        raise ValueError("Order has no line items")

    now = datetime.now(timezone.utc)

    # ── STEP 1: Pre-check all lines before touching anything ────────────────
    for line in lines:
        qty_needed = Decimal(str(line.qty))
        shortfall = _check_stock(db, line.product_id, line.product_type, qty_needed)
        if shortfall is not None:
            raise ValueError(
                f"Insufficient stock for '{line.name}': "
                f"need {_fmt(qty_needed)}, available {_fmt(shortfall)}"
            )

    # ── STEP 2: Deduct all lines ─────────────────────────────────────────────
    sale_uuid = str(uuid.uuid4())

    # Look up the virtual Online Shop user to tag sales correctly
    online_user = db.session.execute(
        text("SELECT id FROM users WHERE username = 'Online Shop' LIMIT 1")
    ).fetchone()
    online_user_id = online_user.id if online_user else None

    web_customer_id = db.session.execute(
        text("SELECT web_customer_id FROM online_orders WHERE id = :id"),
        {"id": online_order_id}
    ).scalar()

    for line in lines:
        qty = Decimal(str(line.qty))
        unit_price = db.session.execute(
            text("SELECT unit_price FROM online_order_lines WHERE id = :id"),
            {"id": line.id}
        ).scalar()

        # Create Sale record tagged as Online Shop user
        db.session.execute(text("""
            INSERT INTO sales (sale_id, date_time, product_id, qty, unit_price, customer_id, user_id, voided, flagged, flag_resolved)
            VALUES (:sale_id, :dt, :pid, :qty, :price, :cid, :uid, false, false, false)
        """), {
            "sale_id": sale_uuid,
            "dt":      now,
            "pid":     line.product_id,
            "qty":     float(qty),
            "price":   float(unit_price),
            "cid":     web_customer_id,
            "uid":     online_user_id,
        })

        # Deduct stock
        _deduct_stock(db, line.product_id, line.product_type, qty, sale_uuid, now)

    # ── STEP 3: Mark order confirmed ─────────────────────────────────────────
    db.session.execute(text("""
        UPDATE online_orders
        SET status = 'confirmed', pos_sale_id = :sid, updated_at = :now
        WHERE id = :id
    """), {"sid": sale_uuid, "now": now, "id": online_order_id})

    log.info('{"action":"stock_deducted","order_id":%d,"pos_sale_id":"%s"}',
             online_order_id, sale_uuid)
    return sale_uuid


def get_available_qty(db, product_id: int, product_type: str) -> float:
    """Return available quantity for display on the farmshop."""
    if product_type == "simple":
        row = db.session.execute(
            text("SELECT stock_qty FROM products WHERE id = :id"),
            {"id": product_id}
        ).fetchone()
        return float(row.stock_qty or 0) if row else 0.0

    if product_type == "stock_item":
        result = db.session.execute(
            text("SELECT COALESCE(SUM(qty_remaining_base),0) FROM stock_batches WHERE product_id = :id"),
            {"id": product_id}
        ).scalar()
        return float(result or 0)

    if product_type == "recipe":
        return _recipe_available_qty(db, product_id)

    return 0.0


# ── Internal helpers ────────────────────────────────────────────────────────

def _check_stock(db, product_id: int, product_type: str, qty_needed: Decimal):
    """
    Returns None if stock is sufficient.
    Returns the actual available qty (Decimal) if insufficient.
    """
    available = Decimal(str(get_available_qty(db, product_id, product_type)))
    if available >= qty_needed:
        return None
    return available


def _deduct_stock(db, product_id: int, product_type: str, qty: Decimal,
                  sale_uuid: str, now: datetime, _depth: int = 0):
    """Port of POS consume_fifo() — verbatim logic, strict version."""
    if _depth > 10:
        return Decimal("0")

    if product_type == "simple":
        db.session.execute(text("""
            UPDATE products
            SET stock_qty = GREATEST(0, stock_qty - :qty)
            WHERE id = :id
        """), {"qty": int(qty), "id": product_id})
        return Decimal("0")

    if product_type in ("stock_item", "recipe_ingredient"):
        return _consume_fifo(db, product_id, qty, sale_uuid, now, _depth)

    if product_type == "recipe":
        lines = db.session.execute(
            text("SELECT ingredient_id, qty_base FROM recipe_lines WHERE product_id = :id"),
            {"id": product_id}
        ).fetchall()
        total_cost = Decimal("0")
        for rl in lines:
            ingredient_qty = Decimal(str(rl.qty_base)) * qty
            ing_type = db.session.execute(
                text("SELECT product_type FROM products WHERE id = :id"),
                {"id": rl.ingredient_id}
            ).scalar()
            total_cost += _consume_fifo(db, rl.ingredient_id, ingredient_qty,
                                        sale_uuid, now, _depth + 1)
        return total_cost

    return Decimal("0")


def _consume_fifo(db, ingredient_id: int, qty_needed: Decimal,
                  sale_uuid: str, now: datetime, _depth: int = 0) -> Decimal:
    """
    Direct port of POS consume_fifo().
    Handles compound ingredients (recipe_lines on a stock_item) recursively.
    """
    if _depth > 10:
        return Decimal("0")

    qty_needed = Decimal(str(qty_needed))

    # Check if this ingredient is itself a compound recipe
    sub_lines = db.session.execute(
        text("SELECT ingredient_id, qty_base FROM recipe_lines WHERE product_id = :id"),
        {"id": ingredient_id}
    ).fetchall()

    if sub_lines:
        total_cost = Decimal("0")
        for sub in sub_lines:
            sub_qty = Decimal(str(sub.qty_base)) * qty_needed
            total_cost += _consume_fifo(db, sub.ingredient_id, sub_qty,
                                        sale_uuid, now, _depth + 1)
        return total_cost

    # Direct ingredient: consume from FIFO batches oldest-first with FOR UPDATE lock
    qty_to_consume = qty_needed
    total_cost = Decimal("0")

    batches = db.session.execute(text("""
        SELECT id, qty_remaining_base, cost_per_base_unit
        FROM stock_batches
        WHERE product_id = :pid
          AND qty_remaining_base > 0
          AND purchased_at <= :now
        ORDER BY purchased_at ASC, id ASC
        FOR UPDATE
    """), {"pid": ingredient_id, "now": now}).fetchall()

    if not batches:
        # Fallback: any available batch (covers opening stock)
        batches = db.session.execute(text("""
            SELECT id, qty_remaining_base, cost_per_base_unit
            FROM stock_batches
            WHERE product_id = :pid AND qty_remaining_base > 0
            ORDER BY purchased_at ASC, id ASC
            FOR UPDATE
        """), {"pid": ingredient_id}).fetchall()

    for batch in batches:
        if qty_to_consume <= 0:
            break
        take = min(Decimal(str(batch.qty_remaining_base)), qty_to_consume)
        cost = take * Decimal(str(batch.cost_per_base_unit))
        total_cost += cost

        db.session.execute(text("""
            UPDATE stock_batches
            SET qty_remaining_base = qty_remaining_base - :take
            WHERE id = :id
        """), {"take": float(take), "id": batch.id})

        db.session.execute(text("""
            INSERT INTO stock_consumption
                (sale_id, ingredient_id, batch_id, qty_consumed_base, cost_per_base_unit, consumed_at)
            VALUES (:sale_id, :ing_id, :batch_id, :qty, :cost, :now)
        """), {
            "sale_id":  sale_uuid,
            "ing_id":   ingredient_id,
            "batch_id": batch.id,
            "qty":      float(take),
            "cost":     float(batch.cost_per_base_unit),
            "now":      now,
        })
        qty_to_consume -= take

    return total_cost


def _recipe_available_qty(db, product_id: int) -> float:
    """
    For a recipe product, available qty = min(ingredient_available / ingredient_required)
    across all recipe lines. Returns how many full units of the recipe can be made.
    """
    lines = db.session.execute(
        text("SELECT ingredient_id, qty_base FROM recipe_lines WHERE product_id = :id"),
        {"id": product_id}
    ).fetchall()

    if not lines:
        return 0.0

    min_batches = float("inf")
    for rl in lines:
        avail = db.session.execute(
            text("SELECT COALESCE(SUM(qty_remaining_base),0) FROM stock_batches WHERE product_id = :id"),
            {"id": rl.ingredient_id}
        ).scalar()
        if float(rl.qty_base) <= 0:
            continue
        possible = float(avail or 0) / float(rl.qty_base)
        min_batches = min(min_batches, possible)

    return float(min_batches) if min_batches != float("inf") else 0.0


def _fmt(qty: Decimal) -> str:
    f = float(qty)
    return f"{f:.0f}" if f >= 10 else f"{f:.1f}"


def reverse_sale_consumption(db, sale_uuid: str) -> None:
    """
    Restore all stock consumed by a sale — mirrors farm_pos_web reverse_fifo().
    Restores FIFO batch quantities, restores simple product stock, deletes consumption records.
    """
    # Restore FIFO batch stock
    rows = db.session.execute(
        text("SELECT batch_id, qty_consumed_base FROM stock_consumption WHERE sale_id = :sid"),
        {"sid": sale_uuid}
    ).fetchall()
    for row in rows:
        db.session.execute(text("""
            UPDATE stock_batches
            SET qty_remaining_base = qty_remaining_base + :qty
            WHERE id = :bid
        """), {"qty": float(row.qty_consumed_base), "bid": row.batch_id})

    # Restore simple product stock
    simple_sales = db.session.execute(text("""
        SELECT s.product_id, s.qty
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.sale_id = :sid AND p.product_type = 'simple' AND s.voided = false
    """), {"sid": sale_uuid}).fetchall()
    for s in simple_sales:
        db.session.execute(text("""
            UPDATE products SET stock_qty = stock_qty + :qty WHERE id = :id
        """), {"qty": int(float(s.qty)), "id": s.product_id})

    # Delete consumption records (safe — sale rows remain for audit)
    db.session.execute(
        text("DELETE FROM stock_consumption WHERE sale_id = :sid"),
        {"sid": sale_uuid}
    )

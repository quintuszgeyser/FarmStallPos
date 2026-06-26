import json
import logging
from decimal import Decimal
from flask import Blueprint, request, jsonify, render_template, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, verify_jwt_in_request
from models import db
from sqlalchemy import text
from services.events import emit

log = logging.getLogger(__name__)
farmshop_bp = Blueprint("farmshop", __name__)

LOW_STOCK_THRESHOLD = 5


# ── Public pages ──────────────────────────────────────────────────────────────

@farmshop_bp.route("/farmshop")
def landing():
    from flask import redirect
    return redirect("/farmshop/products")


@farmshop_bp.route("/farmshop/products")
def products():
    search = (request.args.get("q") or "").strip()

    sql = """
        SELECT id, name, COALESCE(price, 0) AS price, product_type, unit_type, base_unit,
               sold_by_weight, price_per_unit, stock_qty, image_url, description
        FROM products
        WHERE is_for_sale = true AND is_available_online = true AND is_archived = false
    """
    params = {}
    if search:
        sql += " AND name ILIKE :q"
        params["q"] = f"%{search}%"
    sql += " ORDER BY name ASC"

    rows = db.session.execute(text(sql), params).fetchall()

    from services.stock import get_available_qty
    items = []
    for r in rows:
        avail = get_available_qty(db, r.id, r.product_type)
        items.append({
            "id":             r.id,
            "name":           r.name,
            "price":          float(r.price),
            "product_type":   r.product_type,
            "unit_type":      r.unit_type,
            "base_unit":      r.base_unit,
            "sold_by_weight": r.sold_by_weight,
            "price_per_unit": float(r.price_per_unit) if r.price_per_unit else None,
            "available_qty":  avail,
            "stock_status":   _stock_status(avail, r.product_type),
            "image_url":      r.image_url,
        })

    return render_template("farmshop/products.html", items=items, search=search)


@farmshop_bp.route("/farmshop/cart")
def cart():
    return render_template("farmshop/cart.html")


@farmshop_bp.route("/farmshop/checkout")
def checkout():
    return render_template("farmshop/checkout.html")


@farmshop_bp.route("/farmshop/products/<int:product_id>")
def product_detail(product_id):
    product = db.session.execute(text("""
        SELECT p.id, p.name, COALESCE(p.price,0) AS price, p.product_type,
               p.unit_type, p.base_unit, p.sold_by_weight, p.price_per_unit,
               p.stock_qty, p.image_url, p.description
        FROM products p
        WHERE p.id = :id AND p.is_for_sale = true
          AND p.is_available_online = true AND p.is_archived = false
    """), {"id": product_id}).fetchone()
    if not product:
        from flask import abort
        abort(404)

    # Try to load multi-images; fall back to single image_url if table doesn't exist yet
    try:
        images = db.session.execute(text("""
            SELECT filename, is_primary, display_order
            FROM product_images
            WHERE product_id = :id
            ORDER BY display_order ASC
        """), {"id": product_id}).fetchall()
    except Exception:
        images = []
    # If no product_images rows but image_url exists, create a synthetic list
    if not images and product.image_url:
        from collections import namedtuple
        FakeImg = namedtuple('FakeImg', ['filename', 'is_primary', 'display_order'])
        images = [FakeImg(product.image_url, True, 0)]

    from services.stock import get_available_qty
    avail = get_available_qty(db, product.id, product.product_type)

    return render_template("farmshop/product_detail.html",
                           product=product, images=images, available_qty=avail)


@farmshop_bp.route("/farmshop/orders/<reference>")
def order_status(reference):
    order = db.session.execute(
        text("SELECT * FROM online_orders WHERE reference = :ref"), {"ref": reference}
    ).fetchone()
    if not order:
        from flask import abort
        abort(404)
    lines = db.session.execute(text("""
        SELECT ol.*, p.name as product_name
        FROM online_order_lines ol
        JOIN products p ON p.id = ol.product_id
        WHERE ol.online_order_id = :oid
    """), {"oid": order.id}).fetchall()
    return render_template("farmshop/order_status.html", order=order, lines=lines)


# ── Public API ────────────────────────────────────────────────────────────────

@farmshop_bp.route("/api/farmshop/products")
def api_products():
    from services.stock import get_available_qty
    rows = db.session.execute(text("""
        SELECT id, name, COALESCE(price, 0) AS price, product_type, unit_type, base_unit,
               sold_by_weight, price_per_unit, image_url, description
        FROM products
        WHERE is_for_sale = true AND is_available_online = true AND is_archived = false
        ORDER BY name ASC
    """)).fetchall()

    result = []
    for r in rows:
        avail = get_available_qty(db, r.id, r.product_type)
        result.append({
            "id":             r.id,
            "name":           r.name,
            "price":          float(r.price),
            "product_type":   r.product_type,
            "sold_by_weight": r.sold_by_weight,
            "price_per_unit": float(r.price_per_unit) if r.price_per_unit else None,
            "available_qty":  avail,
            "stock_status":   _stock_status(avail, r.product_type),
            "image_url":      r.image_url,
        })
    return jsonify(products=result)


def process_payment(amount: float) -> dict:
    """
    Mock payment — instant approval.
    Replace with PayFast when account is verified.
    """
    import uuid as _uuid
    return {"status": "success", "reference": f"PAY-{_uuid.uuid4().hex[:8].upper()}"}


@farmshop_bp.route("/api/farmshop/orders", methods=["POST"])
def place_order():
    """
    Payment-first checkout: payment is processed BEFORE the order is created.
    No order exists until payment succeeds.
    """
    customer_id = None
    try:
        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
        if identity:
            customer_id = int(identity)
    except Exception:
        pass

    data             = request.get_json(silent=True) or {}
    cart_items       = data.get("items", [])
    delivery_method  = data.get("delivery_method", "collection")
    delivery_address = (data.get("delivery_address") or "").strip()
    notes            = (data.get("notes") or "").strip()

    # Guest fields
    guest_name  = (data.get("guest_name") or "").strip()
    guest_email = (data.get("guest_email") or "").strip().lower()
    guest_phone = (data.get("guest_phone") or "").strip()

    # Pudo fields
    pudo_point_name = (data.get("pudo_point_name") or "").strip()
    pudo_suburb     = (data.get("pudo_suburb") or "").strip()
    pudo_city       = (data.get("pudo_city") or "").strip()
    pudo_point_id   = (data.get("pudo_point_id") or "").strip()

    # ── Validation ────────────────────────────────────────────────────────────
    if not cart_items:
        return jsonify(error="Cart is empty"), 400
    if delivery_method not in ("collection", "delivery", "pudo"):
        return jsonify(error="Invalid delivery method"), 422
    if delivery_method == "delivery" and not delivery_address:
        return jsonify(error="Delivery address required"), 422
    if delivery_method == "pudo":
        if not pudo_point_name or not pudo_suburb or not pudo_city:
            return jsonify(error="Pudo point name, suburb and city are required"), 422
    if not customer_id and not guest_email:
        return jsonify(error="Email required for checkout"), 422

    # ── Validate and snapshot products ────────────────────────────────────────
    line_data = []
    subtotal  = Decimal("0")
    for item in cart_items:
        pid = int(item.get("product_id", 0))
        qty = Decimal(str(item.get("qty", 1)))
        if qty <= 0:
            continue
        row = db.session.execute(
            text("SELECT id, name, price, product_type, is_for_sale, is_available_online, is_archived FROM products WHERE id = :id"),
            {"id": pid}
        ).fetchone()
        if not row or not row.is_for_sale or not row.is_available_online or row.is_archived:
            return jsonify(error=f"Product {pid} is not available"), 422
        unit_price = Decimal(str(row.price or 0))
        line_total = qty * unit_price
        subtotal  += line_total
        line_data.append({
            "product_id":            pid,
            "product_name_snapshot": row.name,
            "qty":                   qty,
            "unit_price":            unit_price,
            "line_total":            line_total,
        })

    total = subtotal

    # ── Resolve customer info ─────────────────────────────────────────────────
    customer_name  = guest_name
    customer_email = guest_email
    customer_phone = guest_phone
    if customer_id and not customer_email:
        wc = db.session.execute(
            text("SELECT name, email, phone FROM web_customers WHERE id = :id"),
            {"id": customer_id}
        ).fetchone()
        if wc:
            customer_name  = customer_name or wc.name
            customer_email = wc.email
            customer_phone = customer_phone or wc.phone

    # ── Step 1: Process payment FIRST — no order created before this ──────────
    pay = process_payment(float(total))
    if pay.get("status") != "success":
        return jsonify(error="Payment failed — please try again"), 402
    pay_ref = pay["reference"]

    # ── Step 2: Create order + deduct stock in a single transaction ────────────
    from services.stock import check_and_deduct_order
    from datetime import datetime, timezone

    try:
        # 2a. Insert order (status='confirmed' — paid upfront)
        result = db.session.execute(text("""
            INSERT INTO online_orders
                (web_customer_id, guest_name, guest_email, guest_phone,
                 status, delivery_method, delivery_address, notes,
                 pudo_point_name, pudo_suburb, pudo_city, pudo_point_id,
                 subtotal, total, payment_reference, created_at, updated_at)
            VALUES
                (:cid, :gname, :gemail, :gphone,
                 'pending', :dm, :da, :notes,
                 :pname, :psuburb, :pcity, :ppid,
                 :sub, :total, :payref, now(), now())
            RETURNING id
        """), {
            "cid":     customer_id,
            "gname":   customer_name or None,
            "gemail":  customer_email or None,
            "gphone":  customer_phone or None,
            "dm":      delivery_method,
            "da":      delivery_address or None,
            "notes":   notes or None,
            "pname":   pudo_point_name or None,
            "psuburb": pudo_suburb or None,
            "pcity":   pudo_city or None,
            "ppid":    pudo_point_id or None,
            "sub":     float(subtotal),
            "total":   float(total),
            "payref":  pay_ref,
        })
        order_id = result.fetchone()[0]

        reference = f"LC-ORD-{order_id:06d}"
        db.session.execute(
            text("UPDATE online_orders SET reference = :ref WHERE id = :id"),
            {"ref": reference, "id": order_id}
        )

        # 2b. Insert line items
        for ld in line_data:
            db.session.execute(text("""
                INSERT INTO online_order_lines
                    (online_order_id, product_id, product_name_snapshot, qty, unit_price, line_total)
                VALUES (:oid, :pid, :name, :qty, :price, :total)
            """), {
                "oid":   order_id,
                "pid":   ld["product_id"],
                "name":  ld["product_name_snapshot"],
                "qty":   float(ld["qty"]),
                "price": float(ld["unit_price"]),
                "total": float(ld["line_total"]),
            })

        db.session.flush()  # get order_id into DB without committing yet

        # 2c. Deduct stock immediately (FIFO)
        sale_uuid = check_and_deduct_order(db, order_id, None)

        # 2d. Insert payment record
        now = datetime.now(timezone.utc)
        db.session.execute(text("""
            INSERT INTO payments (reference, order_type, order_id, amount, method, status, paid_at)
            VALUES (:ref, 'farmshop', :oid, :amount, 'paygate', 'paid', :now)
        """), {"ref": pay_ref, "oid": order_id, "amount": float(total), "now": now})

        # 2e. Create paid POS invoice
        delivery_note = _delivery_note(delivery_method, delivery_address,
                                       pudo_point_name, pudo_suburb, pudo_city)
        inv_notes = (f"[ONLINE - FARMSHOP ORDER] {reference}. {delivery_note}. "
                     f"Payment: {pay_ref}")
        inv_lines = [{
            "name":       ld["product_name_snapshot"],
            "qty":        float(ld["qty"]),
            "unit_price": float(ld["unit_price"]),
            "unit":       "unit",
            "subtotal":   float(ld["line_total"]),
        } for ld in line_data]

        invoice_id = _create_paid_invoice(
            reference=reference,
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            notes=inv_notes,
            lines=inv_lines,
            subtotal=float(subtotal),
            total=float(total),
            sale_id=sale_uuid,
        )

        # 2f. Link invoice and sale back to order, and set status=confirmed now that pos_sale_id is set
        db.session.execute(text("""
            UPDATE online_orders
            SET status = 'confirmed', invoice_id = :iid, pos_sale_id = :sid, updated_at = now()
            WHERE id = :oid
        """), {"iid": invoice_id, "sid": sale_uuid, "oid": order_id})

        db.session.commit()

    except ValueError as e:
        db.session.rollback()
        log.error("Order creation failed (stock) for payment %s: %s", pay_ref, e)
        return jsonify(error=str(e)), 422
    except Exception as e:
        db.session.rollback()
        log.error("Order creation failed for payment %s: %s", pay_ref, e)
        return jsonify(error="Order creation failed — please contact us"), 500

    # ── Step 3: Auto-create POS customer (best-effort, non-blocking) ──────────
    from services.customers import ensure_pos_customer
    pos_id = ensure_pos_customer(db, customer_name, customer_email, customer_phone,
                                 web_customer_id=customer_id)
    if pos_id and customer_id:
        try:
            db.session.execute(
                text("UPDATE web_customers SET pos_customer_id = :pid WHERE id = :cid AND pos_customer_id IS NULL"),
                {"pid": pos_id, "cid": customer_id}
            )
            db.session.commit()
        except Exception as e:
            log.warning("Could not link pos_customer_id for order %s: %s", reference, e)

    emit("order_created", {"type": "farmshop", "id": order_id, "reference": reference,
                           "customer": customer_email or f"customer:{customer_id}",
                           "payment_reference": pay_ref})

    # ── Step 4: Send emails after commit (failures do NOT affect order) ────────
    order_full = db.session.execute(
        text("SELECT * FROM online_orders WHERE id = :id"), {"id": order_id}
    ).fetchone()

    if customer_email:
        from services.email import send_order_confirmation_customer
        send_order_confirmation_customer(
            order=order_full, lines=line_data,
            pay_ref=pay_ref, delivery_note=delivery_note
        )
    if current_app.config.get("ADMIN_EMAIL"):
        from services.email import send_order_notification_admin
        send_order_notification_admin(
            order=order_full, lines=line_data,
            pay_ref=pay_ref, delivery_note=delivery_note
        )

    return jsonify(
        reference=reference,
        order_id=order_id,
        total=float(total),
        payment_reference=pay_ref,
    ), 201


@farmshop_bp.route("/api/farmshop/orders")
@jwt_required()
def my_orders():
    customer_id = int(get_jwt_identity())
    # Also match by guest_email in case order was placed without JWT (e.g. checkout form)
    wc = db.session.execute(
        text("SELECT email FROM web_customers WHERE id = :id"),
        {"id": customer_id}
    ).fetchone()
    customer_email = wc.email.lower() if wc else None

    orders = db.session.execute(text("""
        SELECT o.id, o.reference, o.status, o.total, o.delivery_method, o.created_at
        FROM online_orders o
        WHERE o.web_customer_id = :cid
           OR (o.web_customer_id IS NULL AND LOWER(o.guest_email) = :email)
        ORDER BY o.created_at DESC
        LIMIT 50
    """), {"cid": customer_id, "email": customer_email or ''}).fetchall()
    return jsonify(orders=[dict(r._mapping) for r in orders])


# ── PayFast payment flow ──────────────────────────────────────────────────────

@farmshop_bp.route("/api/farmshop/payfast/initiate", methods=["POST"])
def payfast_initiate():
    """
    Step 1: Validate cart, store session, return PayFast form data.
    The browser will POST the form fields to PayFast's payment page.
    """
    import uuid as _uuid

    customer_id = None
    try:
        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
        if identity:
            customer_id = int(identity)
    except Exception:
        pass

    data             = request.get_json(silent=True) or {}
    cart_items       = data.get("items", [])
    delivery_method  = data.get("delivery_method", "collection")
    delivery_address = (data.get("delivery_address") or "").strip()
    guest_name       = (data.get("guest_name") or "").strip()
    guest_email      = (data.get("guest_email") or "").strip().lower()
    guest_phone      = (data.get("guest_phone") or "").strip()
    pudo_point_name  = (data.get("pudo_point_name") or "").strip()
    pudo_suburb      = (data.get("pudo_suburb") or "").strip()
    pudo_city        = (data.get("pudo_city") or "").strip()
    pudo_point_id    = (data.get("pudo_point_id") or "").strip()
    notes            = (data.get("notes") or "").strip()

    # Validation
    if not cart_items:
        return jsonify(error="Cart is empty"), 400
    if delivery_method not in ("collection", "delivery", "pudo"):
        return jsonify(error="Invalid delivery method"), 422
    if delivery_method == "delivery" and not delivery_address:
        return jsonify(error="Delivery address required"), 422
    if delivery_method == "pudo" and (not pudo_point_name or not pudo_suburb or not pudo_city):
        return jsonify(error="Pudo point name, suburb and city are required"), 422
    if not customer_id and not guest_email:
        return jsonify(error="Email required for checkout"), 422

    # Resolve customer info
    customer_name  = guest_name
    customer_email = guest_email
    customer_phone = guest_phone
    if customer_id and not customer_email:
        wc = db.session.execute(
            text("SELECT name, email, phone FROM web_customers WHERE id = :id"),
            {"id": customer_id}
        ).fetchone()
        if wc:
            customer_name  = customer_name or wc.name
            customer_email = wc.email
            customer_phone = customer_phone or wc.phone

    # Validate and snapshot products + compute total
    line_data = []
    subtotal  = Decimal("0")
    for item in cart_items:
        pid = int(item.get("product_id", 0))
        qty = Decimal(str(item.get("qty", 1)))
        if qty <= 0:
            continue
        row = db.session.execute(
            text("SELECT id, name, price, product_type, is_for_sale, is_available_online, is_archived FROM products WHERE id = :id"),
            {"id": pid}
        ).fetchone()
        if not row or not row.is_for_sale or not row.is_available_online or row.is_archived:
            return jsonify(error=f"Product {pid} is not available"), 422
        unit_price = Decimal(str(row.price or 0))
        line_total = qty * unit_price
        subtotal  += line_total
        line_data.append({
            "product_id": pid, "product_name_snapshot": row.name,
            "qty": str(qty), "unit_price": str(unit_price), "line_total": str(line_total),
        })

    total = float(subtotal)

    # Store session in DB
    session_id = _uuid.uuid4().hex
    db.session.execute(text("""
        INSERT INTO payment_sessions (session_id, cart_json, customer_json, delivery_json, amount)
        VALUES (:sid, :cart, :cust, :deliv, :amount)
    """), {
        "sid":    session_id,
        "cart":   json.dumps({"items": line_data, "customer_id": customer_id}),
        "cust":   json.dumps({"name": customer_name, "email": customer_email, "phone": customer_phone}),
        "deliv":  json.dumps({
            "method": delivery_method, "address": delivery_address, "notes": notes,
            "pudo_point_name": pudo_point_name, "pudo_suburb": pudo_suburb,
            "pudo_city": pudo_city, "pudo_point_id": pudo_point_id,
        }),
        "amount": total,
    })
    db.session.commit()

    # Build PayFast form data
    from services.payfast import build_payfast_form
    form = build_payfast_form(
        session_id=session_id,
        amount=total,
        item_name="Lady Coleen Farmshop Order",
        customer_name=customer_name,
        customer_email=customer_email,
    )

    # Send fields as an ORDERED list of [key, value] pairs, NOT a dict.
    # jsonify sorts dict keys alphabetically, which scrambles the field order
    # PayFast rebuilds the signature from — causing a guaranteed signature mismatch.
    return jsonify(payfast_url=form["action"], fields=list(form["fields"].items())), 200


@farmshop_bp.route("/api/farmshop/payfast/notify", methods=["POST"])
def payfast_notify():
    """
    Step 2: PayFast ITN (Instant Transaction Notification).
    Called server-to-server by PayFast after successful payment.
    Creates the order, deducts stock, sends emails.
    """
    from services.payfast import verify_itn
    from services.stock import check_and_deduct_order
    from datetime import datetime, timezone

    form_data  = request.form.to_dict()
    session_id = form_data.get("m_payment_id", "")
    pf_amount  = form_data.get("amount_gross", "0")

    # Verify authenticity
    if not verify_itn(form_data):
        log.warning("PayFast ITN failed verification for session=%s", session_id)
        return "INVALID", 400

    # Load session
    sess = db.session.execute(
        text("SELECT * FROM payment_sessions WHERE session_id = :sid FOR UPDATE"),
        {"sid": session_id}
    ).fetchone()
    if not sess:
        log.error("PayFast ITN: session not found %s", session_id)
        return "OK", 200   # Return 200 to prevent PayFast retries
    if sess.status != "pending":
        log.info("PayFast ITN: session %s already processed (status=%s)", session_id, sess.status)
        return "OK", 200   # Idempotent

    cart_data     = json.loads(sess.cart_json)
    cust_data     = json.loads(sess.customer_json)
    deliv_data    = json.loads(sess.delivery_json)
    customer_id   = cart_data.get("customer_id")
    line_data     = cart_data["items"]
    customer_name  = cust_data["name"]
    customer_email = cust_data["email"]
    customer_phone = cust_data["phone"]
    delivery_method  = deliv_data["method"]
    delivery_address = deliv_data.get("address")
    notes            = deliv_data.get("notes")
    pudo_point_name  = deliv_data.get("pudo_point_name")
    pudo_suburb      = deliv_data.get("pudo_suburb")
    pudo_city        = deliv_data.get("pudo_city")
    pudo_point_id    = deliv_data.get("pudo_point_id")
    total            = float(sess.amount)
    pay_ref          = form_data.get("pf_payment_id", session_id)

    # Verify the amount PayFast charged matches our expected order total (within 1c).
    # Guards against a tampered/mismatched notification before we mark anything paid.
    try:
        if abs(total - float(pf_amount)) > 0.01:
            log.warning("PayFast ITN: amount mismatch session=%s expected=%.2f gross=%s",
                        session_id, total, pf_amount)
            return "OK", 200   # 200 so PayFast stops retrying; investigate manually
    except (TypeError, ValueError):
        log.warning("PayFast ITN: unparseable amount_gross=%s session=%s", pf_amount, session_id)
        return "OK", 200

    # Convert line_data back to Decimal
    for ld in line_data:
        ld["qty"]        = Decimal(str(ld["qty"]))
        ld["unit_price"] = Decimal(str(ld["unit_price"]))
        ld["line_total"] = Decimal(str(ld["line_total"]))

    try:
        # Insert order (pending first, then confirmed after stock deduction)
        result = db.session.execute(text("""
            INSERT INTO online_orders
                (web_customer_id, guest_name, guest_email, guest_phone,
                 status, delivery_method, delivery_address, notes,
                 pudo_point_name, pudo_suburb, pudo_city, pudo_point_id,
                 subtotal, total, payment_reference, created_at, updated_at)
            VALUES
                (:cid, :gname, :gemail, :gphone,
                 'pending', :dm, :da, :notes,
                 :pname, :psuburb, :pcity, :ppid,
                 :sub, :total, :payref, now(), now())
            RETURNING id
        """), {
            "cid": customer_id, "gname": customer_name or None,
            "gemail": customer_email or None, "gphone": customer_phone or None,
            "dm": delivery_method, "da": delivery_address or None, "notes": notes or None,
            "pname": pudo_point_name or None, "psuburb": pudo_suburb or None,
            "pcity": pudo_city or None, "ppid": pudo_point_id or None,
            "sub": total, "total": total, "payref": pay_ref,
        })
        order_id  = result.fetchone()[0]
        reference = f"LC-ORD-{order_id:06d}"
        db.session.execute(
            text("UPDATE online_orders SET reference = :ref WHERE id = :id"),
            {"ref": reference, "id": order_id}
        )

        # Insert line items
        for ld in line_data:
            db.session.execute(text("""
                INSERT INTO online_order_lines
                    (online_order_id, product_id, product_name_snapshot, qty, unit_price, line_total)
                VALUES (:oid, :pid, :name, :qty, :price, :total)
            """), {
                "oid": order_id, "pid": ld["product_id"],
                "name": ld["product_name_snapshot"],
                "qty": float(ld["qty"]), "price": float(ld["unit_price"]),
                "total": float(ld["line_total"]),
            })

        db.session.flush()

        # Ensure POS customer exists and is linked BEFORE stock deduction
        # so the sale record gets the correct customer_id
        from services.customers import ensure_pos_customer as _ensure_pos
        _pos_id = _ensure_pos(db, customer_name, customer_email, customer_phone,
                              web_customer_id=customer_id)
        if _pos_id and customer_id:
            try:
                db.session.execute(
                    text("UPDATE web_customers SET pos_customer_id=:pid WHERE id=:cid AND pos_customer_id IS NULL"),
                    {"pid": _pos_id, "cid": customer_id}
                )
                db.session.flush()
            except Exception:
                pass

        # Deduct stock
        sale_uuid = check_and_deduct_order(db, order_id, None)

        # Payment record
        now = datetime.now(timezone.utc)
        db.session.execute(text("""
            INSERT INTO payments (reference, order_type, order_id, amount, method, status, paid_at)
            VALUES (:ref, 'farmshop', :oid, :amount, 'payfast', 'paid', :now)
        """), {"ref": pay_ref, "oid": order_id, "amount": total, "now": now})

        # Paid POS invoice
        delivery_note = _delivery_note(delivery_method, delivery_address,
                                       pudo_point_name, pudo_suburb, pudo_city)
        inv_notes = (f"[ONLINE - FARMSHOP ORDER] {reference}. {delivery_note}. "
                     f"Payment: {pay_ref}")
        inv_lines = [{
            "name": ld["product_name_snapshot"], "qty": float(ld["qty"]),
            "unit_price": float(ld["unit_price"]), "unit": "unit",
            "subtotal": float(ld["line_total"]),
        } for ld in line_data]

        invoice_id = _create_paid_invoice(
            reference=reference, customer_name=customer_name,
            customer_phone=customer_phone, customer_email=customer_email,
            notes=inv_notes, lines=inv_lines, subtotal=total, total=total,
            sale_id=sale_uuid,
        )

        # Confirm order
        db.session.execute(text("""
            UPDATE online_orders
            SET status='confirmed', pos_sale_id=:sid, invoice_id=:iid, updated_at=now()
            WHERE id=:oid
        """), {"sid": sale_uuid, "iid": invoice_id, "oid": order_id})

        # Mark session used
        db.session.execute(
            text("UPDATE payment_sessions SET status='completed' WHERE session_id=:sid"),
            {"sid": session_id}
        )
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        log.error("PayFast ITN: order creation failed for session %s: %s", session_id, e)
        return "OK", 200   # Still return 200 — log and investigate manually

    # Emails (non-blocking)
    order_full = db.session.execute(
        text("SELECT * FROM online_orders WHERE id=:id"), {"id": order_id}
    ).fetchone()
    if customer_email:
        from services.email import send_order_confirmation_customer
        send_order_confirmation_customer(order=order_full, lines=line_data,
                                         pay_ref=pay_ref, delivery_note=delivery_note)
    if current_app.config.get("ADMIN_EMAIL"):
        from services.email import send_order_notification_admin
        send_order_notification_admin(order=order_full, lines=line_data,
                                      pay_ref=pay_ref, delivery_note=delivery_note)

    log.info('{"action":"payfast_order_created","order_id":%d,"reference":"%s","pay_ref":"%s"}',
             order_id, reference, pay_ref)
    return "OK", 200


@farmshop_bp.route("/farmshop/payment/success")
def payment_success():
    session_id = request.args.get("session", "")
    # Try to find the order created for this session
    order_ref = None
    if session_id:
        row = db.session.execute(
            text("""
                SELECT o.reference FROM online_orders o
                WHERE o.payment_reference IN (
                    SELECT pf_payment_id FROM payment_sessions WHERE session_id = :sid
                    UNION SELECT session_id FROM payment_sessions WHERE session_id = :sid
                )
                ORDER BY o.created_at DESC LIMIT 1
            """),
            {"sid": session_id}
        ).fetchone()
        if not row:
            # ITN may not have fired yet — look up by payment_reference = session_id fallback
            sess = db.session.execute(
                text("SELECT status FROM payment_sessions WHERE session_id = :sid"),
                {"sid": session_id}
            ).fetchone()
        else:
            order_ref = row.reference
    return render_template("farmshop/payment_success.html", order_ref=order_ref, session_id=session_id)


@farmshop_bp.route("/farmshop/payment/cancel")
def payment_cancel():
    session_id = request.args.get("session", "")
    if session_id:
        db.session.execute(
            text("UPDATE payment_sessions SET status='cancelled' WHERE session_id=:sid AND status='pending'"),
            {"sid": session_id}
        )
        db.session.commit()
    return render_template("farmshop/payment_cancel.html")


# ── Admin ─────────────────────────────────────────────────────────────────────

@farmshop_bp.route("/admin/farmshop")
def admin_orders():
    from blueprints.auth import require_admin
    redir, code = require_admin()
    if redir:
        return redir, code

    status_filter = request.args.get("status", "active")
    if status_filter == "active":
        rows = db.session.execute(text("""
            SELECT o.*, p.status as payment_status
            FROM online_orders o
            LEFT JOIN payments p ON p.order_type='farmshop' AND p.order_id=o.id AND p.status='paid'
            WHERE o.status IN ('draft','pending','confirmed','ready')
            ORDER BY o.created_at DESC
        """)).fetchall()
    else:
        rows = db.session.execute(text("""
            SELECT o.*, p.status as payment_status
            FROM online_orders o
            LEFT JOIN payments p ON p.order_type='farmshop' AND p.order_id=o.id AND p.status='paid'
            ORDER BY o.created_at DESC LIMIT 200
        """)).fetchall()

    return render_template("admin/farmshop.html", orders=rows, status_filter=status_filter)


@farmshop_bp.route("/admin/farmshop/<int:order_id>")
def admin_order_detail(order_id):
    from blueprints.auth import require_admin
    redir, code = require_admin()
    if redir:
        return redir, code

    order = db.session.execute(
        text("SELECT * FROM online_orders WHERE id = :id"), {"id": order_id}
    ).fetchone()
    if not order:
        from flask import abort
        abort(404)

    lines = db.session.execute(text("""
        SELECT ol.*, p.product_type,
               COALESCE(ol.product_name_snapshot, p.name) as display_name
        FROM online_order_lines ol
        JOIN products p ON p.id = ol.product_id
        WHERE ol.online_order_id = :oid
    """), {"oid": order_id}).fetchall()

    from services.stock import get_available_qty
    stock_issues = []
    for line in lines:
        avail = get_available_qty(db, line.product_id, line.product_type)
        if avail < float(line.qty):
            stock_issues.append({
                "name":      line.display_name,
                "needed":    float(line.qty),
                "available": avail,
            })

    payment = db.session.execute(
        text("SELECT * FROM payments WHERE order_type='farmshop' AND order_id=:id"),
        {"id": order_id}
    ).fetchone()

    return render_template("admin/farmshop_detail.html",
                           order=order, lines=lines,
                           stock_issues=stock_issues, payment=payment)


@farmshop_bp.route("/api/admin/farmshop/<int:order_id>/confirm", methods=["POST"])
def admin_confirm_order(order_id):
    from blueprints.auth import require_admin
    from flask import session as flask_session
    redir, code = require_admin()
    if redir:
        return redir, code

    from services.stock import check_and_deduct_order
    try:
        sale_uuid = check_and_deduct_order(db, order_id, flask_session.get("admin_id"))
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return jsonify(error=str(e)), 400
    except Exception as e:
        db.session.rollback()
        log.error("Order confirm failed order_id=%d: %s", order_id, e)
        return jsonify(error="Stock deduction failed — order remains pending. Check logs."), 500

    # Link POS sale to invoice — idempotent (only if not already linked)
    order = db.session.execute(
        text("SELECT invoice_id FROM online_orders WHERE id = :id"), {"id": order_id}
    ).fetchone()
    if order and order.invoice_id:
        result = db.session.execute(text("""
            UPDATE invoices SET status = 'sent', sale_id = :sid
            WHERE id = :iid AND sale_id IS NULL
        """), {"sid": sale_uuid, "iid": order.invoice_id})
        db.session.commit()
        if result.rowcount == 0:
            log.info("Invoice %d already linked to a sale — skipped update", order.invoice_id)

    emit("order_confirmed", {"type": "farmshop", "id": order_id, "pos_sale_id": sale_uuid})

    order_full = db.session.execute(
        text("SELECT * FROM online_orders WHERE id = :id"), {"id": order_id}
    ).fetchone()
    _send_farmshop_email(order_full, "farmshop_order_confirmed")

    return jsonify(ok=True, pos_sale_id=sale_uuid, status="confirmed")


@farmshop_bp.route("/api/admin/farmshop/<int:order_id>/status", methods=["POST"])
def admin_update_status(order_id):
    from blueprints.auth import require_admin
    redir, code = require_admin()
    if redir:
        return redir, code

    data       = request.get_json(silent=True) or {}
    new_status = data.get("status")

    order = db.session.execute(
        text("SELECT id, status FROM online_orders WHERE id = :id FOR UPDATE"),
        {"id": order_id}
    ).fetchone()
    if not order:
        return jsonify(error="Not found"), 404

    transitions = {
        "confirmed":  ["ready", "cancelled"],
        "ready":      ["dispatched", "completed", "cancelled"],
        "dispatched": ["completed"],
    }
    if new_status not in transitions.get(order.status, []):
        return jsonify(error=f"Cannot transition from '{order.status}' to '{new_status}'"), 400

    db.session.execute(text("""
        UPDATE online_orders SET status = :s, updated_at = now() WHERE id = :id
    """), {"s": new_status, "id": order_id})
    db.session.commit()

    emit("order_status_changed", {"type": "farmshop", "id": order_id, "status": new_status})

    if new_status == "ready":
        order_full = db.session.execute(
            text("SELECT * FROM online_orders WHERE id = :id"), {"id": order_id}
        ).fetchone()
        _send_farmshop_email(order_full, "farmshop_order_ready")

    return jsonify(ok=True, status=new_status)


@farmshop_bp.route("/api/admin/farmshop/<int:order_id>/undo", methods=["POST"])
def admin_undo_order(order_id):
    """
    Reverse stock deduction and reset order to draft.
    Payment record is NOT deleted — preserved as proof of transaction.
    Idempotent: safe to call multiple times.
    """
    from blueprints.auth import require_admin
    from flask import session as flask_session
    from datetime import datetime, timezone
    redir, code = require_admin()
    if redir:
        return redir, code

    order = db.session.execute(
        text("SELECT id, pos_sale_id, status, invoice_id FROM online_orders WHERE id = :id FOR UPDATE"),
        {"id": order_id}
    ).fetchone()
    if not order:
        return jsonify(error="Not found"), 404

    # Idempotent — already undone
    if not order.pos_sale_id:
        return jsonify(ok=True, message="Order already undone"), 200

    sale_uuid  = order.pos_sale_id
    admin_user = flask_session.get("admin_user", "?")
    now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    # 1. Restore stock (reverse FIFO batches + simple stock)
    from services.stock import reverse_sale_consumption
    reverse_sale_consumption(db, sale_uuid)

    # 2. Void the sale records — preserve for audit trail, do NOT delete
    db.session.execute(text("""
        UPDATE sales SET voided = true, voided_at = now(),
            void_reason = :reason, flag_resolved = false
        WHERE sale_id = :sid AND voided = false
    """), {"sid": sale_uuid, "reason": f"Undone by {admin_user} @ {now_str}"})

    # 3. Reset order to draft
    db.session.execute(text("""
        UPDATE online_orders
        SET status = 'draft', pos_sale_id = NULL, updated_at = now()
        WHERE id = :id
    """), {"id": order_id})

    # 4. Reset POS invoice to draft if linked
    if order.invoice_id:
        audit = f"[UNDO] Sale {sale_uuid[:8]} reversed by {admin_user} @ {now_str} UTC"
        db.session.execute(text("""
            UPDATE invoices
            SET status = 'draft', sale_id = NULL,
                notes = TRIM(CONCAT(COALESCE(notes, ''), ' ', :audit))
            WHERE id = :iid
        """), {"iid": order.invoice_id, "audit": audit})

    # Payment record intentionally NOT touched — proof of transaction

    db.session.commit()
    log.info('{"action":"order_undone","order_id":%d,"sale_id":"%s","by":"%s"}',
             order_id, sale_uuid, admin_user)
    return jsonify(ok=True, message="Order undone — stock restored")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_paid_invoice(reference, customer_name, customer_phone,
                         customer_email, notes, lines, subtotal, total,
                         sale_id=None) -> int | None:
    """
    Create a POS invoice in 'sent' status — paid and ready for fulfilment.
    Links to the POS sale record if sale_id is provided.
    """
    try:
        result = db.session.execute(text("""
            INSERT INTO invoices
                (invoice_number, created_at, customer_name, customer_phone, customer_email,
                 notes, lines_json, subtotal, discount_pct, total, status, sale_id)
            VALUES
                (:ref, now(), :cname, :cphone, :cemail,
                 :notes, :lines, :sub, 0, :total, 'paid', :sale_id)
            RETURNING id
        """), {
            "ref":     reference,
            "cname":   customer_name or "",
            "cphone":  customer_phone or "",
            "cemail":  customer_email or "",
            "notes":   notes,
            "lines":   json.dumps(lines),
            "sub":     float(subtotal),
            "total":   float(total),
            "sale_id": sale_id,
        })
        db.session.flush()
        invoice_id = result.fetchone()[0]
        log.info('{"action":"paid_invoice_created","invoice_id":%d,"reference":"%s"}',
                 invoice_id, reference)
        return invoice_id
    except Exception as e:
        log.error('{"action":"paid_invoice_failed","reference":"%s","error":"%s"}', reference, e)
        return None


def _create_draft_invoice(reference, customer_name, customer_phone,
                          customer_email, notes, lines, subtotal, total) -> int | None:
    try:
        result = db.session.execute(text("""
            INSERT INTO invoices
                (invoice_number, created_at, customer_name, customer_phone, customer_email,
                 notes, lines_json, subtotal, discount_pct, total, status)
            VALUES
                (:ref, now(), :cname, :cphone, :cemail,
                 :notes, :lines, :sub, 0, :total, 'draft')
            RETURNING id
        """), {
            "ref":    reference,
            "cname":  customer_name or "",
            "cphone": customer_phone or "",
            "cemail": customer_email or "",
            "notes":  notes,
            "lines":  json.dumps(lines),
            "sub":    float(subtotal),
            "total":  float(total),
        })
        db.session.flush()
        invoice_id = result.fetchone()[0]
        log.info('{"action":"draft_invoice_created","invoice_id":%d,"reference":"%s"}',
                 invoice_id, reference)
        return invoice_id
    except Exception as e:
        db.session.rollback()
        log.error('{"action":"draft_invoice_failed","reference":"%s","error":"%s"}', reference, e)
        return None


def _delivery_note(method, address, pudo_name, pudo_suburb, pudo_city) -> str:
    if method == "collection":
        return "Delivery: Collection from farm stall"
    if method == "pudo":
        return f"Delivery: Pudo — {pudo_name}, {pudo_suburb}, {pudo_city}"
    return f"Delivery: {address or 'Address not provided'}"


def _stock_status(avail: float, product_type: str) -> str:
    if avail <= 0:
        return "out_of_stock"
    if avail <= LOW_STOCK_THRESHOLD:
        return "low_stock"
    return "in_stock"


def _send_farmshop_email(order, template: str):
    if not order:
        return
    customer_email = order.guest_email
    if not customer_email and order.web_customer_id:
        row = db.session.execute(
            text("SELECT email FROM web_customers WHERE id = :id"),
            {"id": order.web_customer_id}
        ).fetchone()
        if row:
            customer_email = row.email
    if customer_email:
        from services.email import send_email
        send_email(to=customer_email, subject=f"Order update — {order.reference}",
                   template=template, order=order)

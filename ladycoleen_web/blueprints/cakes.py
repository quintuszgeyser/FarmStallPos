import json
import logging
from datetime import date, timedelta
from flask import Blueprint, request, jsonify, render_template, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, verify_jwt_in_request
from models import db, CakeOrder, WebCustomer, Payment
from services.files import save_upload
from services.email import send_email
from services.events import emit
from blueprints.auth import require_admin

log = logging.getLogger(__name__)
cakes_bp = Blueprint("cakes", __name__)


# ── Public pages ──────────────────────────────────────────────────────────────

# Cakes hidden for now - public cake pages redirect to the farm shop.
# Order-status tracking (/cakes/orders/<ref>), admin, and APIs stay live so
# existing cake orders and their email links keep working.
@cakes_bp.route("/cakes")
def landing():
    from flask import redirect
    return redirect("/farmshop")


@cakes_bp.route("/cakes/order")
def order_form():
    from flask import redirect
    return redirect("/farmshop")


@cakes_bp.route("/cakes/orders/<reference>")
def order_status(reference):
    order = CakeOrder.query.filter_by(reference=reference).first_or_404()
    return render_template("cakes/order_status.html", order=order)


# ── Public API ────────────────────────────────────────────────────────────────

@cakes_bp.route("/api/cakes/orders", methods=["POST"])
def submit_order():
    from sqlalchemy import text

    min_days = current_app.config["CAKE_MIN_NOTICE_DAYS"]

    customer_id = None
    try:
        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
        if identity:
            customer_id = int(identity)
    except Exception:
        pass

    date_str = (request.form.get("date_required") or "").strip()
    size     = (request.form.get("size") or "").strip()
    flavor   = (request.form.get("flavor") or "").strip()
    serves   = request.form.get("serves")
    desc     = (request.form.get("design_description") or "").strip()

    guest_name  = (request.form.get("guest_name") or "").strip()
    guest_email = (request.form.get("guest_email") or "").strip().lower()
    guest_phone = (request.form.get("guest_phone") or "").strip()

    errors = {}
    if not date_str:
        errors["date_required"] = "Required"
    else:
        try:
            req_date = date.fromisoformat(date_str)
            if req_date < date.today() + timedelta(days=min_days):
                errors["date_required"] = f"We need at least {min_days} days notice. For urgent orders please contact us directly."
        except ValueError:
            errors["date_required"] = "Invalid date"

    if not size:
        errors["size"] = "Required"
    if not flavor:
        errors["flavor"] = "Required"

    if not customer_id:
        if not guest_name:
            errors["guest_name"] = "Required"
        if not guest_email:
            errors["guest_email"] = "Required"

    if errors:
        return jsonify(errors=errors), 422

    image_path = None
    if "image" in request.files:
        f = request.files["image"]
        if f and f.filename:
            image_path, err = save_upload(f, "cake_images", current_app.config["ALLOWED_IMAGE_EXT"])
            if err:
                return jsonify(error=err), 422

    order = CakeOrder(
        web_customer_id=customer_id,
        guest_name=guest_name or None,
        guest_email=guest_email or None,
        guest_phone=guest_phone or None,
        status="pending",
        date_required=date.fromisoformat(date_str),
        size=size,
        flavor=flavor,
        serves=int(serves) if serves and serves.isdigit() else None,
        design_description=desc or None,
        image_path=image_path,
    )
    db.session.add(order)
    db.session.flush()
    order.reference = f"LC-CAKE-{order.id:06d}"
    db.session.commit()

    # ── Create draft POS invoice immediately ─────────────────────────────────
    invoice_id = _create_draft_invoice(
        db=db,
        reference=order.reference,
        customer_name=order.customer_name,
        customer_phone=order.customer_phone or "",
        customer_email=order.customer_email or "",
        notes=f"[ONLINE - CAKE ORDER] {order.reference}. Date required: {order.date_required}. Size: {order.size}, {order.flavor}",
        lines=[{"name": f"Custom Cake - {order.size}, {order.flavor}",
                "qty": 1, "unit_price": 0, "unit": "unit", "subtotal": 0}],
        subtotal=0,
        total=0,
    )
    if invoice_id:
        order.invoice_id = invoice_id
        db.session.commit()

    # ── Auto-create POS customer ──────────────────────────────────────────────
    _link_pos_customer(order, customer_id, guest_name, guest_email, guest_phone)

    emit("order_created", {"type": "cake", "id": order.id, "reference": order.reference,
                           "customer": order.customer_email})

    send_email(
        to=order.customer_email,
        subject=f"Order received - {order.reference}",
        template="cake_order_received",
        order=order
    )
    if current_app.config.get("ADMIN_EMAIL"):
        send_email(
            to=current_app.config["ADMIN_EMAIL"],
            subject=f"New cake order - {order.reference}",
            template="cake_order_admin_notify",
            order=order
        )

    return jsonify(reference=order.reference, status=order.status), 201


@cakes_bp.route("/api/cakes/orders/<reference>/status")
def api_order_status(reference):
    order = CakeOrder.query.filter_by(reference=reference).first_or_404()
    return jsonify(
        reference=order.reference,
        status=order.status,
        date_required=order.date_required.isoformat(),
        quoted_price=float(order.quoted_price) if order.quoted_price else None,
        has_invoice=order.invoice_id is not None
    )


@cakes_bp.route("/api/cakes/orders/<reference>/accept", methods=["POST"])
def accept_quote(reference):
    """Customer accepts their quote. No auth - reference is the token."""
    from datetime import datetime, timezone
    order = CakeOrder.query.filter_by(reference=reference).first_or_404()

    if order.status == "customer_confirmed":
        return jsonify(ok=True, status=order.status), 200

    if order.status != "quoted":
        return jsonify(error=f"This order cannot be accepted (status: {order.status})"), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    audit = f"[Accepted {order.reference} by customer @ {stamp} UTC from {ip}]"
    order.admin_notes = ((order.admin_notes or "") + " " + audit).strip()
    order.status = "customer_confirmed"
    db.session.commit()

    emit("order_customer_confirmed", {"type": "cake", "id": order.id, "reference": order.reference})

    if current_app.config.get("ADMIN_EMAIL"):
        send_email(
            to=current_app.config["ADMIN_EMAIL"],
            subject=f"Customer confirmed quote - {order.reference}",
            template="cake_order_admin_notify",
            order=order
        )

    return jsonify(ok=True, status=order.status), 200


# ── Admin ─────────────────────────────────────────────────────────────────────

@cakes_bp.route("/admin/cakes")
def admin_orders():
    redir, code = require_admin()
    if redir:
        return redir, code

    status_filter = request.args.get("status", "active")
    if status_filter == "active":
        orders = CakeOrder.query.filter(
            CakeOrder.status.in_(["pending", "quoted", "customer_confirmed", "confirmed", "in_production"])
        ).order_by(CakeOrder.created_at.desc()).all()
    elif status_filter == "all":
        orders = CakeOrder.query.order_by(CakeOrder.created_at.desc()).limit(200).all()
    else:
        orders = CakeOrder.query.filter_by(status=status_filter)\
                          .order_by(CakeOrder.created_at.desc()).all()

    return render_template("admin/cakes.html", orders=orders, status_filter=status_filter)


@cakes_bp.route("/admin/cakes/<int:order_id>")
def admin_order_detail(order_id):
    redir, code = require_admin()
    if redir:
        return redir, code
    order = CakeOrder.query.get_or_404(order_id)
    return render_template("admin/cake_detail.html", order=order)


@cakes_bp.route("/api/admin/cakes/<int:order_id>/quote", methods=["POST"])
def admin_quote(order_id):
    from sqlalchemy import text

    redir, code = require_admin()
    if redir:
        return redir, code

    order = CakeOrder.query.get_or_404(order_id)
    if order.status not in ("pending", "quoted"):
        return jsonify(error="Can only quote pending orders"), 400

    data  = request.get_json(silent=True) or {}
    price = data.get("price")
    notes = (data.get("notes") or "").strip()

    try:
        price = float(price)
        if price <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(error="Invalid price"), 422

    order.quoted_price = price
    order.admin_notes  = notes or order.admin_notes
    order.status       = "quoted"
    db.session.commit()

    # Update POS invoice with actual price - only if still draft and not yet linked to a sale
    if order.invoice_id:
        lines_json = json.dumps([{
            "name": f"Custom Cake - {order.size}, {order.flavor}",
            "qty": 1,
            "unit_price": price,
            "unit": "unit",
            "subtotal": price,
        }])
        result = db.session.execute(text("""
            UPDATE invoices
            SET subtotal = :p, total = :p, lines_json = :lj, status = 'sent'
            WHERE id = :invoice_id AND status = 'draft' AND sale_id IS NULL
        """), {"p": price, "lj": lines_json, "invoice_id": order.invoice_id})
        db.session.commit()
        if result.rowcount == 0:
            log.warning("Invoice %d not updated on quote (already sent or linked)", order.invoice_id)

    emit("order_quoted", {"type": "cake", "id": order.id, "reference": order.reference, "price": price})

    send_email(
        to=order.customer_email,
        subject=f"Your cake quote is ready - {order.reference}",
        template="cake_quote_ready",
        order=order
    )
    return jsonify(ok=True, status=order.status, quoted_price=float(order.quoted_price))


@cakes_bp.route("/api/admin/cakes/<int:order_id>/status", methods=["POST"])
def admin_update_status(order_id):
    redir, code = require_admin()
    if redir:
        return redir, code

    order    = CakeOrder.query.get_or_404(order_id)
    data     = request.get_json(silent=True) or {}
    new_status = data.get("status")

    transitions = {
        "pending":            ["quoted", "cancelled"],
        "quoted":             ["customer_confirmed", "confirmed", "cancelled"],
        "customer_confirmed": ["in_production", "cancelled"],
        "confirmed":          ["in_production", "cancelled"],
        "in_production":      ["completed", "cancelled"],
    }
    allowed = transitions.get(order.status, [])
    if new_status not in allowed:
        return jsonify(error=f"Cannot transition from '{order.status}' to '{new_status}'"), 400

    if new_status == "confirmed" and not order.quoted_price:
        return jsonify(error="Set a quoted price before confirming"), 400

    order.status = new_status
    db.session.commit()

    emit("order_status_changed", {"type": "cake", "id": order.id,
                                  "reference": order.reference, "status": new_status})

    if new_status == "confirmed":
        send_email(
            to=order.customer_email,
            subject=f"Your cake order is confirmed - {order.reference}",
            template="cake_order_confirmed",
            order=order
        )

    return jsonify(ok=True, status=order.status)


@cakes_bp.route("/api/admin/cakes/<int:order_id>/invoice", methods=["POST"])
def admin_create_invoice(order_id):
    """Fallback: manually create POS invoice if it wasn't created automatically."""
    from sqlalchemy import text
    from datetime import date as dt

    redir, code = require_admin()
    if redir:
        return redir, code

    order = CakeOrder.query.get_or_404(order_id)

    if order.status not in ("confirmed", "customer_confirmed", "in_production", "completed"):
        return jsonify(error="Order must be confirmed before creating invoice"), 400
    if not order.quoted_price:
        return jsonify(error="Set a quoted price first"), 400
    if order.invoice_id:
        return jsonify(error="Invoice already exists", invoice_id=order.invoice_id), 409

    from flask import session as flask_session
    lines_json = json.dumps([{
        "name": f"Custom Cake - {order.size}, {order.flavor}",
        "qty": 1, "unit_price": float(order.quoted_price),
        "unit": "unit", "subtotal": float(order.quoted_price),
    }])

    result = db.session.execute(text("""
        INSERT INTO invoices (invoice_number, created_at, due_date,
            customer_name, customer_phone, customer_email,
            notes, lines_json, subtotal, discount_pct, total, status, created_by)
        VALUES (
            :ref, now(), :due,
            :cname, :cphone, :cemail,
            :notes, :lines,
            :total, 0, :total,
            'sent', :admin_id
        )
        RETURNING id
    """), {
        "ref":      order.reference,
        "due":      dt.today().isoformat(),
        "cname":    order.customer_name or "",
        "cphone":   order.customer_phone or "",
        "cemail":   order.customer_email or "",
        "notes":    f"[ONLINE - CAKE ORDER] {order.reference}. Date required: {order.date_required}",
        "lines":    lines_json,
        "total":    float(order.quoted_price),
        "admin_id": flask_session.get("admin_id", 1),
    })
    invoice_id = result.fetchone()[0]
    order.invoice_id = invoice_id
    db.session.commit()

    emit("invoice_created", {"type": "cake", "id": order.id, "invoice_id": invoice_id})
    return jsonify(ok=True, invoice_id=invoice_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_draft_invoice(db, reference, customer_name, customer_phone,
                          customer_email, notes, lines, subtotal, total) -> int | None:
    """Insert a draft invoice into the POS invoices table. Returns invoice id or None on failure."""
    from sqlalchemy import text
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


def _link_pos_customer(order, customer_id, guest_name, guest_email, guest_phone):
    """Create/find POS customer and link to web_customer record."""
    from services.customers import ensure_pos_customer
    name  = order.customer_name or guest_name
    email = order.customer_email or guest_email
    phone = order.customer_phone or guest_phone

    pos_id = ensure_pos_customer(db, name, email, phone, web_customer_id=customer_id)
    if pos_id and customer_id:
        try:
            from sqlalchemy import text
            db.session.execute(
                text("UPDATE web_customers SET pos_customer_id = :pid WHERE id = :cid AND pos_customer_id IS NULL"),
                {"pid": pos_id, "cid": customer_id}
            )
            db.session.commit()
        except Exception as e:
            log.warning("Could not link pos_customer_id: %s", e)

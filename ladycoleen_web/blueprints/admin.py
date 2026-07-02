from flask import Blueprint, render_template, jsonify, request, current_app
from sqlalchemy import text
from models import db, CakeOrder, Payment
from blueprints.auth import require_admin

admin_bp = Blueprint("admin", __name__)


@admin_bp.app_context_processor
def inject_email_configured():
    return {"email_configured": bool(current_app.config.get("SMTP_HOST"))}


@admin_bp.route("/admin")
def dashboard():
    redir, code = require_admin()
    if redir:
        return redir, code

    # Summary counts
    pending_cakes = CakeOrder.query.filter(
        CakeOrder.status.in_(["pending", "quoted"])
    ).count()
    active_cakes = CakeOrder.query.filter(
        CakeOrder.status.in_(["confirmed", "in_production"])
    ).count()
    pending_payments = Payment.query.filter_by(status="pending").count()

    # Revenue last 7 days (paid payments)
    revenue = db.session.execute(text("""
        SELECT COALESCE(SUM(amount), 0)
        FROM payments
        WHERE status = 'paid'
          AND paid_at >= now() - interval '7 days'
    """)).scalar()

    # Recent orders (last 10)
    recent_cake_orders = CakeOrder.query.order_by(CakeOrder.created_at.desc()).limit(10).all()

    return render_template(
        "admin/dashboard.html",
        pending_cakes=pending_cakes,
        active_cakes=active_cakes,
        pending_payments=pending_payments,
        revenue_7d=float(revenue or 0),
        recent_cake_orders=recent_cake_orders,
    )


@admin_bp.route("/admin/payments")
def payments():
    redir, code = require_admin()
    if redir:
        return redir, code

    status_filter = request.args.get("status", "pending")
    q = Payment.query
    if status_filter != "all":
        q = q.filter_by(status=status_filter)
    payments_list = q.order_by(Payment.created_at.desc()).limit(100).all()
    return render_template("admin/payments.html", payments=payments_list, status_filter=status_filter)


@admin_bp.route("/api/admin/payments/<int:payment_id>/mark_paid", methods=["POST"])
def mark_payment_paid(payment_id):
    redir, code = require_admin()
    if redir:
        return redir, code

    payment = Payment.query.get_or_404(payment_id)
    if payment.status == "paid":
        return jsonify(error="Already marked as paid"), 409

    from datetime import datetime, timezone
    from flask import session as flask_session
    data = request.get_json(silent=True) or {}

    reference = (data.get("reference") or "").strip()
    if not reference:
        return jsonify(error="Bank reference is required to mark as paid"), 422

    now = datetime.now(timezone.utc)
    stamp = f"[Marked paid by {flask_session.get('admin_user', '?')} @ {now.strftime('%Y-%m-%d %H:%M')} UTC]"

    payment.status    = "paid"
    payment.paid_at   = now
    payment.reference = reference
    payment.notes     = ((payment.notes or "") + " " + (data.get("notes") or "") + " " + stamp).strip()
    db.session.commit()

    from services.events import emit
    emit("payment_received", {"payment_id": payment.id, "order_type": payment.order_type,
                               "order_id": payment.order_id, "amount": float(payment.amount)})

    # Email customer
    if payment.order_type == "cake":
        order = CakeOrder.query.get(payment.order_id)
        if order:
            from services.email import send_email
            send_email(
                to=order.customer_email,
                subject=f"Payment received - {order.reference}",
                template="payment_received",
                order=order,
                payment=payment
            )

    return jsonify(ok=True)


@admin_bp.route("/api/admin/payments/<int:payment_id>/upload_proof", methods=["POST"])
def upload_proof(payment_id):
    redir, code = require_admin()
    if redir:
        return redir, code

    payment = Payment.query.get_or_404(payment_id)
    if "proof" not in request.files:
        return jsonify(error="No file"), 400

    from flask import current_app
    from services.files import save_upload
    path, err = save_upload(
        request.files["proof"],
        "payment_proofs",
        current_app.config["ALLOWED_PROOF_EXT"]
    )
    if err:
        return jsonify(error=err), 422

    payment.proof_path = path
    db.session.commit()
    return jsonify(ok=True, proof_path=path)


@admin_bp.route("/admin/customers")
def customers():
    redir, code = require_admin()
    if redir:
        return redir, code

    from models import WebCustomer
    page     = int(request.args.get("page", 1))
    tab      = request.args.get("tab", "all")  # "all" | "online"
    per_page = 50

    if tab == "online":
        # Online-only: web customers whose linked POS customer exists and has NOT been merged
        rows = db.session.execute(text("""
            SELECT wc.id
            FROM web_customers wc
            JOIN customers c ON c.id = wc.pos_customer_id
            WHERE wc.deleted_at IS NULL
              AND wc.pos_customer_id IS NOT NULL
              AND c.merged_into IS NULL
              AND c.active = true
            ORDER BY wc.created_at DESC
        """)).fetchall()
        online_ids = [r.id for r in rows]
        q = WebCustomer.query.filter(
            WebCustomer.id.in_(online_ids or [-1])
        )
    else:
        q = WebCustomer.query.filter_by(deleted_at=None)

    customers_page = q.order_by(WebCustomer.created_at.desc())\
                      .paginate(page=page, per_page=per_page, error_out=False)
    return render_template("admin/customers.html", customers=customers_page, tab=tab)


@admin_bp.route("/api/admin/test-email", methods=["POST"])
def send_test_email():
    redir, code = require_admin()
    if redir:
        return redir, code
    from services.email import send_email
    from flask import session as flask_session
    recipient = current_app.config.get("ADMIN_EMAIL") or current_app.config.get("FROM_EMAIL")
    if not recipient:
        return jsonify(error="No ADMIN_EMAIL or FROM_EMAIL configured"), 422
    admin_name = flask_session.get("admin_user", "Admin")
    send_email(
        to=recipient,
        subject="✅ Test Email - Lady Coleen System",
        template="farmshop_order_ready",
        order=type("FakeOrder", (), {
            "reference": "TEST-EMAIL-001",
            "total": 99.00,
            "guest_name": admin_name,
            "delivery_method": "collection",
        })()
    )
    return jsonify(ok=True, message=f"Test email sent to {recipient} - check your inbox and logs")


@admin_bp.route("/admin/logs")
def logs():
    redir, code = require_admin()
    if redir:
        return redir, code

    import os
    log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "app.log")
    lines = []
    try:
        with open(log_path, "r") as f:
            all_lines = f.readlines()
            # Last 100 lines, filter to ERROR/WARNING
            for line in reversed(all_lines[-500:]):
                if "ERROR" in line or "WARNING" in line or "CRITICAL" in line:
                    lines.append(line.strip())
                    if len(lines) >= 100:
                        break
    except FileNotFoundError:
        lines = ["No log file found yet"]
    return render_template("admin/logs.html", lines=lines)

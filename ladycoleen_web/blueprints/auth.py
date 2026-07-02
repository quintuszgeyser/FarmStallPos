import logging
from flask import Blueprint, request, jsonify, session, render_template, current_app
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from werkzeug.security import generate_password_hash, check_password_hash
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from models import db, WebCustomer
from services.events import emit
from sqlalchemy import text

log = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    name  = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    pw    = data.get("password") or ""

    if not name or not email or not pw:
        return jsonify(error="name, email and password are required"), 400
    if len(pw) < 8:
        return jsonify(error="Password must be at least 8 characters"), 400

    if WebCustomer.query.filter_by(email=email).first():
        return jsonify(error="An account with that email already exists"), 409

    customer = WebCustomer(
        name=name,
        email=email,
        phone=phone or None,
        password_hash=generate_password_hash(pw)
    )
    db.session.add(customer)
    db.session.commit()

    token = create_access_token(identity=str(customer.id))
    emit("customer_registered", {"customer_id": customer.id, "email": email})

    # Auto-create POS customer record
    from services.customers import ensure_pos_customer
    pos_id = ensure_pos_customer(db, name, email, phone or None)
    if pos_id:
        from sqlalchemy import text
        try:
            db.session.execute(
                text("UPDATE web_customers SET pos_customer_id = :pid WHERE id = :cid"),
                {"pid": pos_id, "cid": customer.id}
            )
            db.session.commit()
        except Exception:
            pass

    return jsonify(token=token, customer=_customer_dict(customer)), 201


@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    pw    = data.get("password") or ""

    customer = WebCustomer.query.filter_by(email=email, deleted_at=None).first()
    if not customer or not check_password_hash(customer.password_hash, pw):
        return jsonify(error="Invalid email or password"), 401

    token = create_access_token(identity=str(customer.id))
    return jsonify(token=token, customer=_customer_dict(customer))


@auth_bp.route("/api/auth/check-email", methods=["POST"])
def check_email():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify(error="email required"), 400
    exists = WebCustomer.query.filter_by(email=email, deleted_at=None).first() is not None
    return jsonify(exists=exists)


@auth_bp.route("/api/auth/me")
@jwt_required()
def me():
    customer = WebCustomer.query.get(int(get_jwt_identity()))
    if not customer or customer.deleted_at:
        return jsonify(error="Not found"), 404
    return jsonify(customer=_customer_dict(customer))


# ── Customer account pages ───────────────────────────────────────────────────

@auth_bp.route("/login")
def login_page():
    from flask import render_template
    return render_template("auth/login.html")


@auth_bp.route("/register")
def register_page():
    from flask import render_template
    return render_template("auth/register.html")


@auth_bp.route("/account")
def account_page():
    from flask import render_template
    return render_template("auth/account.html")


@auth_bp.route("/api/account/cake-orders")
@jwt_required()
def account_cake_orders():
    from models import CakeOrder
    customer_id = int(get_jwt_identity())
    orders = CakeOrder.query.filter_by(web_customer_id=customer_id)\
                            .order_by(CakeOrder.created_at.desc()).all()
    return jsonify(orders=[{
        "id":           o.id,
        "reference":    o.reference,
        "status":       o.status,
        "date_required": o.date_required.isoformat(),
        "size":         o.size,
        "flavor":       o.flavor,
        "quoted_price": float(o.quoted_price) if o.quoted_price else None,
    } for o in orders])


# ── Admin auth (session-based, uses existing POS users table) ────────────────

@auth_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    from flask import render_template, redirect, url_for
    from werkzeug.security import check_password_hash
    from sqlalchemy import text

    if request.method == "GET":
        return render_template("admin/login.html")

    username = (request.form.get("username") or "").strip()
    password  = request.form.get("password") or ""

    row = db.session.execute(
        text("SELECT id, password_hash, role FROM users WHERE username = :u AND active = true"),
        {"u": username}
    ).fetchone()

    if not row or not check_password_hash(row.password_hash, password):
        return render_template("admin/login.html", error="Invalid credentials")

    # Require admin role
    roles = (row.role or "").split(",")
    if "admin" not in roles:
        return render_template("admin/login.html", error="Admin access required")

    session["admin_id"]   = row.id
    session["admin_user"] = username
    return redirect(url_for("admin.dashboard"))


@auth_bp.route("/admin/logout")
def admin_logout():
    from flask import redirect, url_for
    session.clear()
    return redirect(url_for("auth.admin_login"))


def require_admin():
    """Call at start of admin routes. Returns (None, None) on success or (response, status) on fail."""
    from flask import redirect, url_for
    if not session.get("admin_id"):
        return redirect(url_for("auth.admin_login")), 302
    return None, None


def _customer_dict(c: WebCustomer):
    return {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone}


# ── Password Reset ────────────────────────────────────────────────────────────

@auth_bp.route("/forgot-password")
def forgot_password_page():
    return render_template("auth/forgot_password.html")


@auth_bp.route("/api/auth/forgot-password", methods=["POST"])
def forgot_password():
    """Send password reset email. Always returns 200 to prevent email enumeration."""
    import secrets
    from datetime import datetime, timezone

    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify(ok=True), 200   # silent - don't reveal if email exists

    customer = WebCustomer.query.filter_by(email=email, deleted_at=None).first()
    if customer:
        # Invalidate any existing unused tokens for this customer
        db.session.execute(text("""
            UPDATE password_reset_tokens SET used = true
            WHERE customer_id = :cid AND used = false
        """), {"cid": customer.id})

        token = secrets.token_urlsafe(32)
        db.session.execute(text("""
            INSERT INTO password_reset_tokens (token, customer_id)
            VALUES (:token, :cid)
        """), {"token": token, "cid": customer.id})
        db.session.commit()

        site_url  = current_app.config.get("SITE_URL", "https://ladycoleen.co.za")
        reset_url = f"{site_url}/reset-password/{token}"

        from services.email import send_email
        send_email(
            to=customer.email,
            subject="Reset your Lady Coleen password",
            template="password_reset",
            customer=customer,
            reset_url=reset_url,
        )
        log.info('{"action":"password_reset_requested","customer_id":%d}', customer.id)

    return jsonify(ok=True), 200


@auth_bp.route("/reset-password/<token>")
def reset_password_page(token):
    # Validate token before showing page
    row = db.session.execute(text("""
        SELECT id, customer_id, expires_at, used
        FROM password_reset_tokens
        WHERE token = :token
    """), {"token": token}).fetchone()

    if not row or row.used:
        return render_template("auth/reset_password.html", error="This link has already been used or is invalid.", token=None)

    from datetime import datetime, timezone
    if datetime.now(timezone.utc) > row.expires_at.replace(tzinfo=timezone.utc):
        return render_template("auth/reset_password.html", error="This link has expired. Please request a new one.", token=None)

    return render_template("auth/reset_password.html", token=token, error=None)


@auth_bp.route("/api/auth/reset-password", methods=["POST"])
def reset_password():
    from datetime import datetime, timezone

    data     = request.get_json(silent=True) or {}
    token    = (data.get("token") or "").strip()
    password = data.get("password") or ""

    if not token or not password:
        return jsonify(error="Token and password are required"), 400
    if len(password) < 8:
        return jsonify(error="Password must be at least 8 characters"), 400

    row = db.session.execute(text("""
        SELECT id, customer_id, expires_at, used
        FROM password_reset_tokens
        WHERE token = :token
        FOR UPDATE
    """), {"token": token}).fetchone()

    if not row or row.used:
        return jsonify(error="This link has already been used or is invalid"), 400

    if datetime.now(timezone.utc) > row.expires_at.replace(tzinfo=timezone.utc):
        return jsonify(error="This link has expired. Please request a new one"), 400

    # Update password
    customer = db.session.get(WebCustomer, row.customer_id)
    if not customer or customer.deleted_at:
        return jsonify(error="Account not found"), 404

    customer.password_hash = generate_password_hash(password)

    # Mark token used
    db.session.execute(text("""
        UPDATE password_reset_tokens SET used = true WHERE id = :id
    """), {"id": row.id})

    db.session.commit()
    log.info('{"action":"password_reset_completed","customer_id":%d}', customer.id)

    # Auto-login
    token_jwt = create_access_token(identity=str(customer.id))
    return jsonify(ok=True, token=token_jwt), 200

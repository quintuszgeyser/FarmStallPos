import logging
from sqlalchemy import text

log = logging.getLogger(__name__)


def ensure_pos_customer(db, name: str, email: str | None, phone: str | None,
                        web_customer_id: int | None = None) -> int | None:
    """
    Find or create a POS customers record. Returns the POS customers.id or None if skipped.

    Dedup order:
    1. web_customers.pos_customer_id already set — return it directly (handles post-merge case).
    2. Email match (case-insensitive, trimmed) on active customers — follow merged_into chain.
    3. Phone match on active customers — only if no email.
    4. Create new record if nothing matched.
    5. Skip entirely if neither email nor phone.
    """
    email = email.strip().lower() if email else None
    phone = phone.strip() if phone else None
    name  = (name or "").strip() or None

    if not email and not phone:
        return None

    try:
        # 1. Already linked — return current pos_customer_id (may have been updated after a merge)
        if web_customer_id:
            linked = db.session.execute(
                text("SELECT pos_customer_id FROM web_customers WHERE id = :id"),
                {"id": web_customer_id}
            ).fetchone()
            if linked and linked.pos_customer_id:
                pos_id = _follow_merge(db, linked.pos_customer_id)
                log.info('{"action":"pos_customer_linked","id":%d,"web_id":%d}', pos_id, web_customer_id)
                return pos_id

        # 2. Dedup by email
        if email:
            existing = db.session.execute(
                text("""
                    SELECT id FROM customers
                    WHERE LOWER(TRIM(email)) = LOWER(TRIM(:email))
                      AND active = true
                    LIMIT 1
                """),
                {"email": email}
            ).fetchone()
            if existing:
                pos_id = _follow_merge(db, existing.id)
                log.info('{"action":"pos_customer_found_email","id":%d,"email":"%s"}', pos_id, email)
                return pos_id

        # 3. Dedup by phone (only when no email)
        if phone and not email:
            existing = db.session.execute(
                text("""
                    SELECT id FROM customers
                    WHERE TRIM(phone) = TRIM(:phone)
                      AND active = true
                    LIMIT 1
                """),
                {"phone": phone}
            ).fetchone()
            if existing:
                pos_id = _follow_merge(db, existing.id)
                log.info('{"action":"pos_customer_found_phone","id":%d,"phone":"%s"}', pos_id, phone)
                return pos_id

        # 4. Create
        row = db.session.execute(
            text("""
                INSERT INTO customers (name, phone, email, notes, enrolled_at, auto_enrolled, visit_count, active, is_employee, is_online_customer, is_pos_customer)
                VALUES (:name, :phone, :email, :notes, now(), false, 0, true, false, true, false)
                RETURNING id
            """),
            {
                "name":  name,
                "phone": phone,
                "email": email,
                "notes": "[Web order] Created from online order/registration",
            }
        ).fetchone()
        db.session.commit()
        log.info('{"action":"pos_customer_created","id":%d,"email":"%s"}', row.id, email or "")
        return row.id

    except Exception as e:
        db.session.rollback()
        log.error('{"action":"pos_customer_error","email":"%s","error":"%s"}', email or "", e)
        return None


def _follow_merge(db, customer_id: int) -> int:
    """Follow merged_into chain to find the surviving customer. Max 5 hops."""
    current = customer_id
    for _ in range(5):
        row = db.session.execute(
            text("SELECT merged_into FROM customers WHERE id = :id"),
            {"id": current}
        ).fetchone()
        if not row or not row.merged_into:
            break
        current = row.merged_into
    return current

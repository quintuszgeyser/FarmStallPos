import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import current_app, render_template

log = logging.getLogger(__name__)


def send_order_confirmation_customer(order, lines, pay_ref, delivery_note):
    """Post-payment order confirmation to customer. Failure logs but never raises."""
    if not order:
        return
    recipient = order.guest_email
    if not recipient and order.web_customer_id:
        from sqlalchemy import text
        from models import db
        row = db.session.execute(
            text("SELECT email FROM web_customers WHERE id = :id"),
            {"id": order.web_customer_id}
        ).fetchone()
        if row:
            recipient = row.email
    if not recipient:
        return
    send_email(
        to=recipient,
        subject=f"Your Order Confirmation — {order.reference}",
        template="farmshop_order_confirmation",
        order=order,
        lines=lines,
        pay_ref=pay_ref,
        delivery_note=delivery_note,
    )


def send_order_notification_admin(order, lines, pay_ref, delivery_note):
    """New paid order notification to admin. Failure logs but never raises."""
    # Fall back to FROM_EMAIL if ADMIN_EMAIL not set — ensures admin always gets notified
    admin_email = current_app.config.get("ADMIN_EMAIL") or current_app.config.get("FROM_EMAIL")
    if not admin_email or not order:
        return
    send_email(
        to=admin_email,
        subject=f"New Paid Order — {order.reference}",
        template="farmshop_order_admin_paid",
        order=order,
        lines=lines,
        pay_ref=pay_ref,
        delivery_note=delivery_note,
    )

def send_email(to: str, subject: str, template: str, **ctx):
    """
    Send an HTML email. Template is loaded from templates/email/<template>.html.
    Failure is logged but never raises — does NOT block order flow.
    """
    if not to:
        log.warning("send_email called with no recipient for template=%s", template)
        return

    cfg = current_app.config
    if not cfg.get("SMTP_HOST") or not cfg.get("SMTP_USER"):
        log.warning("SMTP not configured — skipping email to %s (subject: %s)", to, subject)
        return

    try:
        html_body = render_template(f"email/{template}.html", **ctx)
    except Exception as e:
        log.error("Email template render failed template=%s: %s", template, e)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["FROM_EMAIL"]
    msg["To"]      = to
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"]) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            smtp.sendmail(cfg["FROM_EMAIL"], [to], msg.as_string())
        log.info('{"action":"email_sent","to":"%s","template":"%s"}', to, template)
    except Exception as e:
        log.error('{"action":"email_failed","to":"%s","template":"%s","error":"%s"}', to, template, e)

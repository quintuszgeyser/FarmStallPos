"""
PayFast payment integration.
Docs: https://developers.payfast.co.za/docs
"""
import hashlib
import logging
import urllib.parse
from flask import current_app

log = logging.getLogger(__name__)

PAYFAST_LIVE_URL    = "https://www.payfast.co.za/eng/process"
PAYFAST_SANDBOX_URL = "https://sandbox.payfast.co.za/eng/process"


def _signature(data: dict, passphrase: str, skip_empty: bool = True) -> str:
    """
    Generate MD5 signature for PayFast request.
    IMPORTANT: Fields must remain in insertion order — PayFast docs explicitly say
    DO NOT sort alphabetically. The pairs must appear in the order defined in the spec.
    skip_empty=True for outgoing form (skip blank fields).
    skip_empty=False for ITN verification (include all fields PayFast sent).
    """
    items = [(k, str(v)) for k, v in data.items() if k != "signature"]
    if skip_empty:
        items = [(k, v) for k, v in items if v != ""]
    query = "&".join(f"{k}={urllib.parse.quote_plus(v)}" for k, v in items)
    if passphrase and passphrase.strip():
        query += f"&passphrase={urllib.parse.quote_plus(passphrase.strip())}"
    return hashlib.md5(query.encode("utf-8")).hexdigest()


def build_payfast_form(session_id: str, amount: float,
                       item_name: str, customer_name: str,
                       customer_email: str) -> dict:
    """
    Build the PayFast form fields to POST to their payment page.
    Field order matches the PayFast spec exactly — merchant → customer → transaction.
    Returns dict of fields to include in a hidden-field HTML form.
    """
    cfg      = current_app.config
    sandbox  = cfg.get("PAYFAST_SANDBOX", True)
    site_url = cfg.get("SITE_URL", "https://ladycoleen.co.za").rstrip("/")

    name_parts = (customer_name or "").strip().split()
    name_first = name_parts[0][:100] if name_parts else ""
    name_last  = " ".join(name_parts[1:])[:100] if len(name_parts) > 1 else ""

    # Field order must match PayFast spec: merchant → customer → transaction
    data = {}
    # Merchant details
    data["merchant_id"]  = cfg["PAYFAST_MERCHANT_ID"]
    data["merchant_key"] = cfg["PAYFAST_MERCHANT_KEY"]
    data["return_url"]   = f"{site_url}/farmshop/payment/success?session={session_id}"
    data["cancel_url"]   = f"{site_url}/farmshop/payment/cancel?session={session_id}"
    data["notify_url"]   = f"{site_url}/api/farmshop/payfast/notify"
    # Customer details
    if name_first:
        data["name_first"] = name_first
    if name_last:
        data["name_last"] = name_last
    if customer_email:
        data["email_address"] = customer_email[:254]
    # Transaction details
    data["m_payment_id"] = session_id
    data["amount"]       = f"{amount:.2f}"
    data["item_name"]    = (item_name or "Lady Coleen Order")[:100]

    passphrase = (cfg.get("PAYFAST_PASSPHRASE") or "").strip()
    data["signature"] = _signature(data, passphrase)

    action = PAYFAST_SANDBOX_URL if sandbox else PAYFAST_LIVE_URL
    log.info("PayFast form built: sandbox=%s amount=%s merchant_id=%s signature=%s",
             sandbox, data["amount"], data["merchant_id"], data["signature"])

    return {
        "action": action,
        "fields": data,
    }


def verify_itn(form_data: dict) -> bool:
    """
    Verify a PayFast ITN (Instant Transaction Notification).
    Returns True if the notification is authentic.
    """
    import requests as _req

    cfg     = current_app.config
    sandbox = cfg.get("PAYFAST_SANDBOX", True)
    pfhost  = "sandbox.payfast.co.za" if sandbox else "www.payfast.co.za"

    # 1. Verify signature — preserve received field order, exclude 'signature' field
    received_sig = form_data.get("signature", "")
    data_no_sig  = {k: v for k, v in form_data.items() if k != "signature"}
    passphrase   = cfg.get("PAYFAST_PASSPHRASE", "")
    expected_sig = _signature(data_no_sig, passphrase, skip_empty=False)
    log.info("PayFast ITN: fields=%s passphrase_set=%s received=%s expected=%s",
             list(data_no_sig.keys()), bool(passphrase), received_sig, expected_sig)
    if received_sig != expected_sig:
        log.warning("PayFast ITN: signature mismatch received=%s expected=%s",
                    received_sig, expected_sig)
        return False

    # 2. Server-side validation — POST param string back to PayFast in received order
    try:
        # Build param string preserving received order (excluding signature)
        param_string = "&".join(
            f"{k}={urllib.parse.quote_plus(str(v))}"
            for k, v in form_data.items()
            if k != "signature"
        )
        r = _req.post(
            f"https://{pfhost}/eng/query/validate",
            data=param_string,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if r.text.strip() != "VALID":
            log.warning("PayFast ITN: server validation returned: %s", r.text.strip())
            return False
    except Exception as e:
        log.error("PayFast ITN: server validation failed: %s", e)
        return False

    # 3. Check payment status
    if form_data.get("payment_status") != "COMPLETE":
        log.info("PayFast ITN: payment_status=%s — not COMPLETE, ignoring",
                 form_data.get("payment_status"))
        return False

    return True

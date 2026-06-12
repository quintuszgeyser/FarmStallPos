from flask import Blueprint, send_file, abort, current_app, render_template
from sqlalchemy import text
from models import db
from blueprints.auth import require_admin
import io

invoices_bp = Blueprint("invoices", __name__)


@invoices_bp.route("/admin/invoices/<int:invoice_id>/pdf")
def download_invoice_pdf(invoice_id):
    redir, code = require_admin()
    if redir:
        return redir, code

    invoice = db.session.execute(
        text("SELECT * FROM invoices WHERE id = :id"),
        {"id": invoice_id}
    ).fetchone()
    if not invoice:
        abort(404)

    html = render_template("invoice_pdf.html", invoice=invoice,
                           lines=_parse_lines(invoice.lines_json))
    pdf_bytes = _render_pdf(html)

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"invoice-{invoice.invoice_number}.pdf"
    )


@invoices_bp.route("/admin/cakes/<int:order_id>/invoice/pdf")
def cake_order_invoice_pdf(order_id):
    redir, code = require_admin()
    if redir:
        return redir, code

    from models import CakeOrder
    order = CakeOrder.query.get_or_404(order_id)
    if not order.invoice_id:
        abort(404)

    invoice = db.session.execute(
        text("SELECT * FROM invoices WHERE id = :id"),
        {"id": order.invoice_id}
    ).fetchone()
    if not invoice:
        abort(404)

    html = render_template("invoice_pdf.html", invoice=invoice,
                           lines=_parse_lines(invoice.lines_json))
    pdf_bytes = _render_pdf(html)

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"invoice-{order.reference}.pdf"
    )


def _render_pdf(html: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html, base_url=current_app.static_folder).write_pdf()


def _parse_lines(lines_json):
    import json
    try:
        return json.loads(lines_json) if lines_json else []
    except Exception:
        return []

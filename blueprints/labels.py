"""
Label Printing Subsystem — Farm POS
====================================
Routes:
  GET  /api/label-templates                     list all templates
  POST /api/label-templates                     create template (admin/developer)
  GET  /api/label-templates/<id>                get one template
  PUT  /api/label-templates/<id>                update template (admin/developer)
  DELETE /api/label-templates/<id>              delete template (admin/developer)
  POST /api/label-templates/<id>/duplicate      duplicate a template

  POST /api/labels/preview                      render PNG preview of a label
  POST /api/labels/print                        queue / execute a print job
  POST /api/labels/print-bulk                   bulk print job (multiple products)

  GET  /api/label-print-jobs                    audit log (admin only)
  GET  /api/label-printers                      list configured printers
  POST /api/label-printers                      add/update printer config (admin)
  DELETE /api/label-printers/<id>               remove printer config
"""

import io
import json
import logging
import hashlib
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request, send_file

from helpers import require_login, require_role, current_user, get_setting
from models import db, Product, LabelTemplate, LabelPrintJob, LabelPrinter
from services.label_service import LabelRenderService, PrintDispatchService

log = logging.getLogger('pos')
bp  = Blueprint('labels', __name__)

# Roles that may design / manage templates
_DESIGNER_ROLES = ('admin', 'developer')


def _can_design():
    from helpers import current_user as _cu
    u = _cu()
    return u and u.has_role(*_DESIGNER_ROLES)


# ── Templates ─────────────────────────────────────────────────────────────────

@bp.route('/api/label-templates', methods=['GET'])
def api_label_templates_list():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    rows = LabelTemplate.query.filter_by(is_archived=False).order_by(LabelTemplate.name).all()
    return jsonify([_tmpl_dict(t) for t in rows])


@bp.route('/api/label-templates', methods=['POST'])
def api_label_templates_create():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    if not _can_design():
        return jsonify({'error': 'Forbidden — admin/developer only'}), 403
    data = request.json or {}
    err = _validate_template(data)
    if err:
        return jsonify({'error': err}), 400
    u = current_user()
    t = LabelTemplate(
        name            = data['name'].strip(),
        description     = (data.get('description') or '').strip(),
        width_mm        = float(data['width_mm']),
        height_mm       = float(data['height_mm']),
        category        = data.get('category', 'general'),
        elements_json   = json.dumps(data.get('elements', [])),
        background_color= data.get('background_color', '#ffffff'),
        border          = bool(data.get('border', False)),
        created_by      = u.id if u else None,
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(_tmpl_dict(t)), 201


@bp.route('/api/label-templates/<int:tmpl_id>', methods=['GET'])
def api_label_templates_get(tmpl_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    t = db.session.get(LabelTemplate, tmpl_id)
    if not t or t.is_archived:
        return jsonify({'error': 'Template not found'}), 404
    return jsonify(_tmpl_dict(t))


@bp.route('/api/label-templates/<int:tmpl_id>', methods=['PUT'])
def api_label_templates_update(tmpl_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    if not _can_design():
        return jsonify({'error': 'Forbidden'}), 403
    t = db.session.get(LabelTemplate, tmpl_id)
    if not t or t.is_archived:
        return jsonify({'error': 'Template not found'}), 404
    data = request.json or {}
    err = _validate_template(data)
    if err:
        return jsonify({'error': err}), 400
    t.name             = data['name'].strip()
    t.description      = (data.get('description') or '').strip()
    t.width_mm         = float(data['width_mm'])
    t.height_mm        = float(data['height_mm'])
    t.category         = data.get('category', t.category)
    t.elements_json    = json.dumps(data.get('elements', json.loads(t.elements_json)))
    t.background_color = data.get('background_color', t.background_color)
    t.border           = bool(data.get('border', t.border))
    t.updated_at       = datetime.utcnow()
    db.session.commit()
    return jsonify(_tmpl_dict(t))


@bp.route('/api/label-templates/<int:tmpl_id>', methods=['DELETE'])
def api_label_templates_delete(tmpl_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    if not _can_design():
        return jsonify({'error': 'Forbidden'}), 403
    t = db.session.get(LabelTemplate, tmpl_id)
    if not t:
        return jsonify({'error': 'Template not found'}), 404
    t.is_archived = True
    t.updated_at  = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/label-templates/<int:tmpl_id>/duplicate', methods=['POST'])
def api_label_templates_duplicate(tmpl_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    if not _can_design():
        return jsonify({'error': 'Forbidden'}), 403
    src = db.session.get(LabelTemplate, tmpl_id)
    if not src or src.is_archived:
        return jsonify({'error': 'Template not found'}), 404
    u = current_user()
    copy = LabelTemplate(
        name             = f'Copy of {src.name}',
        description      = src.description,
        width_mm         = src.width_mm,
        height_mm        = src.height_mm,
        category         = src.category,
        elements_json    = src.elements_json,
        background_color = src.background_color,
        border           = src.border,
        created_by       = u.id if u else None,
    )
    db.session.add(copy)
    db.session.commit()
    return jsonify(_tmpl_dict(copy)), 201


# ── Preview ───────────────────────────────────────────────────────────────────

@bp.route('/api/labels/preview', methods=['POST'])
def api_labels_preview():
    """Return a PNG image of the rendered label. Used by the designer live-preview."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data       = request.json or {}
    product_id = data.get('product_id')
    template   = data.get('template')   # inline template dict (designer) OR id
    tmpl_id    = data.get('template_id')

    if tmpl_id:
        tmpl_row = db.session.get(LabelTemplate, int(tmpl_id))
        if not tmpl_row:
            return jsonify({'error': 'Template not found'}), 404
        template = _tmpl_dict(tmpl_row)

    if not template:
        return jsonify({'error': 'template or template_id required'}), 400

    product = db.session.get(Product, int(product_id)) if product_id else None

    branding = _get_branding()
    svc = LabelRenderService(branding)
    try:
        png_bytes = svc.render_png(template, product)
    except Exception as e:
        log.exception('Label preview failed')
        return jsonify({'error': str(e)}), 500

    return send_file(
        io.BytesIO(png_bytes),
        mimetype='image/png',
        as_attachment=False,
    )


# ── Print ─────────────────────────────────────────────────────────────────────

@bp.route('/api/labels/print', methods=['POST'])
def api_labels_print():
    """Print one product's label N times."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}

    product_id  = data.get('product_id')
    tmpl_id     = data.get('template_id')
    qty         = int(data.get('qty', 1))
    printer_id  = data.get('printer_id')

    if not product_id or not tmpl_id:
        return jsonify({'error': 'product_id and template_id required'}), 400
    if qty < 1 or qty > 500:
        return jsonify({'error': 'qty must be 1–500'}), 400

    product = db.session.get(Product, int(product_id))
    if not product:
        return jsonify({'error': 'Product not found'}), 404
    tmpl = db.session.get(LabelTemplate, int(tmpl_id))
    if not tmpl or tmpl.is_archived:
        return jsonify({'error': 'Template not found'}), 404

    u        = current_user()
    branding = _get_branding()
    svc      = LabelRenderService(branding)
    dispatch = PrintDispatchService()

    tmpl_dict = _tmpl_dict(tmpl)
    try:
        for _ in range(qty):
            img    = svc.render_image(tmpl_dict, product)
            result = dispatch.send(img, printer_id=printer_id,
                                   width_mm=float(tmpl.width_mm),
                                   height_mm=float(tmpl.height_mm))
    except Exception as e:
        log.exception('Label print failed')
        return jsonify({'error': str(e)}), 500

    # Audit
    job = LabelPrintJob(
        template_id = tmpl.id,
        product_id  = product.id,
        qty         = qty,
        printer_id  = printer_id,
        status      = result.get('status', 'sent'),
        user_id     = u.id if u else None,
        notes       = result.get('notes'),
    )
    db.session.add(job)
    db.session.commit()

    return jsonify({'ok': True, 'job_id': job.id, 'status': job.status})


@bp.route('/api/labels/print-bulk', methods=['POST'])
def api_labels_print_bulk():
    """Print labels for multiple products in one job."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}

    items      = data.get('items', [])   # [{product_id, qty, template_id}]
    printer_id = data.get('printer_id')
    tmpl_id    = data.get('template_id')  # fallback if item has no template_id

    if not items:
        return jsonify({'error': 'items required'}), 400
    if len(items) > 200:
        return jsonify({'error': 'max 200 items per bulk job'}), 400

    u        = current_user()
    branding = _get_branding()
    svc      = LabelRenderService(branding)
    dispatch = PrintDispatchService()

    audit_rows = []
    images     = []   # list of (img, tmpl, product, qty) — one entry per unique product
    for item in items:
        pid  = int(item.get('product_id', 0))
        qty  = max(1, min(500, int(item.get('qty', 1))))
        tid  = int(item.get('template_id') or tmpl_id or 0)
        if not pid or not tid:
            continue
        product = db.session.get(Product, pid)
        tmpl    = db.session.get(LabelTemplate, tid)
        if not product or not tmpl or tmpl.is_archived:
            continue
        try:
            img = svc.render_image(_tmpl_dict(tmpl), product)
            images.append((img, tmpl, product, qty))
            audit_rows.append({'product': product, 'tmpl': tmpl, 'qty': qty})
        except Exception as e:
            log.warning('Bulk label render failed for product %d: %s', pid, e)

    if not images:
        return jsonify({'error': 'No labels could be rendered'}), 400

    total_pages = 0
    last_result = {}
    try:
        for img, tmpl, product, qty in images:
            for _ in range(qty):
                last_result = dispatch.send(img, printer_id=printer_id,
                                             width_mm=float(tmpl.width_mm),
                                             height_mm=float(tmpl.height_mm))
                total_pages += 1
    except Exception as e:
        log.exception('Bulk label print failed')
        return jsonify({'error': str(e)}), 500
    result = last_result

    job_ids = []
    for row in audit_rows:
        job = LabelPrintJob(
            template_id = row['tmpl'].id,
            product_id  = row['product'].id,
            qty         = row['qty'],
            printer_id  = printer_id,
            status      = result.get('status', 'sent'),
            user_id     = u.id if u else None,
            notes       = 'bulk',
        )
        db.session.add(job)
        db.session.flush()
        job_ids.append(job.id)
    db.session.commit()

    return jsonify({'ok': True, 'job_ids': job_ids, 'pages': total_pages})


# ── Audit log ─────────────────────────────────────────────────────────────────

@bp.route('/api/label-print-jobs', methods=['GET'])
def api_label_print_jobs():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    limit = min(int(request.args.get('limit', 100)), 1000)
    rows  = LabelPrintJob.query.order_by(LabelPrintJob.printed_at.desc()).limit(limit).all()
    return jsonify([_job_dict(j) for j in rows])


# ── Printers ──────────────────────────────────────────────────────────────────

@bp.route('/api/label-printers', methods=['GET'])
def api_label_printers_list():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    rows = LabelPrinter.query.filter_by(is_active=True).all()
    return jsonify([_printer_dict(p) for p in rows])


@bp.route('/api/label-printers', methods=['POST'])
def api_label_printers_create():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    name  = (data.get('name') or '').strip()
    model = (data.get('model') or 'xprinter_xp365b').strip()
    connection = data.get('connection', 'usb')   # usb | bluetooth | network
    address    = (data.get('address') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    existing = LabelPrinter.query.filter_by(name=name, is_active=True).first()
    if existing:
        # Update in place
        existing.model      = model
        existing.connection = connection
        existing.address    = address
        db.session.commit()
        return jsonify(_printer_dict(existing))
    p = LabelPrinter(name=name, model=model, connection=connection, address=address)
    db.session.add(p)
    db.session.commit()
    return jsonify(_printer_dict(p)), 201


@bp.route('/api/label-printers/<int:printer_id>', methods=['DELETE'])
def api_label_printers_delete(printer_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = db.session.get(LabelPrinter, printer_id)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    p.is_active = False
    db.session.commit()
    return jsonify({'ok': True})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_template(data):
    if not data.get('name', '').strip():
        return 'name required'
    try:
        w = float(data.get('width_mm', 0))
        h = float(data.get('height_mm', 0))
    except (TypeError, ValueError):
        return 'width_mm and height_mm must be numbers'
    if not (10 <= w <= 120):
        return 'width_mm must be 10–120'
    if not (10 <= h <= 300):
        return 'height_mm must be 10–300'
    elements = data.get('elements', [])
    if not isinstance(elements, list):
        return 'elements must be a list'
    for el in elements:
        if 'type' not in el:
            return 'each element must have a type'
    return None


def _tmpl_dict(t):
    return {
        'id':               t.id,
        'name':             t.name,
        'description':      t.description,
        'width_mm':         float(t.width_mm),
        'height_mm':        float(t.height_mm),
        'category':         t.category,
        'elements':         json.loads(t.elements_json or '[]'),
        'background_color': t.background_color,
        'border':           t.border,
        'created_at':       t.created_at.isoformat(),
        'updated_at':       t.updated_at.isoformat() if t.updated_at else None,
    }


def _job_dict(j):
    product = db.session.get(Product, j.product_id) if j.product_id else None
    tmpl    = db.session.get(LabelTemplate, j.template_id) if j.template_id else None
    return {
        'id':           j.id,
        'product_id':   j.product_id,
        'product_name': product.name if product else None,
        'template_id':  j.template_id,
        'template_name':tmpl.name if tmpl else None,
        'qty':          j.qty,
        'printer_id':   j.printer_id,
        'status':       j.status,
        'user_id':      j.user_id,
        'printed_at':   j.printed_at.isoformat(),
        'notes':        j.notes,
    }


def _printer_dict(p):
    return {
        'id':         p.id,
        'name':       p.name,
        'model':      p.model,
        'connection': p.connection,
        'address':    p.address,
        'is_active':  p.is_active,
    }


def _get_branding():
    return {
        'store_name':  get_setting('branding_store_name', ''),
        'logo_file':   get_setting('branding_logo_file', ''),
        'primary':     get_setting('branding_primary', '#2d6a4f'),
        'font':        get_setting('branding_font', 'Arial'),
    }

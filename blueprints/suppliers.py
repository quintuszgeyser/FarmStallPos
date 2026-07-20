import json as _json
import os
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request, current_app, send_from_directory, abort
from sqlalchemy import func

from helpers import require_login, require_role, current_user, _gen_barcode
from models import db, Supplier, StockBatch, Purchase, Product, SupplierDocument, PurchaseRun

bp = Blueprint('suppliers', __name__)


def _parse_addl_costs(raw, source='manual_edit', source_id=None):
    """Validate and normalize additional_costs list from request.
    Returns list of dicts with Decimal-safe float amounts."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError('additional_costs must be a list')
    result = []
    for i, entry in enumerate(raw):
        label      = str(entry.get('label') or '').strip()
        ctype      = str(entry.get('type') or 'other').strip() or 'other'
        amount_raw = entry.get('amount')
        if not label:
            raise ValueError(f'additional_costs[{i}].label is required')
        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, TypeError):
            raise ValueError(f'additional_costs[{i}].amount is invalid')
        result.append({
            'label':       label,
            'type':        ctype,
            'amount':      float(amount.quantize(Decimal('0.01'))),
            'source':      source,
            'source_id':   source_id,
            'invoice_ref': str(entry.get('invoice_ref') or '').strip() or None,
        })
    return result


def _split_costs(line_totals, total_addl):
    """Proportional split of total_addl across lines by their base cost.
    Returns list of Decimal shares in the same order as line_totals.
    Last item absorbs rounding remainder so sum(shares) == total_addl exactly."""
    if not line_totals or total_addl == Decimal('0'):
        return [Decimal('0')] * len(line_totals)
    grand = sum(line_totals)
    if grand == Decimal('0'):
        # Equal split when all line totals are zero
        per = (total_addl / len(line_totals)).quantize(Decimal('0.01'))
        shares = [per] * len(line_totals)
        shares[-1] += total_addl - sum(shares)
        return shares
    shares = []
    running = Decimal('0')
    for i, lt in enumerate(line_totals):
        if i == len(line_totals) - 1:
            shares.append(total_addl - running)
        else:
            s = (lt / grand * total_addl).quantize(Decimal('0.01'))
            shares.append(s)
            running += s
    return shares


_UNIT_CONVERSIONS = {
    'g': 1, 'kg': 1000,
    'ml': 1, 'L': 1000,
    'unit': 1, 'dozen': 12,
}


@bp.route('/api/suppliers', methods=['GET'])
def api_suppliers_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    suppliers = Supplier.query.order_by(Supplier.name.asc()).all()
    return jsonify([{
        'id': s.id, 'name': s.name, 'phone': s.phone,
        'email': s.email, 'website': s.website, 'notes': s.notes,
        'last_run_costs': s.last_run_costs,
    } for s in suppliers])


@bp.route('/api/suppliers', methods=['POST'])
def api_suppliers_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data    = request.json or {}
    name    = data.get('name', '').strip()
    phone   = data.get('phone',   '').strip() or None
    email   = data.get('email',   '').strip() or None
    website = data.get('website', '').strip() or None
    notes   = data.get('notes',   '').strip() or None
    if not name:
        return jsonify({'error': 'name required'}), 400
    if Supplier.query.filter_by(name=name).first():
        return jsonify({'error': 'Supplier already exists'}), 409
    s = Supplier(name=name, phone=phone, email=email, website=website, notes=notes)
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id})


@bp.route('/api/suppliers/<int:sid>', methods=['POST'])
def api_suppliers_update(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'name' in data:
        name  = data['name'].strip()
        clash = Supplier.query.filter(Supplier.id != sid, Supplier.name == name).first()
        if clash:
            return jsonify({'error': 'Supplier name already exists'}), 409
        s.name = name
    if 'phone'   in data: s.phone   = data['phone'].strip()   or None
    if 'email'   in data: s.email   = data['email'].strip()   or None
    if 'website' in data: s.website = data['website'].strip() or None
    if 'notes'   in data: s.notes   = data['notes'].strip()   or None
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/suppliers/<int:sid>', methods=['DELETE'])
def api_suppliers_delete(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    StockBatch.query.filter_by(supplier_id=sid).update({'supplier_id': None})
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/suppliers/<int:sid>/products', methods=['GET'])
def api_suppliers_products(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    batches = (db.session.query(
                   StockBatch.product_id,
                   func.max(StockBatch.purchased_at).label('last_received'),
               )
               .filter_by(supplier_id=sid)
               .group_by(StockBatch.product_id)
               .all())
    result = []
    for prod_id, last_received in batches:
        p = db.session.get(Product, prod_id)
        if p:
            result.append({
                'id': p.id,
                'name': p.name,
                'product_type': p.product_type,
                'last_received': last_received.date().isoformat() if last_received else None,
            })
    result.sort(key=lambda x: x['name'])
    return jsonify(result)


@bp.route('/api/suppliers/<int:sid>/purchase_run', methods=['POST'])
def api_suppliers_purchase_run(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404

    data            = request.json or {}
    lines           = data.get('lines', [])
    date_str        = data.get('date')
    addl_costs_raw  = data.get('additional_costs', [])
    invoice_ref     = str(data.get('invoice_ref') or '').strip() or None
    invoice_addl_total = data.get('invoice_additional_total')

    if not lines:
        return jsonify({'error': 'No lines provided'}), 400

    from datetime import date as _date
    purchase_date = datetime.now()
    run_date      = _date.today()
    if date_str:
        try:
            parts = date_str.split('-')
            run_date      = _date(int(parts[0]), int(parts[1]), int(parts[2]))
            purchase_date = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            return jsonify({'error': 'Invalid date format'}), 400

    # Validate and normalize additional costs
    try:
        addl_costs = _parse_addl_costs(addl_costs_raw, source='supplier_run')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    u = current_user()
    created_products = []
    batches_created  = 0

    # Create PurchaseRun record
    run = PurchaseRun(
        supplier_id=sid,
        date=run_date,
        invoice_ref=invoice_ref,
        invoice_additional_total=float(invoice_addl_total) if invoice_addl_total is not None else None,
        created_at=datetime.utcnow(),
        created_by=u.id if u else None,
    )
    db.session.add(run)
    db.session.flush()
    run_id = run.id

    # First pass: build line items with base costs for proportional split
    prepared_lines = []
    for line in lines:
        pid      = line.get('product_id')
        new_prod = line.get('new_product')
        qty      = line.get('qty')
        unit     = line.get('unit', 'unit')
        total_price = line.get('total_price')

        try:
            qty         = float(qty)
            total_price = float(total_price)
        except Exception:
            return jsonify({'error': 'Invalid qty or total_price'}), 400

        if new_prod:
            name = new_prod.get('name', '').strip()
            if not name:
                return jsonify({'error': 'new_product.name required'}), 400
            if Product.query.filter_by(name=name).first():
                return jsonify({'error': f'Product name "{name}" already exists'}), 409
            next_id      = (db.session.query(func.max(Product.id)).scalar() or 0) + 1
            barcode      = _gen_barcode(next_id)
            price        = new_prod.get('price')
            product_type = 'stock_item'
            base_unit    = new_prod.get('base_unit') or None
            unit_type    = new_prod.get('unit_type') or None
            try:
                price = float(price) if price is not None else None
            except Exception:
                return jsonify({'error': 'Invalid price'}), 400
            p = Product(
                name=name, barcode=barcode, stock_qty=0,
                price=price, product_type=product_type,
                unit_type=unit_type, base_unit=base_unit,
            )
            db.session.add(p)
            db.session.flush()
            pid = p.id
            created_products.append({'id': p.id, 'name': p.name})
        else:
            try:
                pid = int(pid)
            except Exception:
                return jsonify({'error': 'product_id required'}), 400

        p = db.session.get(Product, pid)
        if not p:
            return jsonify({'error': f'Product id {pid} not found'}), 404

        if p.product_type == 'stock_item':
            conversion = _UNIT_CONVERSIONS.get(unit, 1)
            qty_base   = qty * conversion
            if qty_base == 0:
                return jsonify({'error': f'qty converts to 0 base units for product {pid}'}), 400
            prepared_lines.append({
                'pid': pid, 'qty_base': qty_base,
                'base_cost_total': Decimal(str(total_price)),
            })

    # Proportional split of additional costs across lines (by base_cost_total)
    total_addl = sum(Decimal(str(c['amount'])) for c in addl_costs)
    shares = _split_costs([l['base_cost_total'] for l in prepared_lines], total_addl)

    for i, pl in enumerate(prepared_lines):
        share = shares[i]
        # Build per-batch additional_costs with the allocated share
        batch_addl = []
        if share != Decimal('0') and addl_costs:
            if len(addl_costs) == 1:
                batch_addl = [{**addl_costs[0], 'amount': float(share.quantize(Decimal('0.01')))}]
            else:
                # Scale each entry proportionally to their fraction of total_addl
                if total_addl != Decimal('0'):
                    batch_addl = []
                    running = Decimal('0')
                    for j, ac in enumerate(addl_costs):
                        if j == len(addl_costs) - 1:
                            entry_share = share - running
                        else:
                            entry_share = (Decimal(str(ac['amount'])) / total_addl * share).quantize(Decimal('0.01'))
                            running += entry_share
                        batch_addl.append({**ac, 'amount': float(entry_share)})

        cost_per_base = (pl['base_cost_total'] + share) / Decimal(str(pl['qty_base']))
        db.session.add(StockBatch(
            product_id=pl['pid'],
            qty_purchased_base=pl['qty_base'],
            qty_remaining_base=pl['qty_base'],
            cost_per_base_unit=cost_per_base,
            base_cost_total=pl['base_cost_total'],
            additional_costs=_json.dumps(batch_addl) if batch_addl else None,
            supplier_id=sid,
            user_id=u.id if u else None,
            purchased_at=purchase_date,
            purchase_run_id=run_id,
        ))
        batches_created += 1

    # Update supplier's last_run_costs for pre-population next time
    if addl_costs and batches_created > 0:
        run_level = [{'label': c['label'], 'type': c['type'], 'amount': float(Decimal(str(c['amount'])).quantize(Decimal('0.01')))} for c in addl_costs]
        s.last_run_costs = _json.dumps(run_level)

    db.session.commit()
    return jsonify({
        'ok': True,
        'created_products': created_products,
        'batches_created':  batches_created,
        'run_id':           run_id,
        'invoice_ref':      invoice_ref,
    })


_ALLOWED_DOC_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.doc', '.docx', '.xls', '.xlsx', '.csv', '.txt'}
_MAX_DOC_SIZE = 20 * 1024 * 1024  # 20 MB


def _doc_dir():
    d = os.path.join(current_app.static_folder, 'supplier_docs')
    os.makedirs(d, exist_ok=True)
    return d


@bp.route('/api/suppliers/<int:sid>/documents', methods=['GET'])
def api_supplier_docs_list(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    docs = SupplierDocument.query.filter_by(supplier_id=sid).order_by(SupplierDocument.uploaded_at.desc()).all()
    return jsonify([{
        'id': d.id,
        'original_name': d.original_name,
        'filename': d.filename,
        'uploaded_at': d.uploaded_at.date().isoformat() if d.uploaded_at else None,
    } for d in docs])


@bp.route('/api/suppliers/<int:sid>/documents', methods=['POST'])
def api_supplier_docs_upload(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file provided'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in _ALLOWED_DOC_EXTENSIONS:
        return jsonify({'error': f'File type {ext} not allowed'}), 400
    content = f.read()
    if len(content) > _MAX_DOC_SIZE:
        return jsonify({'error': 'File too large (max 20 MB)'}), 400
    stored_name = f'{uuid.uuid4().hex}{ext}'
    path = os.path.join(_doc_dir(), stored_name)
    with open(path, 'wb') as fh:
        fh.write(content)
    u = current_user()
    doc = SupplierDocument(
        supplier_id=sid,
        filename=stored_name,
        original_name=f.filename,
        uploaded_by=u.id if u else None,
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify({'ok': True, 'id': doc.id, 'original_name': doc.original_name,
                    'filename': doc.filename, 'uploaded_at': doc.uploaded_at.date().isoformat()})


@bp.route('/api/suppliers/<int:sid>/documents/<int:did>/download', methods=['GET'])
def api_supplier_docs_download(sid, did):
    if not require_role('admin'):
        abort(403)
    doc = db.session.get(SupplierDocument, did)
    if not doc or doc.supplier_id != sid:
        abort(404)
    return send_from_directory(_doc_dir(), doc.filename, as_attachment=True,
                               download_name=doc.original_name)


@bp.route('/api/suppliers/<int:sid>/documents/<int:did>', methods=['DELETE'])
def api_supplier_docs_delete(sid, did):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    doc = db.session.get(SupplierDocument, did)
    if not doc or doc.supplier_id != sid:
        return jsonify({'error': 'Not found'}), 404
    path = os.path.join(_doc_dir(), doc.filename)
    try:
        os.remove(path)
    except OSError:
        pass
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/suppliers/<int:sid>/batches', methods=['GET'])
def api_supplier_batches(sid):
    """Return recent batches for a supplier for retrospective cost application."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    batches = (StockBatch.query
               .filter_by(supplier_id=sid)
               .order_by(StockBatch.purchased_at.desc(), StockBatch.id.desc())
               .limit(100)
               .all())
    pids = {b.product_id for b in batches}
    prod_map = {p.id: p for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    from collections import OrderedDict
    runs = OrderedDict()
    for b in batches:
        p = prod_map.get(b.product_id)
        qty_purchased = float(b.qty_purchased_base)
        qty_remaining = float(b.qty_remaining_base)
        consumed_pct  = round((1 - qty_remaining / qty_purchased) * 100, 1) if qty_purchased > 0 else 0
        key = b.purchase_run_id if b.purchase_run_id else f'solo_{b.id}'
        if key not in runs:
            runs[key] = {
                'run_id': b.purchase_run_id,
                'date':   b.purchased_at.strftime('%Y-%m-%d'),
                'batches': [],
            }
        runs[key]['batches'].append({
            'id':                 b.id,
            'product_id':         b.product_id,
            'product_name':       p.name if p else str(b.product_id),
            'base_unit':          p.base_unit if p else None,
            'unit_type':          p.unit_type if p else None,
            'purchased_at':       b.purchased_at.isoformat(),
            'qty_purchased_base': qty_purchased,
            'qty_remaining_base': qty_remaining,
            'consumed_pct':       consumed_pct,
            'cost_per_base_unit': float(b.cost_per_base_unit),
            'base_cost_total':    float(b.base_cost_total) if b.base_cost_total is not None else None,
            'additional_costs':   b.additional_costs,
        })
    # Fetch PurchaseRun metadata for known run IDs
    known_run_ids = [k for k in runs if isinstance(k, int)]
    run_meta = {}
    if known_run_ids:
        for pr in db.session.query(PurchaseRun).filter(PurchaseRun.id.in_(known_run_ids)).all():
            run_meta[pr.id] = {
                'invoice_ref': pr.invoice_ref,
                'invoice_additional_total': float(pr.invoice_additional_total) if pr.invoice_additional_total is not None else None,
            }
    result = []
    for key, run in runs.items():
        meta = run_meta.get(key, {})
        run['invoice_ref']              = meta.get('invoice_ref')
        run['invoice_additional_total'] = meta.get('invoice_additional_total')
        run['batch_count'] = len(run['batches'])
        run['base_total']  = sum(b['base_cost_total'] or 0 for b in run['batches'])
        result.append(run)
    return jsonify(result)

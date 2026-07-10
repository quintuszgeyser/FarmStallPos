import os
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, current_app, send_from_directory, abort
from sqlalchemy import func

from helpers import require_login, require_role, current_user, _gen_barcode
from models import db, Supplier, StockBatch, Purchase, Product, SupplierDocument

bp = Blueprint('suppliers', __name__)

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

    data     = request.json or {}
    lines    = data.get('lines', [])
    date_str = data.get('date')

    if not lines:
        return jsonify({'error': 'No lines provided'}), 400

    purchase_date = datetime.now()
    if date_str:
        try:
            parts = date_str.split('-')
            purchase_date = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            return jsonify({'error': 'Invalid date format'}), 400

    u = current_user()
    created_products = []
    batches_created  = 0

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
            product_type = new_prod.get('product_type', 'simple')
            base_unit    = new_prod.get('base_unit') or None
            unit_type    = new_prod.get('unit_type') or None
            if product_type not in ('simple', 'stock_item'):
                return jsonify({'error': 'Invalid product_type'}), 400
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
            conversion  = _UNIT_CONVERSIONS.get(unit, 1)
            qty_base    = qty * conversion
            cost_per_base = total_price / qty_base
            db.session.add(StockBatch(
                product_id=pid,
                qty_purchased_base=qty_base,
                qty_remaining_base=qty_base,
                cost_per_base_unit=cost_per_base,
                supplier_id=sid,
                user_id=u.id if u else None,
                purchased_at=purchase_date,
            ))
            batches_created += 1
        elif p.product_type == 'simple':
            p.stock_qty = (p.stock_qty or 0) + int(qty)
            price_per_unit = total_price / int(qty) if int(qty) > 0 else total_price
            db.session.add(Purchase(product_id=pid, qty_added=int(qty), purchase_price=price_per_unit,
                                    date_time=purchase_date, user_id=u.id if u else None))
            batches_created += 1

    db.session.commit()
    return jsonify({
        'ok': True,
        'created_products': created_products,
        'batches_created':  batches_created,
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

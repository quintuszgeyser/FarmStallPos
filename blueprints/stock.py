import uuid
from decimal import Decimal
from datetime import datetime

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from helpers import (
    require_login, require_role, current_user,
    get_stock_level, consume_fifo, _gen_barcode, _parse_dt,
)
from models import (
    db,
    Product, RecipeLine, StockBatch, StockAdjustment, Purchase, Supplier, User,
)

bp = Blueprint('stock', __name__)

_UNIT_CONV = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1, 'dozen': 12}


def _unit_conversion(p, unit):
    if p.package_size and p.package_unit and unit == p.package_unit:
        return float(p.package_size)
    return _UNIT_CONV.get(unit, 1)


@bp.route('/api/stock/ingredients', methods=['GET'])
def api_stock_ingredients():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    products = Product.query.filter(Product.product_type == 'stock_item').order_by(Product.name.asc()).all()
    result = []
    for p in products:
        batches     = StockBatch.query.filter_by(product_id=p.id).filter(StockBatch.qty_remaining_base > 0).order_by(StockBatch.purchased_at.desc()).all()
        stock_level = sum(float(b.qty_remaining_base) for b in batches)
        result.append({
            'id': p.id, 'name': p.name, 'unit_type': p.unit_type, 'base_unit': p.base_unit,
            'package_size': float(p.package_size) if p.package_size else None,
            'package_size_unit': p.package_size_unit, 'package_unit': p.package_unit,
            'stock_level': stock_level,
            'low_stock': p.low_stock_threshold is not None and stock_level < float(p.low_stock_threshold),
            'low_stock_threshold': float(p.low_stock_threshold) if p.low_stock_threshold else None,
            'sold_by_weight': p.sold_by_weight, 'is_for_sale': p.is_for_sale,
            'price_per_unit': float(p.price_per_unit) if p.price_per_unit else None,
            'batches': [{
                'id': b.id,
                'qty_purchased_base': float(b.qty_purchased_base),
                'qty_remaining_base': float(b.qty_remaining_base),
                'cost_per_base_unit': float(b.cost_per_base_unit),
                'purchased_at': b.purchased_at.isoformat(),
                'supplier_id': b.supplier_id,
                'supplier_name': db.session.get(Supplier, b.supplier_id).name if b.supplier_id else None,
            } for b in batches],
            'sell_packages': [{
                'id': pkg.id, 'name': pkg.name,
                'price': float(pkg.price) if pkg.price else None,
                'barcode': pkg.barcode,
                'qty_base': float(RecipeLine.query.filter_by(product_id=pkg.id).first().qty_base)
                            if RecipeLine.query.filter_by(product_id=pkg.id).first() else None,
            } for pkg in Product.query.filter_by(parent_stock_item_id=p.id).all()],
        })
    return jsonify(result)


@bp.route('/api/stock/receive', methods=['POST'])
def api_stock_receive():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data           = request.json or {}
    pid            = data.get('product_id')
    qty            = data.get('qty')
    unit           = data.get('unit', '')
    total_price    = data.get('total_price')
    price_per_unit = data.get('price_per_unit')
    supplier_id    = data.get('supplier_id') or None
    if supplier_id:
        try: supplier_id = int(supplier_id)
        except Exception: supplier_id = None
    try:
        pid = int(pid); qty = float(qty)
    except Exception:
        return jsonify({'error': 'Invalid product_id or qty'}), 400
    p = db.session.get(Product, pid)
    if not p or p.product_type != 'stock_item':
        return jsonify({'error': 'Product not found or not a stock_item'}), 404
    conversion = _unit_conversion(p, unit)
    qty_base   = qty * conversion
    if total_price is not None:
        try: cost_per_base = float(total_price) / qty_base
        except Exception: return jsonify({'error': 'Invalid total_price'}), 400
    elif price_per_unit is not None:
        try: cost_per_base = float(price_per_unit) / conversion
        except Exception: return jsonify({'error': 'Invalid price_per_unit'}), 400
    else:
        return jsonify({'error': 'total_price or price_per_unit required'}), 400
    u     = current_user()
    batch = StockBatch(product_id=pid, qty_purchased_base=qty_base, qty_remaining_base=qty_base,
                       cost_per_base_unit=cost_per_base, supplier_id=supplier_id, user_id=u.id if u else None)
    db.session.add(batch)
    db.session.commit()
    return jsonify({'ok': True, 'batch_id': batch.id, 'qty_base': qty_base, 'base_unit': p.base_unit, 'cost_per_base_unit': round(cost_per_base, 6)})


@bp.route('/api/stock/adjust', methods=['POST'])
def api_stock_adjust():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data       = request.json or {}
    pid        = data.get('product_id')
    actual_qty = data.get('actual_qty')
    unit       = data.get('unit', '')
    reason     = data.get('reason', '').strip()
    try:
        pid = int(pid); actual_qty = float(actual_qty)
    except Exception:
        return jsonify({'error': 'Invalid product_id or actual_qty'}), 400
    if not reason:
        return jsonify({'error': 'reason required'}), 400
    p = db.session.get(Product, pid, with_for_update=True)
    if not p or p.product_type != 'stock_item':
        return jsonify({'error': 'Product not found or not a stock_item'}), 404
    conversion  = _unit_conversion(p, unit)
    actual_base = Decimal(str(actual_qty * conversion))
    system_base = Decimal(str(get_stock_level(pid)))
    diff        = actual_base - system_base
    u   = current_user()
    now = datetime.utcnow()
    cost_written_off = Decimal('0')
    if diff < 0:
        loss_qty = abs(diff); remaining = loss_qty
        for b in StockBatch.query.filter_by(product_id=pid).filter(StockBatch.qty_remaining_base > 0).order_by(StockBatch.purchased_at.asc(), StockBatch.id.asc()).all():
            take = min(Decimal(str(b.qty_remaining_base)), remaining)
            cost_written_off += take * Decimal(str(b.cost_per_base_unit))
            remaining -= take
            if remaining <= 0: break
        consume_fifo(pid, loss_qty, f'adj-{uuid.uuid4()}', now)
    elif diff > 0:
        latest = StockBatch.query.filter_by(product_id=pid).filter(StockBatch.qty_remaining_base > 0).order_by(StockBatch.purchased_at.desc(), StockBatch.id.desc()).first()
        if latest:
            latest.qty_remaining_base = Decimal(str(latest.qty_remaining_base)) + diff
        else:
            db.session.add(StockBatch(product_id=pid, qty_purchased_base=diff, qty_remaining_base=diff, cost_per_base_unit=Decimal('0'), purchased_at=now, user_id=u.id if u else None))
    adj_type = 'writeoff' if diff < 0 else 'stocktake'
    db.session.add(StockAdjustment(product_id=pid, adjustment_type=adj_type, qty_change_base=diff, system_qty_before=system_base, cost_written_off=cost_written_off if diff < 0 else None, reason=reason, adjusted_at=now, user_id=u.id if u else None))
    db.session.commit()
    return jsonify({'ok': True, 'system_before': float(system_base), 'actual': float(actual_base), 'difference': float(diff), 'base_unit': p.base_unit})


@bp.route('/api/stock/batches/<int:batch_id>', methods=['PATCH'])
def api_stock_batch_edit(batch_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data  = request.json or {}
    batch = db.session.get(StockBatch, batch_id)
    if not batch:
        return jsonify({'error': 'Batch not found'}), 404
    if 'supplier_id' in data:
        sid = data['supplier_id']
        batch.supplier_id = int(sid) if sid else None
    if 'purchased_at' in data:
        try: batch.purchased_at = datetime.fromisoformat(data['purchased_at'])
        except Exception: return jsonify({'error': 'Invalid purchased_at date'}), 400
    if 'qty_purchased_base' in data:
        try:
            new_qty  = Decimal(str(float(data['qty_purchased_base'])))
            if new_qty <= 0: return jsonify({'error': 'qty_purchased_base must be positive'}), 400
            consumed = Decimal(str(batch.qty_purchased_base)) - Decimal(str(batch.qty_remaining_base))
            if new_qty < consumed: return jsonify({'error': f'Cannot reduce below already-consumed qty ({float(consumed):.4f})'}), 400
            current_total            = Decimal(str(batch.cost_per_base_unit)) * Decimal(str(batch.qty_purchased_base))
            batch.qty_purchased_base = new_qty
            batch.qty_remaining_base = new_qty - consumed
            batch.cost_per_base_unit = current_total / new_qty
        except Exception: return jsonify({'error': 'Invalid qty_purchased_base'}), 400
    if 'total_price' in data:
        try:
            total = Decimal(str(float(data['total_price'])))
            batch.cost_per_base_unit = total / Decimal(str(batch.qty_purchased_base))
        except Exception: return jsonify({'error': 'Invalid total_price'}), 400
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/stock/writeoff', methods=['POST'])
def api_stock_writeoff():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data   = request.json or {}
    pid    = data.get('product_id')
    qty    = data.get('qty')
    unit   = data.get('unit', '')
    reason = data.get('reason', '').strip()
    try:
        pid = int(pid); qty = float(qty)
    except Exception:
        return jsonify({'error': 'Invalid product_id or qty'}), 400
    if not reason: return jsonify({'error': 'reason required'}), 400
    if qty <= 0:   return jsonify({'error': 'qty must be positive'}), 400
    p = db.session.get(Product, pid, with_for_update=True)
    if not p or p.product_type != 'stock_item':
        return jsonify({'error': 'Product not found or not a stock_item'}), 404
    conversion     = _unit_conversion(p, unit)
    qty_base       = Decimal(str(qty * conversion))
    system_before  = Decimal(str(get_stock_level(pid)))
    if qty_base > system_before:
        return jsonify({'error': f'Cannot write off {float(qty_base)}{p.base_unit} — only {float(system_before)}{p.base_unit} in stock'}), 400
    u   = current_user(); now = datetime.utcnow()
    cost_written_off = consume_fifo(pid, qty_base, f'wo-{uuid.uuid4()}', now)
    db.session.add(StockAdjustment(product_id=pid, adjustment_type='writeoff', qty_change_base=-qty_base, system_qty_before=system_before, cost_written_off=cost_written_off, reason=reason, adjusted_at=now, user_id=u.id if u else None))
    db.session.commit()
    return jsonify({'ok': True, 'qty_written_off': float(qty_base), 'base_unit': p.base_unit, 'cost_written_off': float(cost_written_off)})


@bp.route('/api/stock/adjustments/<int:adj_id>', methods=['PATCH'])
def api_stock_adjustment_edit(adj_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    adj  = db.session.get(StockAdjustment, adj_id)
    if not adj: return jsonify({'error': 'Adjustment not found'}), 404
    if adj.adjustment_type != 'writeoff': return jsonify({'error': 'Only write-offs can be edited'}), 400
    new_qty    = data.get('qty')
    new_unit   = data.get('unit', '')
    new_reason = data.get('reason', '').strip() or adj.reason
    try:
        new_qty = float(new_qty)
    except Exception:
        return jsonify({'error': 'Invalid qty'}), 400
    if new_qty <= 0: return jsonify({'error': 'qty must be positive'}), 400
    p = db.session.get(Product, adj.product_id)
    if not p: return jsonify({'error': 'Product not found'}), 404
    conversion   = _unit_conversion(p, new_unit)
    new_qty_base = Decimal(str(new_qty * conversion))
    old_qty_base = abs(Decimal(str(adj.qty_change_base)))
    diff         = new_qty_base - old_qty_base
    u   = current_user(); now = datetime.utcnow()
    if diff > 0:
        current_stock = Decimal(str(get_stock_level(p.id)))
        if diff > current_stock: return jsonify({'error': f'Cannot write off additional {float(diff)}{p.base_unit} — only {float(current_stock)}{p.base_unit} in stock'}), 400
        extra_cost = consume_fifo(p.id, diff, f'wo-edit-{uuid.uuid4()}', now)
        adj.cost_written_off = Decimal(str(adj.cost_written_off or 0)) + Decimal(str(extra_cost))
    elif diff < 0:
        restore_qty = abs(diff)
        batch = StockBatch.query.filter_by(product_id=p.id).order_by(StockBatch.purchased_at.desc()).first()
        if batch: batch.qty_remaining_base = float(Decimal(str(batch.qty_remaining_base)) + restore_qty)
        if old_qty_base > 0: adj.cost_written_off = Decimal(str(adj.cost_written_off or 0)) * (new_qty_base / old_qty_base)
    adj.qty_change_base = -new_qty_base
    adj.reason = new_reason
    db.session.commit()
    return jsonify({'ok': True, 'new_qty_base': float(new_qty_base), 'cost_written_off': float(adj.cost_written_off or 0)})


@bp.route('/api/stock/adjustments', methods=['GET'])
def api_stock_adjustments():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    pid         = request.args.get('product_id')
    adj_type    = request.args.get('type')
    start_param = request.args.get('start')
    end_param   = request.args.get('end')
    q = StockAdjustment.query
    if pid:        q = q.filter_by(product_id=int(pid))
    if adj_type:   q = q.filter_by(adjustment_type=adj_type)
    if start_param:
        start_dt = _parse_dt(start_param)
        if start_dt: q = q.filter(StockAdjustment.adjusted_at >= start_dt)
    if end_param:
        end_dt = _parse_dt(end_param, is_end=True)
        if end_dt: q = q.filter(StockAdjustment.adjusted_at <= end_dt)
    rows = q.order_by(StockAdjustment.adjusted_at.desc()).limit(500).all()
    user_names = {usr.id: usr.username for usr in User.query.filter(User.id.in_({r.user_id for r in rows if r.user_id})).all()}
    prod_names = {prod.id: (prod.name, prod.base_unit) for prod in Product.query.filter(Product.id.in_({r.product_id for r in rows})).all()}
    result = []
    for r in rows:
        pname, bunit = prod_names.get(r.product_id, ('?', '?'))
        result.append({'id': r.id, 'product_id': r.product_id, 'product_name': pname, 'base_unit': bunit, 'adjustment_type': r.adjustment_type, 'qty_change_base': float(r.qty_change_base), 'system_qty_before': float(r.system_qty_before), 'cost_written_off': float(r.cost_written_off) if r.cost_written_off else None, 'reason': r.reason, 'adjusted_at': r.adjusted_at.isoformat(), 'adjusted_by': user_names.get(r.user_id, '')})
    return jsonify(result)


@bp.route('/api/purchases', methods=['GET'])
def api_purchases_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    rows = Purchase.query.order_by(Purchase.date_time.desc()).all()
    return jsonify([{'id': r.id, 'product_id': r.product_id, 'product_name': (db.session.get(Product, r.product_id) or Product(name=None)).name, 'qty_added': r.qty_added, 'purchase_price': float(r.purchase_price), 'date_time': r.date_time.isoformat()} for r in rows])


@bp.route('/api/purchases', methods=['POST'])
def api_purchases_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data  = request.json or {}
    pid   = data.get('product_id')
    qty   = data.get('qty_added')
    price = data.get('purchase_price')
    try:
        pid = int(pid); qty = int(qty); price = float(price)
    except Exception:
        return jsonify({'error': 'Invalid product_id/qty/price'}), 400
    p = db.session.get(Product, pid, with_for_update=True)
    if not p: return jsonify({'error': 'Product not found'}), 404
    if qty <= 0 or price < 0: return jsonify({'error': 'Invalid values'}), 400
    u = current_user()
    db.session.add(Purchase(product_id=pid, qty_added=qty, purchase_price=price, user_id=u.id if u else None))
    p.stock_qty = (p.stock_qty or 0) + qty
    db.session.commit()
    return jsonify({'ok': True})

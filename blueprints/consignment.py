from datetime import datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request

from helpers import require_role, current_user
from models import (
    db,
    Product, StockBatch, Supplier,
    ConsignmentLiability, ConsignmentSettlement, ConsignmentSettlementLine,
)

bp = Blueprint('consignment', __name__)


@bp.route('/api/consignment/summary', methods=['GET'])
def api_consignment_summary():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    liabilities = ConsignmentLiability.query.filter_by(status='outstanding').all()

    total_outstanding = sum(Decimal(str(l.amount_owed)) for l in liabilities)

    by_supplier = {}
    for lib in liabilities:
        sid = lib.supplier_id
        if sid not in by_supplier:
            by_supplier[sid] = {'supplier_id': sid, 'name': '', 'outstanding': Decimal('0'), 'units': Decimal('0')}
        by_supplier[sid]['outstanding'] += Decimal(str(lib.amount_owed))
        by_supplier[sid]['units']       += Decimal(str(lib.qty_consumed))

    # Enrich supplier names
    supplier_ids = list(by_supplier.keys())
    if supplier_ids:
        suppliers = Supplier.query.filter(Supplier.id.in_(supplier_ids)).all()
        name_map = {s.id: s.name for s in suppliers}
        for sid, row in by_supplier.items():
            row['name'] = name_map.get(sid, f'Supplier {sid}')

    # Unsold consignment stock value (qty_remaining × consignment_unit_cost)
    consignment_batches = (StockBatch.query
                           .filter_by(ownership_type='CONSIGNMENT')
                           .filter(StockBatch.qty_remaining_base > 0)
                           .all())
    unsold_value = Decimal('0')
    for b in consignment_batches:
        cuc = b.consignment_unit_cost or b.cost_per_base_unit
        unsold_value += Decimal(str(b.qty_remaining_base)) * Decimal(str(cuc))

    # Settled this calendar month
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_settlements = ConsignmentSettlement.query.filter(
        ConsignmentSettlement.created_at >= month_start
    ).all()
    settled_this_month = sum(Decimal(str(s.total_amount)) for s in month_settlements)

    return jsonify({
        'total_outstanding': float(total_outstanding.quantize(Decimal('0.01'))),
        'total_units_pending': float(sum(r['units'] for r in by_supplier.values()).quantize(Decimal('0.01'))),
        'unsold_stock_value': float(unsold_value.quantize(Decimal('0.01'))),
        'settled_this_month': float(settled_this_month.quantize(Decimal('0.01'))),
        'suppliers': [
            {
                'supplier_id': row['supplier_id'],
                'name': row['name'],
                'outstanding': float(row['outstanding'].quantize(Decimal('0.01'))),
                'units': float(row['units'].quantize(Decimal('0.01'))),
            }
            for row in sorted(by_supplier.values(), key=lambda r: r['outstanding'], reverse=True)
        ],
    })


@bp.route('/api/consignment/supplier/<int:sid>', methods=['GET'])
def api_consignment_supplier(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    supplier = db.session.get(Supplier, sid)
    if not supplier:
        return jsonify({'error': 'Not found'}), 404

    # Outstanding liabilities grouped by batch
    liabilities = (ConsignmentLiability.query
                   .filter_by(supplier_id=sid, status='outstanding')
                   .order_by(ConsignmentLiability.created_at.asc())
                   .all())

    # All consignment batches for this supplier
    batches = (StockBatch.query
               .filter_by(supplier_id=sid, ownership_type='CONSIGNMENT')
               .order_by(StockBatch.purchased_at.asc())
               .all())

    # Build batch map with liability sums
    batch_map = {}
    for b in batches:
        prod = db.session.get(Product, b.product_id)
        batch_map[b.id] = {
            'batch_id': b.id,
            'product_id': b.product_id,
            'product_name': prod.name if prod else f'Product {b.product_id}',
            'qty_received': float(b.qty_purchased_base),
            'qty_remaining': float(b.qty_remaining_base),
            'qty_sold': float(b.qty_purchased_base) - float(b.qty_remaining_base),
            'consignment_unit_cost': float(b.consignment_unit_cost or b.cost_per_base_unit),
            'amount_owed': 0.0,
            'purchased_at': b.purchased_at.date().isoformat() if b.purchased_at else None,
        }

    total_outstanding = Decimal('0')
    for lib in liabilities:
        total_outstanding += Decimal(str(lib.amount_owed))
        if lib.batch_id in batch_map:
            batch_map[lib.batch_id]['amount_owed'] = round(
                batch_map[lib.batch_id]['amount_owed'] + float(lib.amount_owed), 2
            )

    # Recent settlements
    settlements = (ConsignmentSettlement.query
                   .filter_by(supplier_id=sid)
                   .order_by(ConsignmentSettlement.created_at.desc())
                   .limit(10)
                   .all())

    return jsonify({
        'supplier_id': sid,
        'name': supplier.name,
        'outstanding': float(total_outstanding.quantize(Decimal('0.01'))),
        'batches': [b for b in batch_map.values() if b['qty_received'] > 0],
        'settlements': [
            {
                'id': s.id,
                'total_amount': float(s.total_amount),
                'note': s.note,
                'created_at': s.created_at.date().isoformat(),
            }
            for s in settlements
        ],
    })


@bp.route('/api/consignment/settle', methods=['POST'])
def api_consignment_settle():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.json or {}
    sid  = data.get('supplier_id')
    note = str(data.get('note') or '').strip() or None

    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return jsonify({'error': 'supplier_id required'}), 400

    supplier = db.session.get(Supplier, sid)
    if not supplier:
        return jsonify({'error': 'Supplier not found'}), 404

    liabilities = (ConsignmentLiability.query
                   .filter_by(supplier_id=sid, status='outstanding')
                   .with_for_update()
                   .all())

    if not liabilities:
        return jsonify({'error': 'No outstanding liabilities for this supplier'}), 400

    total = sum(Decimal(str(l.amount_owed)) for l in liabilities)
    u     = current_user()
    now   = datetime.utcnow()

    settlement = ConsignmentSettlement(
        supplier_id=sid,
        total_amount=float(total.quantize(Decimal('0.01'))),
        note=note,
        created_by=u.id if u else None,
        created_at=now,
    )
    db.session.add(settlement)
    db.session.flush()  # get settlement.id

    for lib in liabilities:
        db.session.add(ConsignmentSettlementLine(
            settlement_id=settlement.id,
            liability_id=lib.id,
            supplier_id=lib.supplier_id,
            product_id=lib.product_id,
            batch_id=lib.batch_id,
            qty=lib.qty_consumed,
            unit_cost=lib.unit_cost,
            amount=lib.amount_owed,
        ))
        lib.status        = 'settled'
        lib.settlement_id = settlement.id
        lib.settled_at    = now

    db.session.commit()
    return jsonify({
        'ok': True,
        'settlement_id': settlement.id,
        'supplier_name': supplier.name,
        'total_amount': float(total.quantize(Decimal('0.01'))),
        'lines_settled': len(liabilities),
    })


@bp.route('/api/consignment/settlements/<int:settlement_id>', methods=['GET'])
def api_consignment_settlement_detail(settlement_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    s = db.session.get(ConsignmentSettlement, settlement_id)
    if not s:
        return jsonify({'error': 'Not found'}), 404

    supplier = db.session.get(Supplier, s.supplier_id)
    lines = ConsignmentSettlementLine.query.filter_by(settlement_id=settlement_id).all()
    product_names = {}
    for ln in lines:
        if ln.product_id not in product_names:
            p = db.session.get(Product, ln.product_id)
            product_names[ln.product_id] = p.name if p else f'Product {ln.product_id}'

    return jsonify({
        'id': s.id,
        'supplier_name': supplier.name if supplier else '',
        'total_amount': float(s.total_amount),
        'note': s.note,
        'created_at': s.created_at.date().isoformat(),
        'lines': [
            {
                'product_name': product_names.get(ln.product_id, ''),
                'batch_id': ln.batch_id,
                'qty': float(ln.qty),
                'unit_cost': float(ln.unit_cost),
                'amount': float(ln.amount),
            }
            for ln in lines
        ],
    })

from collections import defaultdict
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

    total_outstanding = sum((Decimal(str(l.amount_owed)) for l in liabilities), Decimal('0'))

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
    settled_this_month = sum((Decimal(str(s.total_amount)) for s in month_settlements), Decimal('0'))

    return jsonify({
        'total_outstanding': float(total_outstanding.quantize(Decimal('0.01'))),
        'total_units_pending': float(sum((r['units'] for r in by_supplier.values()), Decimal('0')).quantize(Decimal('0.01'))),
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
    # current_unit_cost = what the batch is set to NOW (for reference/recalculate)
    batch_map = {}
    for b in batches:
        prod = db.session.get(Product, b.product_id)
        batch_map[b.id] = {
            'batch_id': b.id,
            'product_id': b.product_id,
            'product_name': prod.name if prod else f'Product {b.product_id}',
            'unit_type': prod.unit_type if prod else 'count',
            'base_unit': prod.base_unit if prod else 'unit',
            'qty_received': float(b.qty_purchased_base),
            'qty_remaining': float(b.qty_remaining_base),
            'qty_sold': 0.0,  # accumulated from liabilities below
            'consignment_unit_cost': float(b.consignment_unit_cost or b.cost_per_base_unit),
            'current_unit_cost': float(b.consignment_unit_cost or b.cost_per_base_unit),
            'amount_owed': 0.0,
            'purchased_at': b.purchased_at.date().isoformat() if b.purchased_at else None,
            'is_backfill': False,
        }

    # Backfill: liabilities with no batch_id (sold before stock arrived), grouped by product
    backfill_by_product: dict = defaultdict(lambda: {'qty': 0.0, 'amount': 0.0, 'product_name': '', 'unit_cost': 0.0, 'unit_type': 'count', 'base_unit': 'unit'})

    total_outstanding = Decimal('0')
    for lib in liabilities:
        total_outstanding += Decimal(str(lib.amount_owed))
        if lib.batch_id in batch_map:
            batch_map[lib.batch_id]['amount_owed'] = round(
                batch_map[lib.batch_id]['amount_owed'] + float(lib.amount_owed), 2
            )
            batch_map[lib.batch_id]['qty_sold'] = round(
                batch_map[lib.batch_id]['qty_sold'] + float(lib.qty_consumed), 2
            )
        else:
            bf = backfill_by_product[lib.product_id]
            bf['qty']    = round(bf['qty']    + float(lib.qty_consumed), 2)
            bf['amount'] = round(bf['amount'] + float(lib.amount_owed),  2)
            bf['unit_cost'] = float(lib.unit_cost or 0)
            if not bf['product_name']:
                p = db.session.get(Product, lib.product_id)
                bf['product_name'] = p.name if p else f'Product {lib.product_id}'
                if p:
                    bf['unit_type'] = p.unit_type or 'count'
                    bf['base_unit'] = p.base_unit or 'unit'

    # Derive effective unit cost from what was actually charged (amount_owed / qty_sold).
    # This ensures the displayed unit cost always matches the owed amount, even if the
    # batch cost was updated after liabilities were created.
    # Also compute qty_other = units that left the batch without a ConsignmentLiability
    # (write-offs, adjustments, pre-batch backfill absorption) — for reconciliation display.
    for entry in batch_map.values():
        if entry['qty_sold'] > 0:
            entry['consignment_unit_cost'] = round(entry['amount_owed'] / entry['qty_sold'], 6)
        qty_consumed_total = entry['qty_received'] - entry['qty_remaining']
        entry['qty_other'] = round(max(0.0, qty_consumed_total - entry['qty_sold']), 4)

    backfill_rows = [
        {
            'batch_id': None,
            'product_id': pid,
            'product_name': bf['product_name'],
            'unit_type': bf['unit_type'],
            'base_unit': bf['base_unit'],
            'qty_received': None,
            'qty_remaining': None,
            'qty_sold': bf['qty'],
            'qty_other': 0.0,
            'consignment_unit_cost': round(bf['amount'] / bf['qty'], 6) if bf['qty'] > 0 else bf['unit_cost'],
            'current_unit_cost': bf['unit_cost'],
            'amount_owed': bf['amount'],
            'purchased_at': None,
            'is_backfill': True,
        }
        for pid, bf in backfill_by_product.items()
        if bf['amount'] > 0
    ]

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
        'batches': [b for b in batch_map.values() if b['qty_received'] > 0] + backfill_rows,
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


@bp.route('/api/consignment/recalculate-costs/<int:sid>', methods=['POST'])
def api_consignment_recalculate_costs(sid):
    """Retroactively update all outstanding liability amounts to use the current batch cost."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    liabilities = (ConsignmentLiability.query
                   .filter_by(supplier_id=sid, status='outstanding')
                   .with_for_update()
                   .all())

    batches = StockBatch.query.filter_by(supplier_id=sid, ownership_type='CONSIGNMENT').all()
    batch_cost_map = {b.id: Decimal(str(b.consignment_unit_cost or b.cost_per_base_unit)) for b in batches}

    # For backfill liabilities (no batch): use most recent consignment batch cost for that product
    all_consignment_batches = (StockBatch.query
                               .filter_by(ownership_type='CONSIGNMENT')
                               .order_by(StockBatch.purchased_at.desc())
                               .all())
    product_latest_cost: dict = {}
    for b in all_consignment_batches:
        if b.product_id not in product_latest_cost:
            product_latest_cost[b.product_id] = Decimal(str(b.consignment_unit_cost or b.cost_per_base_unit))

    updated = 0
    total_delta = Decimal('0')
    for lib in liabilities:
        if lib.batch_id is not None:
            new_cost = batch_cost_map.get(lib.batch_id)
        else:
            new_cost = product_latest_cost.get(lib.product_id)
        if new_cost is None:
            continue
        old_amount = Decimal(str(lib.amount_owed))
        new_amount = (Decimal(str(lib.qty_consumed)) * new_cost).quantize(Decimal('0.000001'))
        if abs(new_amount - old_amount) >= Decimal('0.01'):
            total_delta += new_amount - old_amount
            lib.unit_cost   = float(new_cost)
            lib.amount_owed = float(new_amount.quantize(Decimal('0.01')))
            updated += 1

    db.session.commit()
    return jsonify({
        'ok': True,
        'liabilities_updated': updated,
        'amount_delta': float(total_delta.quantize(Decimal('0.01'))),
    })


@bp.route('/api/consignment/batches/<int:batch_id>/settlement-rate', methods=['PATCH'])
def api_update_settlement_rate(batch_id):
    """Update only the consignment_unit_cost on a batch (settlement rate owed per base unit sold)."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    rate = data.get('rate')
    if rate is None:
        return jsonify({'error': 'rate is required'}), 400
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return jsonify({'error': 'rate must be a number'}), 400
    if rate < 0:
        return jsonify({'error': 'rate must be >= 0'}), 400
    batch = StockBatch.query.get_or_404(batch_id)
    if batch.ownership_type != 'CONSIGNMENT':
        return jsonify({'error': 'Not a consignment batch'}), 400
    batch.consignment_unit_cost = rate
    db.session.commit()
    return jsonify({'ok': True, 'batch_id': batch_id, 'consignment_unit_cost': rate})


@bp.route('/api/consignment/settle', methods=['POST'])
def api_consignment_settle():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.json or {}
    sid  = data.get('supplier_id')
    note = str(data.get('note') or '').strip() or None
    settlement_amount_raw = data.get('settlement_amount')

    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return jsonify({'error': 'supplier_id required'}), 400

    supplier = db.session.get(Supplier, sid)
    if not supplier:
        return jsonify({'error': 'Supplier not found'}), 404

    liabilities = (ConsignmentLiability.query
                   .filter_by(supplier_id=sid, status='outstanding')
                   .order_by(ConsignmentLiability.created_at.asc())
                   .with_for_update()
                   .all())

    if not liabilities:
        return jsonify({'error': 'No outstanding liabilities for this supplier'}), 400

    total_owed = sum(Decimal(str(l.amount_owed)) for l in liabilities)
    u          = current_user()
    now        = datetime.utcnow()

    # Determine actual payment amount (may be partial)
    if settlement_amount_raw is not None:
        try:
            settlement_amount = Decimal(str(settlement_amount_raw)).quantize(Decimal('0.01'))
        except Exception:
            return jsonify({'error': 'Invalid settlement_amount'}), 400
        if settlement_amount <= 0:
            return jsonify({'error': 'settlement_amount must be positive'}), 400
        settlement_amount = min(settlement_amount, total_owed)
    else:
        settlement_amount = total_owed

    partial = settlement_amount < total_owed

    settlement = ConsignmentSettlement(
        supplier_id=sid,
        total_amount=float(settlement_amount.quantize(Decimal('0.01'))),
        note=note,
        created_by=u.id if u else None,
        created_at=now,
    )
    db.session.add(settlement)
    db.session.flush()  # get settlement.id

    # Settle liabilities oldest-first up to settlement_amount
    remaining = settlement_amount
    lines_settled = 0
    for lib in liabilities:
        lib_amount = Decimal(str(lib.amount_owed))
        if remaining <= 0:
            break
        db.session.add(ConsignmentSettlementLine(
            settlement_id=settlement.id,
            liability_id=lib.id,
            supplier_id=lib.supplier_id,
            product_id=lib.product_id,
            batch_id=lib.batch_id,
            qty=lib.qty_consumed,
            unit_cost=lib.unit_cost,
            amount=float(min(lib_amount, remaining).quantize(Decimal('0.01'))),
        ))
        lib.status        = 'settled'
        lib.settlement_id = settlement.id
        lib.settled_at    = now
        remaining -= lib_amount
        lines_settled += 1

    db.session.commit()
    return jsonify({
        'ok': True,
        'settlement_id': settlement.id,
        'supplier_name': supplier.name,
        'settlement_amount': float(settlement_amount.quantize(Decimal('0.01'))),
        'total_owed': float(total_owed.quantize(Decimal('0.01'))),
        'lines_settled': lines_settled,
        'partial': partial,
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

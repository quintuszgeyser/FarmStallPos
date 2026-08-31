"""
blueprints/cost_corrections.py — Retroactive batch cost corrections.

Scopes:
  remaining    — update batch.cost_per_base_unit only; future consumption uses new rate,
                 historical Sale.cogs and StockConsumption records unchanged.
  entire_batch — also rewrites StockConsumption.cost_per_base_unit for every record that
                 consumed from this batch, then rebuilds Sale.cogs for every affected
                 transaction from all its consumption records. Outstanding consignment
                 liabilities are adjusted. Production-run and write-off consumptions are
                 detected and surfaced in the preview but NOT automatically corrected.
"""

import json as _json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from flask import Blueprint, jsonify, request

from models import (
    db,
    StockBatch, StockConsumption, Sale, ConsignmentLiability,
    RecipeLine,
    CostAdjustment, CostAdjustmentLine,
)
from helpers import require_role, current_user

bp = Blueprint('cost_corrections', __name__)


# ── Attribution helpers ───────────────────────────────────────────────────────

def _find_recipe_parent_in_sale(ingredient_id, sale_product_ids, _depth=0):
    """Walk RecipeLine upwards to find which sold recipe product uses this ingredient."""
    if _depth > 8:
        return None
    for rl in RecipeLine.query.filter_by(ingredient_id=ingredient_id).all():
        if rl.product_id in sale_product_ids:
            return rl.product_id
        parent = _find_recipe_parent_in_sale(rl.product_id, sale_product_ids, _depth + 1)
        if parent:
            return parent
    return None


def _rebuild_sale_cogs_map(sale_id, sale_rows, all_consumes, corrected_batch_id, new_cpu):
    """
    For each Sale row in a transaction, compute (old_cogs, new_cogs).

    Attribution order:
      1. Direct: consumption.ingredient_id == sale.product_id
      2. Recipe: walk RecipeLine upwards to find the recipe product in this transaction
      3. Single-product fallback: if there is exactly one Sale row, attribute all cost to it

    Returns: {sale_row_id: (old_cogs_decimal, new_cogs_decimal)}
    """
    product_to_sale = {s.product_id: s for s in sale_rows}
    sale_product_ids = set(product_to_sale.keys())
    sale_new_cogs = {s.id: Decimal('0') for s in sale_rows}

    # Cache of ingredient_id → recipe parent product_id so we don't re-walk repeatedly
    recipe_parent_cache = {}

    for c in all_consumes:
        cpu = new_cpu if c.batch_id == corrected_batch_id else Decimal(str(c.cost_per_base_unit))
        contrib = Decimal(str(c.qty_consumed_base)) * cpu

        if c.ingredient_id in product_to_sale:
            sale_new_cogs[product_to_sale[c.ingredient_id].id] += contrib
        else:
            if c.ingredient_id not in recipe_parent_cache:
                recipe_parent_cache[c.ingredient_id] = _find_recipe_parent_in_sale(
                    c.ingredient_id, sale_product_ids
                )
            parent_pid = recipe_parent_cache[c.ingredient_id]
            if parent_pid and parent_pid in product_to_sale:
                sale_new_cogs[product_to_sale[parent_pid].id] += contrib
            elif len(sale_rows) == 1:
                sale_new_cogs[sale_rows[0].id] += contrib
            # else: unattributed — production ingredient mixed into a sale; skip

    result = {}
    for s in sale_rows:
        if s.cogs is not None:
            old_cogs = Decimal(str(s.cogs))
        else:
            # Legacy row (pre-cogs column): derive from consumption records as-is
            old_cogs = Decimal('0')
            recipe_old_cache = {}
            for c in all_consumes:
                rate = Decimal(str(c.cost_per_base_unit))
                contrib_old = Decimal(str(c.qty_consumed_base)) * rate
                if c.ingredient_id in product_to_sale:
                    if product_to_sale[c.ingredient_id].id == s.id:
                        old_cogs += contrib_old
                else:
                    if c.ingredient_id not in recipe_old_cache:
                        recipe_old_cache[c.ingredient_id] = _find_recipe_parent_in_sale(
                            c.ingredient_id, sale_product_ids
                        )
                    parent_pid = recipe_old_cache[c.ingredient_id]
                    if parent_pid and product_to_sale.get(parent_pid) and product_to_sale[parent_pid].id == s.id:
                        old_cogs += contrib_old
                    elif len(sale_rows) == 1:
                        old_cogs += contrib_old

        result[s.id] = (old_cogs, sale_new_cogs[s.id])
    return result


_WO_PREFIXES = ('wo-', 'adj-', 'archive-wo-', 'wo-edit-', 'adj-del-')


def _classify_sale_id(sale_id):
    """Classify a consumption sale_id as sale | writeoff | production | unknown."""
    if any(str(sale_id).startswith(p) for p in _WO_PREFIXES):
        return 'writeoff'
    has_sale = db.session.query(Sale.id).filter_by(sale_id=sale_id).limit(1).scalar() is not None
    if has_sale:
        return 'sale'
    is_produce = db.session.query(StockBatch.id).filter(
        StockBatch.produce_ref == str(sale_id)
    ).limit(1).scalar() is not None
    return 'production' if is_produce else 'unknown'


# ── Impact calculation (read-only, no mutations) ──────────────────────────────

def _compute_impact(batch, new_unit_cost_raw, scope):
    """
    Calculate what would change if this correction were applied.
    Returns a dict describing the full impact — used by both preview and apply.
    """
    new_cpu = Decimal(str(new_unit_cost_raw))
    old_cpu = Decimal(str(batch.cost_per_base_unit))
    qty_purchased = Decimal(str(batch.qty_purchased_base))
    qty_remaining = Decimal(str(batch.qty_remaining_base))
    qty_consumed = qty_purchased - qty_remaining

    lines = [{
        'entity_type': 'batch',
        'entity_id': batch.id,
        'sale_id': None,
        'old_value': float(old_cpu),
        'new_value': float(new_cpu),
        'qty': float(qty_purchased),
        'old_total': float(old_cpu * qty_remaining),
        'new_total': float(new_cpu * qty_remaining),
    }]

    if scope == 'remaining':
        return {
            'scope': 'remaining',
            'batch_id': batch.id,
            'old_unit_cost': float(old_cpu),
            'new_unit_cost': float(new_cpu),
            'qty_remaining': float(qty_remaining),
            'qty_consumed': float(qty_consumed),
            'inventory_value_delta': float((new_cpu - old_cpu) * qty_remaining),
            'cogs_delta': 0.0,
            'sales_affected': 0,
            'consumptions_affected': 0,
            'liability_delta': 0.0,
            'production_warned': False,
            'production_qty': 0.0,
            'writeoff_qty': 0.0,
            'lines': lines,
        }

    # entire_batch ─────────────────────────────────────────────────────────────
    consumptions = StockConsumption.query.filter_by(batch_id=batch.id).all()

    sale_ids_by_type = {}
    for c in consumptions:
        if c.sale_id not in sale_ids_by_type:
            sale_ids_by_type[c.sale_id] = _classify_sale_id(c.sale_id)

    direct_sale_ids = {sid for sid, t in sale_ids_by_type.items() if t == 'sale'}
    production_ids  = {sid for sid, t in sale_ids_by_type.items() if t == 'production'}
    writeoff_ids    = {sid for sid, t in sale_ids_by_type.items() if t == 'writeoff'}

    for c in consumptions:
        lines.append({
            'entity_type': 'consumption',
            'entity_id': c.id,
            'sale_id': c.sale_id,
            'old_value': float(c.cost_per_base_unit),
            'new_value': float(new_cpu),
            'qty': float(c.qty_consumed_base),
            'old_total': float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit))),
            'new_total': float(Decimal(str(c.qty_consumed_base)) * new_cpu),
        })

    total_cogs_delta = Decimal('0')
    for sale_id in direct_sale_ids:
        sale_rows = Sale.query.filter_by(sale_id=sale_id).all()
        all_consumes = StockConsumption.query.filter_by(sale_id=sale_id).all()
        cogs_map = _rebuild_sale_cogs_map(sale_id, sale_rows, all_consumes, batch.id, new_cpu)
        for sale_row_id, (old_cogs, new_cogs) in cogs_map.items():
            delta = new_cogs - (old_cogs or Decimal('0'))
            total_cogs_delta += delta
            lines.append({
                'entity_type': 'sale',
                'entity_id': sale_row_id,
                'sale_id': sale_id,
                'old_value': float(old_cogs or 0),
                'new_value': float(new_cogs),
                'qty': None,
                'old_total': float(old_cogs or 0),
                'new_total': float(new_cogs),
            })

    total_liability_delta = Decimal('0')
    is_consignment = getattr(batch, 'ownership_type', 'NORMAL') == 'CONSIGNMENT'
    if is_consignment:
        for lib in ConsignmentLiability.query.filter_by(
            batch_id=batch.id, status='outstanding'
        ).all():
            old_amount = Decimal(str(lib.amount_owed))
            new_amount = (Decimal(str(lib.qty_consumed)) * new_cpu).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
            total_liability_delta += new_amount - old_amount
            lines.append({
                'entity_type': 'liability',
                'entity_id': lib.id,
                'sale_id': lib.sale_id,
                'old_value': float(lib.unit_cost),
                'new_value': float(new_cpu),
                'qty': float(lib.qty_consumed),
                'old_total': float(old_amount),
                'new_total': float(new_amount),
            })

    _D = Decimal
    prod_qty = sum(_D(str(c.qty_consumed_base)) for c in consumptions if c.sale_id in production_ids)
    wo_qty   = sum(_D(str(c.qty_consumed_base)) for c in consumptions if c.sale_id in writeoff_ids)

    return {
        'scope': 'entire_batch',
        'batch_id': batch.id,
        'old_unit_cost': float(old_cpu),
        'new_unit_cost': float(new_cpu),
        'qty_remaining': float(qty_remaining),
        'qty_consumed': float(qty_consumed),
        'inventory_value_delta': float((new_cpu - old_cpu) * qty_remaining),
        'cogs_delta': float(total_cogs_delta),
        'sales_affected': len(direct_sale_ids),
        'consumptions_affected': len([c for c in consumptions if c.sale_id in direct_sale_ids]),
        'liability_delta': float(total_liability_delta),
        'production_warned': len(production_ids) > 0,
        'production_qty': float(prod_qty),
        'writeoff_qty': float(wo_qty),
        'is_consignment': is_consignment,
        'lines': lines,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@bp.route('/api/stock/batches/<int:batch_id>/cost-correction/preview', methods=['POST'])
def api_cost_correction_preview(batch_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    batch = db.session.get(StockBatch, batch_id)
    if not batch:
        return jsonify({'error': 'Batch not found'}), 404
    data = request.json or {}
    try:
        new_unit_cost = Decimal(str(float(data['new_unit_cost'])))
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'new_unit_cost is required and must be a number'}), 400
    if new_unit_cost <= 0:
        return jsonify({'error': 'new_unit_cost must be positive'}), 400
    scope = data.get('scope', 'remaining')
    if scope not in ('remaining', 'entire_batch'):
        return jsonify({'error': 'scope must be "remaining" or "entire_batch"'}), 400
    try:
        return jsonify(_compute_impact(batch, new_unit_cost, scope))
    except Exception as e:
        return jsonify({'error': f'Impact calculation failed: {e}'}), 500


@bp.route('/api/stock/batches/<int:batch_id>/cost-correction', methods=['POST'])
def api_cost_correction_apply(batch_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}

    idem_key = (data.get('idempotency_key') or '').strip() or None
    if idem_key:
        existing = CostAdjustment.query.filter_by(idempotency_key=idem_key).first()
        if existing:
            return jsonify({'ok': True, 'adjustment_id': existing.id, 'idempotent': True})

    batch = db.session.get(StockBatch, batch_id, with_for_update=True)
    if not batch:
        return jsonify({'error': 'Batch not found'}), 404

    try:
        new_unit_cost = Decimal(str(float(data['new_unit_cost'])))
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'new_unit_cost is required'}), 400
    if new_unit_cost <= 0:
        return jsonify({'error': 'new_unit_cost must be positive'}), 400

    scope = data.get('scope', 'remaining')
    if scope not in ('remaining', 'entire_batch'):
        return jsonify({'error': 'scope must be "remaining" or "entire_batch"'}), 400

    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': 'reason is required'}), 400

    u = current_user()
    now = datetime.utcnow()
    old_cpu = Decimal(str(batch.cost_per_base_unit))
    old_base_cost_total = batch.base_cost_total  # capture before mutation

    try:
        impact = _compute_impact(batch, new_unit_cost, scope)

        # ── 1. Update batch ───────────────────────────────────────────────────
        batch.cost_per_base_unit = new_unit_cost
        if batch.base_cost_total is not None:
            qty = Decimal(str(batch.qty_purchased_base))
            if qty > 0:
                addl_list = _json.loads(batch.additional_costs) if batch.additional_costs else []
                overhead = sum(Decimal(str(a.get('amount', 0))) for a in addl_list)
                new_base = (new_unit_cost * qty) - overhead
                batch.base_cost_total = new_base
                if batch.vat_amount is not None:
                    batch.base_cost_incl_vat = new_base + Decimal(str(batch.vat_amount))
                    batch.final_cost_incl_vat = batch.base_cost_incl_vat + overhead - Decimal(str(batch.allocated_discount or 0))
        batch.cost_adjustment_reason = reason
        batch.updated_at = now
        batch.updated_by = u.id if u else None

        if data.get('update_consignment_cost') and getattr(batch, 'ownership_type', 'NORMAL') == 'CONSIGNMENT':
            batch.consignment_unit_cost = new_unit_cost

        if scope == 'entire_batch':
            consumptions = StockConsumption.query.filter_by(batch_id=batch_id).all()
            sale_ids_by_type = {}
            for c in consumptions:
                if c.sale_id not in sale_ids_by_type:
                    sale_ids_by_type[c.sale_id] = _classify_sale_id(c.sale_id)
            direct_sale_ids = {sid for sid, t in sale_ids_by_type.items() if t == 'sale'}

            # ── 2. Update StockConsumption records from this batch ────────────
            for c in consumptions:
                c.cost_per_base_unit = new_unit_cost

            # ── 3. Rebuild Sale.cogs for each affected transaction ────────────
            for sale_id in direct_sale_ids:
                sale_rows = Sale.query.filter_by(sale_id=sale_id).with_for_update().all()
                # Re-query consumptions — they now have the updated cost_per_base_unit.
                # To correctly compute new_cogs we must pass the NEW rate via new_cpu param
                # rather than reading c.cost_per_base_unit (which is already updated).
                all_consumes = StockConsumption.query.filter_by(sale_id=sale_id).all()
                # For non-corrected batches we use their current (already-correct) rate.
                # For the corrected batch, _rebuild reads c.cost_per_base_unit which is now
                # already new_unit_cost — passing corrected_batch_id still works since
                # cpu = new_cpu if c.batch_id == corrected_batch_id, so they're identical.
                cogs_map = _rebuild_sale_cogs_map(
                    sale_id, sale_rows, all_consumes, batch_id, new_unit_cost
                )
                for sale_row in sale_rows:
                    _, new_cogs = cogs_map.get(sale_row.id, (None, Decimal(str(sale_row.cogs or 0))))
                    sale_row.cogs = new_cogs

            # ── 4. Update outstanding consignment liabilities ─────────────────
            if getattr(batch, 'ownership_type', 'NORMAL') == 'CONSIGNMENT':
                for lib in ConsignmentLiability.query.filter_by(
                    batch_id=batch_id, status='outstanding'
                ).all():
                    lib.unit_cost = float(new_unit_cost)
                    lib.amount_owed = float(
                        (Decimal(str(lib.qty_consumed)) * new_unit_cost)
                        .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    )

        # ── 5. CostAdjustment header ──────────────────────────────────────────
        adj = CostAdjustment(
            batch_id=batch_id,
            product_id=batch.product_id,
            supplier_id=batch.supplier_id,
            scope=scope,
            old_cost_per_unit=old_cpu,
            new_cost_per_unit=new_unit_cost,
            old_base_cost_total=old_base_cost_total,
            new_base_cost_total=batch.base_cost_total,
            reason=reason,
            status='applied',
            created_by=u.id if u else None,
            created_at=now,
            idempotency_key=idem_key,
            sales_affected=impact.get('sales_affected', 0),
            consumptions_affected=impact.get('consumptions_affected', 0),
            cogs_delta=Decimal(str(impact.get('cogs_delta', 0))).quantize(Decimal('0.0001')),
            liability_delta=Decimal(str(impact.get('liability_delta', 0))).quantize(Decimal('0.0001')),
        )
        db.session.add(adj)
        db.session.flush()

        # ── 6. CostAdjustmentLine rows ────────────────────────────────────────
        for line in impact['lines']:
            db.session.add(CostAdjustmentLine(
                adjustment_id=adj.id,
                entity_type=line['entity_type'],
                entity_id=line.get('entity_id'),
                sale_id_str=str(line['sale_id']) if line.get('sale_id') else None,
                old_value=Decimal(str(line['old_value'])) if line.get('old_value') is not None else None,
                new_value=Decimal(str(line['new_value'])) if line.get('new_value') is not None else None,
                qty=Decimal(str(line['qty'])) if line.get('qty') is not None else None,
                old_total=Decimal(str(line['old_total'])) if line.get('old_total') is not None else None,
                new_total=Decimal(str(line['new_total'])) if line.get('new_total') is not None else None,
            ))

        db.session.commit()

        try:
            from helpers import _auto_price_products
            _auto_price_products([batch.product_id])
        except Exception:
            pass

        return jsonify({
            'ok': True,
            'adjustment_id': adj.id,
            'sales_affected': impact.get('sales_affected', 0),
            'cogs_delta': float(adj.cogs_delta or 0),
            'inventory_value_delta': impact.get('inventory_value_delta', 0),
        })

    except Exception as e:
        db.session.rollback()
        import traceback
        import logging
        logging.getLogger('pos').error('cost_correction apply error: %s', traceback.format_exc())
        return jsonify({'error': str(e)}), 500


@bp.route('/api/stock/cost-corrections/<int:adj_id>/reverse', methods=['POST'])
def api_cost_correction_reverse(adj_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    adj = db.session.get(CostAdjustment, adj_id)
    if not adj:
        return jsonify({'error': 'Correction not found'}), 404
    if adj.status == 'reversed':
        return jsonify({'error': 'This correction has already been reversed'}), 409
    data = request.json or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({'error': 'reason is required for reversal'}), 400

    batch = db.session.get(StockBatch, adj.batch_id, with_for_update=True)
    if not batch:
        return jsonify({'error': 'Batch no longer exists'}), 404

    u = current_user()
    now = datetime.utcnow()
    old_cpu_to_restore = Decimal(str(adj.old_cost_per_unit))

    try:
        batch.cost_per_base_unit = old_cpu_to_restore
        if adj.old_base_cost_total is not None:
            batch.base_cost_total = adj.old_base_cost_total
        batch.cost_adjustment_reason = f'Reversal: {reason}'
        batch.updated_at = now
        batch.updated_by = u.id if u else None

        for line in CostAdjustmentLine.query.filter_by(adjustment_id=adj_id).all():
            if line.entity_type == 'consumption' and line.entity_id and line.old_value is not None:
                c = db.session.get(StockConsumption, line.entity_id)
                if c:
                    c.cost_per_base_unit = line.old_value
            elif line.entity_type == 'sale' and line.entity_id and line.old_value is not None:
                s = db.session.get(Sale, line.entity_id)
                if s:
                    s.cogs = line.old_value
            elif line.entity_type == 'liability' and line.entity_id:
                lib = db.session.get(ConsignmentLiability, line.entity_id)
                if lib and lib.status == 'outstanding':
                    if line.old_value is not None:
                        lib.unit_cost = float(line.old_value)
                    if line.old_total is not None:
                        lib.amount_owed = float(line.old_total)

        adj.status = 'reversed'

        rev = CostAdjustment(
            batch_id=adj.batch_id,
            product_id=adj.product_id,
            supplier_id=adj.supplier_id,
            scope=adj.scope,
            old_cost_per_unit=adj.new_cost_per_unit,
            new_cost_per_unit=adj.old_cost_per_unit,
            old_base_cost_total=adj.new_base_cost_total,
            new_base_cost_total=adj.old_base_cost_total,
            reason=f'Reversal of #{adj_id}: {reason}',
            status='applied',
            reversed_by_id=adj_id,
            created_by=u.id if u else None,
            created_at=now,
        )
        db.session.add(rev)
        db.session.commit()

        try:
            from helpers import _auto_price_products
            _auto_price_products([batch.product_id])
        except Exception:
            pass

        return jsonify({'ok': True, 'reversal_id': rev.id})

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.route('/api/stock/batches/<int:batch_id>/cost-corrections', methods=['GET'])
def api_cost_correction_list(batch_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    adjs = (CostAdjustment.query
            .filter_by(batch_id=batch_id)
            .order_by(CostAdjustment.created_at.desc())
            .all())
    return jsonify([{
        'id': a.id,
        'scope': a.scope,
        'old_cost_per_unit': float(a.old_cost_per_unit),
        'new_cost_per_unit': float(a.new_cost_per_unit),
        'reason': a.reason,
        'status': a.status,
        'created_at': a.created_at.isoformat(),
        'sales_affected': a.sales_affected,
        'cogs_delta': float(a.cogs_delta) if a.cogs_delta is not None else None,
    } for a in adjs])

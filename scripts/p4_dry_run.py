#!/usr/bin/env python3
"""
Rev 5 P4-1 — dry-run repair proposal.

Read-only, always — same guarantee as reconcile.py: no create_app(), no
strong_migrate(), a plain SQLAlchemy session bound to DATABASE_URL. This
script proposes corrections; it never applies them. Per the plan's own gate,
every PROPOSED item here is reviewed by the owner before a separate,
apply-only script runs it — against a quiesced database, inside one
transaction that re-checks all invariants and rolls back on any remaining
failure.

Built against reports/p0-3-baseline-prod-20260919.json, the real production
baseline (the earlier QA baseline overstated INV-2/INV-3 due to a QA-only
data-drift artifact — see that commit message). This covers the five
invariants that baseline actually found FAIL on: INV-4, INV-5, INV-6, INV-8,
INV-9. It reuses reconcile.py's own check functions to find violations, so
the violation set here is always exactly what reconcile.py would report
right now — no separate, divergent detection logic to keep in sync.

Per-invariant correction mechanism, and why each one is either PROPOSED
(computable from data already on the row, or from an explicit plan formula)
or NEEDS_MANUAL_REVIEW (guessing would produce a confident-looking number
that could be wrong in a way nothing would ever catch):

  INV-4 (no free stock) — PROPOSED. Rev 5 P4-2's own formula: re-cost at the
    qty-weighted average of the product's other normal batches purchased
    at or before this one's purchased_at (falls back to all other normal
    batches for the product if none exist before it, flagged as such). A
    batch with qty_remaining_base > 0 is flagged separately: it is still
    live sellable stock understating today's inventory value, not just a
    closed historical margin error.

  INV-8 (negative qty_remaining_base) — turned out to have ZERO real
    violations once reconcile.py's own bug was fixed. The first pass here
    proposed zeroing 19 "oversold" batches and splitting their shortfall
    into a new negative_placeholder batch, on the theory that these were the
    pre-P2-1 defect (shortfall posted against a live batch instead of a
    placeholder). Inspecting the actual rows before applying anything showed
    all 19 were already batch_type='negative_placeholder' — legitimate,
    currently-correct oversell tracking dated across a month, which
    check_inv8_value_baseline was wrongly flagging because it checked every
    batch's qty for negativity without excluding the one batch_type that is
    supposed to be negative. Fixed in reconcile.py; this function is kept
    as-is (it will still propose a correction if a genuine negative-quantity
    'normal' batch ever appears) but should propose nothing under normal
    operation.

  INV-9 (allocation closure) — mechanical recomputation from the batch's own
    stored inputs (base_cost_total, vat_amount, additional_costs,
    qty_purchased_base), which are still trustworthy — only the derived
    column disagrees with them. PROPOSED for deviations consistent with the
    documented rounding-mode bug (this invariant's own 0.01 tolerance note);
    NEEDS_MANUAL_REVIEW for any deviation far outside that scale, since a
    large gap between stored and expected is more likely a real data-entry
    error than a rounding bug, and silently "correcting" a real error to
    the wrong formula would make it worse, not better.

  INV-5 (consignment closure) — NEEDS_MANUAL_REVIEW, always. reconcile.py's
    own module docstring documents that this check does not net returned
    quantity (no consumption<->return link existed before P2-2), so part of
    a mismatch may be a real return, not corruption. It is also a supplier
    liability figure — inventing a corrected number here risks over- or
    under-paying a real supplier. This script reports the gap and direction
    (liability > consumed means the supplier may be owed for stock no
    longer tracked as consumed; liability < consumed means consumption
    exists with no liability ever recorded) for a human decision, and
    proposes nothing.

  INV-6 (COGS agrees) — NEEDS_MANUAL_REVIEW, always. Every violation in the
    production baseline has consumption_derived_cogs == 0.00 against a real,
    non-zero Sale.cogs — meaning no StockConsumption rows exist at all for
    that (sale_id, product_id), not that the wrong batch was charged. There
    is nothing on any surviving row to reconstruct which batch(es) were
    actually consumed; fabricating a plausible-looking consumption trail
    would misattribute cost to invented batches. The honest position is:
    Sale.cogs already reflects what appeared on that day's reports and
    should not be changed on the strength of a guess, and the gap should be
    documented as an unrecoverable pre-ledger audit-trail gap rather than
    "fixed."
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Product, StockBatch  # noqa: E402
from reconcile import (  # noqa: E402
    _make_session, _d, _q4,
    check_inv4_no_free_stock, check_inv5_consignment_closure,
    check_inv6_cogs_agrees, check_inv8_value_baseline, check_inv9_allocation_closure,
)

Q6 = Decimal('0.000001')


def _weighted_avg_cost(session, product_id, before_dt, exclude_batch_id):
    q = session.query(StockBatch).filter(
        StockBatch.product_id == product_id,
        StockBatch.batch_type == 'normal',
        StockBatch.id != exclude_batch_id,
        StockBatch.cost_per_base_unit > 0,
    )
    prior = [b for b in q.all() if before_dt is None or b.purchased_at is None or b.purchased_at <= before_dt]
    pool, fallback_used = (prior, False) if prior else (q.all(), True)
    if not pool:
        return None, fallback_used
    total_qty = sum((_d(b.qty_purchased_base) for b in pool), Decimal('0'))
    if total_qty == 0:
        return None, fallback_used
    weighted = sum((_d(b.qty_purchased_base) * _d(b.cost_per_base_unit) for b in pool), Decimal('0'))
    return (weighted / total_qty).quantize(Q6, rounding=ROUND_HALF_UP), fallback_used


def propose_inv4(session, run_id):
    result = check_inv4_no_free_stock(session)
    proposals = []
    for v in result.violations:
        batch = session.get(StockBatch, v['batch_id'])
        avg_cost, fallback_used = _weighted_avg_cost(session, batch.product_id, batch.purchased_at, batch.id)
        proposal = {
            'batch_id': batch.id,
            'product_id': batch.product_id,
            'qty_remaining_base': str(batch.qty_remaining_base),
            'purchased_at': batch.purchased_at.isoformat() if batch.purchased_at else None,
            'still_live_stock': _d(batch.qty_remaining_base) > 0,
        }
        if batch.base_cost_incl_vat is not None:
            proposal['status'] = 'NEEDS_MANUAL_REVIEW'
            proposal['reason'] = (f'base_cost_incl_vat is populated ({batch.base_cost_incl_vat}, not NULL) — '
                                   f'this batch is also in INV-9\'s checked set, so a plain cost_per_base_unit '
                                   f'fix would desync it from base_cost_incl_vat/final_cost_incl_vat. Needs a '
                                   f'combined correction, reviewed by a human — see apply_inv4\'s matching guard.')
        elif avg_cost is None:
            proposal['status'] = 'NEEDS_MANUAL_REVIEW'
            proposal['reason'] = ('no other normal batch with a positive cost exists for this product — '
                                   'no weighted average can be computed; owner must supply a cost or '
                                   'confirm this is genuinely free stock.')
        else:
            proposal['status'] = 'PROPOSED'
            proposal['proposed_cost_per_base_unit'] = str(avg_cost)
            proposal['cost_basis'] = ('weighted average of this product\'s other normal batches purchased '
                                       'at or before this one' if not fallback_used else
                                       'weighted average of this product\'s other normal batches — none '
                                       'existed before this one\'s purchased_at, so this is a fallback, '
                                       'not a point-in-time figure')
            proposal['run_id'] = run_id
        proposals.append(proposal)
    return result, proposals


def propose_inv8(session, run_id):
    result = check_inv8_value_baseline(session)
    proposals = []
    for v in result.violations:
        if v.get('issue') != 'negative qty_remaining_base':
            proposals.append({**v, 'status': 'NEEDS_MANUAL_REVIEW',
                               'reason': 'not the negative-quantity pattern this script handles'})
            continue
        batch = session.get(StockBatch, v['batch_id'])
        shortfall = -_d(batch.qty_remaining_base)  # positive number
        proposals.append({
            'batch_id': batch.id,
            'product_id': batch.product_id,
            'current_qty_remaining_base': str(batch.qty_remaining_base),
            'status': 'PROPOSED',
            'correction': {
                'zero_out_batch_id': batch.id,
                'zeroed_qty_remaining_base': '0.0000',
                'new_negative_placeholder': {
                    'product_id': batch.product_id,
                    'qty_remaining_base': str((-shortfall).quantize(Q6)),
                    'estimated_unit_cost': str(_d(batch.cost_per_base_unit)),
                    'cost_estimation_method': 'historical_repair',
                    'batch_type': 'negative_placeholder',
                    'cost_reconciled': False,
                    'note': (f'P4-1 reconciliation, run {run_id}. Historical oversell shortfall of '
                             f'{shortfall} moved off batch {batch.id} (pre-P2-1 defect posted it directly '
                             f'against a live batch instead of a placeholder). Will be absorbed by '
                             f'absorb_neg_placeholder the next time product {batch.product_id} is received.'),
                },
            },
        })
    return result, proposals


def propose_inv9(session, run_id):
    """CORRECTED classification (superseding the raw-deviation-size heuristic this
    started with). Investigating all 8 production violations — not just the smallest
    one — found the real safety criterion isn't how big the base_cost_incl_vat gap is;
    it's whether recomputing the FULL chain (base_cost_incl_vat, final_cost_incl_vat,
    cost_per_base_unit) together from the batch's own base_cost_total/vat_amount/
    additional_costs/allocated_discount/qty_purchased_base lands on a cost_per_base_unit
    that's already correct. In 7 of 8 real violations, cost_per_base_unit already
    exactly matched base_cost_total/qty — only the incl-VAT display fields were wrong,
    by amounts from R0.10 to over R3000, and fixing them changes nothing costing-
    related. Only 1 of 8 (batch 588) needed cost_per_base_unit itself corrected, and
    that's additionally gated on the batch being fully unconsumed (qty_remaining_base
    == qty_purchased_base), so no historical COGS is in play. See apply_inv9_correction
    in p4_apply.py for the applied version of this same logic — this function mirrors
    it exactly so the dry-run classification matches what would actually be applied.
    """
    result = check_inv9_allocation_closure(session)
    proposals = []
    for v in result.violations:
        batch = session.get(StockBatch, v['batch_id'])
        if batch is None or not _d(batch.qty_purchased_base):
            proposals.append({**v, 'status': 'NEEDS_MANUAL_REVIEW',
                               'reason': 'batch missing or qty_purchased_base is zero — cannot recompute'})
            continue

        # base_cost_incl_vat/final_cost_incl_vat are Numeric(18,4); cost_per_base_unit is
        # Numeric(10,6) — quantizing to reconcile.py's 2dp comparison precision here would
        # preview a value the apply step would never actually write (e.g. 1100.00/20000 =
        # 0.055 would round to 0.06 at 2dp, a full order of magnitude off). Match
        # apply_inv9_correction's precision exactly so the dry-run preview is honest.
        base_total = _d(batch.base_cost_total) or Decimal('0')
        vat_amount = _d(batch.vat_amount) or Decimal('0')
        expected_base_incl_vat = _q4(base_total + vat_amount)
        overhead_total = Decimal('0')
        if batch.additional_costs:
            try:
                entries = json.loads(batch.additional_costs)
                overhead_total = sum((_d(e.get('amount', 0)) for e in entries
                                      if e.get('type') != 'discount'), Decimal('0'))
            except (ValueError, TypeError):
                proposals.append({**v, 'status': 'NEEDS_MANUAL_REVIEW', 'reason': 'unparseable additional_costs'})
                continue
        allocated_discount = _d(batch.allocated_discount) or Decimal('0')
        expected_final = _q4(expected_base_incl_vat + overhead_total - allocated_discount)
        expected_cost_per_unit = (expected_final / _d(batch.qty_purchased_base)).quantize(Q6, rounding=ROUND_HALF_UP)
        current_cost_per_unit = _d(batch.cost_per_base_unit)
        cost_per_unit_changing = current_cost_per_unit.quantize(Q6, rounding=ROUND_HALF_UP) != expected_cost_per_unit

        proposal = dict(v)
        proposal['recomputed'] = {
            'base_cost_incl_vat': str(expected_base_incl_vat), 'final_cost_incl_vat': str(expected_final),
            'cost_per_base_unit': str(expected_cost_per_unit),
        }
        if cost_per_unit_changing and _d(batch.qty_remaining_base) != _d(batch.qty_purchased_base):
            proposal['status'] = 'NEEDS_MANUAL_REVIEW'
            proposal['reason'] = (f'cost_per_base_unit would change from {current_cost_per_unit} to '
                                   f'{expected_cost_per_unit} and this batch has already been partially '
                                   f'consumed ({batch.qty_remaining_base} of {batch.qty_purchased_base} '
                                   f'remaining) — not assumed safe without a human look.')
        else:
            proposal['status'] = 'PROPOSED'
            proposal['cost_per_unit_changing'] = cost_per_unit_changing
            proposal['correction'] = (
                f'recompute base_cost_incl_vat/final_cost_incl_vat/cost_per_base_unit from '
                f'base_cost_total+vat_amount(+overhead-discount); cost_per_base_unit '
                f'{"changes to " + str(expected_cost_per_unit) if cost_per_unit_changing else "unchanged"}'
            )
        proposals.append(proposal)
    return result, proposals


def propose_inv5(session):
    result = check_inv5_consignment_closure(session)
    proposals = []
    for v in result.violations:
        diff = Decimal(v['difference'])
        proposal = dict(v)
        proposal['status'] = 'NEEDS_MANUAL_REVIEW'
        proposal['direction'] = (
            'liability recorded EXCEEDS consumption — possible unreturned-quantity artifact '
            '(this check does not net returns pre-P2-2) or an overstated liability'
            if diff > 0 else
            'consumption EXCEEDS liability recorded — consumption exists with no liability ever '
            'written, a possible real underpayment to the supplier'
        )
        proposal['reason'] = ('supplier liability figure — reconcile.py\'s own docstring documents this '
                               'check does not net returned quantity, so part of this gap may be a real '
                               'return rather than corruption. Needs a human decision against the actual '
                               'supplier relationship, not an automatic correction.')
        proposals.append(proposal)
    return result, proposals


def propose_inv6(session):
    result = check_inv6_cogs_agrees(session)
    proposals = []
    for v in result.violations:
        proposal = dict(v)
        proposal['status'] = 'NEEDS_MANUAL_REVIEW'
        proposal['reason'] = ('consumption_derived_cogs is 0.00 — no StockConsumption rows exist at all '
                               'for this (sale_id, product_id), not that the wrong batch was charged. '
                               'There is nothing left to reconstruct which batch(es) were actually '
                               'consumed; Sale.cogs should stand as recorded (it is what appeared on that '
                               'day\'s reports) and this gap should be documented as an unrecoverable '
                               'pre-ledger audit-trail gap, not silently "fixed" with an invented number.')
        proposals.append(proposal)
    return result, proposals


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json-out', help='Write the full JSON proposal to this path')
    args = parser.parse_args()

    run_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    session = _make_session()
    try:
        inv4_result, inv4_proposals = propose_inv4(session, run_id)
        inv5_result, inv5_proposals = propose_inv5(session)
        inv6_result, inv6_proposals = propose_inv6(session)
        inv8_result, inv8_proposals = propose_inv8(session, run_id)
        inv9_result, inv9_proposals = propose_inv9(session, run_id)
    finally:
        session.close()

    sections = {
        'INV-4': inv4_proposals, 'INV-5': inv5_proposals, 'INV-6': inv6_proposals,
        'INV-8': inv8_proposals, 'INV-9': inv9_proposals,
    }
    summary = {}
    for inv_id, proposals in sections.items():
        ready = sum(1 for p in proposals if p['status'] == 'PROPOSED')
        review = sum(1 for p in proposals if p['status'] == 'NEEDS_MANUAL_REVIEW')
        summary[inv_id] = {'total': len(proposals), 'proposed': ready, 'needs_manual_review': review}

    report = {
        'run_id': run_id,
        'generated_at': generated_at,
        'mode': 'DRY_RUN — no writes performed',
        'phase': 'P4-1',
        'source_baseline': 'reports/p0-3-baseline-prod-20260919.json',
        'summary': summary,
        'proposals': sections,
    }

    print(f"P4-1 dry-run repair proposal (run {run_id})")
    print(f"Generated: {generated_at}  |  DRY_RUN — no writes performed\n")
    total_proposed = total_review = 0
    for inv_id, s in summary.items():
        print(f"  {inv_id}: {s['total']} violations — {s['proposed']} proposed, {s['needs_manual_review']} need manual review")
        total_proposed += s['proposed']
        total_review += s['needs_manual_review']
    print(f"\nTotals: {total_proposed} proposed for owner sign-off, {total_review} need manual review before anything is decided.")

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nFull proposal written to {args.json_out}")


if __name__ == '__main__':
    main()

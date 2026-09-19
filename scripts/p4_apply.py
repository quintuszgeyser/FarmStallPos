#!/usr/bin/env python3
"""
Rev 5 P4-1 — apply the owner-approved dry-run proposals.

Scope: ONLY the PROPOSED items from reports/p4-1-dry-run-prod-20260919.json —
19 INV-4 re-costings (batch_type='normal', cost_per_base_unit=0 -> the
product's own weighted-average cost) and 1 INV-9 rounding correction (batch
1682's base_cost_incl_vat/final_cost_incl_vat). The 73 NEEDS_MANUAL_REVIEW
items (INV-5, INV-6, the 7 real INV-9 outliers, 13 INV-4 unrecoverable-cost
cases) are explicitly out of scope and untouched by this script.

Every value is RECOMPUTED fresh against the live database inside this run's
own transaction — the dry-run report is used only to select which batch ids
are in scope, never to supply the numbers written. If a batch's state has
drifted since the dry-run (no longer zero-cost, no longer 'normal', a
different stored value than expected), it is skipped and reported, not
forced.

Process, per Rev 5 P4-1:
  1. Every write happens inside ONE transaction.
  2. Each correction writes a StockMovement (source_type='reconciliation',
     source_id=this run's id) and an AuditLog row (source='repair',
     correlation_id=this run's id) alongside it — an auditable trail, not a
     silent UPDATE.
  3. Before commit, INV-4 and INV-9 are re-run against the SAME transaction
     (flushed, uncommitted) to confirm every targeted batch id no longer
     violates, and that nothing outside the targeted set changed. Any
     surprise rolls the whole transaction back — nothing partial is ever
     left committed.
  4. --execute is required to actually commit; without it this performs the
     full computation and verification and then rolls back regardless, so
     it can be rehearsed exactly as it will run, with zero risk.

This does not touch stock_consumption, does not change any Sale.cogs, and
does not create any new StockBatch row — every correction here is an UPDATE
to a cost field already present on an existing batch.
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

from models import AuditLog, StockBatch, StockMovement  # noqa: E402
from reconcile import (  # noqa: E402
    _make_session, _d, _q2,
    check_inv4_no_free_stock, check_inv9_allocation_closure,
)
from p4_dry_run import _weighted_avg_cost  # noqa: E402

PROPOSAL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'reports', 'p4-1-dry-run-prod-20260919.json')


def _target_batch_ids(proposal):
    inv4_ids = [p['batch_id'] for p in proposal['proposals']['INV-4'] if p['status'] == 'PROPOSED']
    inv9_ids = [p['batch_id'] for p in proposal['proposals']['INV-9'] if p['status'] == 'PROPOSED']
    return inv4_ids, inv9_ids


def apply_inv4(session, batch_id, run_id, log):
    batch = session.get(StockBatch, batch_id)
    if batch is None:
        log.append({'batch_id': batch_id, 'action': 'SKIPPED', 'reason': 'batch no longer exists'})
        return False
    if batch.batch_type != 'normal' or _d(batch.cost_per_base_unit) != Decimal('0'):
        log.append({'batch_id': batch_id, 'action': 'SKIPPED',
                     'reason': f'state drifted since dry-run: batch_type={batch.batch_type!r}, '
                               f'cost_per_base_unit={batch.cost_per_base_unit}'})
        return False
    if batch.base_cost_incl_vat is not None:
        # Found during rehearsal: two of the 19 proposed batches (1335, 1394) have
        # base_cost_incl_vat/final_cost_incl_vat stored as literal 0.0000 (not NULL),
        # meaning they're also in INV-9's checked set. Setting cost_per_base_unit alone
        # would desync it from base_cost_incl_vat/final_cost_incl_vat — both still 0 —
        # turning a resolved INV-4 violation into a fresh INV-9 one. Recomputing all
        # three fields together is a bigger, more consequential change than "set one
        # cost field," so this is a manual-review case, not an automatic one.
        log.append({'batch_id': batch_id, 'action': 'SKIPPED',
                     'reason': f'base_cost_incl_vat is populated ({batch.base_cost_incl_vat}, not NULL) — '
                               f'this batch is also in INV-9\'s checked set, so a plain cost_per_base_unit '
                               f'fix would desync it from base_cost_incl_vat/final_cost_incl_vat and create '
                               f'a new INV-9 violation. Needs a combined correction, reviewed by a human, '
                               f'not this script\'s single-field fix.'})
        return False

    avg_cost, fallback_used = _weighted_avg_cost(session, batch.product_id, batch.purchased_at, batch.id)
    if avg_cost is None:
        log.append({'batch_id': batch_id, 'action': 'SKIPPED',
                     'reason': 'no positive-cost batch available to average against (drifted since dry-run)'})
        return False

    before = {'cost_per_base_unit': '0.000000'}
    batch.cost_per_base_unit = avg_cost
    batch.cost_adjustment_reason = (
        f'P4-1 reconciliation repair, run {run_id}. Re-costed from 0 to the qty-weighted '
        f'average cost ({avg_cost}) of this product\'s other normal batches'
        f'{" purchased at or before this one" if not fallback_used else " (fallback: none existed before this one)"}'
        f', per Rev 5 P4-2. This restates historical margin for any sale drawing from this '
        f'batch — it does not change Sale.cogs retroactively, only the batch\'s recorded cost.'
    )
    batch.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    after = {'cost_per_base_unit': str(avg_cost)}

    session.add(StockMovement(
        movement_type='COST_CORRECTION', batch_id=batch.id, qty_delta=Decimal('0.0000'),
        unit_cost=avg_cost, source_type='reconciliation', source_id=run_id,
        note=f'P4-1 INV-4 re-cost, run {run_id}: 0 -> {avg_cost}',
    ))
    session.add(AuditLog(
        event_type='p4_recost_zero_cost_batch', target_table='stock_batches', target_id=str(batch.id),
        before_json=json.dumps(before), after_json=json.dumps(after),
        note=batch.cost_adjustment_reason, correlation_id=run_id, source='repair',
    ))
    log.append({'batch_id': batch_id, 'action': 'APPLIED', 'old_cost': '0.000000', 'new_cost': str(avg_cost)})
    return True


def apply_inv9_rounding(session, batch_id, run_id, log):
    batch = session.get(StockBatch, batch_id)
    if batch is None:
        log.append({'batch_id': batch_id, 'action': 'SKIPPED', 'reason': 'batch no longer exists'})
        return False

    base_total = _d(batch.base_cost_total) or Decimal('0')
    vat_amount = _d(batch.vat_amount) or Decimal('0')
    expected_base_incl_vat = _q2(base_total + vat_amount)
    deviation = abs(_q2(batch.base_cost_incl_vat) - expected_base_incl_vat)
    if deviation == Decimal('0') or deviation > Decimal('0.10'):
        log.append({'batch_id': batch_id, 'action': 'SKIPPED',
                     'reason': f'state drifted since dry-run: deviation is now {deviation}, not the '
                               f'expected ~0.10 rounding case'})
        return False

    overhead_total = Decimal('0')
    if batch.additional_costs:
        entries = json.loads(batch.additional_costs)
        overhead_total = sum((_d(e.get('amount', 0)) for e in entries if e.get('type') != 'discount'), Decimal('0'))
    allocated_discount = _d(batch.allocated_discount) or Decimal('0')
    expected_final = _q2(expected_base_incl_vat + overhead_total - allocated_discount)
    qty_purchased = _d(batch.qty_purchased_base)
    expected_cost_per_unit = (expected_final / qty_purchased) if qty_purchased else None

    # Safety guard: this is meant to be a pure rounding fix. If recomputing the full
    # chain would move cost_per_base_unit by more than a cent, that's a bigger change
    # than "rounding" and this batch needs a human, not an automatic correction.
    current_cost_per_unit = _d(batch.cost_per_base_unit)
    if expected_cost_per_unit is not None and abs(_q2(current_cost_per_unit) - _q2(expected_cost_per_unit)) > Decimal('0.01'):
        log.append({'batch_id': batch_id, 'action': 'SKIPPED',
                     'reason': f'recomputing the full chain would move cost_per_base_unit from '
                               f'{current_cost_per_unit} to {expected_cost_per_unit} — too large a '
                               f'change to treat as a rounding fix'})
        return False

    before = {
        'base_cost_incl_vat': str(batch.base_cost_incl_vat),
        'final_cost_incl_vat': str(batch.final_cost_incl_vat),
    }
    batch.base_cost_incl_vat = expected_base_incl_vat
    batch.final_cost_incl_vat = expected_final
    batch.cost_adjustment_reason = (
        f'P4-1 reconciliation repair, run {run_id}. INV-9 rounding correction: '
        f'base_cost_incl_vat {before["base_cost_incl_vat"]} -> {expected_base_incl_vat}, '
        f'final_cost_incl_vat {before["final_cost_incl_vat"]} -> {expected_final}, recomputed '
        f'from base_cost_total + vat_amount (+ non-discount overhead - allocated_discount). '
        f'cost_per_base_unit unchanged ({current_cost_per_unit}) — already consistent with the '
        f'corrected total.'
    )
    batch.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    after = {'base_cost_incl_vat': str(expected_base_incl_vat), 'final_cost_incl_vat': str(expected_final)}

    session.add(StockMovement(
        movement_type='COST_CORRECTION', batch_id=batch.id, qty_delta=Decimal('0.0000'),
        unit_cost=current_cost_per_unit, source_type='reconciliation', source_id=run_id,
        note=f'P4-1 INV-9 rounding fix, run {run_id}: base/final_cost_incl_vat corrected by R0.10',
    ))
    session.add(AuditLog(
        event_type='p4_fix_allocation_rounding', target_table='stock_batches', target_id=str(batch.id),
        before_json=json.dumps(before), after_json=json.dumps(after),
        note=batch.cost_adjustment_reason, correlation_id=run_id, source='repair',
    ))
    log.append({'batch_id': batch_id, 'action': 'APPLIED', 'before': before, 'after': after})
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--execute', action='store_true',
                         help='Actually commit. Without this flag, everything runs and verifies then rolls back.')
    parser.add_argument('--json-out', help='Write the full run report to this path')
    args = parser.parse_args()

    with open(PROPOSAL_PATH) as f:
        proposal = json.load(f)
    inv4_ids, inv9_ids = _target_batch_ids(proposal)

    run_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    log = []

    session = _make_session()
    committed = False
    try:
        # Snapshot violations BEFORE any change, so verification can catch a fix on one
        # batch creating a new violation on an UNTARGETED batch — not just check that the
        # targeted ids themselves are clear. A rehearsal run caught exactly this: recosting
        # batches 1335/1394 (INV-4 targets) desynced them from their own (literal-zero, not
        # NULL) base_cost_incl_vat/final_cost_incl_vat, creating two new INV-9 violations
        # that a target-list-only check would have missed and committed anyway.
        inv4_before = {v['batch_id'] for v in check_inv4_no_free_stock(session).violations}
        inv9_before = {v['batch_id'] for v in check_inv9_allocation_closure(session).violations}

        for bid in inv4_ids:
            apply_inv4(session, bid, run_id, log)
        for bid in inv9_ids:
            apply_inv9_rounding(session, bid, run_id, log)

        session.flush()

        inv4_result = check_inv4_no_free_stock(session)
        inv9_result = check_inv9_allocation_closure(session)
        inv4_remaining = {v['batch_id'] for v in inv4_result.violations}
        inv9_remaining = {v['batch_id'] for v in inv9_result.violations}

        applied = [e for e in log if e['action'] == 'APPLIED']
        skipped = [e for e in log if e['action'] == 'SKIPPED']
        applied_ids = {e['batch_id'] for e in applied}

        # Only a batch this run actually attempted (APPLIED) and that still shows up as
        # violating is a real failure. A SKIPPED batch is an intentional exclusion (moved
        # to manual review) — it's expected to still violate, and is not this run's fault.
        still_broken = sorted((set(inv4_ids) & applied_ids & inv4_remaining)
                               | (set(inv9_ids) & applied_ids & inv9_remaining))
        newly_broken = sorted((inv4_remaining - inv4_before) | (inv9_remaining - inv9_before))

        verification = {
            'inv4_total_violations_after': len(inv4_result.violations),
            'inv9_total_violations_after': len(inv9_result.violations),
            'targeted_batches_still_violating': still_broken,
            'newly_broken_untargeted_batches': newly_broken,
            'applied_count': len(applied),
            'skipped_count': len(skipped),
        }

        if still_broken:
            raise RuntimeError(
                f'Verification failed: {len(still_broken)} targeted batch(es) still violate after '
                f'the fix ({still_broken}) — rolling back the entire transaction.'
            )
        if newly_broken:
            raise RuntimeError(
                f'Verification failed: {len(newly_broken)} untargeted batch(es) newly violate after '
                f'the fix ({newly_broken}) — a correction had a side effect nothing accounted for. '
                f'Rolling back the entire transaction.'
            )

        if args.execute:
            session.commit()
            committed = True
        else:
            session.rollback()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    report = {
        'run_id': run_id, 'generated_at': generated_at,
        'mode': 'COMMITTED' if committed else 'REHEARSAL (rolled back — pass --execute to commit)',
        'phase': 'P4-1', 'source_proposal': PROPOSAL_PATH,
        'targets': {'INV-4': inv4_ids, 'INV-9': inv9_ids},
        'log': log, 'verification': verification,
    }

    print(f"P4-1 apply run {run_id} — {report['mode']}")
    print(f"Generated: {generated_at}")
    for e in log:
        print(f"  [{e['action']}] batch {e['batch_id']}: {e.get('reason') or e}")
    print(f"\nApplied: {verification['applied_count']}  Skipped: {verification['skipped_count']}")
    print(f"Post-fix violation counts — INV-4: {verification['inv4_total_violations_after']}, "
          f"INV-9: {verification['inv9_total_violations_after']}")
    print(f"Targeted batches still violating after fix: {verification['targeted_batches_still_violating']}")
    print(f"Untargeted batches newly broken by the fix: {verification['newly_broken_untargeted_batches']}")

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nFull run report written to {args.json_out}")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Rev 5 P4-1 — apply the owner-approved dry-run proposals.

Scope: ONLY the PROPOSED items in reports/p4-1-dry-run-prod-20260919.json at
run time — this file is read fresh on every invocation, so it always reflects
whatever wave of corrections the dry-run report currently proposes. NEEDS_
MANUAL_REVIEW items (currently: INV-5's 6 supplier-liability gaps, INV-6's 47
unrecoverable-audit-trail cases, and 15 INV-4 batches with no comparable cost
to average against or a populated VAT-costing chain) are explicitly out of
scope and untouched by this script no matter which wave is current.

Every value is RECOMPUTED fresh against the live database inside this run's
own transaction — the dry-run report is used only to select which batch ids
are in scope, never to supply the numbers written. If a batch's state has
drifted since the dry-run (no longer zero-cost, no longer 'normal', a
different stored value than expected), it is skipped and reported, not
forced.

Process, per Rev 5 P4-1:
  1. Every write happens inside ONE transaction.
  2. Each correction writes a StockMovement (source_type='reconciliation',
     source_id=the batch's own id — see _batch_has_no_prior_movements'
     docstring for why an OPENING_BALANCE_BACKFILL sometimes accompanies it)
     and an AuditLog row (source='repair', correlation_id=this run's id)
     alongside it — an auditable trail, not a silent UPDATE.
  3. Before commit, INV-3, INV-4, INV-9, and INV-11 are all re-run against
     the SAME transaction (flushed, uncommitted) — not just the invariants
     this run's corrections directly target, but also the ones a
     StockMovement write can incidentally affect, which the first apply run
     learned the hard way. Any targeted batch still violating, or ANY new
     violation anywhere in that set, rolls the whole transaction back.
  4. --execute is required to actually commit; without it this performs the
     full computation and verification and then rolls back regardless, so
     it can be rehearsed exactly as it will run, with zero risk.

This does not touch stock_consumption and does not change any Sale.cogs. It
updates cost fields on existing batches, and may insert an
OPENING_BALANCE_BACKFILL StockMovement (never a new StockBatch row) when a
correction is the first movement ever written against a batch that still
carries live quantity.
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

Q6 = Decimal('0.000001')  # matches StockBatch.cost_per_base_unit's Numeric(10, 6)

from models import AuditLog, StockBatch, StockMovement  # noqa: E402
from reconcile import (  # noqa: E402
    _make_session, _d, _q4,
    check_inv4_no_free_stock, check_inv9_allocation_closure,
    check_inv3_typed_source_resolves, check_inv11_projection_rebuild,
)
from p4_dry_run import _weighted_avg_cost  # noqa: E402

PROPOSAL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'reports', 'p4-1-dry-run-prod-20260919.json')


def _target_batch_ids(proposal):
    inv4_ids = [p['batch_id'] for p in proposal['proposals']['INV-4'] if p['status'] == 'PROPOSED']
    inv9_ids = [p['batch_id'] for p in proposal['proposals']['INV-9'] if p['status'] == 'PROPOSED']
    return inv4_ids, inv9_ids


def _batch_has_no_prior_movements(session, batch_id):
    """Must be called BEFORE this run adds its own correction movement for the batch —
    it answers "did this batch have any movement before this run touched it," which
    determines whether the correction's own movement needs an opening-balance backfill
    alongside it. Calling it after the correction movement is added (even unflushed
    would still see it via autoflush) would always see >=1 and wrongly skip the backfill.
    """
    return session.query(StockMovement).filter(StockMovement.batch_id == batch_id).count() == 0


def _maybe_backfill_opening_balance(session, batch, was_first_movement, run_id, log):
    """Writing a batch's first-ever StockMovement pulls it into INV-11's checked set,
    which sums qty_delta and compares against the live qty_remaining_base. A pure
    cost-correction movement carries qty_delta=0, which only matches a batch that
    happens to be fully sold out (qty_remaining_base == 0) already. Found the hard way
    in the first P4-1 apply run: 4 of 18 movements created exactly this INV-11
    regression, caught only by an independent post-commit reconcile.py run and fixed
    separately afterward. This time it's handled inline: if this run's correction was
    this batch's first-ever movement and it still carries live quantity, back that
    quantity in as one OPENING_BALANCE_BACKFILL movement (source_type='migration',
    exempt from INV-3 by the ledger's own design) in the SAME transaction.
    """
    if not was_first_movement:
        return
    live_qty = _d(batch.qty_remaining_base)
    if live_qty == Decimal('0'):
        return
    session.add(StockMovement(
        movement_type='OPENING_BALANCE_BACKFILL', batch_id=batch.id, qty_delta=live_qty,
        unit_cost=_d(batch.cost_per_base_unit), source_type='migration', source_id=None,
        note=(f'P4-1 reconciliation, run {run_id}. This batch had zero prior movements; this '
              f'correction\'s own movement would otherwise be the only one, and its qty_delta=0 '
              f'does not account for the batch\'s pre-existing live quantity of {live_qty}. This '
              f'opening-balance movement brings the ledger sum into agreement with '
              f'qty_remaining_base, exempt from INV-3 as a migration row by design.'),
    ))
    log.append({'batch_id': batch.id, 'action': 'BACKFILLED_OPENING_BALANCE', 'qty_delta': str(live_qty)})


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

    was_first_movement = _batch_has_no_prior_movements(session, batch.id)
    session.add(StockMovement(
        movement_type='COST_CORRECTION', batch_id=batch.id, qty_delta=Decimal('0.0000'),
        unit_cost=avg_cost, source_type='reconciliation', source_id=str(batch.id),
        note=f'P4-1 INV-4 re-cost, run {run_id}: 0 -> {avg_cost}',
    ))
    session.add(AuditLog(
        event_type='p4_recost_zero_cost_batch', target_table='stock_batches', target_id=str(batch.id),
        before_json=json.dumps(before), after_json=json.dumps(after),
        note=batch.cost_adjustment_reason, correlation_id=run_id, source='repair',
    ))
    log.append({'batch_id': batch_id, 'action': 'APPLIED', 'old_cost': '0.000000', 'new_cost': str(avg_cost)})
    _maybe_backfill_opening_balance(session, batch, was_first_movement, run_id, log)
    return True


def apply_inv9_correction(session, batch_id, run_id, log):
    """Recompute base_cost_incl_vat, final_cost_incl_vat, and cost_per_base_unit
    together from the batch's own trustworthy inputs (base_cost_total, vat_amount,
    non-discount additional_costs, allocated_discount, qty_purchased_base), and write
    whichever of the three actually disagree with that recomputation.

    Investigating all 8 production INV-9 violations (not just the smallest one) found
    every single one fits one of two clean, low-risk shapes — never a case where the
    trustworthy inputs themselves look wrong:
      - 7 of 8: cost_per_base_unit already exactly matches base_cost_total / qty (i.e.
        it was computed correctly from the ex-VAT total); base_cost_incl_vat and/or
        final_cost_incl_vat are the ones that disagree with the formula (inflated,
        deflated, or zeroed — invoice 58's five batches and batch 2057). Fixing them
        changes nothing cost_per_base_unit-related — pure ledger-consistency.
      - 1 of 8 (batch 588): the reverse — base_cost_incl_vat/final_cost_incl_vat are
        already internally consistent, but cost_per_base_unit was computed from
        base_cost_total (ex-VAT) instead of final_cost_incl_vat (incl-VAT). This DOES
        change the batch's forward cost. It is still low-risk here because
        qty_remaining_base == qty_purchased_base — nothing has been sold from it yet,
        so no historical Sale.cogs is affected; StockConsumption snapshots its own
        cost_per_base_unit at consumption time regardless, so even a partially-consumed
        batch's past sales would be unaffected by changing this field going forward.
    """
    batch = session.get(StockBatch, batch_id)
    if batch is None:
        log.append({'batch_id': batch_id, 'action': 'SKIPPED', 'reason': 'batch no longer exists'})
        return False

    qty_purchased = _d(batch.qty_purchased_base)
    if not qty_purchased:
        log.append({'batch_id': batch_id, 'action': 'SKIPPED', 'reason': 'qty_purchased_base is zero or missing'})
        return False

    # base_cost_incl_vat/final_cost_incl_vat are Numeric(18,4); cost_per_base_unit is
    # Numeric(10,6) — quantizing all three to reconcile.py's 2dp comparison precision
    # (as check_inv9_allocation_closure does, correctly, for a REPORTING comparison)
    # would silently truncate real precision when WRITING them. 1100.00/20000 = 0.055
    # rounded to 2dp is 0.06 — a wrong value one order of magnitude off from what the
    # column is meant to hold. Caught by test_apply_inv9_correction's own assertion
    # before this ever ran against production.
    base_total = _d(batch.base_cost_total) or Decimal('0')
    vat_amount = _d(batch.vat_amount) or Decimal('0')
    expected_base_incl_vat = _q4(base_total + vat_amount)

    overhead_total = Decimal('0')
    if batch.additional_costs:
        entries = json.loads(batch.additional_costs)
        overhead_total = sum((_d(e.get('amount', 0)) for e in entries if e.get('type') != 'discount'), Decimal('0'))
    allocated_discount = _d(batch.allocated_discount) or Decimal('0')
    expected_final = _q4(expected_base_incl_vat + overhead_total - allocated_discount)
    expected_cost_per_unit = (expected_final / qty_purchased).quantize(Q6, rounding=ROUND_HALF_UP)

    before = {
        'base_cost_incl_vat': str(batch.base_cost_incl_vat),
        'final_cost_incl_vat': str(batch.final_cost_incl_vat),
        'cost_per_base_unit': str(batch.cost_per_base_unit),
    }
    current_cost_per_unit = _d(batch.cost_per_base_unit)
    cost_per_unit_changing = current_cost_per_unit.quantize(Q6, rounding=ROUND_HALF_UP) != expected_cost_per_unit

    if cost_per_unit_changing and _d(batch.qty_remaining_base) != qty_purchased:
        # Extra caution, not currently hit by any of the 8: only auto-apply a
        # cost_per_base_unit change (as opposed to a pure base/final_incl_vat fix) when
        # the batch is still fully unconsumed. A batch already partially sold down can
        # still be fixed safely in principle (StockConsumption already snapshotted its
        # own cost independently), but that combination hasn't been verified against a
        # real case, so it's held for manual review rather than assumed safe.
        log.append({'batch_id': batch_id, 'action': 'SKIPPED',
                     'reason': f'cost_per_base_unit would change from {current_cost_per_unit} to '
                               f'{expected_cost_per_unit} and this batch has already been partially '
                               f'consumed (qty_remaining {batch.qty_remaining_base} of '
                               f'{qty_purchased} purchased) — needs manual review, not assumed safe.'})
        return False

    batch.base_cost_incl_vat = expected_base_incl_vat
    batch.final_cost_incl_vat = expected_final
    batch.cost_per_base_unit = expected_cost_per_unit
    batch.cost_adjustment_reason = (
        f'P4-1 reconciliation repair, run {run_id}. INV-9 allocation correction, recomputed '
        f'from base_cost_total + vat_amount (+ non-discount overhead - allocated_discount): '
        f'base_cost_incl_vat {before["base_cost_incl_vat"]} -> {expected_base_incl_vat}, '
        f'final_cost_incl_vat {before["final_cost_incl_vat"]} -> {expected_final}, '
        f'cost_per_base_unit {before["cost_per_base_unit"]} -> {expected_cost_per_unit}'
        f'{" (unchanged)" if not cost_per_unit_changing else " — this batch had zero consumption "
                                                               "to date, so no historical COGS is affected"}.'
    )
    batch.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    after = {'base_cost_incl_vat': str(expected_base_incl_vat), 'final_cost_incl_vat': str(expected_final),
              'cost_per_base_unit': str(expected_cost_per_unit)}

    was_first_movement = _batch_has_no_prior_movements(session, batch.id)
    session.add(StockMovement(
        movement_type='COST_CORRECTION', batch_id=batch.id, qty_delta=Decimal('0.0000'),
        unit_cost=expected_cost_per_unit, source_type='reconciliation', source_id=str(batch.id),
        note=f'P4-1 INV-9 allocation fix, run {run_id}: cost_per_base_unit {before["cost_per_base_unit"]} -> {expected_cost_per_unit}',
    ))
    session.add(AuditLog(
        event_type='p4_fix_allocation_closure', target_table='stock_batches', target_id=str(batch.id),
        before_json=json.dumps(before), after_json=json.dumps(after),
        note=batch.cost_adjustment_reason, correlation_id=run_id, source='repair',
    ))
    log.append({'batch_id': batch_id, 'action': 'APPLIED', 'before': before, 'after': after,
                 'cost_per_unit_changed': cost_per_unit_changing})
    _maybe_backfill_opening_balance(session, batch, was_first_movement, run_id, log)
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
        inv3_before = {v['movement_id'] for v in check_inv3_typed_source_resolves(session).violations}
        inv11_before = {v['batch_id'] for v in check_inv11_projection_rebuild(session).violations}

        for bid in inv4_ids:
            apply_inv4(session, bid, run_id, log)
        for bid in inv9_ids:
            apply_inv9_correction(session, bid, run_id, log)

        session.flush()

        inv4_result = check_inv4_no_free_stock(session)
        inv9_result = check_inv9_allocation_closure(session)
        inv3_result = check_inv3_typed_source_resolves(session)
        inv11_result = check_inv11_projection_rebuild(session)
        inv4_remaining = {v['batch_id'] for v in inv4_result.violations}
        inv9_remaining = {v['batch_id'] for v in inv9_result.violations}
        inv3_remaining = {v['movement_id'] for v in inv3_result.violations}
        inv11_remaining = {v['batch_id'] for v in inv11_result.violations}

        applied = [e for e in log if e['action'] == 'APPLIED']
        skipped = [e for e in log if e['action'] == 'SKIPPED']
        applied_ids = {e['batch_id'] for e in applied}

        # Only a batch this run actually attempted (APPLIED) and that still shows up as
        # violating is a real failure. A SKIPPED batch is an intentional exclusion (moved
        # to manual review) — it's expected to still violate, and is not this run's fault.
        still_broken = sorted((set(inv4_ids) & applied_ids & inv4_remaining)
                               | (set(inv9_ids) & applied_ids & inv9_remaining))
        # INV-3/INV-11 are invariants THIS run's own StockMovement writes can affect (the
        # first apply run learned this the hard way) even though neither is a target list —
        # any new violation here, not just a "still broken target," is a hard stop.
        newly_broken = sorted((inv4_remaining - inv4_before) | (inv9_remaining - inv9_before)
                               | (inv3_remaining - inv3_before) | (inv11_remaining - inv11_before))

        verification = {
            'inv4_total_violations_after': len(inv4_result.violations),
            'inv9_total_violations_after': len(inv9_result.violations),
            'inv3_total_violations_after': len(inv3_result.violations),
            'inv11_total_violations_after': len(inv11_result.violations),
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
          f"INV-9: {verification['inv9_total_violations_after']}, "
          f"INV-3: {verification['inv3_total_violations_after']}, "
          f"INV-11: {verification['inv11_total_violations_after']}")
    print(f"Targeted batches still violating after fix: {verification['targeted_batches_still_violating']}")
    print(f"Untargeted batches newly broken by the fix: {verification['newly_broken_untargeted_batches']}")

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nFull run report written to {args.json_out}")


if __name__ == '__main__':
    main()

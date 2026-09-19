#!/usr/bin/env python3
"""
Rev 5 P4-1 — corrective follow-up for the apply run's own stock_movements bug.

scripts/p4_apply.py's COST_CORRECTION movements (run a6675182-f526-4e55-a051-
d2db23b897b7, committed 2026-09-19) used source_id=<run_id> for source_type=
'reconciliation'. INV-3's documented resolution rule for 'reconciliation' is
a SupplierInvoice.id or StockBatch.id, not an arbitrary run id — every one of
the 18 movements failed INV-3 as a result. Separately, writing ANY movement
against a batch that had zero prior movements pulls it into INV-11's checked
set; 4 of the 18 batches (1479, 1453, 1663, 1682) still carry real live
quantity that a qty_delta=0 cost-correction movement doesn't account for, so
their ledger sum (0) disagreed with their live qty_remaining_base — a real,
if narrow, INV-11 regression introduced by the apply run.

This script, inside one transaction, verified before commit exactly like
p4_apply.py:
  1. Repoints all 18 movements' source_id from the run id to their own
     batch_id (str), matching INV-3's actual resolution rule.
  2. Writes one additional OPENING_BALANCE_BACKFILL movement per one of the
     4 nonzero-quantity batches, source_type='migration' (exempt from INV-3
     by the ledger's own design), qty_delta = that batch's live
     qty_remaining_base, unit_cost = its current (already P4-1-corrected)
     cost_per_base_unit — bringing the ledger sum back into agreement with
     the live quantity for exactly the batches this run's own movements
     newly covered.
  3. Re-runs INV-3 and INV-11 before commit; rolls back on anything
     unexpected.

--execute is required to commit; without it this rehearses and rolls back.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import StockBatch, StockMovement  # noqa: E402
from reconcile import _make_session, _d, check_inv3_typed_source_resolves, check_inv11_projection_rebuild  # noqa: E402

BROKEN_RUN_ID = 'a6675182-f526-4e55-a051-d2db23b897b7'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--json-out')
    args = parser.parse_args()

    fix_run_id = str(uuid.uuid4())
    log = []
    session = _make_session()
    committed = False
    try:
        inv3_before = {(v['movement_id']) for v in check_inv3_typed_source_resolves(session).violations}
        inv11_before = {v['batch_id'] for v in check_inv11_projection_rebuild(session).violations}

        movements = session.query(StockMovement).filter(
            StockMovement.source_type == 'reconciliation',
            StockMovement.source_id == BROKEN_RUN_ID,
        ).all()
        if not movements:
            raise RuntimeError(f'No movements found with source_id={BROKEN_RUN_ID} — nothing to fix, refusing to proceed blind.')

        for m in movements:
            old_source_id = m.source_id
            m.source_id = str(m.batch_id)
            log.append({'movement_id': m.id, 'batch_id': m.batch_id, 'action': 'REPOINTED',
                        'old_source_id': old_source_id, 'new_source_id': m.source_id})

        backfilled = []
        for m in movements:
            batch = session.get(StockBatch, m.batch_id)
            live_qty = _d(batch.qty_remaining_base)
            if live_qty == Decimal('0'):
                continue
            if batch.id in backfilled:
                continue
            session.add(StockMovement(
                movement_type='OPENING_BALANCE_BACKFILL', batch_id=batch.id, qty_delta=live_qty,
                unit_cost=_d(batch.cost_per_base_unit), source_type='migration', source_id=None,
                note=(f'P4-1 corrective backfill, run {fix_run_id} (fixing apply run {BROKEN_RUN_ID}). '
                      f'This batch had zero movements before that run touched it with a cost-only '
                      f'correction (qty_delta=0), which does not account for its pre-existing live '
                      f'quantity of {live_qty}. This opening-balance movement brings the ledger sum '
                      f'back into agreement with qty_remaining_base, exempt from INV-3 as a migration '
                      f'row by the ledger\'s own design.'),
            ))
            backfilled.append(batch.id)
            log.append({'batch_id': batch.id, 'action': 'BACKFILLED', 'qty_delta': str(live_qty)})

        session.flush()

        inv3_after = check_inv3_typed_source_resolves(session)
        inv11_after = check_inv11_projection_rebuild(session)
        inv3_remaining_ids = {v['movement_id'] for v in inv3_after.violations}
        inv11_remaining_ids = {v['batch_id'] for v in inv11_after.violations}

        fixed_movement_ids = {m.id for m in movements}
        still_broken_inv3 = sorted(fixed_movement_ids & inv3_remaining_ids)
        still_broken_inv11 = sorted(set(backfilled) & inv11_remaining_ids)
        newly_broken_inv3 = sorted(inv3_remaining_ids - inv3_before - fixed_movement_ids)
        newly_broken_inv11 = sorted(inv11_remaining_ids - inv11_before)

        verification = {
            'inv3_violations_after': len(inv3_after.violations),
            'inv11_violations_after': len(inv11_after.violations),
            'still_broken_inv3_movement_ids': still_broken_inv3,
            'still_broken_inv11_batch_ids': still_broken_inv11,
            'newly_broken_inv3_movement_ids': newly_broken_inv3,
            'newly_broken_inv11_batch_ids': newly_broken_inv11,
        }

        if still_broken_inv3 or still_broken_inv11 or newly_broken_inv3 or newly_broken_inv11:
            raise RuntimeError(f'Verification failed: {verification} — rolling back.')

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
        'fix_run_id': fix_run_id, 'fixes_run_id': BROKEN_RUN_ID,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'COMMITTED' if committed else 'REHEARSAL (rolled back — pass --execute to commit)',
        'log': log, 'verification': verification,
    }
    print(f"P4-1 corrective fix run {fix_run_id} — {report['mode']}")
    for e in log:
        print(' ', e)
    print('\nVerification:', json.dumps(verification, indent=2))

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump(report, f, indent=2, default=str)


if __name__ == '__main__':
    main()

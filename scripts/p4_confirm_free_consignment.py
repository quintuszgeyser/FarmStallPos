#!/usr/bin/env python3
"""
Rev 5 P4-1 — stamp the owner-confirmed-free consignment gap.

The owner confirmed directly (2026-09-19): batch 1574 (Flowers/Blomme
Speldekussing/Pin Cushions) — the last remaining INV-5 violation, and the
one that could never have been backfilled anyway since it has no
supplier_id on record — involves no real money owed. "Leave it, it's free."

Stamps CONFIRMED_FREE_CONSIGNMENT_MARKER onto the batch's
cost_adjustment_reason (see reconcile.py's check_inv5_consignment_closure),
appended to the existing "Wrong price" note rather than replacing it, so
that earlier observation isn't lost. Nothing numeric changes — no
ConsignmentLiability row is created, voided, or edited; this only documents
a decision.

Rehearse-then-execute, same as every other P4-1 script.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import AuditLog, StockBatch  # noqa: E402
from reconcile import _make_session, CONFIRMED_FREE_CONSIGNMENT_MARKER, check_inv5_consignment_closure  # noqa: E402

TARGET_BATCH_ID = 1574


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--json-out')
    args = parser.parse_args()

    run_id = str(uuid.uuid4())
    log = []
    session = _make_session()
    committed = False
    try:
        before_ids = {v['batch_id'] for v in check_inv5_consignment_closure(session).violations}

        batch = session.get(StockBatch, TARGET_BATCH_ID)
        if batch is None:
            log.append({'action': 'SKIPPED', 'reason': 'batch no longer exists'})
        elif batch.cost_adjustment_reason and CONFIRMED_FREE_CONSIGNMENT_MARKER in batch.cost_adjustment_reason:
            log.append({'action': 'SKIPPED', 'reason': 'already marked'})
        else:
            before_reason = batch.cost_adjustment_reason
            addition = (f'{CONFIRMED_FREE_CONSIGNMENT_MARKER} owner confirmed directly, 2026-09-19, '
                        f'run {run_id}: this consignment gap is genuinely free, no supplier owed.')
            batch.cost_adjustment_reason = f'{before_reason} | {addition}' if before_reason else addition
            batch.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(AuditLog(
                event_type='p4_confirm_free_consignment', target_table='stock_batches',
                target_id=str(batch.id), before_json=json.dumps({'cost_adjustment_reason': before_reason}),
                after_json=json.dumps({'cost_adjustment_reason': batch.cost_adjustment_reason}),
                note=batch.cost_adjustment_reason[:500], correlation_id=run_id, source='repair',
            ))
            log.append({'action': 'CONFIRMED_FREE', 'batch_id': batch.id})

        session.flush()
        after_ids = {v['batch_id'] for v in check_inv5_consignment_closure(session).violations}
        if TARGET_BATCH_ID in after_ids and any(e['action'] == 'CONFIRMED_FREE' for e in log):
            raise RuntimeError(f'Verification failed: batch {TARGET_BATCH_ID} still violates INV-5 after '
                                f'stamping the marker — rolling back.')
        newly_broken = sorted(after_ids - before_ids)
        if newly_broken:
            raise RuntimeError(f'Verification failed: {newly_broken} newly violate INV-5 — rolling back.')

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

    mode = 'COMMITTED' if committed else 'REHEARSAL (rolled back — pass --execute to commit)'
    print(f"P4-1 confirm-free-consignment run {run_id} — {mode}")
    for e in log:
        print(' ', e)

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump({'run_id': run_id, 'mode': mode, 'log': log}, f, indent=2, default=str)


if __name__ == '__main__':
    main()

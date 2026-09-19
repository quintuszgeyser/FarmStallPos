#!/usr/bin/env python3
"""
Rev 5 P4-1 — stamp owner-confirmed-free stock.

The owner confirmed directly (2026-09-19) that four specific historical
batches were genuinely given away, not mispriced:
  1522  Water              — internal use, not sold directly (is_for_sale=False)
  1918  Springbok Droewors — confirmed free
  1515  Biltong - Kudu     — a small (qty 5) first/taster batch, confirmed free
  1529  Biltong - Kudu     — the larger batch from the same day, also confirmed free

This stamps CONFIRMED_FREE_MARKER (see reconcile.py's check_inv4_no_free_stock)
onto each one's cost_adjustment_reason, so INV-4 stops flagging them — not as
a database default, but as this specific, dated, sourced decision. Nothing
about cost_per_base_unit changes; it was already 0 and stays 0. No
StockMovement is written: nothing numeric changes, only the row's own
documentation, so there is no ledger event to record — the AuditLog entry is
the durable trail here.

Rehearse-then-execute, same as every other P4-1 script this session:
--execute is required to commit; without it this verifies and rolls back.
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
from reconcile import _make_session, _d, CONFIRMED_FREE_MARKER, check_inv4_no_free_stock  # noqa: E402
from decimal import Decimal  # noqa: E402

CONFIRMATIONS = {
    1522: 'Water — internal use, not sold directly (is_for_sale=False on the product).',
    1918: 'Springbok Droewors — owner confirmed this batch was genuinely given away.',
    1515: 'Biltong - Kudu — small (qty 5) first/taster batch, owner confirmed genuinely free.',
    1529: 'Biltong - Kudu — larger batch from the same day (2026-08-22), owner confirmed genuinely free too.',
}


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
        before_ids = {v['batch_id'] for v in check_inv4_no_free_stock(session).violations}

        for batch_id, reason_detail in CONFIRMATIONS.items():
            batch = session.get(StockBatch, batch_id)
            if batch is None:
                log.append({'batch_id': batch_id, 'action': 'SKIPPED', 'reason': 'batch no longer exists'})
                continue
            if _d(batch.cost_per_base_unit) != Decimal('0'):
                log.append({'batch_id': batch_id, 'action': 'SKIPPED',
                             'reason': f'state drifted since this was decided: cost_per_base_unit is now '
                                       f'{batch.cost_per_base_unit}, not 0 — no longer a free-stock case'})
                continue
            before = {'cost_adjustment_reason': batch.cost_adjustment_reason}
            batch.cost_adjustment_reason = (
                f'{CONFIRMED_FREE_MARKER} {reason_detail} Confirmed directly by the owner, 2026-09-19, '
                f'run {run_id}. cost_per_base_unit remains 0 — this marks it as a decision, not an '
                f'unexplained gap.'
            )
            batch.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            after = {'cost_adjustment_reason': batch.cost_adjustment_reason}
            session.add(AuditLog(
                event_type='p4_confirm_free_stock', target_table='stock_batches', target_id=str(batch.id),
                before_json=json.dumps(before), after_json=json.dumps(after),
                note=batch.cost_adjustment_reason[:500], correlation_id=run_id, source='repair',
            ))
            log.append({'batch_id': batch_id, 'action': 'CONFIRMED_FREE'})

        session.flush()
        after_result = check_inv4_no_free_stock(session)
        after_ids = {v['batch_id'] for v in after_result.violations}
        confirmed_ids = {e['batch_id'] for e in log if e['action'] == 'CONFIRMED_FREE'}
        still_flagged = sorted(confirmed_ids & after_ids)
        newly_flagged = sorted(after_ids - before_ids)

        if still_flagged:
            raise RuntimeError(f'Verification failed: {still_flagged} still show as INV-4 violations '
                                f'after stamping the marker — rolling back.')
        if newly_flagged:
            raise RuntimeError(f'Verification failed: {newly_flagged} newly violate INV-4 — rolling back.')

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
    print(f"P4-1 confirm-free run {run_id} — {mode}")
    for e in log:
        print(' ', e)

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump({'run_id': run_id, 'mode': mode, 'log': log}, f, indent=2, default=str)


if __name__ == '__main__':
    main()

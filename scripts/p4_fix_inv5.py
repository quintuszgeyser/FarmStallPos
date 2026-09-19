#!/usr/bin/env python3
"""
Rev 5 P4-1 — the two real INV-5 fixes, after the checker itself was fixed.

Investigating all 6 production INV-5 violations (previous commits) found 4
were checker false positives (opening-balance liability rows, write-off
consumption never owed to a supplier). Of the 2 real ones:

  Batch 1850 (Whole Chicken, Eikeboom): one sale (9274f0f0...) produced THREE
  StockConsumption rows against this batch — 542, 1044, and 1554 units, all
  at the same cost_per_base_unit (0.045) — but only the FIRST got a matching
  ConsignmentLiability row (id 175). The other 1044 + 1554 = 2598 units were
  never billed to the supplier. This backfills the two missing liability
  rows at the exact same unit_cost as the one that WAS created, for the same
  sale_id/batch/supplier — not an estimate, a direct copy of the pattern
  that already exists for the first third of the same sale.

  Batch 1388 (Skaap Pasteie, Jagkolk Ansie): liability row 181 has sale_id
  'wo-a3a9cec1-...' — a write-off token, not a real sale. consume_fifo's own
  write-off path explicitly skips liability creation ("Write-offs are
  absorbed as your own loss — supplier is not owed for spoilage/damage"),
  so a liability existing for a write-off event contradicts the system's
  own stated policy. This voids it (status='voided', never a hard delete —
  matches this project's append-only audit trail elsewhere).

Batch 1574 (Flowers/Pin Cushions) is NOT in this script — it has no
supplier_id at all on the batch, and ConsignmentLiability.supplier_id is
NOT NULL, so there is nothing to attach a backfilled liability to without
guessing who the supplier is. That one still needs a human.

Rehearse-then-execute, same as every other P4-1 script: --execute is
required to commit; without it this verifies and rolls back.
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

from models import AuditLog, ConsignmentLiability, StockConsumption  # noqa: E402
from reconcile import _make_session, _d, check_inv5_consignment_closure  # noqa: E402

VOID_LIABILITY_ID = 181  # batch 1388, sale_id='wo-a3a9cec1-...'
BACKFILL_BATCH_ID = 1850
BACKFILL_SALE_ID = '9274f0f0-c083-49da-924c-fb777bb709c8'
BACKFILL_SUPPLIER_ID = 42
BACKFILL_PRODUCT_ID = 442
BACKFILL_CONSUMPTION_IDS = [4265, 4266]  # the two StockConsumption rows with no matching liability


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
        before = {v['batch_id']: v for v in check_inv5_consignment_closure(session).violations}

        # --- Void the write-off-tagged liability on batch 1388 ---
        liab = session.get(ConsignmentLiability, VOID_LIABILITY_ID)
        if liab is None:
            log.append({'action': 'SKIPPED', 'target': f'liability {VOID_LIABILITY_ID}',
                         'reason': 'no longer exists'})
        elif not (liab.sale_id and liab.sale_id.startswith('wo-')) or liab.status == 'voided':
            log.append({'action': 'SKIPPED', 'target': f'liability {VOID_LIABILITY_ID}',
                         'reason': f'state drifted: sale_id={liab.sale_id!r}, status={liab.status!r}'})
        else:
            before_snap = {'status': liab.status}
            liab.status = 'voided'
            after_snap = {'status': liab.status}
            session.add(AuditLog(
                event_type='p4_void_writeoff_liability', target_table='consignment_liabilities',
                target_id=str(liab.id), before_json=json.dumps(before_snap), after_json=json.dumps(after_snap),
                note=(f'P4-1 reconciliation, run {run_id}. Voided: this liability\'s sale_id '
                      f'({liab.sale_id}) is a write-off token, and consume_fifo\'s own write-off '
                      f'path never creates supplier liability by policy. Supplier was incorrectly '
                      f'billed for stock that was actually written off as the store\'s own loss.'),
                correlation_id=run_id, source='repair',
            ))
            log.append({'action': 'VOIDED', 'target': f'liability {liab.id}', 'batch_id': 1388})

        # --- Backfill the two missing liabilities on batch 1850 ---
        for cons_id in BACKFILL_CONSUMPTION_IDS:
            cons = session.get(StockConsumption, cons_id)
            if cons is None:
                log.append({'action': 'SKIPPED', 'target': f'consumption {cons_id}', 'reason': 'no longer exists'})
                continue
            if cons.batch_id != BACKFILL_BATCH_ID or cons.sale_id != BACKFILL_SALE_ID:
                log.append({'action': 'SKIPPED', 'target': f'consumption {cons_id}',
                             'reason': f'state drifted: batch_id={cons.batch_id}, sale_id={cons.sale_id!r}'})
                continue
            existing = session.query(ConsignmentLiability).filter(
                ConsignmentLiability.batch_id == BACKFILL_BATCH_ID,
                ConsignmentLiability.sale_id == BACKFILL_SALE_ID,
                ConsignmentLiability.qty_consumed == _d(cons.qty_consumed_base),
                ConsignmentLiability.status != 'voided',
            ).first()
            if existing:
                log.append({'action': 'SKIPPED', 'target': f'consumption {cons_id}',
                             'reason': f'a matching liability ({existing.id}) already exists — already fixed'})
                continue
            qty = _d(cons.qty_consumed_base)
            unit_cost = _d(cons.cost_per_base_unit)
            amount = (qty * unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            new_liab = ConsignmentLiability(
                supplier_id=BACKFILL_SUPPLIER_ID, product_id=BACKFILL_PRODUCT_ID, batch_id=BACKFILL_BATCH_ID,
                sale_id=BACKFILL_SALE_ID, qty_consumed=qty, unit_cost=unit_cost, amount_owed=amount,
                status='outstanding',
            )
            session.add(new_liab)
            session.flush()
            session.add(AuditLog(
                event_type='p4_backfill_missing_liability', target_table='consignment_liabilities',
                target_id=str(new_liab.id), before_json=None,
                after_json=json.dumps({'qty_consumed': str(qty), 'unit_cost': str(unit_cost), 'amount_owed': str(amount)}),
                note=(f'P4-1 reconciliation, run {run_id}. Backfilled: StockConsumption {cons_id} '
                      f'(sale {BACKFILL_SALE_ID}, qty {qty}) had no matching liability, unlike the '
                      f'first portion of the same sale (liability 175, qty 542) which did. Same '
                      f'unit_cost ({unit_cost}) copied from that existing row, not estimated.'),
                correlation_id=run_id, source='repair',
            ))
            log.append({'action': 'BACKFILLED', 'target': f'consumption {cons_id}', 'batch_id': BACKFILL_BATCH_ID,
                         'qty': str(qty), 'amount_owed': str(amount)})

        session.flush()
        after_result = check_inv5_consignment_closure(session)
        after = {v['batch_id']: v for v in after_result.violations}

        still_broken = []
        if 1388 in after:
            still_broken.append(1388)
        if 1850 in after:
            still_broken.append(1850)
        newly_broken = sorted(set(after) - set(before) - {1388, 1850})

        if still_broken:
            raise RuntimeError(f'Verification failed: {still_broken} still violate INV-5 after the fix — rolling back.')
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
    print(f"P4-1 INV-5 fix run {run_id} — {mode}")
    for e in log:
        print(' ', e)
    print('INV-5 violations remaining after (informational, includes untouched batch 1574):',
          sorted(after.keys()))

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump({'run_id': run_id, 'mode': mode, 'log': log}, f, indent=2, default=str)


if __name__ == '__main__':
    main()

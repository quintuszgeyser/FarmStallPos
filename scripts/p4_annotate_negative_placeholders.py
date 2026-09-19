#!/usr/bin/env python3
"""
Rev 5 P4-1 — annotate currently-open negative-placeholder batches.

The owner asked (2026-09-19), using Bee Long Drink (batch 1925) as the
example: for stock sold before it was ever received, add a note on the
negative item so that when it's finally received, the situation is visible
and it "should be in place."

The mechanics already exist and already work — helpers.py's
absorb_neg_placeholder() runs on every receive, automatically absorbs a
matching negative-placeholder batch first, writes a RECEIPT_ABSORB_SHORTFALL
StockMovement recording it, and (if the actual receive cost differs from
the shortfall's cost estimate) a COST_VARIANCE movement so the difference is
visible in the P&L rather than silently absorbed. Of the 181 negative-
placeholder batches in production, 162 have already gone through exactly
that cycle (qty_remaining_base back to 0, cost_reconciled=True) — the
mechanism is proven, not theoretical.

What was missing: none of them, reconciled or still open, carry a human-
readable note explaining what they are. This stamps cost_adjustment_reason
on the 19 CURRENTLY OPEN ones (qty_remaining_base < 0 right now) — the
already-reconciled 162 need no note, their own RECEIPT_ABSORB_SHORTFALL
movement already documents what happened and when.

Pure documentation: qty_remaining_base, cost_per_base_unit, and every other
numeric field are untouched. No invariant depends on cost_adjustment_reason,
so there is nothing to verify against reconcile.py here beyond "the write
succeeded" — still run through the same rehearse-then-execute shape as
every other P4-1 script for consistency, not because there's a real risk.
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

from models import AuditLog, Product, StockBatch  # noqa: E402
from reconcile import _make_session, _d  # noqa: E402


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
        negs = session.query(StockBatch).filter(
            StockBatch.batch_type == 'negative_placeholder',
            StockBatch.qty_remaining_base < 0,
        ).all()

        for b in negs:
            if b.cost_adjustment_reason:
                log.append({'batch_id': b.id, 'action': 'SKIPPED',
                             'reason': 'already has a note — not overwriting'})
                continue
            product = session.get(Product, b.product_id)
            shortfall = abs(_d(b.qty_remaining_base))
            note = (
                f'P4-1 annotation, run {run_id}. This represents {shortfall} unit(s) of '
                f'{product.name if product else b.product_id!r} sold before enough stock had '
                f'been received to cover it — a real, tracked shortfall, not an error. It will '
                f'resolve automatically the next time {product.name if product else "this product"} '
                f'is received: absorb_neg_placeholder() applies incoming stock against this batch '
                f'first, writes a RECEIPT_ABSORB_SHORTFALL movement recording exactly when and how '
                f'much was absorbed, and — if the real receive cost differs from the '
                f'{b.cost_estimation_method or "estimated"} cost used here — a COST_VARIANCE '
                f'movement so the difference is visible in the P&L rather than absorbed silently. '
                f'No action is needed beyond receiving the stock normally.'
            )
            b.cost_adjustment_reason = note
            b.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(AuditLog(
                event_type='p4_annotate_negative_placeholder', target_table='stock_batches',
                target_id=str(b.id), before_json=json.dumps({'cost_adjustment_reason': None}),
                after_json=json.dumps({'cost_adjustment_reason': note}),
                note=note[:500], correlation_id=run_id, source='repair',
            ))
            log.append({'batch_id': b.id, 'product': product.name if product else None,
                         'action': 'ANNOTATED', 'shortfall': str(shortfall)})

        session.flush()

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
    print(f"P4-1 annotate-negative-placeholders run {run_id} — {mode}")
    for e in log:
        print(' ', e)

    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump({'run_id': run_id, 'mode': mode, 'log': log}, f, indent=2, default=str)


if __name__ == '__main__':
    main()

"""Transaction locking / concurrent stock mutation characterization test
(Rev 5 P0-1 / Section 5).

This is the one test in the suite that deliberately does NOT use the
`db_session` fixture. That fixture binds ALL ORM activity (including the
app's own internal commits) onto a single shared Connection/SAVEPOINT so each
test can be rolled back in isolation — which is exactly wrong for a locking
test, since it collapses two "concurrent" threads onto one physical
connection and one transaction, making real Postgres row-level blocking
impossible to observe.

Instead this test talks to the REAL app engine directly: each thread pushes
its own `app.app_context()` (Flask-SQLAlchemy scopes `db.session` by
`id(app_ctx)` — see flask_sqlalchemy/session.py `_app_ctx_id` — so two
distinct app-context pushes get two distinct sessions and two distinct
Postgres connections/transactions automatically). Because this bypasses the
rollback-based isolation, it does real commits and is responsible for its own
cleanup, done explicitly in a `finally` block.
"""
import threading
import time
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from helpers import consume_fifo
from models import Product, StockBatch, StockConsumption, StockMovement, db
from tests.helpers import D


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def test_with_for_update_serializes_concurrent_fifo_consumption(app):
    """Two threads race to consume from the SAME batch holding exactly 2 units,
    each wanting 1. Thread A acquires the row lock (via consume_fifo's
    `.with_for_update()` at helpers.py ~235), then deliberately holds its
    transaction open for ~1s before committing. Thread B's consume_fifo call —
    issued while A still holds the lock — must BLOCK at the Postgres level
    until A commits. We prove this two ways: (1) directly, by timing how long
    B's call takes (it should take approximately as long as A's held-open
    window, not return instantly); (2) by data integrity, the final
    consumption records must sum to exactly the 2 units the batch actually
    had — no lost update, no phantom double-consumption of a unit that only
    existed once.
    """
    with app.app_context():
        product = Product(name='LockTest Product', product_type='stock_item',
                           price=D('10.00'), base_unit='unit', is_for_sale=True)
        db.session.add(product)
        db.session.flush()
        batch = StockBatch(
            product_id=product.id, qty_purchased_base=D(2), qty_remaining_base=D(2),
            cost_per_base_unit=D('5.000000'), purchased_at=_now(),
        )
        db.session.add(batch)
        db.session.commit()
        product_id = product.id
        batch_id = batch.id
        db.session.remove()

    HOLD_SECONDS = 1.0
    lock_acquired = threading.Event()
    thread_b_elapsed = {}
    errors = []

    def thread_a():
        try:
            with app.app_context():
                consume_fifo(product_id, D(1), 'lock-sale-a', _now())
                # consume_fifo's SELECT ... FOR UPDATE has already executed and
                # returned by this point — the row lock is held at the Postgres
                # level for the remainder of this (uncommitted) transaction.
                lock_acquired.set()
                time.sleep(HOLD_SECONDS)
                db.session.commit()
                db.session.remove()
        except Exception as e:
            errors.append(('A', e))

    def thread_b():
        try:
            lock_acquired.wait(timeout=5)
            with app.app_context():
                t0 = time.monotonic()
                consume_fifo(product_id, D(1), 'lock-sale-b', _now())
                t1 = time.monotonic()
                thread_b_elapsed['seconds'] = t1 - t0
                db.session.commit()
                db.session.remove()
        except Exception as e:
            errors.append(('B', e))

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)

    try:
        assert not errors, f'thread(s) raised: {errors}'
        assert 'seconds' in thread_b_elapsed, 'thread B did not complete'
        # B's call must have been blocked for close to A's hold window — proves
        # real Postgres-level serialization, not two independent, unblocked reads.
        assert thread_b_elapsed['seconds'] >= HOLD_SECONDS * 0.7, (
            f"thread B's consume_fifo returned after only "
            f"{thread_b_elapsed['seconds']:.3f}s — expected it to block for "
            f"most of A's {HOLD_SECONDS}s hold window if the row lock is real."
        )

        with app.app_context():
            final_batch = db.session.get(StockBatch, batch_id)
            consumptions = StockConsumption.query.filter_by(batch_id=batch_id).all()
            total_consumed = sum(D(c.qty_consumed_base) for c in consumptions)
            # Exactly 2 units existed and were consumed — no lost update (which
            # would show the batch still at 1 remaining despite two "successful"
            # normal consumptions), no phantom over-consumption.
            assert D(final_batch.qty_remaining_base) == D(0)
            assert total_consumed == D(2)
            # Both threads got a NORMAL (available-stock) consumption, not one
            # of them falling through to the oversell-shortfall fallback —
            # confirming the lock let each see the true post-A stock level in
            # turn rather than a stale pre-commit read.
            assert len(consumptions) == 2
            assert all(D(c.qty_consumed_base) == D(1) for c in consumptions)
    finally:
        with app.app_context():
            # Rev 5 P2-0: consume_fifo now also writes stock_movements rows FK'd to
            # batch_id — must be purged before the batch itself, or the delete below
            # fails on the FK and leaves this test's rows orphaned for the next run.
            StockMovement.query.filter_by(batch_id=batch_id).delete()
            StockConsumption.query.filter_by(batch_id=batch_id).delete()
            db.session.query(StockBatch).filter_by(id=batch_id).delete()
            db.session.query(Product).filter_by(id=product_id).delete()
            db.session.commit()
            db.session.remove()

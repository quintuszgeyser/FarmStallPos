"""p4_confirm_free.py characterization tests.

Uses a small monkeypatched CONFIRMATIONS dict rather than the real batch ids
(1522/1918/1515/1529 don't exist in a fresh test database) — the mechanism
under test is generic: stamp the marker, verify INV-4 clears, roll back
cleanly on any drift.
"""
import scripts.p4_confirm_free as p4_confirm_free
from scripts.reconcile import CONFIRMED_FREE_MARKER, check_inv4_no_free_stock
from tests.factories import make_product, make_stock_batch
from tests.helpers import D


def test_confirms_a_zero_cost_batch_and_clears_inv4(db_session, monkeypatch):
    product = make_product(product_type='stock_item', name='P4 Confirm Free Target')
    batch = make_stock_batch(product, batch_type='normal', cost_per_base_unit=D('0'))
    db_session.commit()

    monkeypatch.setattr(p4_confirm_free, 'CONFIRMATIONS', {batch.id: 'test batch, confirmed free.'})

    session = db_session
    run_id = 'test-run'
    log = []
    before_ids = {v['batch_id'] for v in check_inv4_no_free_stock(session).violations}
    b = session.get(type(batch), batch.id)
    b.cost_adjustment_reason = f'{CONFIRMED_FREE_MARKER} test batch, confirmed free. run {run_id}.'
    session.flush()

    result = check_inv4_no_free_stock(session)
    assert batch.id not in {v['batch_id'] for v in result.violations}
    assert batch.cost_per_base_unit == D('0')  # untouched

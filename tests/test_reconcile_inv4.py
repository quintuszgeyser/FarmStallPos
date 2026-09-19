"""INV-4 (no free stock) characterization tests (Rev 5 P0-2).

Locks in the owner-confirmed-free exemption: a zero-cost batch whose
cost_adjustment_reason starts with CONFIRMED_FREE_MARKER is not a violation.
Added 2026-09-19 after the owner confirmed specific historical batches
(Water, Springbok Droewors, two Biltong-Kudu batches) were genuinely given
away, not mispriced — see scripts/p4_confirm_free.py for the script that
stamps this marker.
"""
from scripts.reconcile import CONFIRMED_FREE_MARKER, check_inv4_no_free_stock
from tests.factories import make_product, make_stock_batch
from tests.helpers import D


def test_zero_cost_batch_is_a_violation_by_default(db_session):
    product = make_product(product_type='stock_item', name='INV4 Default Zero Cost')
    batch = make_stock_batch(product, batch_type='normal', cost_per_base_unit=D('0'))
    db_session.commit()

    r = check_inv4_no_free_stock(db_session)
    assert batch.id in {v['batch_id'] for v in r.violations}


def test_zero_cost_batch_marked_confirmed_free_is_not_a_violation(db_session):
    product = make_product(product_type='stock_item', name='INV4 Confirmed Free')
    batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        cost_adjustment_reason=f'{CONFIRMED_FREE_MARKER} owner confirmed 2026-09-19, municipal water.',
    )
    db_session.commit()

    r = check_inv4_no_free_stock(db_session)
    assert batch.id not in {v['batch_id'] for v in r.violations}
    assert r.skipped_count == 1


def test_a_reason_that_merely_mentions_free_without_the_exact_marker_still_violates(db_session):
    # The marker must be the exact prefix — a loose text match would let any batch with
    # "free" somewhere in an unrelated note quietly escape this check.
    product = make_product(product_type='stock_item', name='INV4 Loose Text Not Exempt')
    batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        cost_adjustment_reason='This item is sometimes given away for free to regulars.',
    )
    db_session.commit()

    r = check_inv4_no_free_stock(db_session)
    assert batch.id in {v['batch_id'] for v in r.violations}

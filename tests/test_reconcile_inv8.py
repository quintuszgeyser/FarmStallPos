"""INV-8 (value closure) characterization tests (Rev 5 P0-2).

Locks in the P4-1 fix to check_inv8_value_baseline: a batch_type=
'negative_placeholder' row is supposed to carry a negative qty_remaining_base
and a zero cost_per_base_unit by design (see StockBatch's own model comment
and P2-1's oversell handling in helpers.py) — it must not be flagged as a
violation. The pre-fix version flagged every negative-placeholder batch in
the 2026-09-19 production baseline (19 of them, dated across a month of real
oversells), which would have led to "repairing" legitimate, currently-
correct oversell tracking had it not been caught before anything was written.
"""
from decimal import Decimal

from scripts.reconcile import check_inv8_value_baseline
from tests.factories import make_product, make_stock_batch
from tests.helpers import D


def test_negative_placeholder_batch_is_not_a_violation(db_session):
    product = make_product(product_type='stock_item', name='INV8 Placeholder')
    make_stock_batch(
        product,
        qty_purchased_base=D('-18'),
        qty_remaining_base=D('-18'),
        cost_per_base_unit=D('0.000000'),
        batch_type='negative_placeholder',
    )
    db_session.commit()

    r = check_inv8_value_baseline(db_session)
    assert r.violations == []
    assert r.status == 'PASS'


def test_negative_qty_on_a_normal_batch_is_still_a_violation(db_session):
    product = make_product(product_type='stock_item', name='INV8 Real Negative')
    batch = make_stock_batch(
        product,
        qty_purchased_base=D('10'),
        qty_remaining_base=D('-5'),
        cost_per_base_unit=D('3.000000'),
        batch_type='normal',
    )
    db_session.commit()

    r = check_inv8_value_baseline(db_session)
    matching = [v for v in r.violations if v['batch_id'] == batch.id]
    assert len(matching) == 1
    assert matching[0]['issue'] == 'negative qty_remaining_base'
    assert r.status == 'FAIL'


def test_negative_placeholder_is_excluded_from_the_valuation_total(db_session):
    product = make_product(product_type='stock_item', name='INV8 Placeholder Value')
    make_stock_batch(
        product,
        qty_purchased_base=D('-9'),
        qty_remaining_base=D('-9'),
        cost_per_base_unit=D('0.000000'),
        batch_type='negative_placeholder',
    )
    make_stock_batch(
        product,
        qty_purchased_base=D('10'),
        qty_remaining_base=D('10'),
        cost_per_base_unit=D('2.000000'),
        batch_type='normal',
    )
    db_session.commit()

    r = check_inv8_value_baseline(db_session)
    assert r.violations == []
    assert 'R20.00' in r.note  # only the normal batch's value counted
    assert f'across 1 products' in r.note

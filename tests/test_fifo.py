"""FIFO consumption characterization tests (Rev 5 P0-1 / Section 5).

Exercises the real helpers.consume_fifo / reverse_fifo — not mocked. Each test
asserts actual DB state after the call, not just a return value.
"""
from datetime import datetime, UTC

import pytest

from helpers import consume_fifo, reverse_fifo
from models import StockBatch, StockConsumption
from tests.factories import make_product
from tests.helpers import D


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def test_normal_fifo_consumption_single_batch_partial(db_session):
    product = make_product(product_type='stock_item')
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(10), qty_remaining_base=D(10),
        cost_per_base_unit=D('2.500000'), purchased_at=_now(),
    )
    db_session.add(batch)
    db_session.flush()

    cost = consume_fifo(product.id, D(4), 'sale-1', _now())

    assert cost == D('10.00')  # 4 * 2.50
    db_session.flush()
    db_session.refresh(batch)
    assert batch.qty_remaining_base == D(6)
    consumptions = StockConsumption.query.filter_by(sale_id='sale-1').all()
    assert len(consumptions) == 1
    assert consumptions[0].qty_consumed_base == D(4)
    assert consumptions[0].batch_id == batch.id


def test_exact_depletion_to_zero(db_session):
    product = make_product(product_type='stock_item')
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(5), qty_remaining_base=D(5),
        cost_per_base_unit=D('1.000000'), purchased_at=_now(),
    )
    db_session.add(batch)
    db_session.flush()

    consume_fifo(product.id, D(5), 'sale-2', _now())

    db_session.flush()
    db_session.refresh(batch)
    assert batch.qty_remaining_base == D(0)
    # Batch is fully depleted but not deleted — remains for cost history.
    assert db_session.get(StockBatch, batch.id) is not None


def test_multiple_fifo_batches_consumed_in_purchase_order(db_session):
    product = make_product(product_type='stock_item')
    older = StockBatch(
        product_id=product.id, qty_purchased_base=D(3), qty_remaining_base=D(3),
        cost_per_base_unit=D('1.000000'), purchased_at=datetime(2026, 1, 1),
    )
    newer = StockBatch(
        product_id=product.id, qty_purchased_base=D(10), qty_remaining_base=D(10),
        cost_per_base_unit=D('9.000000'), purchased_at=datetime(2026, 2, 1),
    )
    db_session.add_all([older, newer])
    db_session.flush()

    # Needs 5: exhausts the older batch (3 @ 1.00) then 2 from the newer (2 @ 9.00).
    cost = consume_fifo(product.id, D(5), 'sale-3', _now())

    assert cost == D('21.00')  # 3*1 + 2*9
    db_session.flush()
    db_session.refresh(older)
    db_session.flush()
    db_session.refresh(newer)
    assert older.qty_remaining_base == D(0)
    assert newer.qty_remaining_base == D(8)
    consumptions = {c.batch_id: c.qty_consumed_base
                    for c in StockConsumption.query.filter_by(sale_id='sale-3').all()}
    assert consumptions[older.id] == D(3)
    assert consumptions[newer.id] == D(2)


@pytest.mark.known_defect
def test_direct_item_oversell_posts_shortfall_to_last_historical_batch(db_session):
    """KNOWN DEFECT — Rev 5 P2-1 fixes this. TODAY, when FIFO runs out, the
    shortfall is posted against the LAST historical positive-cost batch
    (helpers.py ~305-322), silently over-consuming it below zero, instead of a
    dedicated negative placeholder. This test pins that behavior so P2-1's
    commit is the one that changes this assertion, not a surprise regression.
    """
    product = make_product(product_type='stock_item')
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(2), qty_remaining_base=D(2),
        cost_per_base_unit=D('4.000000'), purchased_at=_now(),
    )
    db_session.add(batch)
    db_session.flush()

    cost = consume_fifo(product.id, D(5), 'sale-4', _now())  # need 5, only 2 available

    # 2 covered normally + 3 shortfall costed at the same batch's cost (only batch that qualifies)
    assert cost == D('20.00')  # (2*4) + (3*4)
    db_session.flush()
    db_session.refresh(batch)
    # The primary FIFO loop drains the batch to 0 covering the first 2 units normally;
    # the shortfall fallback then reuses the SAME batch for costing (it's the only
    # positive-cost batch) but never decrements it further — so it lands at exactly
    # 0, not negative, DESPITE 5 units of COGS having been recognized against it.
    # That gap (5 consumed vs 2 ever actually decremented) is the defect P2-1 fixes.
    assert batch.qty_remaining_base == D(0)
    consumptions = StockConsumption.query.filter_by(sale_id='sale-4').all()
    assert len(consumptions) == 2
    shortfall_row = [c for c in consumptions if c.qty_consumed_base == D(3)][0]
    assert shortfall_row.batch_id == batch.id  # shortfall attached to the SAME historical batch


@pytest.mark.known_defect
def test_recipe_oversell_surfaces_same_shortfall_defect(db_session):
    """KNOWN DEFECT — Rev 5 P2-1. A made-to-order recipe oversells its ingredient
    through the same consume_fifo() shortfall path."""
    ingredient = make_product(product_type='stock_item', name='Flour')
    recipe = make_product(product_type='recipe', name='Bread', is_produced=False)
    from tests.factories import make_recipe_line
    make_recipe_line(recipe, ingredient, qty_base=D(1))

    batch = StockBatch(
        product_id=ingredient.id, qty_purchased_base=D(1), qty_remaining_base=D(1),
        cost_per_base_unit=D('3.000000'), purchased_at=_now(),
    )
    db_session.add(batch)
    db_session.flush()

    # Sell 4 loaves needing 4 units of flour, only 1 in stock.
    cost = consume_fifo(recipe.id, D(4), 'sale-5', _now())

    assert cost == D('12.00')  # 4 * 3.00, all costed at the same batch's cost
    db_session.flush()
    db_session.refresh(batch)
    # Same mechanism as the direct-item case: the primary loop drains the 1 available
    # unit to 0, and the 3-unit shortfall is costed against it without further decrement.
    assert batch.qty_remaining_base == D(0)


def test_reverse_fifo_restores_quantity_and_deletes_consumption_rows(db_session):
    product = make_product(product_type='stock_item')
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(10), qty_remaining_base=D(10),
        cost_per_base_unit=D('2.000000'), purchased_at=_now(),
    )
    db_session.add(batch)
    db_session.flush()

    consume_fifo(product.id, D(4), 'sale-6', _now())
    db_session.flush()
    db_session.refresh(batch)
    assert batch.qty_remaining_base == D(6)

    reverse_fifo('sale-6')

    db_session.flush()
    db_session.refresh(batch)
    assert batch.qty_remaining_base == D(10)
    assert StockConsumption.query.filter_by(sale_id='sale-6').count() == 0

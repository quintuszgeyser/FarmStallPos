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


def test_direct_item_oversell_posts_shortfall_to_negative_placeholder(db_session):
    """Rev 5 P2-1. When FIFO runs out, the shortfall posts to a dedicated
    negative-placeholder batch (carrying the cost estimate), never to a
    historical batch — replaces the old known_defect pinning the opposite.
    """
    product = make_product(product_type='stock_item')
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(2), qty_remaining_base=D(2),
        cost_per_base_unit=D('4.000000'), purchased_at=_now(),
    )
    db_session.add(batch)
    db_session.flush()

    cost = consume_fifo(product.id, D(5), 'sale-4', _now())  # need 5, only 2 available

    # 2 covered normally + 3 shortfall costed at the last known batch's cost (the estimate)
    assert cost == D('20.00')  # (2*4) + (3*4)
    db_session.flush()
    db_session.refresh(batch)
    # The original historical batch is untouched by the shortfall — it only ever
    # reflects the 2 units genuinely taken from it.
    assert batch.qty_remaining_base == D(0)

    placeholder = StockBatch.query.filter_by(
        product_id=product.id, batch_type='negative_placeholder').first()
    assert placeholder is not None
    assert placeholder.qty_remaining_base == D(-3)
    assert placeholder.qty_purchased_base == D(-3)
    assert placeholder.cost_per_base_unit == D('0')
    assert placeholder.estimated_unit_cost == D('4.000000')
    assert placeholder.cost_estimation_method == 'last_known_batch'
    assert placeholder.cost_reconciled is False

    consumptions = StockConsumption.query.filter_by(sale_id='sale-4').all()
    assert len(consumptions) == 2
    shortfall_row = [c for c in consumptions if c.qty_consumed_base == D(3)][0]
    assert shortfall_row.batch_id == placeholder.id  # not the historical batch


def test_recipe_oversell_posts_to_negative_placeholder_too(db_session):
    """Rev 5 P2-1. A made-to-order recipe oversells its ingredient through the
    same consume_fifo() shortfall path — same fix applies without any
    recipe-specific code, since the fix lives inside consume_fifo itself.
    """
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

    assert cost == D('12.00')  # 4 * 3.00, all costed at the estimate
    db_session.flush()
    db_session.refresh(batch)
    assert batch.qty_remaining_base == D(0)

    placeholder = StockBatch.query.filter_by(
        product_id=ingredient.id, batch_type='negative_placeholder').first()
    assert placeholder is not None
    assert placeholder.qty_remaining_base == D(-3)
    assert placeholder.estimated_unit_cost == D('3.000000')


def test_nested_recipe_oversell_posts_to_the_ingredients_own_placeholder(db_session):
    """Rev 5 P2-1. A recipe-within-a-recipe (compound ingredient) oversells its
    own raw ingredient — consume_fifo recurses into the sub-recipe, and the
    shortfall still lands on the raw ingredient's placeholder, not a historical
    batch of either the sub-recipe or the raw ingredient.
    """
    from tests.factories import make_recipe_line

    flour = make_product(product_type='stock_item', name='Flour')
    dough = make_product(product_type='recipe', name='Dough', is_produced=False)
    bread = make_product(product_type='recipe', name='Bread', is_produced=False)
    make_recipe_line(dough, flour, qty_base=D(2))
    make_recipe_line(bread, dough, qty_base=D(1))

    flour_batch = StockBatch(
        product_id=flour.id, qty_purchased_base=D(3), qty_remaining_base=D(3),
        cost_per_base_unit=D('1.500000'), purchased_at=_now(),
    )
    db_session.add(flour_batch)
    db_session.flush()

    # 1 loaf of bread needs 1 dough needs 2 flour; only 3 flour in stock, needs 2 — no
    # shortfall on flour yet. Bump to 3 loaves so flour needed (6) exceeds the 3 on hand.
    cost = consume_fifo(bread.id, D(3), 'sale-6', _now())

    assert cost == D('9.00')  # 6 units flour * 1.50, all costed at the estimate
    db_session.flush()
    db_session.refresh(flour_batch)
    assert flour_batch.qty_remaining_base == D(0)

    placeholder = StockBatch.query.filter_by(
        product_id=flour.id, batch_type='negative_placeholder').first()
    assert placeholder is not None
    assert placeholder.qty_remaining_base == D(-3)
    assert placeholder.estimated_unit_cost == D('1.500000')


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

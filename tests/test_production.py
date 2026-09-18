"""Production (batch-produce) characterization tests (Rev 5 P0-1 / Section 5) —
covers both "production input" (ingredient consumption) and "production output"
(finished-goods StockBatch creation) through the real /api/products/<id>/produce
route, since this codebase implements them as one atomic operation, not two.
"""
from decimal import Decimal

from werkzeug.security import generate_password_hash

from models import StockAdjustment, StockBatch, StockConsumption
from tests.factories import make_admin, make_product, make_recipe_line, make_stock_batch
from tests.helpers import D, login_as, refresh


def _login_admin(client, username='produceadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def test_produce_consumes_ingredients_and_creates_output_batch(db_session, client):
    flour = make_product(product_type='stock_item', name='Flour', price=D('1.00'))
    flour_batch = make_stock_batch(flour, qty_remaining_base=D(50), qty_purchased_base=D(50),
                                    cost_per_base_unit=D('2.000000'))

    bread = make_product(product_type='recipe', name='Batch Bread', is_produced=True,
                          batch_size=D('4'), price=D('15.00'))
    make_recipe_line(bread, flour, qty_base=D(1))  # 1 unit flour per finished loaf

    _login_admin(client)

    resp = client.post(f'/api/products/{bread.id}/produce', json={'batches': 1})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['ok'] is True
    assert body['units_added'] == 4  # batch_size=4, 1 batch run

    # Production INPUT: flour consumed = qty_base(1) * batches(1) * batch_size... no —
    # consume_fifo is called with rl.qty_base * batches directly (products.py:1219),
    # so it's qty_base(1) * batches(1) = 1 unit of flour, NOT scaled by batch_size.
    refresh(db_session, flour_batch)
    assert flour_batch.qty_remaining_base == D(49)
    consumptions = StockConsumption.query.filter_by(ingredient_id=flour.id).all()
    assert len(consumptions) == 1
    assert consumptions[0].qty_consumed_base == D(1)

    # Production OUTPUT: a new finished-goods StockBatch for the recipe product.
    output_batches = StockBatch.query.filter_by(product_id=bread.id).all()
    assert len(output_batches) == 1
    assert output_batches[0].qty_purchased_base == D(4)
    assert output_batches[0].qty_remaining_base == D(4)
    assert output_batches[0].produce_cost == D('2.0000')  # 1 unit flour * 2.00

    adj = StockAdjustment.query.filter_by(product_id=bread.id, adjustment_type='produce').first()
    assert adj is not None
    assert adj.qty_change_base == D(4)


def test_produce_reconciles_outstanding_negative_placeholder(db_session, client):
    """Produce absorbs an existing negative-placeholder batch (created by an
    earlier oversold ALLOW_NEGATIVE sale) into the new output batch instead of
    leaving the negative alongside the new positive stock (products.py ~1262-1272).
    """
    from models import db, Product
    flour = make_product(product_type='stock_item', name='Flour2', price=D('1.00'))
    make_stock_batch(flour, qty_remaining_base=D(50), qty_purchased_base=D(50), cost_per_base_unit=D('2.000000'))

    bread = make_product(product_type='recipe', name='Batch Bread 2', is_produced=True,
                          batch_size=D('4'), price=D('15.00'))
    make_recipe_line(bread, flour, qty_base=D(1))

    # Simulate a prior oversold placeholder: -2 units outstanding on Bread.
    db.session.add(StockBatch(
        product_id=bread.id, qty_purchased_base=D(-2), qty_remaining_base=D(-2),
        cost_per_base_unit=D('0'), batch_type='negative_placeholder',
    ))
    db.session.flush()

    _login_admin(client)
    resp = client.post(f'/api/products/{bread.id}/produce', json={'batches': 1})
    assert resp.status_code == 200, resp.get_json()

    output_batches = StockBatch.query.filter_by(product_id=bread.id, batch_type='normal').all()
    assert len(output_batches) == 1
    # 4 produced, 2 absorbed into the negative placeholder -> net 2 remaining on the new batch.
    assert output_batches[0].qty_remaining_base == D(2)

    placeholder = StockBatch.query.filter_by(product_id=bread.id, batch_type='negative_placeholder').one()
    refresh(db_session, placeholder)
    assert placeholder.qty_remaining_base == D(0)

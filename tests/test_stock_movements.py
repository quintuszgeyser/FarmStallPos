"""stock_movements schema + dual-write characterization tests (Rev 5 P2-0).

The table is additive and not yet authoritative anywhere — StockBatch.qty_
remaining_base remains the real source of truth for every read/write path.
See StockMovement's and write_stock_movement's docstrings in models.py /
helpers.py for the coverage map: which paths dual-write today and which are
still pending.
"""
from decimal import Decimal

from models import db, StockBatch, StockMovement
from tests.factories import make_admin, make_product, make_stock_batch, make_stock_movement
from tests.helpers import D, checkout, login_as


def test_stock_movement_round_trips_all_columns(db_session):
    product = make_product(product_type='stock_item', name='Ledger Item')
    batch = make_stock_batch(product)

    movement = make_stock_movement(
        batch,
        movement_type='RECEIPT',
        qty_delta=D(10),
        unit_cost=D('5.000000'),
        source_type='receipt',
        source_id=str(batch.id),
        source_line_id=None,
        note=None,
    )

    fetched = db.session.get(StockMovement, movement.id)
    assert fetched.movement_type == 'RECEIPT'
    assert fetched.batch_id == batch.id
    assert fetched.qty_delta == D(10)
    assert fetched.unit_cost == D('5.000000')
    assert fetched.source_type == 'receipt'
    assert fetched.source_id == str(batch.id)
    assert fetched.source_line_id is None
    assert fetched.created_at is not None


def test_stock_movement_requires_a_live_batch(db_session):
    """INV-2 proxy: batch_id is a real FK, not just a convention."""
    product = make_product(product_type='stock_item', name='Ledger Item 2')
    batch = make_stock_batch(product)
    movement = make_stock_movement(batch)
    assert movement.batch_id == batch.id
    # The column is declared NOT NULL + FK in both models.py and the migration;
    # asserting the declared nullable/type here catches a drift between the two
    # without needing to provoke a live IntegrityError inside the SAVEPOINT-
    # isolated test transaction (see conftest.py's isolation note).
    col = StockMovement.__table__.c.batch_id
    assert col.nullable is False
    assert len(col.foreign_keys) == 1


def test_stock_movement_ordering_columns_exist():
    """P2-0a requires a deterministic replay order on (created_at, id)."""
    cols = StockMovement.__table__.c
    assert 'created_at' in cols
    assert 'id' in cols


def test_checkout_writes_a_sale_movement_alongside_the_existing_consumption(db_session, client):
    """consume_fifo dual-writes a SALE movement per batch consumed — the first
    write path wired. StockBatch/StockConsumption stay authoritative; this
    just proves the ledger is now observing them correctly.
    """
    from werkzeug.security import generate_password_hash
    make_admin(username='ledgeradmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'ledgeradmin', 'adminpass123')

    product = make_product(product_type='stock_item', name='Dual Write Canary', price=D('10.00'))
    batch = make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                              cost_per_base_unit=D('4.000000'))
    db_session.commit()

    resp = checkout(client, [{'product_id': product.id, 'qty': 1, 'unit_price': 10}])
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']

    movements = StockMovement.query.filter_by(batch_id=batch.id).all()
    assert len(movements) == 1
    m = movements[0]
    assert m.movement_type == 'SALE'
    assert m.source_type == 'sale'
    assert m.source_id == sale_id
    assert m.qty_delta == D(-1)
    assert m.unit_cost == D('4.000000')


def test_void_writes_a_reversal_movement(db_session, client):
    """reverse_fifo dual-writes a SALE_REVERSAL movement restoring the batch."""
    from werkzeug.security import generate_password_hash
    make_admin(username='voidadmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'voidadmin', 'adminpass123')

    product = make_product(product_type='stock_item', name='Void Canary', price=D('10.00'))
    batch = make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                              cost_per_base_unit=D('4.000000'))
    db_session.commit()

    resp = checkout(client, [{'product_id': product.id, 'qty': 1, 'unit_price': 10}])
    sale_id = resp.get_json()['transaction_id']

    void_resp = client.post(f'/api/transactions/{sale_id}/void', json={'reason': 'test void'})
    assert void_resp.status_code == 200, void_resp.get_json()

    movements = StockMovement.query.filter_by(batch_id=batch.id).order_by(StockMovement.id).all()
    assert [m.movement_type for m in movements] == ['SALE', 'SALE_REVERSAL']
    assert movements[1].qty_delta == D(1)
    assert movements[1].source_type == 'sale'
    assert movements[1].source_id == sale_id


def test_stock_receive_writes_a_receipt_movement(db_session, client):
    from werkzeug.security import generate_password_hash
    make_admin(username='receiveadmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'receiveadmin', 'adminpass123')

    product = make_product(product_type='stock_item', name='Receive Canary')
    db_session.commit()

    resp = client.post('/api/stock/receive', json={
        'product_id': product.id, 'qty': 20, 'unit': 'unit', 'total_price': 100,
    })
    assert resp.status_code == 200, resp.get_json()
    batch_id = resp.get_json()['batch_id']

    movements = StockMovement.query.filter_by(batch_id=batch_id).all()
    assert len(movements) == 1
    assert movements[0].movement_type == 'RECEIPT'
    assert movements[0].source_type == 'receipt'
    assert movements[0].qty_delta == D(20)
    assert movements[0].unit_cost == D('5.000000')


def test_positive_stocktake_writes_an_increase_movement(db_session, client):
    from werkzeug.security import generate_password_hash
    make_admin(username='stocktakeadmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'stocktakeadmin', 'adminpass123')

    product = make_product(product_type='stock_item', name='Stocktake Canary')
    make_stock_batch(product, qty_remaining_base=D(5), qty_purchased_base=D(5),
                      cost_per_base_unit=D('3.000000'))
    db_session.commit()

    resp = client.post('/api/stock/adjust', json={
        'product_id': product.id, 'actual_qty': 8, 'unit': 'unit', 'reason': 'stocktake found extra',
    })
    assert resp.status_code == 200, resp.get_json()

    new_batches = StockBatch.query.filter_by(product_id=product.id, cost_per_base_unit=D('0')).all()
    assert len(new_batches) == 1
    movements = StockMovement.query.filter_by(batch_id=new_batches[0].id).all()
    assert len(movements) == 1
    assert movements[0].movement_type == 'STOCKTAKE_INCREASE'
    assert movements[0].source_type == 'stocktake'
    assert movements[0].qty_delta == D(3)


def test_return_writes_a_return_movement(db_session, client):
    from werkzeug.security import generate_password_hash
    make_admin(username='returncanary', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'returncanary', 'adminpass123')

    product = make_product(product_type='stock_item', name='Return Canary', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'))
    db_session.commit()

    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50)
    sale_id = resp.get_json()['transaction_id']

    ret = client.post(f'/api/transactions/{sale_id}/return',
                       json={'lines': [{'product_id': product.id, 'qty': 5}], 'reason': 'test return'})
    assert ret.status_code == 200, ret.get_json()
    return_id = ret.get_json()['return_id']

    new_batch = StockBatch.query.filter_by(product_id=product.id, qty_purchased_base=D(5)).first()
    assert new_batch is not None
    movements = StockMovement.query.filter_by(batch_id=new_batch.id).all()
    assert len(movements) == 1
    assert movements[0].movement_type == 'RETURN_SALEABLE'
    assert movements[0].source_type == 'return'
    assert movements[0].source_id == return_id
    assert movements[0].qty_delta == D(5)


def test_produce_writes_a_production_output_movement(db_session, client):
    from werkzeug.security import generate_password_hash
    from tests.factories import make_recipe_line

    make_admin(username='produceadmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'produceadmin', 'adminpass123')

    flour = make_product(product_type='stock_item', name='Flour Canary', price=D('1.00'))
    make_stock_batch(flour, qty_remaining_base=D(50), qty_purchased_base=D(50), cost_per_base_unit=D('2.000000'))
    bread = make_product(product_type='recipe', name='Produce Canary', is_produced=True,
                          batch_size=D('4'), price=D('15.00'))
    make_recipe_line(bread, flour, qty_base=D(1))
    db_session.commit()

    resp = client.post(f'/api/products/{bread.id}/produce', json={'batches': 1})
    assert resp.status_code == 200, resp.get_json()

    output_batch = StockBatch.query.filter_by(product_id=bread.id).first()
    assert output_batch is not None
    movements = StockMovement.query.filter_by(batch_id=output_batch.id).all()
    assert len(movements) == 1
    assert movements[0].movement_type == 'PRODUCTION_OUTPUT'
    assert movements[0].source_type == 'production'
    assert movements[0].qty_delta == D(4)

    # consume_fifo is called with rl.qty_base * batches directly (not scaled by
    # batch_size — see test_production.py's note on this), so 1 unit of flour
    # is consumed per batch run here, not 4.
    flour_movements = StockMovement.query.join(
        StockBatch, StockMovement.batch_id == StockBatch.id
    ).filter(StockBatch.product_id == flour.id).all()
    assert len(flour_movements) == 1
    assert flour_movements[0].movement_type == 'PRODUCTION'
    assert flour_movements[0].qty_delta == D(-1)

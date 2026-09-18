"""stock_movements schema characterization tests (Rev 5 P2-0, additive step).

The table exists but nothing writes to it yet — see StockMovement's docstring
in models.py for why. These tests pin that "exists but inert" state so the
commit that wires the first dual-write is the one that changes the assertion
in test_checkout_does_not_yet_write_stock_movements below, not a surprise.
"""
from decimal import Decimal

from models import db, StockMovement
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


def test_checkout_does_not_yet_write_stock_movements(db_session, client):
    """Dual-write has not shipped yet. A normal sale still only touches
    StockBatch/StockConsumption, exactly as before P2-0. This test is the
    canary: the commit that adds the first dual-write must update it.
    """
    from werkzeug.security import generate_password_hash
    make_admin(username='ledgeradmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'ledgeradmin', 'adminpass123')

    product = make_product(product_type='stock_item', name='Dual Write Canary', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'))
    db_session.commit()

    resp = checkout(client, [{'product_id': product.id, 'qty': 1, 'unit_price': 10}])
    assert resp.status_code == 200, resp.get_json()

    assert StockMovement.query.count() == 0

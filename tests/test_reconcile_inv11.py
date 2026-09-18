"""INV-11 (projection rebuild) characterization tests (Rev 5 P2-0a).

check_inv11_projection_rebuild() only needs a SQLAlchemy session exposing
.query() — the pytest db_session fixture's Flask-SQLAlchemy session works
fine for it, even though scripts/reconcile.py normally opens its own engine
directly against DATABASE_URL (see that script's docstring for why: read-
only safety against production, not a dependency on the Flask app).
"""
from decimal import Decimal

from scripts.reconcile import check_inv11_projection_rebuild
from tests.factories import make_product, make_stock_batch, make_stock_movement
from tests.helpers import D


def test_batch_with_zero_movements_is_skipped_not_a_violation(db_session):
    product = make_product(product_type='stock_item', name='INV11 No Movements')
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10))
    db_session.commit()

    r = check_inv11_projection_rebuild(db_session)
    assert r.skipped_count >= 1
    assert not any(v for v in r.violations if v.get('product_id') == product.id)


def test_batch_whose_movements_sum_to_live_qty_passes(db_session):
    product = make_product(product_type='stock_item', name='INV11 Agrees')
    batch = make_stock_batch(product, qty_remaining_base=D(6), qty_purchased_base=D(10),
                              cost_per_base_unit=D('2.000000'))
    make_stock_movement(batch, movement_type='RECEIPT', qty_delta=D(10),
                         unit_cost=D('2.000000'), source_type='receipt', source_id=str(batch.id))
    make_stock_movement(batch, movement_type='SALE', qty_delta=D(-4),
                         unit_cost=D('2.000000'), source_type='sale', source_id='fake-sale')
    db_session.commit()

    r = check_inv11_projection_rebuild(db_session)
    matching = [v for v in r.violations if v.get('batch_id') == batch.id]
    assert not matching


def test_batch_whose_movements_disagree_with_live_qty_is_a_violation(db_session):
    """The case this gate exists to catch: a movement was written wrong (or
    the batch was mutated directly without a matching movement)."""
    product = make_product(product_type='stock_item', name='INV11 Disagrees')
    batch = make_stock_batch(product, qty_remaining_base=D(6), qty_purchased_base=D(10),
                              cost_per_base_unit=D('2.000000'))
    make_stock_movement(batch, movement_type='RECEIPT', qty_delta=D(10),
                         unit_cost=D('2.000000'), source_type='receipt', source_id=str(batch.id))
    # Missing the -4 SALE movement a real consumption would have written — ledger
    # sum (10) now disagrees with the live qty_remaining_base (6).
    db_session.commit()

    r = check_inv11_projection_rebuild(db_session)
    matching = [v for v in r.violations if v.get('batch_id') == batch.id]
    assert len(matching) == 1
    assert matching[0]['ledger_sum'] == '10.0000'
    assert matching[0]['live_qty_remaining_base'] == '6.0000'
    assert r.status == 'FAIL'

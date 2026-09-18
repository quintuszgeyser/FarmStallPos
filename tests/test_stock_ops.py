"""Stock receive / stocktake / write-off characterization tests (Rev 5 P0-1 /
Section 5). Routes exercised through the real Flask test client.
"""
import pytest
from werkzeug.security import generate_password_hash

from models import StockAdjustment, StockBatch
from tests.factories import make_admin, make_product
from tests.helpers import D, login_as


def _login_admin(client, username='stockadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def test_stock_receive_creates_batch_at_correct_cost(db_session, client):
    product = make_product(product_type='stock_item', name='Receivable Item')
    _login_admin(client)

    resp = client.post('/api/stock/receive', json={
        'product_id': product.id, 'qty': 20, 'unit': 'unit', 'total_price': 100,
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['ok'] is True

    batches = StockBatch.query.filter_by(product_id=product.id).all()
    assert len(batches) == 1
    batch = batches[0]
    assert batch.qty_purchased_base == D(20)
    assert batch.qty_remaining_base == D(20)
    assert batch.cost_per_base_unit == D('5.000000')  # 100 / 20
    assert batch.ownership_type == 'NORMAL'


def test_positive_stocktake_variance_books_at_weighted_average_cost(db_session, client):
    """Rev 5 P2-3. A positive stocktake variance (found stock exceeding the
    system quantity) books at the current weighted-average open-batch cost,
    not zero — booking at zero understated inventory value and then
    overstated gross profit the moment that "free" stock sold at zero COGS.
    """
    product = make_product(product_type='stock_item', name='Stocktake Item')
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(5), qty_remaining_base=D(5),
        cost_per_base_unit=D('3.000000'),
    )
    from models import db
    db.session.add(batch)
    db.session.flush()

    _login_admin(client)

    # Physical count finds 8, system shows 5 -> +3 positive variance.
    resp = client.post('/api/stock/adjust', json={
        'product_id': product.id, 'actual_qty': 8, 'unit': 'unit', 'reason': 'stocktake count',
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['difference'] == 3.0

    adj = StockAdjustment.query.filter_by(product_id=product.id, adjustment_type='stocktake').first()
    assert adj is not None
    assert adj.qty_change_base == D(3)

    new_batches = StockBatch.query.filter_by(product_id=product.id).filter(StockBatch.id != batch.id).all()
    assert len(new_batches) == 1
    # Only one open batch (5 @ 3.00) -> weighted average is just 3.00.
    assert new_batches[0].cost_per_base_unit == D('3.000000')
    assert new_batches[0].qty_remaining_base == D(3)


def test_positive_stocktake_variance_averages_across_multiple_open_batches(db_session, client):
    product = make_product(product_type='stock_item', name='Weighted Avg Item')
    from models import db
    db.session.add(StockBatch(product_id=product.id, qty_purchased_base=D(4), qty_remaining_base=D(4),
                               cost_per_base_unit=D('2.000000')))
    db.session.add(StockBatch(product_id=product.id, qty_purchased_base=D(6), qty_remaining_base=D(6),
                               cost_per_base_unit=D('4.000000')))
    db.session.flush()
    # system = 10 (4 + 6). Weighted average = (4*2 + 6*4) / 10 = 32/10 = 3.20.

    _login_admin(client)
    resp = client.post('/api/stock/adjust', json={
        'product_id': product.id, 'actual_qty': 15, 'unit': 'unit', 'reason': 'recount',
    })
    assert resp.status_code == 200, resp.get_json()

    new_batch = StockBatch.query.filter_by(product_id=product.id, qty_purchased_base=D(5)).one()
    assert new_batch.cost_per_base_unit == D('3.200000')


def test_positive_stocktake_variance_falls_back_to_last_purchase_cost_when_sold_out(db_session, client):
    """No open batches (product sold out) — fall back to the last known
    purchase cost rather than zero."""
    product = make_product(product_type='stock_item', name='Sold Out Item')
    from models import db
    db.session.add(StockBatch(product_id=product.id, qty_purchased_base=D(5), qty_remaining_base=D(0),
                               cost_per_base_unit=D('7.000000')))
    db.session.flush()

    _login_admin(client)
    resp = client.post('/api/stock/adjust', json={
        'product_id': product.id, 'actual_qty': 2, 'unit': 'unit', 'reason': 'found more stock',
    })
    assert resp.status_code == 200, resp.get_json()

    new_batch = StockBatch.query.filter_by(product_id=product.id, qty_purchased_base=D(2)).one()
    assert new_batch.cost_per_base_unit == D('7.000000')


def test_positive_stocktake_variance_free_stock_opt_out_books_at_zero(db_session, client):
    """free_stock=true is the explicit, deliberate opt-out — never the default."""
    product = make_product(product_type='stock_item', name='Free Sample Item')
    from models import db
    db.session.add(StockBatch(product_id=product.id, qty_purchased_base=D(5), qty_remaining_base=D(5),
                               cost_per_base_unit=D('9.000000')))
    db.session.flush()

    _login_admin(client)
    resp = client.post('/api/stock/adjust', json={
        'product_id': product.id, 'actual_qty': 8, 'unit': 'unit', 'reason': 'donated stock',
        'free_stock': True,
    })
    assert resp.status_code == 200, resp.get_json()

    new_batch = StockBatch.query.filter_by(product_id=product.id, qty_purchased_base=D(3)).one()
    assert new_batch.cost_per_base_unit == D('0.000000')


def test_negative_stocktake_variance_is_a_writeoff_consuming_fifo(db_session, client):
    product = make_product(product_type='stock_item', name='Shrinkage Item')
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(10), qty_remaining_base=D(10),
        cost_per_base_unit=D('2.000000'),
    )
    from models import db
    db.session.add(batch)
    db.session.flush()

    _login_admin(client)

    # Physical count finds 6, system shows 10 -> -4 negative variance (shrinkage).
    resp = client.post('/api/stock/adjust', json={
        'product_id': product.id, 'actual_qty': 6, 'unit': 'unit', 'reason': 'stocktake count',
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['difference'] == -4.0

    adj = StockAdjustment.query.filter_by(product_id=product.id, adjustment_type='writeoff').first()
    assert adj is not None
    assert adj.qty_change_base == D(-4)
    assert adj.cost_written_off == D('8.00')  # 4 * 2.00, via consume_fifo(is_writeoff=True)

    from tests.helpers import refresh
    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(6)


def test_writeoff_endpoint_reduces_stock_and_records_cost(db_session, client):
    product = make_product(product_type='stock_item', name='Damaged Item')
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(10), qty_remaining_base=D(10),
        cost_per_base_unit=D('4.000000'),
    )
    from models import db
    db.session.add(batch)
    db.session.flush()

    _login_admin(client)

    resp = client.post('/api/stock/writeoff', json={
        'product_id': product.id, 'qty': 2, 'unit': 'unit', 'reason': 'damaged in transit',
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['qty_written_off'] == 2.0
    assert body['cost_written_off'] == 8.0  # 2 * 4.00

    adj = StockAdjustment.query.filter_by(product_id=product.id, adjustment_type='writeoff').first()
    assert adj is not None
    assert adj.reason == 'damaged in transit'
    assert adj.qty_change_base == D(-2)

    from tests.helpers import refresh
    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(8)

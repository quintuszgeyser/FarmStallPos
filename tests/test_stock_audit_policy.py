"""Rev 5 P3-1b — proof that every AUDITED route in the newly-adopted `stock`
blueprint actually writes an AuditLog row on completion, not just that it
declares the policy. tests/test_mutation_registry.py checks the static
declaration; these exercise the routes so helpers._install_audit_completeness_
check's runtime half (an AUDITED route completing 2xx with no matching row
raises in TESTING config) actually fires against them at least once, the same
way test_stock_ops.py already covers receive/adjust/writeoff.

Routes covered here had zero prior test coverage of any kind before P3-1b —
batch reorder, batch edit, apply-costs, adjustment edit/delete, purchases,
and opening-stock import/undo.
"""
import io

from werkzeug.security import generate_password_hash

from models import AuditLog, Product, Purchase, StockAdjustment, StockBatch, db
from tests.factories import make_admin, make_product, make_stock_batch
from tests.helpers import D, login_as


def _login_admin(client, username='stockauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last_event(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_batch_reorder_reset_fifo_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Reorder Item')
    batch = make_stock_batch(product, sort_order=1)
    _login_admin(client)

    resp = client.post(f'/api/stock/batches/{batch.id}/reorder', json={'action': 'reset_fifo'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('batch_reorder_reset')
    assert row is not None
    assert row.target_id == str(batch.id)


def test_batch_reorder_use_next_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Reorder Item 2')
    b1 = make_stock_batch(product)
    b2 = make_stock_batch(product)
    _login_admin(client)

    resp = client.post(f'/api/stock/batches/{b2.id}/reorder', json={'action': 'use_next'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('batch_reorder_use_next')
    assert row is not None
    assert row.target_id == str(b2.id)


def test_batch_reorder_move_down_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Reorder Item 3')
    b1 = make_stock_batch(product)
    b2 = make_stock_batch(product)
    _login_admin(client)

    resp = client.post(f'/api/stock/batches/{b1.id}/reorder', json={'action': 'move_down'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('batch_reorder_move_down')
    assert row is not None


def test_reset_batch_order_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Reset Order Item')
    make_stock_batch(product, sort_order=2)
    _login_admin(client)

    resp = client.post(f'/api/stock/products/{product.id}/reset_batch_order')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('batch_order_reset_all')
    assert row is not None
    assert row.target_id == str(product.id)
    assert row.after_json is not None


def test_batch_edit_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Edit Item')
    batch = make_stock_batch(product, cost_per_base_unit=D('5.000000'))
    _login_admin(client)

    resp = client.patch(f'/api/stock/batches/{batch.id}', json={'base_cost_total': 60})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('batch_edited')
    assert row is not None
    assert row.target_id == str(batch.id)
    assert row.before_json is not None and row.after_json is not None


def test_batch_edit_with_retro_cogs_recalc_writes_audit_event(db_session, client):
    from tests.factories import make_stock_consumption

    product = make_product(product_type='stock_item', name='Retro COGS Item')
    batch = make_stock_batch(product, cost_per_base_unit=D('5.000000'))
    make_stock_consumption('some-sale-id', product, batch, cost_per_base_unit=D('5.000000'))
    _login_admin(client)

    resp = client.patch(f'/api/stock/batches/{batch.id}', json={
        'base_cost_total': 80, 'recalculate_historical_cogs': True,
    })
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['retro_updated'] == 1

    row = _last_event('batch_cogs_recalculated')
    assert row is not None
    assert row.target_id == str(batch.id)


def test_batch_delete_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Delete Item')
    batch = make_stock_batch(product)
    _login_admin(client)

    resp = client.delete(f'/api/stock/batches/{batch.id}')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('batch_deleted')
    assert row is not None
    assert row.target_id == str(batch.id)


def test_apply_costs_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Apply Costs Item')
    batch = make_stock_batch(product, base_cost_total=D('50.00'))
    _login_admin(client)

    resp = client.post('/api/stock/batches/apply-costs', json={
        'batch_ids': [batch.id],
        'additional_costs': [{'label': 'Shipping', 'type': 'shipping', 'amount': 10}],
    })
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('batch_costs_applied')
    assert row is not None


def test_adjustment_edit_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Adj Edit Item')
    batch = make_stock_batch(product, qty_remaining_base=D(8), qty_purchased_base=D(10))
    _login_admin(client)

    wo_resp = client.post('/api/stock/writeoff', json={
        'product_id': product.id, 'qty': 2, 'unit': 'unit', 'reason': 'initial writeoff',
    })
    assert wo_resp.status_code == 200, wo_resp.get_json()
    adj = StockAdjustment.query.filter_by(product_id=product.id, adjustment_type='writeoff').one()

    resp = client.patch(f'/api/stock/adjustments/{adj.id}', json={'qty': 1, 'unit': 'unit', 'reason': 'corrected'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('adjustment_edited')
    assert row is not None
    assert row.target_id == str(adj.id)


def test_adjustment_delete_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Adj Delete Item')
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10))
    _login_admin(client)

    wo_resp = client.post('/api/stock/writeoff', json={
        'product_id': product.id, 'qty': 3, 'unit': 'unit', 'reason': 'to be undone',
    })
    assert wo_resp.status_code == 200, wo_resp.get_json()
    adj = StockAdjustment.query.filter_by(product_id=product.id, adjustment_type='writeoff').one()

    resp = client.delete(f'/api/stock/adjustments/{adj.id}')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('adjustment_deleted')
    assert row is not None
    assert row.target_id == str(adj.id)


def test_purchases_post_writes_audit_event(db_session, client):
    product = make_product(product_type='simple', name='Simple Purchase Item')
    _login_admin(client)

    resp = client.post('/api/purchases', json={'product_id': product.id, 'qty_added': 5, 'purchase_price': 25})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('purchase_recorded')
    assert row is not None
    assert row.target_id == str(product.id)


def test_purchases_delete_writes_audit_event(db_session, client):
    product = make_product(product_type='simple', name='Simple Purchase Item 2')
    _login_admin(client)
    client.post('/api/purchases', json={'product_id': product.id, 'qty_added': 5, 'purchase_price': 25})
    purchase = Purchase.query.filter_by(product_id=product.id).one()

    resp = client.delete(f'/api/purchases/{purchase.id}')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('purchase_deleted')
    assert row is not None
    assert row.target_id == str(purchase.id)


def _csv_file(content):
    return (io.BytesIO(content.encode('utf-8')), 'opening_stock.csv')


def test_opening_import_preview_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', name='Opening Import Item', product_code=9001)
    _login_admin(client)

    resp = client.post(
        '/api/stock/opening-import?mode=preview',
        data={'file': _csv_file('product_code,qty,unit,unit_cost\n9001,10,unit,5.00\n')},
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['mode'] == 'preview'

    row = _last_event('stock_opening_import_previewed')
    assert row is not None


def test_opening_import_commit_and_undo_write_audit_events(db_session, client):
    product = make_product(product_type='stock_item', name='Opening Import Item 2', product_code=9002)
    _login_admin(client)

    resp = client.post(
        '/api/stock/opening-import?mode=import',
        data={'file': _csv_file('product_code,qty,unit,unit_cost\n9002,10,unit,5.00\n')},
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200, resp.get_json()
    run_id = resp.get_json()['run_id']

    commit_row = _last_event('stock_opening_import_committed')
    assert commit_row is not None
    assert commit_row.note == f'run_id={run_id}'

    undo_resp = client.delete(f'/api/stock/opening-import/{run_id}')
    assert undo_resp.status_code == 200, undo_resp.get_json()

    undo_row = _last_event('stock_opening_import_undone')
    assert undo_row is not None

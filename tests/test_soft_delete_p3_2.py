"""Soft delete for suppliers, stock_adjustments, invoices, and
supplier_invoices (Rev 5 P3-2). Users already got theirs via the pre-existing
`active` flag (see test_user_soft_delete.py) — these are the four entities
the plan names alongside users that a hard delete either 500s on once the row
has FK-protected history, or silently orphans a stock_movements
'reconciliation' source_id / a stock_adjustments row an audit event still
points at.
"""
from werkzeug.security import generate_password_hash

from models import Invoice, StockAdjustment, StockBatch, StockMovement, Supplier, SupplierInvoice, db
from tests.factories import make_admin, make_product, make_supplier
from tests.helpers import D, login_as, refresh


def _login_admin(client, username='softdeladmin_p32'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def test_deleted_supplier_survives_hidden_from_list_and_name_reusable(db_session, client):
    supplier = make_supplier(name='Reused Name Co')
    product = make_product(product_type='stock_item', price=D('10.00'))
    batch = StockBatch(product_id=product.id, qty_purchased_base=D(5), qty_remaining_base=D(5),
                        cost_per_base_unit=D('2.000000'), supplier_id=supplier.id)
    db.session.add(batch)
    db_session.commit()

    _login_admin(client)
    resp = client.delete(f'/api/suppliers/{supplier.id}')
    assert resp.status_code == 200, resp.get_json()

    row = db.session.get(Supplier, supplier.id)
    assert row is not None  # NOT hard-deleted
    assert row.deleted_at is not None

    # Historical batch keeps resolving its real supplier — no detach, no NULL.
    refresh(db_session, batch)
    assert batch.supplier_id == supplier.id

    # Hidden from the picker...
    listed = client.get('/api/suppliers').get_json()
    assert supplier.id not in {s['id'] for s in listed}

    # ...and the name is free to reuse (partial unique index, not a plain one).
    recreate = client.post('/api/suppliers', json={'name': 'Reused Name Co'})
    assert recreate.status_code == 200, recreate.get_json()


def test_stock_adjustment_delete_is_soft_and_reversal_source_stays_resolvable(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    now_batch = StockBatch(product_id=product.id, qty_purchased_base=D(10), qty_remaining_base=D(8),
                            cost_per_base_unit=D('3.000000'))
    db.session.add(now_batch)
    db_session.flush()
    adj = StockAdjustment(product_id=product.id, adjustment_type='writeoff', qty_change_base=D(-2),
                           system_qty_before=D(10), cost_written_off=D(6), reason='test writeoff')
    db.session.add(adj)
    db_session.commit()
    adj_id = adj.id

    _login_admin(client)
    resp = client.delete(f'/api/stock/adjustments/{adj_id}')
    assert resp.status_code == 200, resp.get_json()

    row = db.session.get(StockAdjustment, adj_id)
    assert row is not None  # NOT hard-deleted
    assert row.deleted_at is not None

    # The WRITEOFF_REVERSAL movement this delete wrote cites adj_id — still resolvable.
    movement = StockMovement.query.filter_by(
        source_type='writeoff', source_id=f'adj-del-{adj_id}', movement_type='WRITEOFF_REVERSAL').first()
    assert movement is not None

    # Hidden from the list.
    listed = client.get('/api/stock/adjustments').get_json()
    assert adj_id not in {a['id'] for a in listed}

    # A second delete attempt reports not-found, not a re-reversal.
    resp2 = client.delete(f'/api/stock/adjustments/{adj_id}')
    assert resp2.status_code == 404


def test_stock_adjustment_write_off_totals_exclude_a_deleted_adjustment(db_session, client):
    """Rev 5 P3-2: stats.py's write-off aggregations must keep excluding a
    reversed/deleted adjustment exactly as the old hard-delete already did —
    otherwise soft delete would silently double-count it back into a report.
    """
    from datetime import datetime, timedelta

    product = make_product(product_type='stock_item', price=D('10.00'))
    batch = StockBatch(product_id=product.id, qty_purchased_base=D(10), qty_remaining_base=D(8),
                        cost_per_base_unit=D('3.000000'))
    db.session.add(batch)
    db.session.flush()
    now = datetime.utcnow()
    adj = StockAdjustment(product_id=product.id, adjustment_type='writeoff', qty_change_base=D(-2),
                           system_qty_before=D(10), cost_written_off=D(6), reason='test writeoff',
                           adjusted_at=now)
    db.session.add(adj)
    db.session.commit()
    adj_id = adj.id

    _login_admin(client)
    resp = client.delete(f'/api/stock/adjustments/{adj_id}')
    assert resp.status_code == 200, resp.get_json()

    start = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    end = (now + timedelta(days=1)).strftime('%Y-%m-%d')
    stats_resp = client.get(f'/api/stats?start={start}&end={end}')
    assert stats_resp.status_code == 200, stats_resp.get_json()
    body = stats_resp.get_json()
    assert float(body.get('total_writeoff_cost', 0)) == 0.0


def test_deleted_invoice_survives_hidden_from_list(db_session, client):
    inv = Invoice(invoice_number='INV-SOFTDEL-1', customer_name='Soft Delete Customer', total=D('10.00'))
    db.session.add(inv)
    db_session.commit()
    inv_id = inv.id

    _login_admin(client)
    resp = client.post(f'/api/invoices/{inv_id}/delete')
    assert resp.status_code == 200, resp.get_json()

    row = db.session.get(Invoice, inv_id)
    assert row is not None  # NOT hard-deleted
    assert row.deleted_at is not None

    listed = client.get('/api/invoices').get_json()
    assert inv_id not in {i['id'] for i in listed}

    get_resp = client.get(f'/api/invoices/{inv_id}')
    assert get_resp.status_code == 404


def test_deleted_supplier_invoice_survives_and_reconciliation_source_stays_resolvable(db_session, client):
    supplier = make_supplier(name='Invoice Softdel Supplier')
    product = make_product(product_type='stock_item', price=D('10.00'))
    _login_admin(client)

    run = client.post(f'/api/suppliers/{supplier.id}/purchase_run', json={
        'lines': [{'product_id': product.id, 'qty': 10, 'unit': 'unit', 'total_price': 40}],
    })
    assert run.status_code == 200, run.get_json()
    inv_id = run.get_json()['invoice_id']

    delete = client.delete(f'/api/suppliers/{supplier.id}/invoices/{inv_id}')
    assert delete.status_code == 200, delete.get_json()

    row = db.session.get(SupplierInvoice, inv_id)
    assert row is not None  # NOT hard-deleted
    assert row.deleted_at is not None

    # The voided batch's 'reconciliation' movement cites inv_id — still resolvable,
    # which a hard delete of the invoice row would have broken permanently.
    still_resolves = db.session.get(SupplierInvoice, int(
        StockMovement.query.filter_by(source_type='reconciliation', movement_type='RECEIPT_VOID')
        .order_by(StockMovement.id.desc()).first().source_id
    ))
    assert still_resolves is not None
    assert still_resolves.id == inv_id

    listed = client.get(f'/api/suppliers/{supplier.id}/invoices').get_json()
    assert inv_id not in {i['id'] for i in listed}


def test_stock_batch_delete_voids_instead_of_hard_deleting(db_session, client):
    """Rev 5 P3-2: api_stock_batch_delete used to purge the batch's own
    stock_movements rows before hard-deleting it — the exact append-only
    violation void_unconsumed_batch exists to avoid. It now voids instead,
    same as the supplier-invoice paths.
    """
    product = make_product(product_type='stock_item', price=D('10.00'))
    _login_admin(client)

    receive = client.post('/api/stock/receive', json={
        'product_id': product.id, 'qty': 5, 'unit': 'unit', 'total_price': 20,
    })
    assert receive.status_code == 200, receive.get_json()
    batch_id = StockBatch.query.filter_by(product_id=product.id).one().id
    assert StockMovement.query.filter_by(batch_id=batch_id).count() == 1

    resp = client.delete(f'/api/stock/batches/{batch_id}')
    assert resp.status_code == 200, resp.get_json()

    batch = db.session.get(StockBatch, batch_id)
    assert batch is not None  # NOT hard-deleted
    assert batch.qty_remaining_base == D(0)
    assert batch.qty_purchased_base == D(0)

    # The original RECEIPT movement was NOT purged — the ledger stays append-only.
    movements = StockMovement.query.filter_by(batch_id=batch_id).order_by(StockMovement.id.asc()).all()
    assert [m.movement_type for m in movements] == ['RECEIPT', 'RECEIPT_VOID']

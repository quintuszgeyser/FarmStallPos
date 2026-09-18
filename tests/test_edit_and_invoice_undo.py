"""Sale edit and invoice-undo characterization tests (Rev 5 P0-1 / Section 5).
Routes exercised through the real Flask test client.
"""
from decimal import Decimal

import pytest
from werkzeug.security import generate_password_hash

from models import AuditLog, Invoice, Sale, StockBatch, StockConsumption
from tests.factories import make_admin, make_product, make_stock_batch
from tests.helpers import D, checkout, login_as, refresh


def _login_admin(client, username='editadmin'):
    admin = make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')
    return admin


def test_sale_edit_voids_original_and_creates_replacement_lines(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    batch = make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                              cost_per_base_unit=D('4.000000'))
    admin = _login_admin(client)

    resp = checkout(client, [{'product_id': product.id, 'qty': 3}], cash_tendered=30)
    sale_id = resp.get_json()['transaction_id']
    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(7)

    edit = client.post(f'/api/transactions/{sale_id}/edit',
                        json={'lines': [{'product_id': product.id, 'qty': 5}]})
    assert edit.status_code == 200, edit.get_json()

    all_rows = Sale.query.filter_by(sale_id=sale_id).order_by(Sale.id).all()
    # Original line voided, new line(s) added under the SAME sale_id.
    voided = [r for r in all_rows if r.voided]
    live = [r for r in all_rows if not r.voided]
    assert len(voided) == 1
    assert voided[0].void_reason == 'superseded by edit'
    assert len(live) == 1
    assert live[0].qty == D(5)

    # FIFO reversed the original 3, then consumed 5 fresh -> net -5 from original 10.
    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(5)

    audit = AuditLog.query.filter_by(event_type='sale_edit', target_id=sale_id).all()
    assert len(audit) == 1
    assert audit[0].actor_user_id == admin.id


def test_sale_edit_uses_current_server_price_not_original(db_session, client):
    """Edit always re-prices from the product's CURRENT price/price_per_unit —
    same rule as checkout — even if the original sale was at a different price.
    """
    from models import db
    product = make_product(product_type='stock_item', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10), cost_per_base_unit=D('4.000000'))
    _login_admin(client)

    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=10)
    sale_id = resp.get_json()['transaction_id']

    product.price = D('15.00')
    db.session.flush()

    edit = client.post(f'/api/transactions/{sale_id}/edit',
                        json={'lines': [{'product_id': product.id, 'qty': 1}]})
    assert edit.status_code == 200, edit.get_json()

    live = Sale.query.filter_by(sale_id=sale_id, voided=False).all()
    assert len(live) == 1
    assert live[0].unit_price == D('15.00')


def _create_and_finalise_invoice(client, product, qty='2', unit_price='10.00'):
    create = client.post('/api/invoices', json={
        'lines': [{'product_id': product.id, 'name': product.name, 'qty': qty,
                   'unit_price': unit_price, 'unit': 'unit', 'subtotal': str(Decimal(qty) * Decimal(unit_price))}],
    })
    assert create.status_code == 200, create.get_json()
    inv_id = create.get_json()['id']
    fin = client.post(f'/api/invoices/{inv_id}/finalise')
    assert fin.status_code == 200, fin.get_json()
    return inv_id, fin.get_json()['sale_id']


def test_invoice_finalise_creates_sale_and_consumes_fifo(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    batch = make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                              cost_per_base_unit=D('4.000000'))
    _login_admin(client)

    inv_id, sale_id = _create_and_finalise_invoice(client, product, qty='2', unit_price='10.00')

    rows = Sale.query.filter_by(sale_id=sale_id).all()
    assert len(rows) == 1
    assert rows[0].qty == D(2)
    assert rows[0].payment_method == 'invoice'

    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(8)

    from models import db as _db
    inv_row = _db.session.get(Invoice, inv_id)
    assert inv_row.status == 'finalised'
    assert inv_row.sale_id == sale_id


def test_invoice_undo_restores_stock_but_misses_lock_and_liability_reversal(db_session, client):
    """Confirms the two REAL defects in invoices.py:239-243 this session's census
    corrected against Rev 5's original text: Rev 5 said undo "never deletes"
    StockConsumption rows — it does (line 243, `.delete()`). What it actually
    does NOT do: (a) take a `with_for_update` lock on the batches it restores,
    and (b) call reverse_consignment_liabilities for a consignment product's
    sale. This test pins (b) concretely — a consignment sale, invoiced then
    undone, leaves its liability 'outstanding' forever. (a) is a race-condition
    property that can't be meaningfully asserted from a single-threaded test;
    see tests/test_locking.py's docstring for why concurrency defects need a
    genuinely concurrent test, not an inline assertion here.
    """
    from models import db, ConsignmentLiability
    from tests.factories import make_supplier

    supplier = make_supplier(name='Undo Supplier')
    product = make_product(product_type='stock_item', price=D('10.00'), is_consignment=True,
                            consignment_supplier_id=supplier.id, settlement_basis='FIXED_COST',
                            consignment_cost_per_unit=D('3.000000'))
    batch = make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                              cost_per_base_unit=D('4.000000'), ownership_type='CONSIGNMENT',
                              supplier_id=supplier.id)
    _login_admin(client)

    inv_id, sale_id = _create_and_finalise_invoice(client, product, qty='3', unit_price='10.00')

    liabilities = ConsignmentLiability.query.filter_by(sale_id=sale_id).all()
    assert len(liabilities) == 1
    assert liabilities[0].status == 'outstanding'

    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(7)

    undo = client.post(f'/api/invoices/{inv_id}/undo')
    assert undo.status_code == 200, undo.get_json()

    # Stock IS correctly restored...
    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(10)
    # ...and consumption rows ARE deleted (correcting Rev 5's "never deletes" claim).
    assert StockConsumption.query.filter_by(sale_id=sale_id).count() == 0

    # ...but the consignment liability is left outstanding despite the sale
    # that created it having been fully reversed. Supplier still shown as owed.
    refresh(db_session, liabilities[0])
    assert liabilities[0].status == 'outstanding'

    inv_row = db.session.get(Invoice, inv_id)
    assert inv_row.status == 'draft'
    assert inv_row.sale_id is None

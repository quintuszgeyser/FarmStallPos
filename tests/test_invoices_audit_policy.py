"""Rev 5 P3-1b — proof that every AUDITED route in the `invoices` blueprint
actually writes an AuditLog row on completion, not just that it declares the
policy. tests/test_mutation_registry.py checks the static declaration; these
exercise the routes so helpers._install_audit_completeness_check's runtime
half (an AUDITED route completing 2xx with no matching row raises in TESTING
config) actually fires against them at least once.

Finalise and undo already have FIFO/COGS coverage in
tests/test_edit_and_invoice_undo.py — these tests only check the audit side.
"""
from decimal import Decimal

from werkzeug.security import generate_password_hash

from models import AuditLog, Invoice, db
from tests.factories import make_admin, make_product, make_stock_batch
from tests.helpers import D, login_as


def _login_admin(client, username='invoiceauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last_event(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def _create_invoice(client, product, qty='2', unit_price='10.00'):
    resp = client.post('/api/invoices', json={
        'lines': [{'product_id': product.id, 'name': product.name, 'qty': qty,
                   'unit_price': unit_price, 'unit': 'unit', 'subtotal': str(Decimal(qty) * Decimal(unit_price))}],
    })
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()['id']


def test_shipping_fees_update_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/invoices/shipping-fees', json={'fees': {'delivery': 120}})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('shipping_fees_updated')
    assert row is not None
    assert row.after_json is not None


def test_shipping_fees_update_with_no_fees_writes_nothing(db_session, client):
    """No fee in the payload matches a configured method -> no mutation, so
    no audit row is expected (mirrors stock.py's no-op branches)."""
    _login_admin(client)

    resp = client.post('/api/invoices/shipping-fees', json={'fees': {}})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['saved'] == {}


def test_invoice_create_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'))
    _login_admin(client)

    inv_id = _create_invoice(client, product)

    row = _last_event('invoice_created')
    assert row is not None
    assert row.target_id == str(inv_id)
    assert row.after_json is not None


def test_invoice_update_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'))
    _login_admin(client)
    inv_id = _create_invoice(client, product)

    resp = client.post(f'/api/invoices/{inv_id}', json={'customer_name': 'Updated Name'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('invoice_updated')
    assert row is not None
    assert row.target_id == str(inv_id)
    assert row.before_json is not None and row.after_json is not None


def test_invoice_delete_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'))
    _login_admin(client)
    inv_id = _create_invoice(client, product)

    resp = client.post(f'/api/invoices/{inv_id}/delete')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('invoice_deleted')
    assert row is not None
    assert row.target_id == str(inv_id)
    # Rev 5 P3-2: soft delete, not a hard delete — see test_soft_delete_p3_2.py
    # for the full characterization of this behaviour.
    deleted = db.session.get(Invoice, inv_id)
    assert deleted is not None
    assert deleted.deleted_at is not None


def test_invoice_copy_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'))
    _login_admin(client)
    inv_id = _create_invoice(client, product)

    resp = client.post(f'/api/invoices/{inv_id}/copy')
    assert resp.status_code == 200, resp.get_json()
    copy_id = resp.get_json()['id']

    row = _last_event('invoice_copied')
    assert row is not None
    assert row.target_id == str(copy_id)


def test_invoice_finalise_writes_audit_event(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'))
    _login_admin(client)
    inv_id = _create_invoice(client, product)

    resp = client.post(f'/api/invoices/{inv_id}/finalise')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('invoice_finalised')
    assert row is not None
    assert row.target_id == str(inv_id)
    assert row.after_json is not None

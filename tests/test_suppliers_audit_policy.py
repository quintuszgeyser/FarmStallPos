"""Rev 5 P3-1b — proof that every AUDITED route in the `suppliers` blueprint
actually writes an AuditLog row on completion, not just that it declares the
policy. tests/test_mutation_registry.py checks the static declaration; these
exercise the routes so helpers._install_audit_completeness_check's runtime
half (an AUDITED route completing 2xx with no matching row raises in TESTING
config) actually fires against them at least once.

purchase_run is the big one: it's the shared costing-input surface with
stock.py's receive/adjust (same write_stock_movement, absorb_neg_placeholder
calls) and the VAT/discount/shipping allocation waterfall P0-2's INV-9
checks.
"""
import io
from datetime import datetime, UTC

from werkzeug.security import generate_password_hash

from models import AuditLog, Supplier, SupplierDocument, SupplierInvoice, SupplierProductMapping, db
from tests.factories import make_admin, make_product, make_supplier
from tests.helpers import D, login_as


def _login_admin(client, username='supplierauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last_event(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_supplier_create_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/suppliers', json={'name': 'New Supplier Co'})
    assert resp.status_code == 200, resp.get_json()
    sid = resp.get_json()['id']

    row = _last_event('supplier_created')
    assert row is not None
    assert row.target_id == str(sid)


def test_supplier_update_writes_audit_event(db_session, client):
    supplier = make_supplier(name='Old Name Ltd')
    _login_admin(client)

    resp = client.post(f'/api/suppliers/{supplier.id}', json={'phone': '0821234567'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('supplier_updated')
    assert row is not None
    assert row.target_id == str(supplier.id)
    assert row.before_json is not None and row.after_json is not None


def test_supplier_delete_writes_audit_event(db_session, client):
    supplier = make_supplier(name='Delete Me Supplier')
    _login_admin(client)

    resp = client.delete(f'/api/suppliers/{supplier.id}')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('supplier_deleted')
    assert row is not None
    assert row.target_id == str(supplier.id)


def test_purchase_run_writes_audit_event(db_session, client):
    supplier = make_supplier(name='Run Supplier')
    product = make_product(product_type='stock_item', price=D('10.00'))
    _login_admin(client)

    resp = client.post(f'/api/suppliers/{supplier.id}/purchase_run', json={
        'lines': [{'product_id': product.id, 'qty': 10, 'unit': 'unit', 'total_price': 40}],
    })
    assert resp.status_code == 200, resp.get_json()
    run_id = resp.get_json()['invoice_id']

    row = _last_event('purchase_run_posted')
    assert row is not None
    assert row.target_id == str(run_id)
    assert row.after_json is not None


def _make_mapping(supplier):
    m = SupplierProductMapping(
        supplier_id=supplier.id,
        raw_description_original='RAW DESC', raw_description_normalized='raw desc',
        raw_description_hash='hash1', mapping_state='SUGGESTED',
    )
    db.session.add(m)
    db.session.flush()
    return m


def test_product_mapping_patch_writes_audit_event(db_session, client):
    supplier = make_supplier(name='Mapping Supplier')
    mapping = _make_mapping(supplier)
    _login_admin(client)

    resp = client.patch(f'/api/suppliers/{supplier.id}/product-mappings/{mapping.id}',
                         json={'mapping_state': 'CONFIRMED'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('supplier_product_mapping_state_changed')
    assert row is not None
    assert row.target_id == str(mapping.id)


def test_product_mapping_delete_writes_audit_event(db_session, client):
    supplier = make_supplier(name='Mapping Supplier 2')
    mapping = _make_mapping(supplier)
    _login_admin(client)

    resp = client.delete(f'/api/suppliers/{supplier.id}/product-mappings/{mapping.id}')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('supplier_product_mapping_deleted')
    assert row is not None
    assert row.target_id == str(mapping.id)


def test_document_upload_and_delete_write_audit_events(db_session, client):
    supplier = make_supplier(name='Doc Supplier')
    _login_admin(client)

    resp = client.post(f'/api/suppliers/{supplier.id}/documents',
                        data={'file': (io.BytesIO(b'%PDF-1.4 fake'), 'invoice.pdf')},
                        content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_json()
    doc_id = resp.get_json()['id']

    upload_row = _last_event('supplier_document_uploaded')
    assert upload_row is not None
    assert upload_row.target_id == str(doc_id)

    resp = client.delete(f'/api/suppliers/{supplier.id}/documents/{doc_id}')
    assert resp.status_code == 200, resp.get_json()

    delete_row = _last_event('supplier_document_deleted')
    assert delete_row is not None
    assert delete_row.target_id == str(doc_id)


def test_supplier_invoice_update_and_delete_write_audit_events(db_session, client):
    supplier = make_supplier(name='Invoice Edit Supplier')
    product = make_product(product_type='stock_item', price=D('10.00'))
    _login_admin(client)

    run = client.post(f'/api/suppliers/{supplier.id}/purchase_run', json={
        'lines': [{'product_id': product.id, 'qty': 10, 'unit': 'unit', 'total_price': 40}],
    })
    assert run.status_code == 200, run.get_json()
    inv_id = run.get_json()['invoice_id']

    update = client.put(f'/api/suppliers/{supplier.id}/invoices/{inv_id}', json={
        'lines': [{'product_id': product.id, 'qty': 5, 'unit': 'unit', 'total_price': 25}],
    })
    assert update.status_code == 200, update.get_json()

    update_row = _last_event('supplier_invoice_updated')
    assert update_row is not None
    assert update_row.target_id == str(inv_id)

    delete = client.delete(f'/api/suppliers/{supplier.id}/invoices/{inv_id}')
    assert delete.status_code == 200, delete.get_json()

    delete_row = _last_event('supplier_invoice_deleted')
    assert delete_row is not None
    assert delete_row.target_id == str(inv_id)

"""Rev 5 P3-1b — proof that every AUDITED route in the `products` blueprint
actually writes an AuditLog row on completion, not just that it declares the
policy. tests/test_mutation_registry.py checks the static declaration; these
exercise the routes so helpers._install_audit_completeness_check's runtime
half (an AUDITED route completing 2xx with no matching row raises in TESTING
config) actually fires against them at least once.

Image routes are deliberately excluded — they're EXPLICITLY_EXEMPT (cosmetic
photography, no financial/inventory impact), so there's nothing to prove
here; a sanity check that they write no audit rows is included instead.
"""
from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin, make_product, make_recipe_line, make_stock_batch
from tests.helpers import D, login_as


def _login_admin(client, username='productauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last_event(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_product_create_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/products', json={
        'name': 'Audit Test Product', 'product_type': 'stock_item', 'price': 9.99,
        'unit_type': 'count', 'base_unit': 'unit',
    })
    assert resp.status_code == 200, resp.get_json()
    pid = resp.get_json()['id']

    row = _last_event('product_created')
    assert row is not None
    assert row.target_id == str(pid)


def test_product_update_writes_audit_event(db_session, client):
    product = make_product(name='Update Me')
    _login_admin(client)

    resp = client.post('/api/products/update', json={'id': product.id, 'price': 25.00})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('product_updated')
    assert row is not None
    assert row.target_id == str(product.id)
    assert row.before_json is not None and row.after_json is not None


def test_product_archive_and_restore_write_audit_events(db_session, client):
    product = make_product(name='Archive Me')
    _login_admin(client)

    resp = client.post(f'/api/products/{product.id}/archive', json={})
    assert resp.status_code == 200, resp.get_json()
    archive_row = _last_event('product_archived')
    assert archive_row is not None
    assert archive_row.target_id == str(product.id)

    resp = client.post(f'/api/products/{product.id}/restore', json={})
    assert resp.status_code == 200, resp.get_json()
    restore_row = _last_event('product_restored')
    assert restore_row is not None
    assert restore_row.target_id == str(product.id)


def test_product_delete_writes_audit_event(db_session, client):
    product = make_product(name='Delete Me No History')
    _login_admin(client)

    resp = client.delete(f'/api/products/{product.name}')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('product_deleted')
    assert row is not None
    assert row.target_id == str(product.id)


def test_product_copy_writes_audit_event(db_session, client):
    product = make_product(name='Copy Source')
    _login_admin(client)

    resp = client.post(f'/api/products/{product.id}/copy')
    assert resp.status_code == 200, resp.get_json()
    copy_id = resp.get_json()['id']

    row = _last_event('product_copied')
    assert row is not None
    assert row.target_id == str(copy_id)


def test_produce_writes_audit_event(db_session, client):
    flour = make_product(product_type='stock_item', name='Audit Flour', price=D('1.00'))
    make_stock_batch(flour, qty_remaining_base=D(50), qty_purchased_base=D(50),
                      cost_per_base_unit=D('2.000000'))
    bread = make_product(product_type='recipe', name='Audit Bread', is_produced=True,
                          batch_size=D('4'), price=D('15.00'))
    make_recipe_line(bread, flour, qty_base=D(1))
    _login_admin(client)

    resp = client.post(f'/api/products/{bread.id}/produce', json={'batches': 1})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('product_produced')
    assert row is not None
    assert row.target_id == str(bread.id)


def test_purchase_option_added_writes_audit_event(db_session, client):
    product = make_product(name='Purchase Option Product')
    _login_admin(client)

    resp = client.post(f'/api/products/{product.id}/purchase_option',
                        json={'package_size': 6, 'package_size_unit': 'unit'})
    assert resp.status_code == 200, resp.get_json()
    opt_id = resp.get_json()['option']['id']

    row = _last_event('purchase_option_added')
    assert row is not None
    assert row.target_id == str(opt_id)


def test_pending_price_apply_dismiss_and_accept_markup_write_audit_events(db_session, client):
    from models import db

    p1 = make_product(name='Pending Apply', price=D('10.00'))
    p1.pending_price = D('12.00')
    p2 = make_product(name='Pending Dismiss', price=D('20.00'))
    p2.pending_price = D('22.00')
    p3 = make_product(name='Pending Accept', price=D('10.00'))
    p3.pending_price = D('11.00')
    make_stock_batch(p3, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'))
    db_session.flush()
    _login_admin(client)

    apply_resp = client.post('/api/products/pending-prices/apply', json={'ids': [p1.id]})
    assert apply_resp.status_code == 200, apply_resp.get_json()
    assert _last_event('pending_price_applied') is not None

    dismiss_resp = client.post('/api/products/pending-prices/dismiss', json={'ids': [p2.id]})
    assert dismiss_resp.status_code == 200, dismiss_resp.get_json()
    assert _last_event('pending_price_dismissed') is not None

    accept_resp = client.post('/api/products/pending-prices/accept-markup', json={'ids': [p3.id]})
    assert accept_resp.status_code == 200, accept_resp.get_json()
    assert _last_event('pending_price_accepted_as_markup') is not None


def test_check_markup_drift_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/products/check-markup-drift')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('markup_drift_scan_run')
    assert row is not None


def test_image_routes_are_exempt_and_write_no_audit_rows(db_session, client):
    """Sanity check on the EXPLICITLY_EXEMPT classification for image routes —
    would make the exemption meaningless if they secretly wrote audit rows."""
    import io
    product = make_product(name='Image Product')
    _login_admin(client)
    before_count = AuditLog.query.count()

    resp = client.post(f'/api/products/{product.id}/image',
                        data={'image': (io.BytesIO(b'\xff\xd8\xff\xe0fakejpeg'), 'test.jpg')},
                        content_type='multipart/form-data')
    # May 422 on a fake JPEG (image processing validation) — either way, no audit row.
    assert resp.status_code in (200, 422)
    assert AuditLog.query.count() == before_count

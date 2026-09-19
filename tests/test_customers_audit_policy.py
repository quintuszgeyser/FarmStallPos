"""Rev 5 P3-1b — proof that every AUDITED route in the `customers` blueprint
actually writes an AuditLog row on completion, not just that it declares the
policy. tests/test_mutation_registry.py checks the static declaration; these
exercise the routes so helpers._install_audit_completeness_check's runtime
half (an AUDITED route completing 2xx with no matching row raises in TESTING
config) actually fires against them at least once.

Recognition-service telemetry routes (identify, log_plate, till/detect,
attributes POST, sessions) are EXPLICITLY_EXEMPT, not AUDITED — a sanity
check that they write no audit rows is included instead of a completeness
proof, mirroring products.py's image-route exemption tests.
"""
import base64

import numpy as np
from werkzeug.security import generate_password_hash

from models import AuditLog, CustomerPlate
from tests.factories import make_admin, make_customer
from tests.helpers import login_as


def _login_admin(client, username='customerauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last_event(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def _b64_embedding(dim=512):
    emb = np.random.default_rng(1).standard_normal(dim).astype(np.float32)
    return base64.b64encode(emb.tobytes()).decode()


def test_customer_create_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/customers', json={'name': 'Audit Test Customer'})
    assert resp.status_code == 200, resp.get_json()
    cid = resp.get_json()['id']

    row = _last_event('customer_created')
    assert row is not None
    assert row.target_id == str(cid)


def test_customer_update_writes_audit_event(db_session, client):
    customer = make_customer(name='Update Me')
    _login_admin(client)

    resp = client.post(f'/api/customers/{customer.id}', json={'phone': '0821234567'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customer_updated')
    assert row is not None
    assert row.target_id == str(customer.id)
    assert row.before_json is not None and row.after_json is not None


def test_customer_deactivate_writes_audit_event(db_session, client):
    customer = make_customer(name='Deactivate Me')
    _login_admin(client)

    resp = client.delete(f'/api/customers/{customer.id}')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customer_deactivated')
    assert row is not None
    assert row.target_id == str(customer.id)


def test_customer_rename_writes_audit_event(db_session, client):
    customer = make_customer(name='Old Name')
    _login_admin(client)

    resp = client.post(f'/api/customers/{customer.id}/name', json={'name': 'New Name'})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customer_renamed')
    assert row is not None
    assert row.target_id == str(customer.id)
    assert row.before_json == '{"name": "Old Name"}'


def test_customer_delete_permanent_writes_audit_event(db_session, client):
    customer = make_customer(name='Permanent Delete Me')
    customer_id = customer.id
    _login_admin(client)

    resp = client.post(f'/api/customers/{customer_id}/delete_permanent')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customer_permanently_deleted')
    assert row is not None
    assert row.target_id == str(customer_id)


def test_customers_cleanup_empty_writes_audit_event(db_session, client):
    from datetime import datetime, timedelta
    stale = make_customer(name=None, auto_enrolled=True, active=True,
                           last_visit=datetime.utcnow() - timedelta(days=60))
    _login_admin(client)

    resp = client.post('/api/customers/cleanup_empty')
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['deleted'] == 1

    row = _last_event('customers_cleanup_empty')
    assert row is not None
    assert str(stale.id) in row.after_json


def test_exclusion_added_writes_audit_event(db_session, client):
    a = make_customer(name='Excl A')
    b = make_customer(name='Excl B')
    _login_admin(client)

    resp = client.post('/api/customers/exclusions', json={'customer_a_id': a.id, 'customer_b_id': b.id})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customer_merge_exclusion_added')
    assert row is not None


def test_merge_writes_audit_event(db_session, client):
    primary = make_customer(name='Merge Primary')
    dup = make_customer(name='Merge Duplicate')
    _login_admin(client)

    resp = client.post('/api/customers/merge', json={'primary_id': primary.id, 'merge_ids': [dup.id]})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customers_merged')
    assert row is not None
    assert row.target_id == str(primary.id)


def test_unmerge_writes_audit_event(db_session, client):
    primary = make_customer(name='Unmerge Primary')
    dup = make_customer(name='Unmerge Duplicate')
    _login_admin(client)

    merge_resp = client.post('/api/customers/merge', json={'primary_id': primary.id, 'merge_ids': [dup.id]})
    assert merge_resp.status_code == 200, merge_resp.get_json()

    from models import db
    log_id = db.session.execute(
        db.text('SELECT id FROM customer_merge_log WHERE primary_id=:p AND source_id=:s'),
        {'p': primary.id, 's': dup.id},
    ).scalar()
    assert log_id is not None

    resp = client.post(f'/api/customers/merge_log/{log_id}/unmerge')
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customer_unmerged')
    assert row is not None
    assert row.target_id == str(dup.id)


def test_plate_enroll_and_remove_write_audit_events(db_session, client):
    customer = make_customer(name='Plate Customer')
    _login_admin(client)

    resp = client.post(f'/api/customers/{customer.id}/enroll/plate', json={'plate_number': 'CA123456'})
    assert resp.status_code == 200, resp.get_json()
    enroll_row = _last_event('customer_plate_enrolled')
    assert enroll_row is not None
    assert enroll_row.target_id == str(
        CustomerPlate.query.filter_by(customer_id=customer.id).first().id
    )

    plate = CustomerPlate.query.filter_by(customer_id=customer.id).first()
    resp = client.delete(f'/api/customers/{customer.id}/enroll/plate/{plate.id}')
    assert resp.status_code == 200, resp.get_json()
    remove_row = _last_event('customer_plate_removed')
    assert remove_row is not None
    assert remove_row.target_id == str(plate.id)


def test_face_enroll_writes_audit_event(db_session, client):
    customer = make_customer(name='Face Customer')
    _login_admin(client)

    resp = client.post(f'/api/customers/{customer.id}/enroll/face',
                        json={'embedding_b64': _b64_embedding()})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customer_face_enrolled')
    assert row is not None


def test_gait_enroll_writes_audit_event(db_session, client):
    customer = make_customer(name='Gait Customer')
    _login_admin(client)

    features = base64.b64encode(b'\x00' * 64).decode()
    resp = client.post(f'/api/customers/{customer.id}/enroll/gait', json={'features_b64': features})
    assert resp.status_code == 200, resp.get_json()

    row = _last_event('customer_gait_enrolled')
    assert row is not None


def test_telemetry_routes_are_exempt_and_write_no_audit_rows(db_session, client):
    """Sanity check on the EXPLICITLY_EXEMPT classification for recognition-
    service telemetry — would make the exemption meaningless if they secretly
    wrote audit rows."""
    customer = make_customer(name='Telemetry Customer')
    _login_admin(client)
    before_count = AuditLog.query.count()

    assert client.post('/api/customers/identify', json={'customer_id': customer.id}).status_code == 200
    assert client.post('/api/customers/log_plate', json={'plate_number': 'CA999999'}).status_code == 200
    assert client.post('/api/till/detect', json={'customer_id': customer.id}).status_code == 200

    assert AuditLog.query.count() == before_count

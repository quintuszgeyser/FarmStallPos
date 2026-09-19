"""Rev 5 P3-1b — proof that the SECURITY_EVENT_ONLY route in `cctv` writes
AuditLog rows on both login outcomes."""
from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_user


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_cctv_login_failure_writes_audit_event(db_session, client):
    resp = client.post('/cctv/login', data={'username': 'nobody', 'password': 'wrong'})
    assert resp.status_code == 401

    row = _last('cctv_login_failed')
    assert row is not None
    assert row.target_id == 'nobody'


def test_cctv_login_success_writes_audit_event(db_session, client):
    user = make_user(username='cctvguard', password_hash=generate_password_hash('adminpass123'), role='cctv')
    db_session.flush()

    resp = client.post('/cctv/login', data={'username': 'cctvguard', 'password': 'adminpass123'})
    assert resp.status_code == 302

    row = _last('cctv_login_succeeded')
    assert row is not None
    assert row.target_id == 'cctvguard'


def test_cctv_login_denies_user_without_cctv_role(db_session, client):
    make_user(username='regularteller', password_hash=generate_password_hash('adminpass123'), role='teller')
    db_session.flush()

    resp = client.post('/cctv/login', data={'username': 'regularteller', 'password': 'adminpass123'})
    assert resp.status_code == 401
    assert _last('cctv_login_failed') is not None

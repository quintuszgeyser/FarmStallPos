"""Rev 5 P3-1b — proof that AUDITED routes in specials, settings, recognition,
and kiosk write AuditLog rows. recognition/kiosk control routes proxy to an
external service, mocked here since it isn't reachable in the test env."""
from unittest.mock import MagicMock, patch

from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin
from tests.helpers import login_as


def _login_admin(client, username='settingsauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_special_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/specials', json={'name': 'Audit Special', 'special_price': 19.99})
    assert resp.status_code == 201, resp.get_json()
    sid = resp.get_json()['id']
    assert _last('special_created') is not None

    resp = client.post(f'/api/specials/{sid}', json={'name': 'Audit Special 2'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('special_updated') is not None

    resp = client.delete(f'/api/specials/{sid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('special_deleted') is not None


def test_settings_update_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/settings', json={'markup_percent': 25})
    assert resp.status_code == 200, resp.get_json()

    row = _last('settings_updated')
    assert row is not None
    assert '"markup_percent": 25' in row.after_json


def test_customisation_rule_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/customisation-rules', json={'rule_type': 'extra', 'to_category': 'Milk', 'price_adj': 5})
    assert resp.status_code == 200, resp.get_json()
    rid = resp.get_json()['id']
    assert _last('customisation_rule_created') is not None

    resp = client.put(f'/api/customisation-rules/{rid}', json={'price_adj': 7})
    assert resp.status_code == 200, resp.get_json()
    assert _last('customisation_rule_updated') is not None

    resp = client.delete(f'/api/customisation-rules/{rid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('customisation_rule_deleted') is not None


def test_recognition_settings_update_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/recognition/settings', json={'face_threshold': 0.4})
    assert resp.status_code == 200, resp.get_json()

    assert _last('recognition_settings_updated') is not None


def test_recognition_control_action_writes_audit_event(db_session, client):
    _login_admin(client)

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {'ok': True}
    with patch('requests.post', return_value=mock_resp):
        resp = client.post('/api/recognition/control/clear_queue', json={})
    assert resp.status_code == 200, resp.get_json()

    row = _last('recognition_control_action')
    assert row is not None
    assert row.target_id == 'clear_queue'


def test_kiosk_tablets_update_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/kiosk/tablets', json={'tablets': [{'ip': '10.0.0.50', 'name': 'Front'}]})
    assert resp.status_code == 200, resp.get_json()

    row = _last('kiosk_tablets_updated')
    assert row is not None


def test_kiosk_control_action_writes_audit_event(db_session, client):
    _login_admin(client)

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {'ok': True}
    with patch('requests.post', return_value=mock_resp):
        resp = client.post('/api/kiosk/control/10.0.0.50', json={'action': 'wake'})
    assert resp.status_code == 200, resp.get_json()

    row = _last('kiosk_control_action')
    assert row is not None
    assert row.target_id == '10.0.0.50'

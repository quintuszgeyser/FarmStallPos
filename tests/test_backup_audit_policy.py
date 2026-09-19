"""Rev 5 P3-1b — proof that AUDITED routes in `backup` write AuditLog rows.

Several routes are deliberately NOT exercised here, same reasoning as
core.py's /api/db-migrate (documented there): they touch real external state
this test environment must never actually trigger —
  - /api/backup/now, /restore: spawn a background thread running real
    pg_dump/pg_restore subprocesses against the live DATABASE_URL, not the
    isolated test Postgres this suite uses.
  - /api/backup/switch-database: hot-swaps which database the running app
    points to and SIGHUPs the worker.
  - /api/backup/database/<dbname> (DELETE): issues a real DROP DATABASE.
Their @audit_policy('AUDITED') declarations and audit_event() calls were
verified by code inspection (see the commit message) instead.
"""
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin
from tests.helpers import login_as


def _login_admin(client, username='backupauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_backup_settings_update_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/backup/settings', json={'enabled': True, 'provider': 'local_folder'})
    assert resp.status_code == 200, resp.get_json()

    row = _last('backup_settings_updated')
    assert row is not None


def test_backup_disconnect_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/backup/disconnect')
    assert resp.status_code == 200, resp.get_json()

    assert _last('backup_gdrive_disconnected') is not None


def test_backup_connect_start_writes_audit_event(db_session, client, monkeypatch):
    import blueprints.backup as backup_mod
    monkeypatch.setattr(backup_mod, '_client_id', lambda: 'fake-client-id')
    _login_admin(client)

    mock_resp = type('R', (), {
        'json': lambda self: {'device_code': 'dc', 'user_code': 'ABCD-1234',
                               'verification_url': 'https://example.com', 'interval': 5, 'expires_in': 300},
        'raise_for_status': lambda self: None,
    })()
    with patch('requests.post', return_value=mock_resp):
        resp = client.post('/api/backup/connect/start')
    assert resp.status_code == 200, resp.get_json()

    assert _last('backup_connect_started') is not None


def test_backup_connect_poll_authorized_writes_audit_event(db_session, client, monkeypatch):
    import blueprints.backup as backup_mod
    monkeypatch.setattr(backup_mod, '_get_pending_auth', lambda nonce: {
        'device_code': 'dc', 'expires_at': __import__('time').time() + 60,
    })
    _login_admin(client)

    mock_token_resp = type('R', (), {'json': lambda self: {
        'refresh_token': 'rt', 'access_token': 'at', 'expires_in': 3600,
    }, 'ok': True})()
    mock_userinfo_resp = type('R', (), {'ok': True, 'json': lambda self: {'email': 'owner@example.com'}})()

    def _fake_post(url, **kwargs):
        return mock_token_resp
    with patch('requests.post', side_effect=_fake_post), \
         patch('requests.get', return_value=mock_userinfo_resp):
        resp = client.get('/api/backup/connect/poll?nonce=anything')
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['status'] == 'authorized'

    assert _last('backup_gdrive_connected') is not None


def test_backup_verify_writes_audit_event(db_session, client, monkeypatch):
    import blueprints.backup as backup_mod
    # _do_verify() itself tries to reach Google Drive and will fail in this test env —
    # that's fine, the audit event is committed before _do_verify() is even called.
    monkeypatch.setattr(backup_mod, '_do_verify', lambda file_id, log_id: {'ok': False, 'error': 'mocked'})
    _login_admin(client)

    resp = client.post('/api/backup/verify', json={'file_id': 'abc123'})
    assert resp.status_code == 200, resp.get_json()

    row = _last('backup_verify_started')
    assert row is not None
    assert row.after_json is not None


def test_backup_file_delete_writes_audit_event(db_session, client, monkeypatch):
    import blueprints.backup as backup_mod
    monkeypatch.setattr(backup_mod, '_gdrive_delete', lambda file_id: None)
    _login_admin(client)

    resp = client.delete('/api/backup/abc123')
    assert resp.status_code == 200, resp.get_json()

    row = _last('backup_file_deleted')
    assert row is not None
    assert row.target_id == 'abc123'

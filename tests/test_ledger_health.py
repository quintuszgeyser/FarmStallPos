"""Ledger health endpoint characterization tests (Rev 5 P3-4).

Mirrors the existing backup_warning pattern in /api/health: an external cron
(scripts/ledger_health_check.py, same cadence as backup.sh) writes a status
file; the app reads it on every /api/health call and surfaces a plain-
language warning to admins, or stays silent if the file is missing/stale/
unreadable — never alarming on a cron that simply hasn't been set up yet.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import mock_open, patch

from werkzeug.security import generate_password_hash

from tests.factories import make_admin, make_user
from tests.helpers import login_as


def _patched_open(status):
    """mock_open, but only intercepts the ledger status file — every other
    open() call (templates, logging, etc.) during the same request passes
    through untouched."""
    real_open = open
    m = mock_open(read_data=json.dumps(status))

    def _selective(path, *a, **kw):
        if str(path).endswith('ledger_health_status.json'):
            return m(path, *a, **kw)
        return real_open(path, *a, **kw)
    return _selective


def _health_client(app):
    """/api/health calls db.engine.connect() directly (bypassing db.session)
    for its import_in_progress check — incompatible with the db_session
    fixture's engine-swap (it replaces db.engine with a raw Connection that
    has no .connect()). Uses the session-scoped `app` fixture's real engine
    directly instead; these tests don't need any DB row setup anyway.
    """
    return app.test_client()


def test_health_has_no_ledger_warning_when_status_file_is_absent(app):
    """The real, common case in this test environment: no cron has ever run,
    no file exists — /api/health must stay silent, not error."""
    resp = _health_client(app).get('/api/health')
    assert resp.status_code == 200
    assert resp.get_json()['ledger_warning'] is None


def test_health_surfaces_a_warning_when_the_ledger_check_failed(app):
    status = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'overall_status': 'FAIL',
        'unresolved_violation_count': 3,
    }
    with patch('builtins.open', _patched_open(status)):
        resp = _health_client(app).get('/api/health')
    assert resp.status_code == 200
    assert resp.get_json()['ledger_warning'] == '3 unresolved ledger invariant violations'


def test_health_surfaces_a_warning_when_the_check_is_stale(app):
    stale_time = datetime.now(timezone.utc) - timedelta(hours=72)
    status = {'generated_at': stale_time.isoformat(), 'overall_status': 'PASS',
              'unresolved_violation_count': 0}
    with patch('builtins.open', _patched_open(status)):
        resp = _health_client(app).get('/api/health')
    assert resp.status_code == 200
    assert "hasn't run in 72h" in resp.get_json()['ledger_warning']


def test_health_stays_silent_when_the_ledger_check_passed(app):
    status = {'generated_at': datetime.now(timezone.utc).isoformat(),
              'overall_status': 'PASS', 'unresolved_violation_count': 0}
    with patch('builtins.open', _patched_open(status)):
        resp = _health_client(app).get('/api/health')
    assert resp.status_code == 200
    assert resp.get_json()['ledger_warning'] is None


def test_admin_ledger_health_requires_admin(db_session, client):
    make_user(username='ledgerteller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'ledgerteller', 'testpass123')
    resp = client.get('/admin/ledger-health')
    assert resp.status_code == 403


def test_admin_ledger_health_404s_when_no_check_has_run(db_session, client):
    make_admin(username='ledgeradmin2', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'ledgeradmin2', 'adminpass123')
    resp = client.get('/admin/ledger-health')
    assert resp.status_code == 404
    assert 'hint' in resp.get_json()


def test_admin_ledger_health_returns_the_full_status_when_present(db_session, client):
    make_admin(username='ledgeradmin3', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'ledgeradmin3', 'adminpass123')

    status = {'generated_at': datetime.now(timezone.utc).isoformat(), 'overall_status': 'PASS',
              'invariants_computable': 8, 'invariants_passing': 8, 'invariants_failing': [],
              'unresolved_violation_count': 0, 'inv11_status': 'PASS',
              'inv11_coverage': {'checked': 10, 'skipped': 5}}
    with patch('builtins.open', _patched_open(status)):
        resp = client.get('/admin/ledger-health')
    assert resp.status_code == 200
    assert resp.get_json() == status

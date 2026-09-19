"""Rev 5 P3-1b — proof that the AUDITED route in the `till_sessions` blueprint
actually writes an AuditLog row on completion, not just that it declares the
policy. tests/test_mutation_registry.py checks the static declaration; this
exercises the close-till route so the runtime completeness check
(helpers._install_audit_completeness_check) actually fires against it.

till_sessions is a small, single-mutation blueprint but the highest-stakes
kind: the Z-report / cash-up record is the primary till-fraud detection
surface (P2-4's note: "Void-with-blank-reason is the classic till-fraud
signature" — an unaudited till close would be the softer twin of that gap).
"""
from decimal import Decimal

from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin
from tests.helpers import login_as


def _login_admin(client, username='tillauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def test_till_close_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/till/sessions', json={'counted_cash': '150.00', 'opening_float': '100.00'})
    assert resp.status_code == 200, resp.get_json()
    session_id = resp.get_json()['id']

    row = AuditLog.query.filter_by(event_type='till_closed').order_by(AuditLog.id.desc()).first()
    assert row is not None
    assert row.target_id == str(session_id)
    assert row.after_json is not None
    assert '"counted_cash": 150.0' in row.after_json


def test_till_summary_and_list_are_read_only(db_session, client):
    """Sanity check that the read routes don't accidentally write audit rows —
    would make the AUDITED coverage test above vacuous if they did."""
    _login_admin(client)
    before_count = AuditLog.query.count()

    assert client.get('/api/till/sessions/summary').status_code == 200
    assert client.get('/api/till/sessions').status_code == 200

    assert AuditLog.query.count() == before_count

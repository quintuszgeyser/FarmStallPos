"""Rev 5 P3-1b — proof that AUDITED routes in `labels` write AuditLog rows.

Print routes (print, print-bulk, browser-print, browser-print-bulk) are
EXPLICITLY_EXEMPT — LabelPrintJob is already a dedicated audit log for print
actions (see /api/label-print-jobs) — so they aren't exercised here."""
from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin
from tests.helpers import login_as


def _login_admin(client, username='labelsauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def _template_payload(name='Audit Template'):
    return {'name': name, 'width_mm': 40, 'height_mm': 30, 'elements': []}


def test_label_template_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/label-templates', json=_template_payload())
    assert resp.status_code == 201, resp.get_json()
    tmpl_id = resp.get_json()['id']
    assert _last('label_template_created') is not None

    resp = client.put(f'/api/label-templates/{tmpl_id}', json=_template_payload('Audit Template 2'))
    assert resp.status_code == 200, resp.get_json()
    assert _last('label_template_updated') is not None

    resp = client.post(f'/api/label-templates/{tmpl_id}/duplicate')
    assert resp.status_code == 201, resp.get_json()
    assert _last('label_template_duplicated') is not None

    resp = client.delete(f'/api/label-templates/{tmpl_id}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('label_template_deleted') is not None


def test_label_printer_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/label-printers', json={'name': 'Front Printer', 'model': 'xprinter_xp365b', 'connection': 'usb'})
    assert resp.status_code == 201, resp.get_json()
    printer_id = resp.get_json()['id']
    assert _last('label_printer_created') is not None

    resp = client.post('/api/label-printers', json={'name': 'Front Printer', 'model': 'xprinter_xp365b', 'connection': 'network'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('label_printer_updated') is not None

    resp = client.delete(f'/api/label-printers/{printer_id}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('label_printer_deleted') is not None

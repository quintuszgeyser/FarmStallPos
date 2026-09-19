"""Rev 5 P3-1b — proof that the AUDITED route in `imports` writes an
AuditLog row on completion."""
import io

from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin
from tests.helpers import login_as


def _login_admin(client, username='importsauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _csv_file(rows='Import Test Product,stock_item,unit,9.99\n'):
    csv_text = 'name,product_type,unit_type,price\n' + rows
    return (io.BytesIO(csv_text.encode()), 'import.csv')


def test_product_import_commit_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/products/import?mode=import',
                        data={'file': _csv_file()}, content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()['summary']['create'] == 1, resp.get_json()

    row = AuditLog.query.filter_by(event_type='product_csv_import_committed').order_by(AuditLog.id.desc()).first()
    assert row is not None
    assert row.after_json is not None


def test_product_import_preview_writes_no_audit_event(db_session, client):
    """Preview mode makes no changes — should write no audit row (would make
    the completeness check on the commit path above vacuous otherwise)."""
    _login_admin(client)
    before_count = AuditLog.query.count()

    resp = client.post('/api/products/import?mode=preview',
                        data={'file': _csv_file()}, content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_json()

    assert AuditLog.query.count() == before_count

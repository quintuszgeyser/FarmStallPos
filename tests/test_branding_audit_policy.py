"""Rev 5 P3-1b — proof that the AUDITED route in `branding` writes an
AuditLog row on completion."""
import io

from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin
from tests.helpers import login_as

def _png_1px():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (1, 1), color='white').save(buf, format='PNG')
    return buf.getvalue()


def test_logo_upload_writes_audit_event(db_session, client, tmp_path, monkeypatch):
    import blueprints.branding as branding_mod
    monkeypatch.setattr(branding_mod, 'BRANDING_DIR', str(tmp_path))

    make_admin(username='brandingadmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'brandingadmin', 'adminpass123')

    resp = client.post('/api/branding/logo', data={'logo': (io.BytesIO(_png_1px()), 'logo.png')},
                        content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_json()

    row = AuditLog.query.filter_by(event_type='branding_logo_uploaded').order_by(AuditLog.id.desc()).first()
    assert row is not None

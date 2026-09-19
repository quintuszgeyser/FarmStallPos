"""Rev 5 P3-1b — sanity check that `stats` (all NO_STATE_CHANGE) writes no
audit rows when exercised. There's nothing to runtime-prove for AUDITED
routes here since the whole blueprint is read-only."""
from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin
from tests.helpers import login_as


def test_stats_today_is_read_only(db_session, client):
    make_admin(username='statsauditadmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'statsauditadmin', 'adminpass123')
    before_count = AuditLog.query.count()

    resp = client.get('/api/stats/today')
    assert resp.status_code == 200

    assert AuditLog.query.count() == before_count

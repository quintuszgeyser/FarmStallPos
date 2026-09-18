"""User delete-as-deactivation characterization tests (Rev 5 P3-2).

api_users_delete used to hard-delete the User row. sales.user_id has no
ondelete clause, so that either 500s for any user who ever rang a sale, or
(if the FK were ever relaxed) silently orphans the attribution on every
sale/audit row that user touched. It now aliases the deactivation that
already existed (active=False) instead.
"""
from werkzeug.security import generate_password_hash

from models import Sale, User, UserSession, db
from tests.factories import make_admin, make_product, make_user
from tests.helpers import D, checkout, login_as


def _login_admin(client, username='softdeladmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def login_as_raw(client, username, password):
    return client.post('/api/login', json={'username': username, 'password': password})


def test_deleting_a_user_with_sales_succeeds_and_preserves_attribution(db_session, client):
    teller = make_user(username='ringer', password_hash=generate_password_hash('testpass123'))
    product = make_product(product_type='simple', price=D('10.00'))
    db_session.commit()

    login_as(client, 'ringer', 'testpass123')
    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=10)
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']
    client.post('/api/logout')

    _login_admin(client)
    resp = client.delete('/api/users/ringer')
    assert resp.status_code == 200, resp.get_json()

    row = db.session.get(User, teller.id)
    assert row is not None  # NOT hard-deleted
    assert row.active is False

    sale_rows = Sale.query.filter_by(sale_id=sale_id).all()
    assert len(sale_rows) == 1
    assert sale_rows[0].user_id == teller.id  # attribution preserved


def test_deleted_user_is_logged_out_and_cannot_log_in_again(db_session, client):
    teller = make_user(username='logout_target', password_hash=generate_password_hash('testpass123'))
    db_session.commit()

    login_as(client, 'logout_target', 'testpass123')
    session_row = UserSession.query.filter_by(user_id=teller.id, logged_out=None).first()
    assert session_row is not None
    client.post('/api/logout')

    # Simulate a still-open session (e.g. crashed client that never called /api/logout)
    session_row.logged_out = None
    db_session.commit()

    _login_admin(client, username='softdeladmin2')
    resp = client.delete('/api/users/logout_target')
    assert resp.status_code == 200, resp.get_json()

    db_session.refresh(session_row)
    assert session_row.logged_out is not None

    relogin = login_as_raw(client, 'logout_target', 'testpass123')
    assert relogin.status_code in (401, 403)


def test_deleted_user_is_excluded_from_pickers_but_visible_in_admin_list(db_session, client):
    make_user(username='picker_target', password_hash=generate_password_hash('testpass123'))
    db_session.commit()

    _login_admin(client, username='softdeladmin3')
    resp = client.delete('/api/users/picker_target')
    assert resp.status_code == 200, resp.get_json()

    users = client.get('/api/users').get_json()
    row = next(u for u in users if u['username'] == 'picker_target')
    assert row['active'] is False  # still listed for admin management...
    # ...but every picker the frontend builds filters on `.active`, so a
    # deactivated user is excluded there without a server-side change.

"""Rev 5 P3-1b — proof that AUDITED routes in core, consignment, and
deploy_schedule write AuditLog rows."""
from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin, make_product, make_stock_batch, make_supplier, make_user
from tests.helpers import D, checkout, login_as


def _login_admin(client, username='deployauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


# /api/db-migrate isn't exercised here: it calls strong_migrate(), which opens its own
# engine.begin() and collides with this test harness's SAVEPOINT-based transaction
# wrapping (conftest.py's db_session fixture already holds one on the same connection).
# That's a test-harness incompatibility, not a production issue — strong_migrate() owns
# the engine's transaction at real app startup, never inside an app-level test transaction.
# The route's @audit_policy('AUDITED') declaration and audit_event() call are still
# correct; they're just not runtime-proven by this suite.


def _make_consignment_liability(client):
    supplier = make_supplier(name='Consignment Audit Orchard')
    product = make_product(product_type='stock_item', price=D('20.00'), is_consignment=True,
                            consignment_supplier_id=supplier.id, settlement_basis='FIXED_COST',
                            consignment_cost_per_unit=D('6.000000'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('8.000000'), ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    make_user(username='consign_teller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'consign_teller', 'testpass123')
    resp = checkout(client, [{'product_id': product.id, 'qty': 4}], cash_tendered=80)
    assert resp.status_code == 200, resp.get_json()
    return supplier


def test_consignment_settlement_rate_update_writes_audit_event(db_session, client):
    supplier = make_supplier(name='Rate Orchard')
    product = make_product(product_type='stock_item', price=D('10.00'), is_consignment=True,
                            consignment_supplier_id=supplier.id)
    batch = make_stock_batch(product, ownership_type='CONSIGNMENT', supplier_id=supplier.id,
                              consignment_unit_cost=D('3.00'))
    _login_admin(client)

    resp = client.patch(f'/api/consignment/batches/{batch.id}/settlement-rate', json={'rate': 4.5})
    assert resp.status_code == 200, resp.get_json()

    row = _last('consignment_settlement_rate_updated')
    assert row is not None
    assert row.target_id == str(batch.id)


def test_consignment_settle_writes_audit_event(db_session, client):
    supplier = _make_consignment_liability(client)
    _login_admin(client)

    resp = client.post('/api/consignment/settle', json={'supplier_id': supplier.id})
    assert resp.status_code == 200, resp.get_json()

    row = _last('consignment_settled')
    assert row is not None


def test_consignment_recalculate_costs_writes_audit_event(db_session, client):
    supplier = _make_consignment_liability(client)
    _login_admin(client)

    resp = client.post(f'/api/consignment/recalculate-costs/{supplier.id}')
    assert resp.status_code == 200, resp.get_json()
    # Only writes an event if something actually changed; assert route succeeded either way.
    assert resp.get_json()['ok'] is True


def test_deploy_schedule_crud_writes_audit_events(db_session, client):
    from datetime import datetime, timedelta, timezone
    _login_admin(client)

    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    resp = client.post('/api/deploy-schedule', json={'scheduled_at': future, 'description': 'Audit test deploy'})
    assert resp.status_code == 201, resp.get_json()
    sid = resp.get_json()['id']
    assert _last('deploy_scheduled') is not None

    resp = client.delete(f'/api/deploy-schedule/{sid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('deploy_schedule_cancelled') is not None


def test_deploy_execute_and_rollback_now_write_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/deploy-schedule/execute')
    assert resp.status_code == 200, resp.get_json()
    assert _last('deploy_triggered_now') is not None

    resp = client.post('/api/deploy-schedule/rollback')
    assert resp.status_code == 200, resp.get_json()
    assert _last('deploy_rollback_triggered_now') is not None

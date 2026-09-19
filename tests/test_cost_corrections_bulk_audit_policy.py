"""Rev 5 P3-1b — proof that AUDITED routes in cost_corrections and bulk
write AuditLog rows."""
from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin, make_product, make_stock_batch
from tests.helpers import D, login_as


def _login_admin(client, username='costauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_cost_correction_apply_and_reverse_write_audit_events(db_session, client):
    product = make_product(product_type='stock_item', name='Cost Correction Item')
    batch = make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                              cost_per_base_unit=D('4.000000'))
    _login_admin(client)

    resp = client.post(f'/api/stock/batches/{batch.id}/cost-correction', json={
        'new_unit_cost': 5.0, 'scope': 'remaining', 'reason': 'Supplier invoice correction',
    })
    assert resp.status_code == 200, resp.get_json()
    adj_id = resp.get_json()['adjustment_id']

    apply_row = _last('cost_correction_applied')
    assert apply_row is not None
    assert apply_row.target_id == str(batch.id)

    resp = client.post(f'/api/stock/cost-corrections/{adj_id}/reverse', json={'reason': 'Entered in error'})
    assert resp.status_code == 200, resp.get_json()

    reverse_row = _last('cost_correction_reversed')
    assert reverse_row is not None
    assert reverse_row.target_id == str(batch.id)


def test_bulk_apply_and_rollback_write_audit_events(db_session, client):
    make_product(name='Bulk Edit Product', is_for_sale=True)
    _login_admin(client)

    resp = client.post('/api/products/bulk/apply', json={
        'conditions': [], 'actions': [{'field': 'is_for_sale', 'op': 'set', 'value': False}],
        'description': 'Audit bulk edit',
    })
    assert resp.status_code == 200, resp.get_json()
    run_id = resp.get_json()['run_id']
    assert run_id is not None

    apply_row = _last('bulk_edit_applied')
    assert apply_row is not None

    resp = client.post(f'/api/products/bulk/rollback/{run_id}')
    assert resp.status_code == 200, resp.get_json()

    rollback_row = _last('bulk_edit_rolled_back')
    assert rollback_row is not None

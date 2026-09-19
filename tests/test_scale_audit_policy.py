"""Rev 5 P3-1b — proof that AUDITED routes in `scale` write AuditLog rows.
External scale/router I/O is monkeypatched since neither is reachable here."""
from werkzeug.security import generate_password_hash

from models import AuditLog
from tests.factories import make_admin, make_product
from tests.helpers import login_as


def _login_admin(client, username='scaleauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_product_sync_and_force_resync_write_audit_events(db_session, client):
    product = make_product(product_type='stock_item', name='Scale Item', sync_to_scale=True)
    _login_admin(client)

    resp = client.post(f'/api/scale/products/{product.id}/sync')
    assert resp.status_code == 200, resp.get_json()
    assert _last('scale_product_resync_forced') is not None

    resp = client.post('/api/scale/force-resync')
    assert resp.status_code == 200, resp.get_json()
    assert _last('scale_force_resync_all') is not None


def test_sync_source_change_writes_audit_event(db_session, client, tmp_path, monkeypatch):
    import blueprints.scale as scale_mod
    monkeypatch.setattr(scale_mod, 'SYNC_SOURCE_FILE', tmp_path / 'sync_source.json')
    _login_admin(client)

    resp = client.post('/api/scale/sync-source', json={'source': 'qa'})
    assert resp.status_code == 200, resp.get_json()

    assert _last('scale_sync_source_changed') is not None


def test_delete_plu_writes_audit_event(db_session, client, monkeypatch):
    import blueprints.scale as scale_mod
    monkeypatch.setattr(scale_mod, '_scale_reachable', lambda ip, port, timeout=5: True)
    monkeypatch.setattr(scale_mod, '_scale_delete_plu', lambda ip, port, plu_no: {'ok': True, 'updated': 1})
    _login_admin(client)

    resp = client.post('/api/scale/delete-plu', json={'plu_no': 123})
    assert resp.status_code == 200, resp.get_json()

    row = _last('scale_plu_deleted')
    assert row is not None
    assert row.target_id == '123'


def test_keyboard_and_adverts_save_write_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/scale/keyboard', json={'slots': [{'key_id': 1, 'label': 'Apples'}]})
    assert resp.status_code == 200, resp.get_json()
    assert _last('scale_keyboard_preset_saved') is not None

    resp = client.post('/api/scale/adverts', json={'slots': [{'slot': 1, 'text': 'Fresh produce daily', 'enabled': True}]})
    assert resp.status_code == 200, resp.get_json()
    assert _last('scale_adverts_saved') is not None


def test_connection_settings_save_writes_audit_event(db_session, client):
    _login_admin(client)

    resp = client.post('/api/scale/connection-settings', json={'scale_ip': '10.0.0.55'})
    assert resp.status_code == 200, resp.get_json()

    row = _last('scale_connection_settings_updated')
    assert row is not None


def test_reserve_dhcp_writes_audit_event(db_session, client, monkeypatch):
    import blueprints.scale as scale_mod
    from helpers import set_setting
    set_setting('router_password', 'test-router-pass')
    set_setting('scale_ip', '10.0.0.103')
    set_setting('scale_mac', '3a:69:43:bc:97:f4')
    monkeypatch.setattr(scale_mod, '_tplink_login', lambda router_ip, password: 'fake-stok')
    monkeypatch.setattr(scale_mod, '_tplink_reserve_ip', lambda *a, **k: None)
    _login_admin(client)

    resp = client.post('/api/scale/reserve-dhcp')
    assert resp.status_code == 200, resp.get_json()

    assert _last('scale_dhcp_reservation_set') is not None

"""Rev 5 P3-1b — proof that AUDITED routes in the small taxonomy blueprints
(categories, subcategories, cost_categories, families) write AuditLog rows."""
from werkzeug.security import generate_password_hash

from models import AuditLog, Category, SubCategory, CostCategory, ProductFamily
from tests.factories import make_admin
from tests.helpers import login_as


def _login_admin(client, username='taxonomyauditadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def _last(event_type):
    return AuditLog.query.filter_by(event_type=event_type).order_by(AuditLog.id.desc()).first()


def test_category_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/categories', json={'name': 'Audit Cat'})
    assert resp.status_code == 200, resp.get_json()
    cid = resp.get_json()['id']
    assert _last('category_created') is not None

    resp = client.post('/api/categories/update', json={'id': cid, 'name': 'Audit Cat 2'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('category_updated') is not None

    resp = client.post('/api/categories/delete', json={'id': cid})
    assert resp.status_code == 200, resp.get_json()
    assert _last('category_deleted') is not None


def test_category_merge_writes_audit_event(db_session, client):
    _login_admin(client)
    source = client.post('/api/categories', json={'name': 'Merge Source'}).get_json()
    target = client.post('/api/categories', json={'name': 'Merge Target'}).get_json()

    resp = client.post('/api/categories/merge', json={'source_id': source['id'], 'target_id': target['id']})
    assert resp.status_code == 200, resp.get_json()
    assert _last('categories_merged') is not None


def test_subcategory_crud_writes_audit_events(db_session, client):
    _login_admin(client)
    cat = client.post('/api/categories', json={'name': 'Parent Cat'}).get_json()

    resp = client.post('/api/subcategories', json={'name': 'Sub A', 'category_id': cat['id']})
    assert resp.status_code == 200, resp.get_json()
    sid = resp.get_json()['id']
    assert _last('subcategory_created') is not None

    resp = client.post('/api/subcategories/update', json={'id': sid, 'name': 'Sub A2'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('subcategory_updated') is not None

    resp = client.post('/api/subcategories/delete', json={'id': sid})
    assert resp.status_code == 200, resp.get_json()
    assert _last('subcategory_deleted') is not None


def test_cost_category_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/cost-categories', json={'label': 'Freight'})
    assert resp.status_code == 200, resp.get_json()
    cid = resp.get_json()['id']
    assert _last('cost_category_created') is not None

    resp = client.patch(f'/api/cost-categories/{cid}', json={'label': 'Freight 2'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('cost_category_updated') is not None

    resp = client.delete(f'/api/cost-categories/{cid}')
    assert resp.status_code == 200, resp.get_json()
    assert _last('cost_category_deactivated') is not None


def test_family_and_attribute_crud_writes_audit_events(db_session, client):
    _login_admin(client)

    resp = client.post('/api/families', json={'name': 'Cheese Wheel'})
    assert resp.status_code == 200, resp.get_json()
    fid = resp.get_json()['id']
    assert _last('product_family_created') is not None

    resp = client.post('/api/families/update', json={'id': fid, 'name': 'Cheese Wheel 2'})
    assert resp.status_code == 200, resp.get_json()
    assert _last('product_family_updated') is not None

    resp = client.post('/api/attributes', json={'name': 'Size'})
    assert resp.status_code == 200, resp.get_json()
    aid = resp.get_json()['id']
    assert _last('attribute_created') is not None

    resp = client.post('/api/attributes/values', json={'attribute_id': aid, 'value': 'Large'})
    assert resp.status_code == 200, resp.get_json()
    vid = resp.get_json()['id']
    assert _last('attribute_value_created') is not None

    resp = client.post('/api/attribute_values/delete', json={'id': vid})
    assert resp.status_code == 200, resp.get_json()
    assert _last('attribute_value_deleted') is not None

    resp = client.post('/api/attributes/delete', json={'id': aid})
    assert resp.status_code == 200, resp.get_json()
    assert _last('attribute_deleted') is not None

    resp = client.post('/api/families/delete', json={'id': fid})
    assert resp.status_code == 200, resp.get_json()
    assert _last('product_family_deleted') is not None

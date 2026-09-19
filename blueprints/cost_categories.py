import re
from datetime import datetime

from flask import Blueprint, jsonify, request, g

from helpers import require_login, current_user, audit_event, audit_policy
from models import db, CostCategory

bp = Blueprint('cost_categories', __name__)


def _slug(s):
    return re.sub(r'[^a-z0-9_]', '_', s.lower().strip())[:64]


@bp.route('/api/cost-categories', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def list_cost_categories():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    cats = (CostCategory.query
            .filter_by(is_active=True)
            .order_by(CostCategory.sort_order, CostCategory.label)
            .all())
    return jsonify([{
        'id': c.id, 'name': c.name, 'label': c.label,
        'color': c.color, 'sort_order': c.sort_order,
    } for c in cats])


@bp.route('/api/cost-categories/all', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def list_all_cost_categories():
    u = current_user()
    if not u or not u.has_role('admin', 'manager'):
        return jsonify({'error': 'Forbidden'}), 403
    cats = (CostCategory.query
            .order_by(CostCategory.sort_order, CostCategory.label)
            .all())
    return jsonify([{
        'id': c.id, 'name': c.name, 'label': c.label,
        'color': c.color, 'is_active': c.is_active, 'sort_order': c.sort_order,
    } for c in cats])


@bp.route('/api/cost-categories', methods=['POST'])
@audit_policy('AUDITED')
def create_cost_category():
    u = current_user()
    if not u or not u.has_role('admin', 'manager'):
        return jsonify({'error': 'Forbidden'}), 403
    data  = request.get_json() or {}
    label = str(data.get('label') or '').strip()
    if not label:
        return jsonify({'error': 'label required'}), 400
    name  = _slug(data.get('name') or label)
    if CostCategory.query.filter_by(name=name).first():
        return jsonify({'error': f'Category "{name}" already exists'}), 409
    color     = str(data.get('color') or '').strip() or None
    max_order = db.session.query(db.func.max(CostCategory.sort_order)).scalar() or 0
    cat = CostCategory(
        name=name, label=label, color=color,
        sort_order=max_order + 1,
        is_active=True,
        created_by=u.id,
        created_at=datetime.utcnow(),
    )
    db.session.add(cat)
    db.session.flush()
    audit_event('cost_category_created', 'cost_categories', cat.id, after={'name': cat.name, 'label': cat.label})
    db.session.commit()
    return jsonify({'ok': True, 'id': cat.id, 'name': cat.name, 'label': cat.label})


@bp.route('/api/cost-categories/<int:cid>', methods=['PATCH'])
@audit_policy('AUDITED')
def update_cost_category(cid):
    u = current_user()
    if not u or not u.has_role('admin', 'manager'):
        return jsonify({'error': 'Forbidden'}), 403
    cat  = db.session.get(CostCategory, cid)
    if not cat:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    _before = {'label': cat.label, 'color': cat.color, 'is_active': cat.is_active, 'sort_order': cat.sort_order}
    if 'label' in data:
        cat.label = str(data['label']).strip() or cat.label
    if 'color' in data:
        cat.color = str(data['color']).strip() or None
    if 'is_active' in data:
        cat.is_active = bool(data['is_active'])
    if 'sort_order' in data:
        cat.sort_order = int(data['sort_order'])
    audit_event('cost_category_updated', 'cost_categories', cat.id, before=_before,
                after={'label': cat.label, 'color': cat.color, 'is_active': cat.is_active, 'sort_order': cat.sort_order})
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/cost-categories/<int:cid>', methods=['DELETE'])
@audit_policy('AUDITED')
def delete_cost_category(cid):
    u = current_user()
    if not u or not u.has_role('admin', 'manager'):
        return jsonify({'error': 'Forbidden'}), 403
    cat = db.session.get(CostCategory, cid)
    if not cat:
        return jsonify({'error': 'Not found'}), 404
    cat.is_active = False   # soft delete
    audit_event('cost_category_deactivated', 'cost_categories', cat.id, before={'is_active': True}, after={'is_active': False})
    db.session.commit()
    return jsonify({'ok': True})

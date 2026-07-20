import re
from datetime import datetime

from flask import Blueprint, jsonify, request, g

from helpers import require_login, current_user
from models import db, CostCategory

bp = Blueprint('cost_categories', __name__)


def _slug(s):
    return re.sub(r'[^a-z0-9_]', '_', s.lower().strip())[:64]


@bp.route('/api/cost-categories', methods=['GET'])
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
    db.session.commit()
    return jsonify({'ok': True, 'id': cat.id, 'name': cat.name, 'label': cat.label})


@bp.route('/api/cost-categories/<int:cid>', methods=['PATCH'])
def update_cost_category(cid):
    u = current_user()
    if not u or not u.has_role('admin', 'manager'):
        return jsonify({'error': 'Forbidden'}), 403
    cat  = db.session.get(CostCategory, cid)
    if not cat:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    if 'label' in data:
        cat.label = str(data['label']).strip() or cat.label
    if 'color' in data:
        cat.color = str(data['color']).strip() or None
    if 'is_active' in data:
        cat.is_active = bool(data['is_active'])
    if 'sort_order' in data:
        cat.sort_order = int(data['sort_order'])
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/cost-categories/<int:cid>', methods=['DELETE'])
def delete_cost_category(cid):
    u = current_user()
    if not u or not u.has_role('admin', 'manager'):
        return jsonify({'error': 'Forbidden'}), 403
    cat = db.session.get(CostCategory, cid)
    if not cat:
        return jsonify({'error': 'Not found'}), 404
    cat.is_active = False   # soft delete
    db.session.commit()
    return jsonify({'ok': True})

import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from helpers import require_login, require_role
from models import db, SubCategory, Category, Product

bp = Blueprint('subcategories', __name__)
logger = logging.getLogger('pos')


def _slug(name):
    import re
    return re.sub(r'[^a-z0-9]+', '-', name.lower().strip()).strip('-')


@bp.route('/api/subcategories', methods=['GET'])
def api_subcategories_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    category_id = request.args.get('category_id', type=int)

    counts = {
        (scid): n
        for scid, n in db.session.query(Product.sub_category_id, func.count(Product.id))
            .filter(Product.sub_category_id.isnot(None))
            .group_by(Product.sub_category_id).all()
    }

    q = SubCategory.query
    if category_id:
        q = q.filter_by(category_id=category_id)
    subs = q.order_by(SubCategory.sort_order.asc(), SubCategory.name.asc()).all()

    return jsonify([{
        'id':           s.id,
        'category_id':  s.category_id,
        'name':         s.name,
        'sort_order':   s.sort_order,
        'product_count': counts.get(s.id, 0),
    } for s in subs])


@bp.route('/api/subcategories', methods=['POST'])
def api_subcategories_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    category_id = data.get('category_id')
    if not category_id or not db.session.get(Category, category_id):
        return jsonify({'error': 'category_id required'}), 400

    norm = name.lower()
    clash = SubCategory.query.filter_by(category_id=category_id, name_norm=norm).first()
    if clash:
        return jsonify({'error': 'Sub-category already exists in this category'}), 409

    s = SubCategory(
        category_id=category_id,
        name=name,
        name_norm=norm,
        sort_order=data.get('sort_order', 0),
    )
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id, 'name': s.name})


@bp.route('/api/subcategories/update', methods=['POST'])
def api_subcategories_update():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    s = db.session.get(SubCategory, data.get('id'))
    if not s:
        return jsonify({'error': 'Sub-category not found'}), 404
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400

    norm = name.lower()
    clash = SubCategory.query.filter(
        SubCategory.id != s.id,
        SubCategory.category_id == s.category_id,
        SubCategory.name_norm == norm,
    ).first()
    if clash:
        return jsonify({'error': 'Another sub-category already uses that name'}), 409

    s.name = name
    s.name_norm = norm
    if 'sort_order' in data:
        s.sort_order = int(data['sort_order'])
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id, 'name': s.name})


@bp.route('/api/subcategories/delete', methods=['POST'])
def api_subcategories_delete():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    s = db.session.get(SubCategory, data.get('id'))
    if not s:
        return jsonify({'error': 'Sub-category not found'}), 404
    Product.query.filter_by(sub_category_id=s.id).update({'sub_category_id': None})
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})

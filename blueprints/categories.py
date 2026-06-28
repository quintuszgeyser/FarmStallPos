import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from helpers import (
    require_login, require_role,
    normalize_category_name, get_or_create_category,
)
from models import db, Category, Product

bp = Blueprint('categories', __name__)
logger = logging.getLogger('pos')


def _counts():
    """category_id -> number of products. None key = uncategorised."""
    rows = db.session.query(Product.category_id, func.count(Product.id)) \
        .group_by(Product.category_id).all()
    return {cid: n for cid, n in rows}


@bp.route('/api/categories', methods=['GET'])
def api_categories_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    counts = _counts()
    cats = Category.query.order_by(Category.name.asc()).all()
    return jsonify([
        {'id': c.id, 'name': c.name, 'product_count': counts.get(c.id, 0)}
        for c in cats
    ])


@bp.route('/api/categories', methods=['POST'])
def api_categories_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    if not normalize_category_name(data.get('name')):
        return jsonify({'error': 'name required'}), 400
    cat = get_or_create_category(data.get('name'))
    db.session.commit()
    return jsonify({'ok': True, 'id': cat.id, 'name': cat.name})


@bp.route('/api/categories/update', methods=['POST'])
def api_categories_update():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    cat = db.session.get(Category, data.get('id'))
    if not cat:
        return jsonify({'error': 'Category not found'}), 404
    clean = normalize_category_name(data.get('name'))
    if not clean:
        return jsonify({'error': 'name required'}), 400
    norm = clean.lower()
    clash = Category.query.filter(Category.id != cat.id, Category.name_norm == norm).first()
    if clash:
        return jsonify({'error': 'Another category already uses that name'}), 409
    cat.name = clean
    cat.name_norm = norm
    db.session.commit()
    return jsonify({'ok': True, 'id': cat.id, 'name': cat.name})


@bp.route('/api/categories/delete', methods=['POST'])
def api_categories_delete():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    cat = db.session.get(Category, data.get('id'))
    if not cat:
        return jsonify({'error': 'Category not found'}), 404
    # Unassign products (keep them, just clear the category) then remove the row
    Product.query.filter_by(category_id=cat.id).update({'category_id': None})
    db.session.delete(cat)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/categories/merge', methods=['POST'])
def api_categories_merge():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    source = db.session.get(Category, data.get('source_id'))
    target = db.session.get(Category, data.get('target_id'))
    if not source or not target:
        return jsonify({'error': 'Both source and target categories required'}), 404
    if source.id == target.id:
        return jsonify({'error': 'Source and target must differ'}), 400
    moved = Product.query.filter_by(category_id=source.id).update({'category_id': target.id})
    db.session.delete(source)
    db.session.commit()
    return jsonify({'ok': True, 'moved': moved, 'target_id': target.id})

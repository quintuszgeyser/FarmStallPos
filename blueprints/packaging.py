import logging

from flask import Blueprint, jsonify, request

from helpers import require_login, qty_bucket
from models import db, Product, Category, PackagingUsage

bp = Blueprint('packaging', __name__)
logger = logging.getLogger('pos')


def _serialize_pkg(p):
    return {
        'id':                 p.id,
        'name':               p.name,
        'price':              float(p.price) if p.price is not None else 0.0,
        'category_id':        p.category_id,
        'packaging_capacity': p.packaging_capacity,
        'stock_qty':          p.stock_qty,
    }


@bp.route('/api/packaging', methods=['GET'])
def api_packaging_list():
    """All products whose category has is_packaging=True, ordered by name."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    pkg_cats = Category.query.filter_by(is_packaging=True).all()
    if not pkg_cats:
        return jsonify({'products': []})
    cat_ids = {c.id for c in pkg_cats}
    products = (Product.query
                .filter(Product.category_id.in_(cat_ids), Product.is_archived == False)
                .order_by(Product.name.asc())
                .all())
    return jsonify({'products': [_serialize_pkg(p) for p in products]})


@bp.route('/api/packaging/suggestions', methods=['GET'])
def api_packaging_suggestions():
    """Top packaging suggestions for a given product_id and qty.

    product_id=0 (or missing/invalid) → cart-level suggestions.
    qty missing/invalid → defaults to 1.
    Returns top 5 sorted by use_count DESC.
    """
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        product_id = int(request.args.get('product_id', 0))
    except (TypeError, ValueError):
        product_id = 0

    try:
        qty = int(request.args.get('qty', 1))
    except (TypeError, ValueError):
        qty = 1

    bucket = qty_bucket(qty)

    rows = (PackagingUsage.query
            .filter_by(product_id=product_id, qty_bucket=bucket)
            .order_by(PackagingUsage.use_count.desc())
            .limit(5)
            .all())

    if not rows:
        return jsonify({'suggestions': []})

    pkg_ids = [r.packaging_product_id for r in rows]
    products = {p.id: p for p in Product.query.filter(Product.id.in_(pkg_ids)).all()}

    suggestions = []
    for row in rows:
        p = products.get(row.packaging_product_id)
        if p and not p.is_archived:
            s = _serialize_pkg(p)
            s['use_count'] = row.use_count
            suggestions.append(s)

    return jsonify({'suggestions': suggestions})

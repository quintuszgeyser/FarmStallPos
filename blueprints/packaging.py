import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from helpers import require_login, qty_bucket
from models import db, Product, Category, PackagingUsage, ProductImage

bp = Blueprint('packaging', __name__)
logger = logging.getLogger('pos')


def _serialize_pkg(p):
    img = ProductImage.query.filter_by(product_id=p.id, is_primary=True).first() or \
          ProductImage.query.filter_by(product_id=p.id).order_by(ProductImage.display_order).first()
    return {
        'id':                 p.id,
        'name':               p.name,
        'price':              float(p.price) if p.price is not None else 0.0,
        'category_id':        p.category_id,
        'packaging_capacity': p.packaging_capacity,
        'stock_qty':          p.stock_qty,
        'img':                img.filename if img else None,
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


@bp.route('/api/packaging/record', methods=['POST'])
def api_packaging_record():
    """Record that a specific packaging was chosen for a specific product (or cart).

    Called at the moment the user picks a package from the modal, so the pairing
    is exact — not inferred from completed-sale co-occurrence.
    product_id=0 means cart-level (till packaging button).
    """
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
    try:
        product_id         = int(data.get('product_id', 0))
        packaging_product_id = int(data['packaging_product_id'])
        qty                = float(data.get('qty', 1) or 1)
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'packaging_product_id required'}), 400
    bucket = qty_bucket(qty)
    try:
        db.session.execute(text("""
            INSERT INTO packaging_usage (product_id, qty_bucket, packaging_product_id, use_count, last_used_at)
            VALUES (:pid, :bucket, :pkg_pid, 1, NOW())
            ON CONFLICT (product_id, qty_bucket, packaging_product_id)
            DO UPDATE SET use_count = packaging_usage.use_count + 1, last_used_at = NOW()
        """), {'pid': product_id, 'bucket': bucket, 'pkg_pid': packaging_product_id})
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})

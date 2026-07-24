import logging
import re
from datetime import datetime

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from helpers import require_login, require_role
from models import db, ProductFamily, Product, Attribute, AttributeValue, ProductVariantAttribute

bp = Blueprint('families', __name__)
logger = logging.getLogger('pos')


def _slugify(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower().strip()).strip('-')


def _unique_slug(name, exclude_id=None):
    base = _slugify(name)
    slug = base
    n = 2
    while True:
        q = ProductFamily.query.filter_by(slug=slug)
        if exclude_id:
            q = q.filter(ProductFamily.id != exclude_id)
        if not q.first():
            return slug
        slug = f'{base}-{n}'
        n += 1


# ── Families ──────────────────────────────────────────────────────────────────

@bp.route('/api/families', methods=['GET'])
def api_families_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    counts = {
        fid: n
        for fid, n in db.session.query(Product.product_family_id, func.count(Product.id))
            .filter(Product.product_family_id.isnot(None))
            .group_by(Product.product_family_id).all()
    }

    families = ProductFamily.query.order_by(ProductFamily.name.asc()).all()
    return jsonify([{
        'id':            f.id,
        'name':          f.name,
        'description':   f.description,
        'slug':          f.slug,
        'variant_count': counts.get(f.id, 0),
    } for f in families])


@bp.route('/api/families', methods=['POST'])
def api_families_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400

    slug = _unique_slug(name)
    f = ProductFamily(name=name, description=data.get('description'), slug=slug)
    db.session.add(f)
    db.session.commit()
    return jsonify({'ok': True, 'id': f.id, 'name': f.name, 'slug': f.slug})


@bp.route('/api/families/update', methods=['POST'])
def api_families_update():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    f = db.session.get(ProductFamily, data.get('id'))
    if not f:
        return jsonify({'error': 'Family not found'}), 404
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    f.name = name
    f.description = data.get('description', f.description)
    f.slug = _unique_slug(name, exclude_id=f.id)
    f.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'id': f.id, 'name': f.name, 'slug': f.slug})


@bp.route('/api/families/delete', methods=['POST'])
def api_families_delete():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    f = db.session.get(ProductFamily, data.get('id'))
    if not f:
        return jsonify({'error': 'Family not found'}), 404
    Product.query.filter_by(product_family_id=f.id).update({
        'product_family_id': None, 'is_default_variant': False
    })
    db.session.delete(f)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/families/<int:fid>/variants', methods=['GET'])
def api_family_variants(fid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    f = db.session.get(ProductFamily, fid)
    if not f:
        return jsonify({'error': 'Family not found'}), 404

    products = Product.query.filter_by(product_family_id=fid, is_archived=False).all()

    def _variant_attrs(product_id):
        rows = db.session.query(Attribute.name, AttributeValue.value).join(
            AttributeValue, AttributeValue.attribute_id == Attribute.id
        ).join(
            ProductVariantAttribute,
            ProductVariantAttribute.attribute_value_id == AttributeValue.id
        ).filter(ProductVariantAttribute.product_id == product_id).all()
        return [{'attribute': r.name, 'value': r.value} for r in rows]

    return jsonify({
        'id':   f.id,
        'name': f.name,
        'slug': f.slug,
        'variants': [{
            'id':                p.id,
            'name':              p.name,
            'price':             float(p.price) if p.price else None,
            'price_per_unit':    float(p.price_per_unit) if p.price_per_unit else None,
            'image_url':         p.image_url,
            'is_default_variant': p.is_default_variant,
            'is_for_sale':       p.is_for_sale,
            'is_available_online': p.is_available_online,
            'attributes':        _variant_attrs(p.id),
        } for p in products],
    })


# ── Attributes ────────────────────────────────────────────────────────────────

@bp.route('/api/attributes', methods=['GET'])
def api_attributes_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    attrs = Attribute.query.order_by(Attribute.name.asc()).all()
    return jsonify([{
        'id':     a.id,
        'name':   a.name,
        'values': [{'id': v.id, 'value': v.value} for v in a.values.order_by(AttributeValue.value.asc())],
    } for a in attrs])


@bp.route('/api/attributes', methods=['POST'])
def api_attributes_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    if Attribute.query.filter_by(name=name).first():
        return jsonify({'error': 'Attribute already exists'}), 409
    a = Attribute(name=name)
    db.session.add(a)
    db.session.commit()
    return jsonify({'ok': True, 'id': a.id, 'name': a.name})


@bp.route('/api/attributes/delete', methods=['POST'])
def api_attributes_delete():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    a = db.session.get(Attribute, data.get('id'))
    if not a:
        return jsonify({'error': 'Attribute not found'}), 404
    db.session.delete(a)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/attributes/values', methods=['POST'])
def api_attribute_values_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    a = db.session.get(Attribute, data.get('attribute_id'))
    if not a:
        return jsonify({'error': 'Attribute not found'}), 404
    value = (data.get('value') or '').strip()
    if not value:
        return jsonify({'error': 'value required'}), 400
    if AttributeValue.query.filter_by(attribute_id=a.id, value=value).first():
        return jsonify({'error': 'Value already exists'}), 409
    v = AttributeValue(attribute_id=a.id, value=value)
    db.session.add(v)
    db.session.commit()
    return jsonify({'ok': True, 'id': v.id, 'value': v.value, 'attribute_id': a.id})


@bp.route('/api/attribute_values/delete', methods=['POST'])
def api_attribute_values_delete():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    v = db.session.get(AttributeValue, data.get('id'))
    if not v:
        return jsonify({'error': 'Value not found'}), 404
    db.session.execute(
        db.delete(ProductVariantAttribute).where(
            ProductVariantAttribute.attribute_value_id == v.id
        )
    )
    db.session.delete(v)
    db.session.commit()
    return jsonify({'ok': True})


# ── Per-product variant attributes ────────────────────────────────────────────

@bp.route('/api/products/<int:pid>/variant_attributes', methods=['GET'])
def api_product_variant_attrs_get(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    rows = db.session.query(Attribute.id.label('attr_id'), Attribute.name.label('attr_name'),
                            AttributeValue.id.label('val_id'), AttributeValue.value.label('val_value')) \
        .join(AttributeValue, AttributeValue.attribute_id == Attribute.id) \
        .join(ProductVariantAttribute, ProductVariantAttribute.attribute_value_id == AttributeValue.id) \
        .filter(ProductVariantAttribute.product_id == pid).all()
    return jsonify([{
        'attribute_id':    r.attr_id,
        'attribute_name':  r.attr_name,
        'value_id':        r.val_id,
        'value':           r.val_value,
    } for r in rows])


@bp.route('/api/products/<int:pid>/variant_attributes', methods=['POST'])
def api_product_variant_attrs_set(pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    value_ids = data.get('value_ids', [])

    # Clear existing, then insert new
    db.session.execute(
        db.delete(ProductVariantAttribute).where(
            ProductVariantAttribute.product_id == pid
        )
    )
    for vid in value_ids:
        v = db.session.get(AttributeValue, vid)
        if v:
            db.session.add(ProductVariantAttribute(product_id=pid, attribute_value_id=vid))
    db.session.commit()
    return jsonify({'ok': True})

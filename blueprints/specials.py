import json as _json
from decimal import Decimal

from flask import Blueprint, jsonify, request

from helpers import require_login, require_role
from models import db, Special, SpecialLine, Product

bp = Blueprint('specials', __name__)


def _serialize_special(s):
    lines = SpecialLine.query.filter_by(special_id=s.id).all()
    try:
        schedule = _json.loads(s.schedule) if s.schedule else []
    except Exception:
        schedule = []
    return {
        'id':            s.id,
        'name':          s.name,
        'special_price': float(s.special_price),
        'active':        s.active,
        'schedule':      schedule,
        'lines': [
            {
                'product_id':   l.product_id,
                'product_name': (db.session.get(Product, l.product_id).name
                                 if db.session.get(Product, l.product_id) else None),
                'qty':          l.qty,
            }
            for l in lines
        ],
    }


@bp.route('/api/specials', methods=['GET'])
def api_specials_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    specials = Special.query.order_by(Special.name.asc()).all()
    return jsonify([_serialize_special(s) for s in specials])


@bp.route('/api/specials', methods=['POST'])
def api_specials_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data  = request.json or {}
    name  = data.get('name', '').strip()
    price = data.get('special_price')
    lines = data.get('lines', [])
    if not name:
        return jsonify({'error': 'Name required'}), 400
    if price is None:
        return jsonify({'error': 'special_price required'}), 400
    schedule = data.get('schedule', [])
    s = Special(
        name=name,
        special_price=Decimal(str(price)),
        active=data.get('active', True),
        schedule=_json.dumps(schedule) if schedule else None,
    )
    db.session.add(s)
    db.session.flush()
    for l in lines:
        db.session.add(SpecialLine(
            special_id=s.id,
            product_id=int(l['product_id']),
            qty=int(l.get('qty', 1)),
        ))
    db.session.commit()
    return jsonify(_serialize_special(s)), 201


@bp.route('/api/specials/<int:sid>', methods=['POST'])
def api_specials_update(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Special, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'name'          in data: s.name          = data['name'].strip()
    if 'special_price' in data: s.special_price = Decimal(str(data['special_price']))
    if 'active'        in data: s.active        = bool(data['active'])
    if 'schedule'      in data:
        s.schedule = _json.dumps(data['schedule']) if data['schedule'] else None
    if 'lines' in data:
        SpecialLine.query.filter_by(special_id=sid).delete()
        for l in data['lines']:
            db.session.add(SpecialLine(
                special_id=sid,
                product_id=int(l['product_id']),
                qty=int(l.get('qty', 1)),
            ))
    db.session.commit()
    return jsonify(_serialize_special(s))


@bp.route('/api/specials/<int:sid>', methods=['DELETE'])
def api_specials_delete(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Special, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    SpecialLine.query.filter_by(special_id=sid).delete()
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})

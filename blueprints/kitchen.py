import json as _json
from datetime import datetime

from flask import Blueprint, jsonify, request

from helpers import require_login, _parse_dt
from models import db, KitchenOrder, User

bp = Blueprint('kitchen', __name__)


def _serialize_kitchen_order(ko):
    u = db.session.get(User, ko.teller_id) if ko.teller_id else None
    wait = None
    if ko.queued_at:
        end  = ko.completed_at or datetime.utcnow()
        wait = int((end - ko.queued_at).total_seconds())
    try:
        ingredients = _json.loads(ko.ingredients) if ko.ingredients else []
    except Exception:
        ingredients = []
    return {
        'id':           ko.id,
        'sale_id':      ko.sale_id,
        'product_id':   ko.product_id,
        'product_name': ko.product_name,
        'qty':          float(ko.qty),
        'ingredients':  ingredients,
        'status':       ko.status,
        'sort_order':   ko.sort_order,
        'queued_at':    ko.queued_at.isoformat() if ko.queued_at else None,
        'completed_at': ko.completed_at.isoformat() if ko.completed_at else None,
        'wait_seconds': wait,
        'teller':       u.username if u else '',
        'notes':        ko.notes,
    }


@bp.route('/api/kitchen/orders', methods=['GET'])
def api_kitchen_orders():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    include_completed = request.args.get('include_completed') == '1'
    date_param        = request.args.get('date')

    if include_completed and date_param:
        dt     = _parse_dt(date_param)
        end_dt = _parse_dt(date_param, is_end=True)
        orders = (KitchenOrder.query
                  .filter(KitchenOrder.queued_at >= dt, KitchenOrder.queued_at <= end_dt)
                  .order_by(KitchenOrder.queued_at.asc()).all())
    else:
        orders = (KitchenOrder.query
                  .filter(KitchenOrder.status == 'pending')
                  .order_by(KitchenOrder.sort_order.asc(), KitchenOrder.queued_at.asc())
                  .all())

    return jsonify([_serialize_kitchen_order(o) for o in orders])


@bp.route('/api/kitchen/orders/count', methods=['GET'])
def api_kitchen_orders_count():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    count = KitchenOrder.query.filter_by(status='pending').count()
    return jsonify({'count': count})


@bp.route('/api/kitchen/orders/<int:order_id>/status', methods=['POST'])
def api_kitchen_order_status(order_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data   = request.json or {}
    status = data.get('status', '').strip()
    if status not in ('completed', 'cancelled'):
        return jsonify({'error': 'status must be completed or cancelled'}), 400

    ko = db.session.get(KitchenOrder, order_id)
    if not ko:
        return jsonify({'error': 'Order not found'}), 404
    if ko.status != 'pending':
        return jsonify({'error': 'Order already resolved'}), 400

    ko.status = status
    if status == 'completed':
        ko.completed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'wait_seconds': _serialize_kitchen_order(ko)['wait_seconds']})


@bp.route('/api/kitchen/orders/<int:order_id>/move', methods=['POST'])
def api_kitchen_order_move(order_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data      = request.json or {}
    direction = data.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be up or down'}), 400

    ko = db.session.get(KitchenOrder, order_id)
    if not ko or ko.status != 'pending':
        return jsonify({'error': 'Order not found or not pending'}), 404

    all_pending = (KitchenOrder.query
                   .filter_by(status='pending')
                   .order_by(KitchenOrder.sort_order.asc(), KitchenOrder.queued_at.asc())
                   .all())

    idx = next((i for i, o in enumerate(all_pending) if o.id == order_id), None)
    if idx is None:
        return jsonify({'error': 'Order not found in pending queue'}), 404

    swap_idx = idx - 1 if direction == 'up' else idx + 1
    if swap_idx < 0 or swap_idx >= len(all_pending):
        return jsonify({'ok': True, 'note': 'Already at boundary'})

    all_pending[idx], all_pending[swap_idx] = all_pending[swap_idx], all_pending[idx]
    for i, order in enumerate(all_pending):
        order.sort_order = i
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/kitchen/orders/sale/<sale_id>/status', methods=['POST'])
def api_kitchen_sale_status(sale_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data   = request.json or {}
    status = data.get('status', '').strip()
    if status not in ('completed', 'cancelled'):
        return jsonify({'error': 'status must be completed or cancelled'}), 400

    orders = KitchenOrder.query.filter_by(sale_id=sale_id, status='pending').all()
    if not orders:
        return jsonify({'error': 'No pending orders found for this sale'}), 404

    now = datetime.utcnow()
    for ko in orders:
        ko.status = status
        if status == 'completed':
            ko.completed_at = now
    db.session.commit()

    wait = int((now - orders[0].queued_at).total_seconds()) if orders[0].queued_at else None
    return jsonify({'ok': True, 'wait_seconds': wait})


@bp.route('/api/kitchen/orders/sale/<sale_id>/move', methods=['POST'])
def api_kitchen_sale_move(sale_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data      = request.json or {}
    direction = data.get('direction')
    if direction not in ('up', 'down'):
        return jsonify({'error': 'direction must be up or down'}), 400

    all_pending = (KitchenOrder.query
                   .filter_by(status='pending')
                   .order_by(KitchenOrder.sort_order.asc(), KitchenOrder.queued_at.asc())
                   .all())

    seen   = {}
    groups = []
    for o in all_pending:
        if o.sale_id not in seen:
            seen[o.sale_id] = len(groups)
            groups.append((o.sale_id, []))
        groups[seen[o.sale_id]][1].append(o)

    group_idx = next((i for i, (sid, _) in enumerate(groups) if sid == sale_id), None)
    if group_idx is None:
        return jsonify({'error': 'Sale not found in pending queue'}), 404

    swap_idx = group_idx - 1 if direction == 'up' else group_idx + 1
    if swap_idx < 0 or swap_idx >= len(groups):
        return jsonify({'ok': True, 'note': 'Already at boundary'})

    groups[group_idx], groups[swap_idx] = groups[swap_idx], groups[group_idx]

    sort_counter = 0
    for _, orders_in_group in groups:
        for o in orders_in_group:
            o.sort_order = sort_counter
            sort_counter += 1
    db.session.commit()
    return jsonify({'ok': True})

import uuid
import json as _json
from decimal import Decimal
from datetime import datetime, date
from collections import defaultdict

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from helpers import (
    require_login, require_role, current_user,
    consume_fifo, reverse_fifo, _parse_dt,
)
from models import (
    db,
    Product, RecipeLine, StockBatch, StockConsumption, KitchenOrder,
    Sale, Purchase, User,
)

bp = Blueprint('transactions', __name__)


@bp.route('/api/transactions', methods=['GET'])
def api_transactions_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    u           = current_user()
    limit_param = request.args.get('limit')
    start_param = request.args.get('start')
    end_param   = request.args.get('end')

    q = db.session.query(Sale).filter(Sale.voided == False)

    if u.role == 'admin':
        today = date.today()
        if start_param or end_param:
            start_dt = _parse_dt(start_param) or datetime(today.year, today.month, today.day)
            end_dt   = _parse_dt(end_param, is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)
        else:
            start_dt = datetime(today.year, today.month, today.day)
            end_dt   = datetime(today.year, today.month, today.day, 23, 59, 59)
        q = q.filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt)

    rows = q.order_by(Sale.id.desc()).limit(2000).all()

    product_names = {prod.id: prod.name for prod in Product.query.filter(Product.id.in_({r.product_id for r in rows})).all()} if rows else {}
    user_names    = {usr.id: usr.username for usr in User.query.filter(User.id.in_({r.user_id for r in rows if r.user_id})).all()} if rows else {}

    grouped = defaultdict(list)
    dates, users_by_sale, flags_by_sale, discounts_by_sale = {}, {}, {}, {}
    for r in rows:
        grouped[r.sale_id].append(r)
        dates.setdefault(r.sale_id, r.date_time)
        if r.user_id: users_by_sale[r.sale_id] = user_names.get(r.user_id, '')
        if r.flagged:
            flags_by_sale[r.sale_id] = {'flagged': True, 'flag_note': r.flag_note, 'flag_resolved': r.flag_resolved}
        if r.discount_json and r.sale_id not in discounts_by_sale:
            try:
                disc = _json.loads(r.discount_json)
                discounts_by_sale[r.sale_id] = {'discount_info': disc, 'discount_by': user_names.get(r.discount_by, '') if r.discount_by else ''}
            except Exception: pass

    sale_ids     = list(grouped.keys())
    cogs_by_sale = defaultdict(Decimal)
    if sale_ids:
        for c in StockConsumption.query.filter(StockConsumption.sale_id.in_(sale_ids)).all():
            cogs_by_sale[c.sale_id] += Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit))

    result = []
    for sid in sorted(grouped.keys(), key=lambda k: max(x.id for x in grouped[k]), reverse=True):
        items, total = [], Decimal('0')
        sale_disc    = discounts_by_sale.get(sid, {})
        for ln in grouped[sid]:
            subtotal = Decimal(str(ln.qty)) * ln.unit_price
            total   += subtotal
            line     = {'product_id': ln.product_id, 'name': product_names.get(ln.product_id, f'Product {ln.product_id}'), 'qty': float(ln.qty), 'unit_price': float(ln.unit_price), 'subtotal': float(subtotal)}
            if ln.discount_json:
                try: line['discount'] = _json.loads(ln.discount_json)
                except Exception: pass
            items.append(line)
        cogs    = float(round(cogs_by_sale.get(sid, Decimal('0')), 4))
        total_f = float(round(total, 2))
        margin  = round((total_f - cogs) / total_f * 100, 1) if total_f > 0 and cogs > 0 else None
        result.append({'id': sid, 'date_time': dates[sid].isoformat(), 'total': total_f, 'lines': items, 'teller': users_by_sale.get(sid, ''), 'cogs': cogs if cogs > 0 else None, 'margin_pct': margin, 'flagged': flags_by_sale.get(sid, {}).get('flagged', False), 'flag_note': flags_by_sale.get(sid, {}).get('flag_note'), 'flag_resolved': flags_by_sale.get(sid, {}).get('flag_resolved', False), 'discount_by': sale_disc.get('discount_by', '')})

    if u.role != 'admin':
        result = result[:5]
    elif limit_param:
        try: result = result[:int(limit_param)]
        except Exception: pass
    return jsonify(result)


@bp.route('/api/transactions', methods=['POST'])
def api_transactions_post():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    cart = data.get('cart', [])
    if not cart:
        return jsonify({'error': 'Empty cart'}), 400

    sale_uuid    = str(uuid.uuid4())
    now          = datetime.utcnow()
    u            = current_user()
    customer_id  = data.get('customer_id')
    cart_discount = data.get('cart_discount')
    has_discount  = cart_discount or any(i.get('item_discount') or i.get('special_name') for i in cart)
    discount_by_id = (u.id if u else None) if has_discount else None

    for item in cart:
        pid        = int(item['product_id'])
        qty        = Decimal(str(item.get('qty', 1)))
        unit_price = Decimal(str(item.get('unit_price')))
        subs_raw   = item.get('subs', {})
        subs       = {int(k): int(v) for k, v in subs_raw.items()} if subs_raw else {}
        extras     = item.get('extras', [])
        item_discount = item.get('item_discount')
        special_name  = item.get('special_name', '')
        sub_log_val  = _json.dumps(subs) if subs else None
        discount_val = None
        if item_discount or cart_discount or special_name:
            discount_val = _json.dumps({
                **(({'item': item_discount}) if item_discount else {}),
                **(({'cart': cart_discount}) if cart_discount else {}),
                **(({'special': special_name}) if special_name else {}),
            })
        db.session.add(Sale(sale_id=sale_uuid, date_time=now, product_id=pid, qty=qty, unit_price=unit_price, user_id=u.id if u else None, customer_id=customer_id, sub_log=sub_log_val, discount_json=discount_val, discount_by=discount_by_id))
        p = db.session.get(Product, pid, with_for_update=True)
        if not p: continue
        if p.product_type == 'simple':
            p.stock_qty = max(0, (p.stock_qty or 0) - int(qty))
        elif p.product_type == 'stock_item':
            consume_fifo(pid, qty, sale_uuid, now)
        elif p.product_type == 'recipe':
            for rl in RecipeLine.query.filter_by(product_id=pid).all():
                actual_id = subs.get(rl.ingredient_id, rl.ingredient_id)
                if actual_id == -1: continue
                consume_fifo(actual_id, Decimal(str(rl.qty_base)) * qty, sale_uuid, now)
            for ex in extras:
                ex_id = int(ex.get('ingredient_id', 0)); ex_qty = Decimal(str(ex.get('qty_base', 0)))
                if ex_id and ex_qty > 0: consume_fifo(ex_id, ex_qty * qty, sale_uuid, now)

    max_sort = db.session.query(func.max(KitchenOrder.sort_order)).filter_by(status='pending').scalar() or 0

    def _collect_kitchen(product_id, qty, depth=0, subs=None, extras=None):
        if depth > 10: return []
        p = db.session.get(Product, product_id)
        if not p: return []
        subs = subs or {}; extras = extras or []
        if p.is_prepared:
            ingredients = []
            for rl in RecipeLine.query.filter_by(product_id=product_id).all():
                actual_id = subs.get(rl.ingredient_id, rl.ingredient_id)
                if actual_id == -1:
                    orig = db.session.get(Product, rl.ingredient_id)
                    ingredients.append({'name': orig.name if orig else str(rl.ingredient_id), 'qty': 0, 'base_unit': '', 'substituted': True, 'removed': True}); continue
                ing      = db.session.get(Product, actual_id)
                orig_ing = db.session.get(Product, rl.ingredient_id) if actual_id != rl.ingredient_id else ing
                if not ing: continue
                substituted = actual_id != rl.ingredient_id
                if ing.product_type == 'stock_item':
                    entry = {'name': ing.name, 'qty': float(rl.qty_base) * float(qty), 'base_unit': ing.base_unit or 'unit', 'substituted': substituted}
                    if substituted and orig_ing: entry['original_name'] = orig_ing.name
                    ingredients.append(entry)
                elif ing.product_type == 'recipe':
                    ingredients.append({'name': ing.name, 'qty': float(qty), 'base_unit': 'portion', 'substituted': substituted})
            for ex in extras:
                ex_id = int(ex.get('ingredient_id', 0)); ex_qty = float(ex.get('qty_base', 0)) * float(qty)
                if ex_id and ex_qty > 0:
                    ex_ing = db.session.get(Product, ex_id)
                    if ex_ing: ingredients.append({'name': ex_ing.name, 'qty': ex_qty, 'base_unit': ex_ing.base_unit or 'unit', 'extra': True})
            return [(p, qty, ingredients)]
        elif p.product_type == 'recipe':
            results = []
            for rl in RecipeLine.query.filter_by(product_id=product_id).all():
                results.extend(_collect_kitchen(rl.ingredient_id, Decimal(str(rl.qty_base)) * qty, depth + 1, subs))
            return results
        return []

    all_kitchen = []
    for item in cart:
        all_kitchen.extend(_collect_kitchen(int(item['product_id']), Decimal(str(item.get('qty', 1))), subs={int(k): int(v) for k, v in item.get('subs', {}).items()}, extras=item.get('extras', [])))
    for pos, (ko_product, ko_qty, ko_ingredients) in enumerate(all_kitchen):
        db.session.add(KitchenOrder(sale_id=sale_uuid, product_id=ko_product.id, product_name=ko_product.name, qty=ko_qty, ingredients=_json.dumps(ko_ingredients), status='pending', sort_order=max_sort + pos + 1, queued_at=now, teller_id=u.id if u else None))
    db.session.commit()
    return jsonify({'ok': True, 'transaction_id': sale_uuid, 'kitchen_orders': len(all_kitchen)})


@bp.route('/api/transactions/<sale_id>/flag', methods=['POST'])
def api_transaction_flag(sale_id):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data    = request.json or {}
    note    = data.get('note', '').strip()
    resolve = data.get('resolve', False)
    rows    = Sale.query.filter_by(sale_id=sale_id).all()
    if not rows: return jsonify({'error': 'Transaction not found'}), 404
    if resolve:
        if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
        for row in rows: row.flag_resolved = True
    else:
        if not note: return jsonify({'error': 'note required'}), 400
        for row in rows: row.flagged = True; row.flag_note = note; row.flag_resolved = False
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/transactions/<sale_id>/void', methods=['POST'])
def api_transaction_void(sale_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data   = request.json or {}
    reason = data.get('reason', '').strip()
    rows   = Sale.query.filter_by(sale_id=sale_id, voided=False).with_for_update().all()
    if not rows: return jsonify({'error': 'Transaction not found or already voided'}), 404
    u = current_user(); now = datetime.utcnow()
    for row in rows:
        row.voided = True; row.voided_by = u.id if u else None; row.voided_at = now; row.void_reason = reason
        p = db.session.get(Product, row.product_id, with_for_update=True)
        if p and p.product_type == 'simple': p.stock_qty = (p.stock_qty or 0) + int(row.qty)
    reverse_fifo(sale_id)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/transactions/<sale_id>/edit', methods=['POST'])
def api_transaction_edit(sale_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data  = request.json or {}
    lines = data.get('lines', [])
    if not lines: return jsonify({'error': 'lines required'}), 400
    rows = Sale.query.filter_by(sale_id=sale_id, voided=False).with_for_update().all()
    if not rows: return jsonify({'error': 'Transaction not found or voided'}), 404
    orig_date = rows[0].date_time
    for row in rows:
        p = db.session.get(Product, row.product_id, with_for_update=True)
        if p and p.product_type == 'simple': p.stock_qty = (p.stock_qty or 0) + int(row.qty)
        db.session.delete(row)
    reverse_fifo(sale_id)
    u = current_user(); now = orig_date
    for item in lines:
        pid = int(item['product_id']); qty = Decimal(str(item.get('qty', 1))); unit_price = Decimal(str(item.get('unit_price')))
        if qty <= 0: continue
        db.session.add(Sale(sale_id=sale_id, date_time=now, product_id=pid, qty=qty, unit_price=unit_price, user_id=u.id if u else None))
        p = db.session.get(Product, pid, with_for_update=True)
        if not p: continue
        if p.product_type == 'simple': p.stock_qty = max(0, (p.stock_qty or 0) - int(qty))
        elif p.product_type == 'stock_item': consume_fifo(pid, qty, sale_id, now)
        elif p.product_type == 'recipe':
            for rl in RecipeLine.query.filter_by(product_id=pid).all():
                consume_fifo(rl.ingredient_id, Decimal(str(rl.qty_base)) * qty, sale_id, now)
    db.session.commit()
    return jsonify({'ok': True})

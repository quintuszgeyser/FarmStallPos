import json as _json
from collections import defaultdict
from datetime import datetime, date, timedelta
from decimal import Decimal
from io import StringIO, BytesIO
from statistics import median

from flask import Blueprint, jsonify, request, send_file
from sqlalchemy import func

from helpers import require_role, get_setting, _parse_dt
from models import (
    db,
    Product, RecipeLine, StockBatch, StockConsumption, StockAdjustment,
    Sale, KitchenOrder, User, UserSession, Supplier, Customer,
)

bp = Blueprint('stats', __name__)


def _today_range():
    today = date.today()
    return datetime(today.year, today.month, today.day), datetime(today.year, today.month, today.day, 23, 59, 59)


def _parse_range(start_arg, end_arg):
    today = date.today()
    try: start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception: start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)
    return start_dt, end_dt


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@bp.route('/api/stats/today')
def api_stats_today():
    today = date.today().isoformat()
    from werkzeug.datastructures import ImmutableMultiDict
    request.args = ImmutableMultiDict([('start', today), ('end', today)])
    return api_stats()


@bp.route('/api/stats')
def api_stats():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    today = date.today()
    try: start_dt = datetime.fromisoformat(request.args.get('start', today.isoformat()))
    except Exception: start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(request.args.get('end', today.isoformat()))
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    try: product_id_filter = int(request.args.get('product_id')) if request.args.get('product_id') else None
    except (ValueError, TypeError): product_id_filter = None
    try: user_id_filter = int(request.args.get('user_id')) if request.args.get('user_id') else None
    except (ValueError, TypeError): user_id_filter = None

    not_return = db.or_(Sale.payment_method.is_(None), Sale.payment_method != 'return')
    sale_q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False, not_return)
    if product_id_filter:
        not_return2 = db.or_(Sale.payment_method.is_(None), Sale.payment_method != 'return')
        sale_ids_with_product = {r.sale_id for r in db.session.query(Sale.sale_id).filter(Sale.product_id == product_id_filter, Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False, not_return2).all()}
        sale_q = sale_q.filter(Sale.product_id == product_id_filter, Sale.sale_id.in_(sale_ids_with_product))
    if user_id_filter:
        sale_ids_by_user = {r.sale_id for r in db.session.query(Sale.sale_id).filter(Sale.user_id == user_id_filter, Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False).all()}
        sale_q = sale_q.filter(Sale.sale_id.in_(sale_ids_by_user))
    rows = sale_q.all()

    transactions_count = len({r.sale_id for r in rows})
    total_sales_value  = float(sum(Decimal(str(r.qty)) * r.unit_price for r in rows))
    total_items_sold   = float(sum(r.qty for r in rows))
    basket_value_map = defaultdict(float); basket_qty_map = defaultdict(float)
    for r in rows:
        val = float(Decimal(str(r.qty)) * r.unit_price)
        basket_value_map[r.sale_id] += val; basket_qty_map[r.sale_id] += float(r.qty)
    avg_basket_value = sum(basket_value_map.values()) / len(basket_value_map) if basket_value_map else 0.0
    avg_basket_qty   = sum(basket_qty_map.values())   / len(basket_qty_map)   if basket_qty_map   else 0.0

    sale_ids = list({r.sale_id for r in rows})
    total_cogs = 0.0; consumptions = []
    if sale_ids:
        consumptions = StockConsumption.query.filter(StockConsumption.sale_id.in_(sale_ids)).all()
        total_cogs   = float(sum(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)) for c in consumptions))
    # Return transactions restore StockBatch rows but leave StockConsumption intact —
    # deduct the batch value added by returns in this period to avoid overstating COGS.
    # Return sale rows have payment_method='return' and negative qty.
    return_rows_in_period = [r for r in rows if r.payment_method == 'return']
    if return_rows_in_period:
        # Find the original sale_ids referenced in void_reason ('return:<orig_id>:<reason>')
        orig_ids_from_returns = set()
        for r in return_rows_in_period:
            try: orig_ids_from_returns.add(r.void_reason.split(':')[1])
            except Exception: pass
        if orig_ids_from_returns:
            ret_consumptions = StockConsumption.query.filter(
                StockConsumption.sale_id.in_(list(orig_ids_from_returns))
            ).all()
            # Proportion of the original sale that was returned (approximate via row count)
            return_product_ids = {r.product_id for r in return_rows_in_period}
            cogs_credit = float(sum(
                Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit))
                for c in ret_consumptions if c.ingredient_id in return_product_ids
            ))
            total_cogs = max(0.0, total_cogs - cogs_credit)
    gross_profit = total_sales_value - total_cogs
    gross_margin = round(gross_profit / total_sales_value * 100, 1) if total_sales_value > 0 else None

    wq = StockAdjustment.query.filter(StockAdjustment.adjustment_type == 'writeoff', StockAdjustment.adjusted_at >= start_dt, StockAdjustment.adjusted_at <= end_dt)
    if product_id_filter: wq = wq.filter(StockAdjustment.product_id == product_id_filter)
    if user_id_filter:    wq = wq.filter(StockAdjustment.user_id    == user_id_filter)
    writeoffs = wq.all()
    total_writeoff_cost  = float(sum(float(w.cost_written_off or 0) for w in writeoffs))
    total_writeoff_count = len(writeoffs)

    kq = KitchenOrder.query.filter(KitchenOrder.queued_at >= start_dt, KitchenOrder.queued_at <= end_dt)
    if product_id_filter: kq = kq.filter(KitchenOrder.product_id == product_id_filter)
    if user_id_filter:    kq = kq.filter(KitchenOrder.teller_id  == user_id_filter)
    kitchen_in_range = kq.all()
    kitchen_completed_list = [k for k in kitchen_in_range if k.status == 'completed']
    now_dt = datetime.utcnow()
    pending_orders = KitchenOrder.query.filter_by(status='pending').order_by(KitchenOrder.queued_at.asc()).all()
    max_wait_seconds   = round((now_dt - pending_orders[0].queued_at).total_seconds(), 0) if pending_orders else None
    completed_waits    = [(k.completed_at - k.queued_at).total_seconds() for k in kitchen_completed_list if k.completed_at and k.queued_at]
    avg_completed_wait = round(sum(completed_waits) / len(completed_waits)) if completed_waits else None

    top_qty_map = defaultdict(float); top_revenue_map = defaultdict(float)
    # For weight/volume products: track sum and count per transaction line to compute avg portion
    _pid_line_qty = defaultdict(list)
    for r in rows:
        top_qty_map[r.product_id] += float(r.qty)
        top_revenue_map[r.product_id] += float(Decimal(str(r.qty)) * r.unit_price)
        _pid_line_qty[r.product_id].append(float(r.qty))
    all_pids = set(top_qty_map.keys()) | set(top_revenue_map.keys())
    _prod_rows = Product.query.filter(Product.id.in_(all_pids)).with_entities(
        Product.id, Product.name, Product.sold_by_weight, Product.unit_type
    ).all() if all_pids else []
    name_map      = {r.id: r.name for r in _prod_rows}
    weighted_pids = {r.id for r in _prod_rows if r.sold_by_weight}
    base_unit_map = {r.id: ('g' if r.unit_type == 'weight' else 'ml') for r in _prod_rows if r.sold_by_weight}
    # Median qty per sale line = typical portion actually bought (median ignores bulk outliers)
    avg_portion_map = {
        pid: median(lines)
        for pid, lines in _pid_line_qty.items()
        if pid in weighted_pids and lines
    }

    def _norm(pid, qty):
        avg = avg_portion_map.get(pid)
        return qty / avg if avg else qty

    top_by_qty = [{
        'product_id': pid, 'name': name_map.get(pid, str(pid)),
        'qty_sold': qty, 'normalized_qty': round(_norm(pid, qty), 2),
        'stat_unit_size': round(avg_portion_map[pid], 1) if pid in avg_portion_map else None,
        'base_unit': base_unit_map.get(pid),
        'revenue': round(top_revenue_map.get(pid, 0), 2),
    } for pid, qty in sorted(top_qty_map.items(), key=lambda x: _norm(x[0], x[1]), reverse=True)[:10]]
    top_by_revenue = [{
        'product_id': pid, 'name': name_map.get(pid, str(pid)),
        'revenue': round(rev, 2), 'qty_sold': round(top_qty_map.get(pid, 0), 2),
        'normalized_qty': round(_norm(pid, top_qty_map.get(pid, 0)), 2),
        'stat_unit_size': round(avg_portion_map[pid], 1) if pid in avg_portion_map else None,
        'base_unit': base_unit_map.get(pid),
    } for pid, rev in sorted(top_revenue_map.items(), key=lambda x: x[1], reverse=True)[:10]]

    revenue_per_hour = defaultdict(float)
    for r in rows: revenue_per_hour[r.date_time.hour] += float(Decimal(str(r.qty)) * r.unit_price)
    hourly = [{'hour': h, 'revenue': round(v, 2)} for h, v in sorted(revenue_per_hour.items())]

    revenue_per_day = defaultdict(float); tx_per_day = defaultdict(set); profit_per_day = defaultdict(float)
    for r in rows:
        d = r.date_time.date().isoformat()
        revenue_per_day[d] += float(Decimal(str(r.qty)) * r.unit_price); tx_per_day[d].add(r.sale_id)
    if sale_ids:
        sale_date_map = {r.sale_id: r.date_time.date().isoformat() for r in rows}
        for c in consumptions:
            d = sale_date_map.get(c.sale_id)
            if d: profit_per_day[d] += float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)))
    daily = [{'date': d, 'revenue': round(revenue_per_day[d], 2), 'profit': round(revenue_per_day[d] - profit_per_day.get(d, 0), 2), 'tx_count': len(tx_per_day[d])} for d in sorted(revenue_per_day.keys())]
    best_day  = max(daily, key=lambda x: x['revenue'], default=None)
    worst_day = min(daily, key=lambda x: x['revenue'], default=None) if len(daily) > 1 else None

    revenue_per_minute = defaultdict(float)
    for r in rows: revenue_per_minute[r.date_time.strftime('%H:%M')] += float(Decimal(str(r.qty)) * r.unit_price)
    minutely = [{'minute': m, 'revenue': round(v, 2)} for m, v in sorted(revenue_per_minute.items())]

    emp_revenue = defaultdict(float); emp_tx = defaultdict(set); emp_items = defaultdict(float); emp_first = {}; emp_last = {}
    for r in rows:
        uid = r.user_id or 0; val = float(Decimal(str(r.qty)) * r.unit_price)
        emp_revenue[uid] += val; emp_tx[uid].add(r.sale_id); emp_items[uid] += float(r.qty)
        dt = r.date_time
        if uid not in emp_first or dt < emp_first[uid]: emp_first[uid] = dt
        if uid not in emp_last  or dt > emp_last[uid]:  emp_last[uid]  = dt

    sq = UserSession.query.filter(UserSession.logged_in >= start_dt, UserSession.logged_in <= end_dt)
    if user_id_filter: sq = sq.filter(UserSession.user_id == user_id_filter)
    sessions_in_range = sq.all()
    emp_session_minutes = defaultdict(float); emp_session_count = defaultdict(int); emp_sessions = defaultdict(list)
    emp_first_login = {}; emp_last_activity = {}
    for s in sessions_in_range:
        natural_end  = s.logged_out or now_dt
        clamped_end  = min(natural_end, end_dt, now_dt)
        duration_min = (clamped_end - s.logged_in).total_seconds() / 60.0
        if duration_min <= 0: continue
        emp_session_minutes[s.user_id] += duration_min; emp_session_count[s.user_id] += 1
        emp_sessions[s.user_id].append({'login': s.logged_in.isoformat(), 'logout': s.logged_out.isoformat() if s.logged_out else None, 'last_active': s.last_active.isoformat() if s.last_active else None, 'duration_min': round(duration_min, 1), 'open': s.logged_out is None})
        uid = s.user_id
        if uid not in emp_first_login or s.logged_in < emp_first_login[uid]: emp_first_login[uid] = s.logged_in
        act = s.last_active or clamped_end
        if uid not in emp_last_activity or act > emp_last_activity[uid]: emp_last_activity[uid] = act

    all_user_ids = list({r.user_id for r in rows if r.user_id} | set(emp_session_minutes.keys()))
    user_name_map = {u.id: u.username for u in User.query.filter(User.id.in_(all_user_ids)).all()} if all_user_ids else {}
    employee_stats = []
    for uid in set(list(emp_revenue.keys()) + list(emp_session_minutes.keys())):
        if uid == 0: continue
        tx_count = len(emp_tx.get(uid, set())); rev = emp_revenue.get(uid, 0); items = emp_items.get(uid, 0)
        sess_mins = emp_session_minutes.get(uid, 0); sess_count = emp_session_count.get(uid, 0)
        first_login = emp_first_login.get(uid); last_activity = emp_last_activity.get(uid)
        work_span_mins = (last_activity - first_login).total_seconds() / 60.0 if (first_login and last_activity and last_activity > first_login) else sess_mins
        rev_per_hour = rev / (work_span_mins / 60) if work_span_mins > 0 else None
        tx_per_hour  = tx_count / (work_span_mins / 60) if work_span_mins > 0 else None
        employee_stats.append({'user_id': uid, 'name': user_name_map.get(uid, f'User {uid}'), 'transactions': tx_count, 'revenue': round(rev, 2), 'items_sold': round(items, 2), 'avg_tx_value': round(rev / tx_count, 2) if tx_count > 0 else 0, 'session_count': sess_count, 'session_minutes': round(sess_mins, 1), 'revenue_per_hour': round(rev_per_hour, 2) if rev_per_hour is not None else None, 'tx_per_hour': round(tx_per_hour, 2) if tx_per_hour is not None else None, 'first_sale': emp_first.get(uid, {}) and emp_first[uid].isoformat() if uid in emp_first else None, 'last_sale': emp_last.get(uid, {}) and emp_last[uid].isoformat() if uid in emp_last else None, 'sessions': sorted(emp_sessions.get(uid, []), key=lambda x: x['login'])})
    employee_stats.sort(key=lambda x: x['revenue'], reverse=True)

    supplier_costs = defaultdict(float)
    bq = StockBatch.query.filter(StockBatch.purchased_at >= start_dt, StockBatch.purchased_at <= end_dt)
    if product_id_filter: bq = bq.filter(StockBatch.product_id == product_id_filter)
    _batches_in_range = bq.all()
    _sup_ids = {b.supplier_id for b in _batches_in_range if b.supplier_id}
    _sup_map = {s.id: s.name for s in Supplier.query.filter(Supplier.id.in_(_sup_ids)).all()} if _sup_ids else {}
    for b in _batches_in_range:
        sup_name = _sup_map.get(b.supplier_id, 'Unknown') if b.supplier_id else 'Unknown'
        supplier_costs[sup_name] += float(b.qty_purchased_base) * float(b.cost_per_base_unit)
    supplier_breakdown = [{'supplier': k, 'total_cost': round(v, 2)} for k, v in sorted(supplier_costs.items(), key=lambda x: x[1], reverse=True)]

    filtered_product_name = (db.session.get(Product, product_id_filter) or Product(name=None)).name if product_id_filter else None
    filtered_user_name    = (db.session.get(User, user_id_filter) or User(username=None)).username if user_id_filter else None

    # ── Customer/channel metrics — online_orders table only exists on Lady Coleen box ──
    from sqlalchemy import text as _text
    _has_online_orders = False
    try:
        db.session.execute(_text("SELECT 1 FROM online_orders LIMIT 1"))
        _has_online_orders = True
    except Exception:
        db.session.rollback()

    if _has_online_orders:
        _oo_join  = "LEFT JOIN online_orders oo ON oo.pos_sale_id::text = s.sale_id"
        _oo_flag  = "MAX(CASE WHEN oo.id IS NOT NULL THEN 1 ELSE 0 END)"
    else:
        _oo_join  = ""
        _oo_flag  = "0"

    # When product/user filters are active, restrict the period CTE to the already-filtered sale_ids
    _period_filter = ""
    if sale_ids:
        sid_csv = ', '.join(f"'{sid}'" for sid in sale_ids)
        _period_filter = f" AND sale_id IN ({sid_csv})"

    cust_row = db.session.execute(_text(f"""
        WITH all_receipts AS (
            SELECT s.sale_id, s.customer_id, MIN(s.date_time) AS sale_at,
                {_oo_flag} AS is_online,
                SUM(CASE WHEN NOT COALESCE(s.voided,FALSE) THEN s.qty * s.unit_price ELSE 0 END) AS receipt_total,
                BOOL_OR(COALESCE(s.voided,FALSE)) AS is_voided
            FROM sales s
            {_oo_join}
            GROUP BY s.sale_id, s.customer_id
        ),
        period AS (SELECT * FROM all_receipts WHERE sale_at >= :s AND sale_at <= :e{_period_filter}),
        first_purchase AS (
            SELECT customer_id, MIN(sale_at) AS first_sale_at
            FROM all_receipts WHERE customer_id IS NOT NULL AND NOT is_voided
            GROUP BY customer_id
        ),
        period_customers AS (
            SELECT customer_id, COUNT(*) AS receipt_count
            FROM period WHERE customer_id IS NOT NULL AND NOT is_voided
            GROUP BY customer_id
        )
        SELECT
            COUNT(DISTINCT period.sale_id) FILTER (WHERE NOT period.is_voided)                AS total_receipts,
            COUNT(DISTINCT period.sale_id) FILTER (WHERE period.is_voided)                    AS voided_receipts,
            COUNT(DISTINCT period.sale_id) FILTER (WHERE NOT period.is_voided AND is_online=1) AS online_receipts,
            COUNT(DISTINCT period.sale_id) FILTER (WHERE NOT period.is_voided AND is_online=0) AS instore_receipts,
            COALESCE(SUM(receipt_total) FILTER (WHERE NOT period.is_voided AND is_online=1),0) AS online_revenue,
            COALESCE(SUM(receipt_total) FILTER (WHERE NOT period.is_voided AND is_online=0),0) AS instore_revenue,
            COUNT(DISTINCT period.customer_id) FILTER (WHERE NOT period.is_voided)             AS distinct_customers,
            (SELECT COUNT(*) FROM first_purchase WHERE first_sale_at >= :s AND first_sale_at <= :e) AS new_customers,
            (SELECT COUNT(*) FROM first_purchase fp JOIN period_customers pc ON pc.customer_id=fp.customer_id WHERE fp.first_sale_at < :s) AS returning_customers,
            (SELECT COUNT(*) FROM period_customers WHERE receipt_count > 1) AS repeat_customers
        FROM period
    """), {'s': start_dt, 'e': end_dt}).fetchone()

    cr = cust_row
    distinct_customers  = int(cr.distinct_customers or 0)
    total_receipts_ct   = int(cr.total_receipts or 0)
    voided_receipts_ct  = int(cr.voided_receipts or 0)
    all_receipts_ct     = total_receipts_ct + voided_receipts_ct
    online_revenue      = float(cr.online_revenue or 0)
    instore_revenue     = float(cr.instore_revenue or 0)
    repeat_customer_rate = round(int(cr.repeat_customers or 0) / distinct_customers * 100, 1) if distinct_customers else 0
    revenue_per_customer = round(total_sales_value / distinct_customers, 2) if distinct_customers else None
    void_receipt_rate    = round(voided_receipts_ct / all_receipts_ct * 100, 1) if all_receipts_ct else 0

    return jsonify({
        'filtered_product_id': product_id_filter, 'filtered_product_name': filtered_product_name,
        'filtered_user_id': user_id_filter, 'filtered_user_name': filtered_user_name,
        'transactions_count': transactions_count, 'total_sales_value': round(total_sales_value, 2),
        'total_items_sold': round(total_items_sold, 2), 'avg_basket_value': round(avg_basket_value, 2),
        'avg_basket_qty': round(avg_basket_qty, 2), 'total_cogs': round(total_cogs, 2),
        'gross_profit': round(gross_profit, 2), 'gross_margin': gross_margin,
        'total_writeoff_cost': round(total_writeoff_cost, 2), 'writeoff_count': total_writeoff_count,
        'kitchen_orders_today': len(kitchen_completed_list), 'avg_wait_seconds': max_wait_seconds,
        'avg_completed_wait': avg_completed_wait, 'top_products': top_by_qty,
        'top_by_revenue': top_by_revenue, 'revenue_per_hour': hourly, 'revenue_per_day': daily,
        'best_day': best_day, 'worst_day': worst_day, 'supplier_breakdown': supplier_breakdown,
        'revenue_per_minute': minutely, 'employee_stats': employee_stats,
        # ── New metrics ──
        'new_customers':         int(cr.new_customers or 0),
        'returning_customers':   int(cr.returning_customers or 0),
        'distinct_customers':    distinct_customers,
        'online_revenue':        round(online_revenue, 2),
        'instore_revenue':       round(instore_revenue, 2),
        'online_receipts':       int(cr.online_receipts or 0),
        'instore_receipts':      int(cr.instore_receipts or 0),
        'repeat_customer_rate':  repeat_customer_rate,
        'revenue_per_customer':  revenue_per_customer,
        'void_receipt_rate':     void_receipt_rate,
    })


@bp.route('/api/stats/drilldown')
def api_stats_drilldown():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    slice_type = request.args.get('type'); slice_val = request.args.get('value')
    start_dt, end_dt = _parse_range(request.args.get('start'), request.args.get('end'))
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)
    voided_filter     = request.args.get('voided') == '1'

    if voided_filter:
        q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == True)
    else:
        q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
    if user_id_filter:    q = q.filter(Sale.user_id    == user_id_filter)
    if product_id_filter: q = q.filter(Sale.product_id == product_id_filter)
    if slice_type == 'day' and slice_val:
        try:
            d = date.fromisoformat(slice_val)
            q = q.filter(Sale.date_time >= datetime(d.year, d.month, d.day), Sale.date_time <= datetime(d.year, d.month, d.day, 23, 59, 59))
        except Exception: pass
    elif slice_type == 'hour' and slice_val is not None:
        q = q.filter(db.func.extract('hour', Sale.date_time) == int(slice_val))
    elif slice_type == 'minute' and slice_val:
        try:
            hh, mm = slice_val.split(':')
            q = q.filter(db.func.extract('hour', Sale.date_time) == int(hh), db.func.extract('minute', Sale.date_time) == int(mm))
        except Exception: pass
    elif slice_type == 'product' and slice_val:
        q = q.filter(Sale.product_id == int(slice_val))
    elif slice_type == 'user' and slice_val:
        q = q.filter(Sale.user_id == int(slice_val))
    rows = q.order_by(Sale.date_time.desc()).all()

    sale_map = defaultdict(list)
    for r in rows: sale_map[r.sale_id].append(r)
    pids = {r.product_id for r in rows}; uids = {r.user_id for r in rows if r.user_id}
    prod_names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    user_names = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    transactions = []
    for sid, sale_rows in sale_map.items():
        sr = sorted(sale_rows, key=lambda r: r.date_time)
        total = float(sum(Decimal(str(r.qty)) * r.unit_price for r in sale_rows))
        transactions.append({'sale_id': sid[:8], 'date_time': sr[0].date_time.isoformat(), 'teller': user_names.get(sr[0].user_id, '-'), 'total': round(total, 2), 'item_count': sum(float(r.qty) for r in sale_rows), 'lines': [{'product': prod_names.get(r.product_id, str(r.product_id)), 'qty': float(r.qty), 'unit_price': float(r.unit_price), 'line_total': round(float(Decimal(str(r.qty)) * r.unit_price), 2)} for r in sorted(sale_rows, key=lambda x: x.product_id)]})
    transactions.sort(key=lambda x: x['date_time'], reverse=True)

    total_revenue = sum(t['total'] for t in transactions); total_tx = len(transactions)
    prod_rev = defaultdict(float); prod_qty = defaultdict(float)
    for t in transactions:
        for l in t['lines']: prod_rev[l['product']] += l['line_total']; prod_qty[l['product']] += l['qty']
    hour_rev = defaultdict(float)
    for t in transactions: hour_rev[int(t['date_time'][11:13])] += t['total']
    teller_rev = defaultdict(float); teller_tx = defaultdict(int)
    for t in transactions: teller_rev[t['teller']] += t['total']; teller_tx[t['teller']] += 1

    summary = {'total_revenue': round(total_revenue, 2), 'tx_count': total_tx, 'avg_tx_value': round(total_revenue / total_tx, 2) if total_tx else 0, 'largest_sale': max(transactions, key=lambda x: x['total'], default=None), 'smallest_sale': min(transactions, key=lambda x: x['total'], default=None) if total_tx > 1 else None, 'top_products': sorted([{'product': p, 'revenue': round(v, 2), 'qty': round(prod_qty[p], 2)} for p, v in prod_rev.items()], key=lambda x: x['revenue'], reverse=True)[:5], 'peak_hour': max(hour_rev, key=hour_rev.get) if hour_rev else None, 'teller_breakdown': sorted([{'teller': k, 'revenue': round(v, 2), 'tx_count': teller_tx[k]} for k, v in teller_rev.items()], key=lambda x: x['revenue'], reverse=True)}
    return jsonify({'summary': summary, 'transactions': transactions})


@bp.route('/api/stats/drilldown/supplier')
def api_stats_drilldown_supplier():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    supplier_name = request.args.get('supplier', '')
    start_dt, end_dt = _parse_range(request.args.get('start'), request.args.get('end'))
    if supplier_name and supplier_name != 'Unknown':
        sup = Supplier.query.filter_by(name=supplier_name).first()
        batches = StockBatch.query.filter(StockBatch.supplier_id == sup.id, StockBatch.purchased_at >= start_dt, StockBatch.purchased_at <= end_dt).order_by(StockBatch.purchased_at.desc()).all() if sup else []
    else:
        batches = StockBatch.query.filter(StockBatch.supplier_id == None, StockBatch.purchased_at >= start_dt, StockBatch.purchased_at <= end_dt).order_by(StockBatch.purchased_at.desc()).all()
    pids = {b.product_id for b in batches}
    prod_names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    return jsonify([{'date': b.purchased_at.isoformat(), 'product': prod_names.get(b.product_id, str(b.product_id)), 'qty_base': float(b.qty_purchased_base), 'cost_per_unit': float(b.cost_per_base_unit), 'total_cost': round(float(b.qty_purchased_base) * float(b.cost_per_base_unit), 2), 'remaining': float(b.qty_remaining_base)} for b in batches])


@bp.route('/api/stats/drilldown/kitchen')
def api_stats_drilldown_kitchen():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt, end_dt = _parse_range(request.args.get('start'), request.args.get('end'))
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)
    kq = KitchenOrder.query.filter(KitchenOrder.queued_at >= start_dt, KitchenOrder.queued_at <= end_dt)
    if user_id_filter:    kq = kq.filter(KitchenOrder.teller_id  == user_id_filter)
    if product_id_filter: kq = kq.filter(KitchenOrder.product_id == product_id_filter)
    orders = kq.order_by(KitchenOrder.queued_at.desc()).all()
    uids = {o.teller_id for o in orders if o.teller_id}
    user_names = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}
    return jsonify([{'id': o.id, 'sale_id': o.sale_id[:8], 'product': o.product_name, 'qty': float(o.qty), 'status': o.status, 'teller': user_names.get(o.teller_id, '-'), 'queued_at': o.queued_at.isoformat() if o.queued_at else None, 'completed_at': o.completed_at.isoformat() if o.completed_at else None, 'wait_seconds': round((o.completed_at - o.queued_at).total_seconds()) if (o.completed_at and o.queued_at) else None, 'notes': o.notes or ''} for o in orders])


@bp.route('/api/stats/drilldown/writeoffs')
def api_stats_drilldown_writeoffs():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt, end_dt = _parse_range(request.args.get('start'), request.args.get('end'))
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)
    wq = StockAdjustment.query.filter(StockAdjustment.adjustment_type == 'writeoff', StockAdjustment.adjusted_at >= start_dt, StockAdjustment.adjusted_at <= end_dt)
    if user_id_filter:    wq = wq.filter(StockAdjustment.user_id    == user_id_filter)
    if product_id_filter: wq = wq.filter(StockAdjustment.product_id == product_id_filter)
    writeoffs = wq.order_by(StockAdjustment.adjusted_at.desc()).all()
    pids = {w.product_id for w in writeoffs}; uids = {w.user_id for w in writeoffs if w.user_id}
    prods = {p.id: p for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    users = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}
    return jsonify([{'date': w.adjusted_at.isoformat() if w.adjusted_at else None, 'product': prods[w.product_id].name if w.product_id in prods else str(w.product_id), 'qty_change': float(w.qty_change_base), 'base_unit': prods[w.product_id].base_unit if w.product_id in prods else '', 'cost': float(w.cost_written_off) if w.cost_written_off else 0, 'by': users.get(w.user_id, '-')} for w in writeoffs])


@bp.route('/api/stats/drilldown/profit')
def api_stats_drilldown_profit():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt, end_dt = _parse_range(request.args.get('start'), request.args.get('end'))
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)
    q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
    if user_id_filter:    q = q.filter(Sale.user_id    == user_id_filter)
    if product_id_filter: q = q.filter(Sale.product_id == product_id_filter)
    rows = q.all()
    sale_ids = list({r.sale_id for r in rows})
    consumptions = StockConsumption.query.filter(StockConsumption.sale_id.in_(sale_ids)).all() if sale_ids else []
    rev_map = defaultdict(float); qty_map = defaultdict(float)
    for r in rows: rev_map[r.product_id] += float(Decimal(str(r.qty)) * r.unit_price); qty_map[r.product_id] += float(r.qty)
    sale_product_map = {}
    for r in rows:
        if r.sale_id not in sale_product_map: sale_product_map[r.sale_id] = r.product_id
    cogs_map = defaultdict(float)
    for c in consumptions:
        pid = sale_product_map.get(c.sale_id)
        if pid: cogs_map[pid] += float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)))
    all_pids = set(rev_map.keys())
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(all_pids)).all()} if all_pids else {}
    result = []
    for pid in sorted(all_pids, key=lambda x: rev_map[x], reverse=True):
        rev = rev_map[pid]; cogs = cogs_map.get(pid, 0); profit = rev - cogs
        result.append({'product': names.get(pid, str(pid)), 'qty_sold': round(qty_map[pid], 2), 'revenue': round(rev, 2), 'cogs': round(cogs, 2), 'profit': round(profit, 2), 'margin': round(profit / rev * 100, 1) if rev > 0 else None})
    return jsonify(result)


# ---------------------------------------------------------------------------
# CSV Exports
# ---------------------------------------------------------------------------

@bp.route('/admin/export/products')
def export_products_csv():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    default_markup = float(get_setting('markup_percent', 40) or 40)
    fifo_costs = {}
    for batch in StockBatch.query.filter(StockBatch.qty_remaining_base > 0).order_by(StockBatch.product_id, StockBatch.purchased_at.asc(), StockBatch.id.asc()).all():
        if batch.product_id not in fifo_costs: fifo_costs[batch.product_id] = float(batch.cost_per_base_unit)
    def recipe_cost(product_id, _depth=0):
        if _depth > 10: return 0.0
        total = 0.0
        for rl in RecipeLine.query.filter_by(product_id=product_id).all():
            ing = db.session.get(Product, rl.ingredient_id)
            if not ing: continue
            total += (recipe_cost(ing.id, _depth + 1) if ing.product_type == 'recipe' else fifo_costs.get(ing.id, 0.0)) * float(rl.qty_base)
        return total
    products = Product.query.filter_by(is_archived=False, is_for_sale=True).order_by(Product.name.asc()).all()
    sio = StringIO()
    sio.write('Product,Barcode,Category,Sold By,Unit,Wholesale Cost,Retail Price,Recommended Retail Price,Stock Available\n')
    for p in products:
        category = {'simple': 'General', 'stock_item': 'Stock Item', 'recipe': 'Prepared / Bundle'}.get(p.product_type, '')
        if p.sold_by_weight and p.unit_type: big = 'kg' if p.unit_type == 'weight' else 'L'; sold_by = f'Per {big}'; unit = big
        elif p.package_unit: sold_by = f'Per {p.package_unit}'; unit = p.package_unit
        else: sold_by = 'Per unit'; unit = 'unit'
        if p.product_type == 'stock_item':
            cost_base = fifo_costs.get(p.id, 0.0)
            if p.sold_by_weight: wholesale = round(cost_base * 1000.0, 4)
            else:
                pkg = float(p.package_size or 0)
                wholesale = round(cost_base * pkg, 4) if pkg else ''
        elif p.product_type == 'recipe': c = recipe_cost(p.id); wholesale = round(c, 4) if c > 0 else ''
        else: wholesale = ''
        if p.sold_by_weight and p.price_per_unit is not None: retail = round(float(p.price_per_unit) * 1000.0, 2)
        elif p.price is not None: retail = round(float(p.price), 2)
        else: retail = ''
        rrp = round(float(wholesale) * (1 + default_markup / 100), 2) if wholesale != '' else ''
        if p.product_type == 'stock_item':
            total_remaining = db.session.query(func.sum(StockBatch.qty_remaining_base)).filter_by(product_id=p.id).scalar() or 0
            stock_disp = f"{round(float(total_remaining)/1000, 3)}{unit}" if p.sold_by_weight else (f"{int(float(total_remaining) / float(p.package_size or 1))} {unit}s" if p.package_size else '')
        elif p.product_type == 'simple': stock_disp = str(p.stock_qty or 0)
        else: stock_disp = ''
        sio.write(f"{(p.name or '').replace(',',';')},{(p.barcode or '').replace(',',';')},{category},{sold_by},{unit},{wholesale},{retail},{rrp},{stock_disp}\n")
    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f"product_catalogue_{date.today().isoformat()}.csv")


@bp.route('/admin/export/transactions')
def export_transactions_csv():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt = _parse_dt(request.args.get('start')) or datetime(*date.today().timetuple()[:3])
    end_dt   = _parse_dt(request.args.get('end'), is_end=True) or datetime(*date.today().timetuple()[:3], 23, 59, 59)
    if end_dt < start_dt: start_dt, end_dt = end_dt, start_dt
    try: pid_filter = int(request.args.get('product_id')) if request.args.get('product_id') else None
    except (ValueError, TypeError): pid_filter = None
    try: uid_filter = int(request.args.get('user_id')) if request.args.get('user_id') else None
    except (ValueError, TypeError): uid_filter = None
    q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
    if pid_filter: q = q.filter(Sale.product_id == pid_filter)
    if uid_filter: q = q.filter(Sale.user_id    == uid_filter)
    rows = q.order_by(Sale.date_time.asc(), Sale.sale_id, Sale.id).all()
    pids = {r.product_id for r in rows}; uids = {r.user_id for r in rows if r.user_id}
    pname = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    uname = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}
    sio = StringIO(); sio.write('sale_id,date_time,product,qty,unit_price,subtotal,teller,payment_method,discount\n')
    for r in rows:
        subtotal = round(float(r.qty * r.unit_price), 2); disc = ''
        if r.discount_json:
            try:
                d = _json.loads(r.discount_json); parts = []
                if d.get('special'): parts.append(f"Special:{d['special']}")
                if d.get('item'):    parts.append(f"Item:{d['item'].get('value')}{d['item'].get('type','')}")
                if d.get('cart'):    parts.append(f"Cart:{d['cart'].get('value')}{d['cart'].get('type','')}")
                disc = ' | '.join(parts)
            except Exception: pass
        sio.write(f"{r.sale_id},{r.date_time.isoformat()},{pname.get(r.product_id, str(r.product_id)).replace(',',';')},{float(r.qty):.4f},{float(r.unit_price):.2f},{subtotal},{uname.get(r.user_id, '').replace(',',';')},{r.payment_method or ''},{disc}\n")
    slug = ''
    if pid_filter:
        fp = db.session.get(Product, pid_filter)
        if fp: slug = '_' + fp.name.replace(' ', '_')[:20]
    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f"sales{slug}_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv")


@bp.route('/admin/export/profit')
def export_profit_csv():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt = _parse_dt(request.args.get('start')) or datetime(*date.today().timetuple()[:3])
    end_dt   = _parse_dt(request.args.get('end'), is_end=True) or datetime(*date.today().timetuple()[:3], 23, 59, 59)
    try: pid_filter = int(request.args.get('product_id')) if request.args.get('product_id') else None
    except: pid_filter = None
    try: uid_filter = int(request.args.get('user_id')) if request.args.get('user_id') else None
    except: uid_filter = None
    q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
    if pid_filter: q = q.filter(Sale.product_id == pid_filter)
    if uid_filter: q = q.filter(Sale.user_id    == uid_filter)
    rows = q.all()
    sale_ids = list({r.sale_id for r in rows})
    consumptions = StockConsumption.query.filter(StockConsumption.sale_id.in_(sale_ids)).all() if sale_ids else []
    rev_map = defaultdict(float); qty_map = defaultdict(float)
    for r in rows: rev_map[r.product_id] += float(Decimal(str(r.qty)) * r.unit_price); qty_map[r.product_id] += float(r.qty)
    sale_product_map = {r.sale_id: r.product_id for r in rows}
    cogs_map = defaultdict(float)
    for c in consumptions:
        pid = sale_product_map.get(c.sale_id)
        if pid: cogs_map[pid] += float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)))
    pids = set(rev_map.keys())
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    sio = StringIO(); sio.write('product,qty_sold,revenue,cogs,gross_profit,margin_pct\n')
    for pid in sorted(pids, key=lambda x: rev_map[x], reverse=True):
        rev = rev_map[pid]; cogs = cogs_map.get(pid, 0); profit = rev - cogs
        sio.write(f"{names.get(pid, str(pid)).replace(',',';')},{round(qty_map[pid],2)},{round(rev,2)},{round(cogs,2)},{round(profit,2)},{round(profit/rev*100,1) if rev>0 else ''}\n")
    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f"profit_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv")


@bp.route('/admin/export/writeoffs')
def export_writeoffs_csv():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt = _parse_dt(request.args.get('start')) or datetime(*date.today().timetuple()[:3])
    end_dt   = _parse_dt(request.args.get('end'), is_end=True) or datetime(*date.today().timetuple()[:3], 23, 59, 59)
    try: pid_filter_wo = int(request.args.get('product_id')) if request.args.get('product_id') else None
    except: pid_filter_wo = None
    try: uid_filter_wo = int(request.args.get('user_id')) if request.args.get('user_id') else None
    except: uid_filter_wo = None
    wo_q = StockAdjustment.query.filter(StockAdjustment.adjustment_type == 'writeoff', StockAdjustment.adjusted_at >= start_dt, StockAdjustment.adjusted_at <= end_dt)
    if pid_filter_wo: wo_q = wo_q.filter(StockAdjustment.product_id == pid_filter_wo)
    if uid_filter_wo: wo_q = wo_q.filter(StockAdjustment.user_id    == uid_filter_wo)
    writeoffs = wo_q.order_by(StockAdjustment.adjusted_at.asc()).all()
    pids = {w.product_id for w in writeoffs}; uids = {w.user_id for w in writeoffs if w.user_id}
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    users = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}
    sio = StringIO(); sio.write('date,product,qty_written_off,base_unit,cost_lost,reason,by\n')
    for w in writeoffs: sio.write(f"{w.adjusted_at.isoformat()},{names.get(w.product_id, str(w.product_id)).replace(',',';')},{abs(float(w.qty_change_base or 0)):.4f},{w.base_unit or ''},{round(float(w.cost_written_off or 0),2)},{(w.reason or '').replace(',',';')},{users.get(w.user_id, '').replace(',',';')}\n")
    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f"writeoffs_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv")


@bp.route('/admin/export/suppliers')
def export_suppliers_csv():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt = _parse_dt(request.args.get('start')) or datetime(*date.today().timetuple()[:3])
    end_dt   = _parse_dt(request.args.get('end'), is_end=True) or datetime(*date.today().timetuple()[:3], 23, 59, 59)
    batches = StockBatch.query.filter(StockBatch.purchased_at >= start_dt, StockBatch.purchased_at <= end_dt).order_by(StockBatch.purchased_at.asc()).all()
    pids = {b.product_id for b in batches}; sids = {b.supplier_id for b in batches if b.supplier_id}
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    sups  = {s.id: s.name for s in Supplier.query.filter(Supplier.id.in_(sids)).all()} if sids else {}
    sio = StringIO(); sio.write('date,supplier,product,qty_purchased,base_unit,cost_per_unit,total_cost\n')
    for b in batches: sio.write(f"{b.purchased_at.isoformat()},{sups.get(b.supplier_id, 'Unknown').replace(',',';')},{names.get(b.product_id, str(b.product_id)).replace(',',';')},{float(b.qty_purchased_base):.4f},{b.base_unit or ''},{float(b.cost_per_base_unit):.4f},{round(float(b.qty_purchased_base)*float(b.cost_per_base_unit),2)}\n")
    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f"supplier_spend_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv")


@bp.route('/admin/export/staff')
def export_staff_csv():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt = _parse_dt(request.args.get('start')) or datetime(*date.today().timetuple()[:3])
    end_dt   = _parse_dt(request.args.get('end'), is_end=True) or datetime(*date.today().timetuple()[:3], 23, 59, 59)
    try: uid_filter = int(request.args.get('user_id')) if request.args.get('user_id') else None
    except: uid_filter = None
    sale_q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
    if uid_filter:
        sids = {r.sale_id for r in db.session.query(Sale.sale_id).filter(Sale.user_id == uid_filter, Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False).all()}
        sale_q = sale_q.filter(Sale.sale_id.in_(sids))
    rows = sale_q.all()
    sess_q = UserSession.query.filter(UserSession.logged_in >= start_dt, UserSession.logged_in <= end_dt)
    if uid_filter: sess_q = sess_q.filter(UserSession.user_id == uid_filter)
    sessions = sess_q.all()
    all_uids = {r.user_id for r in rows if r.user_id} | {s.user_id for s in sessions}
    user_map = {u.id: u for u in User.query.filter(User.id.in_(all_uids)).all()} if all_uids else {}
    emp_revenue = defaultdict(float); emp_tx = defaultdict(set); emp_items = defaultdict(float); emp_first = {}; emp_last = {}
    for r in rows:
        uid = r.user_id or 0
        if not uid: continue
        val = float(Decimal(str(r.qty)) * r.unit_price)
        emp_revenue[uid] += val; emp_tx[uid].add(r.sale_id); emp_items[uid] += float(r.qty)
        dt = r.date_time
        if uid not in emp_first or dt < emp_first[uid]: emp_first[uid] = dt
        if uid not in emp_last  or dt > emp_last[uid]:  emp_last[uid]  = dt
    now_utc = datetime.utcnow()
    emp_session_minutes = defaultdict(float); emp_session_count = defaultdict(int); emp_first_login = {}; emp_last_activity = {}
    for s in sessions:
        natural_end = s.logged_out or now_utc; clamped_end = min(natural_end, end_dt, now_utc)
        dur = (clamped_end - s.logged_in).total_seconds() / 60.0
        if dur <= 0: continue
        emp_session_minutes[s.user_id] += dur; emp_session_count[s.user_id] += 1
        uid = s.user_id
        if uid not in emp_first_login or s.logged_in < emp_first_login[uid]: emp_first_login[uid] = s.logged_in
        act = s.last_active or clamped_end
        if uid not in emp_last_activity or act > emp_last_activity[uid]: emp_last_activity[uid] = act
    sio = StringIO(); sio.write('employee,role,transactions,revenue,avg_sale,items_sold,sessions,time_logged_in_min,revenue_per_hour,sales_per_hour,first_sale,last_sale\n')
    for uid in sorted(set(emp_revenue.keys()) | set(emp_session_minutes.keys()), key=lambda u: emp_revenue.get(u, 0), reverse=True):
        u = user_map.get(uid); tx_count = len(emp_tx.get(uid, set())); rev = emp_revenue.get(uid, 0)
        sess_mins = emp_session_minutes.get(uid, 0); sess_cnt = emp_session_count.get(uid, 0)
        first_login = emp_first_login.get(uid); last_activity = emp_last_activity.get(uid)
        span_mins = (last_activity - first_login).total_seconds() / 60.0 if (first_login and last_activity and last_activity > first_login) else sess_mins
        rev_per_hour = round(rev / (span_mins / 60), 2) if span_mins > 0 else ''
        tx_per_hour  = round(tx_count / (span_mins / 60), 2) if span_mins > 0 else ''
        first_sale_str = emp_first[uid].isoformat() if uid in emp_first else ''
        last_sale_str  = emp_last[uid].isoformat()  if uid in emp_last  else ''
        sio.write(f"{(u.username if u else f'User {uid}').replace(',',';')},{(u.role if u else '').replace(',',';')},{tx_count},{round(rev,2)},{round(rev/tx_count,2) if tx_count>0 else 0},{round(emp_items.get(uid,0),2)},{sess_cnt},{round(sess_mins,1)},{rev_per_hour},{tx_per_hour},{first_sale_str},{last_sale_str}\n")
    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    emp_slug = ''
    if uid_filter:
        fu = db.session.get(User, uid_filter)
        if fu: emp_slug = f"_{fu.username.replace(' ','_')}"
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=f"staff_stats{emp_slug}_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv")


# ── New drilldown: Channels ──────────────────────────────────────────────────

@bp.route('/api/stats/drilldown/channels')
def api_stats_drilldown_channels():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt, end_dt = _parse_range(request.args.get('start'), request.args.get('end'))
    from sqlalchemy import text as _text

    _has_oo = False
    try:
        db.session.execute(_text("SELECT 1 FROM online_orders LIMIT 1"))
        _has_oo = True
    except Exception:
        db.session.rollback()

    _oo_j = "LEFT JOIN online_orders oo ON oo.pos_sale_id::text = s.sale_id" if _has_oo else ""
    _oo_f = "MAX(CASE WHEN oo.id IS NOT NULL THEN 1 ELSE 0 END)" if _has_oo else "0"

    daily_rows = db.session.execute(_text(f"""
        WITH receipts AS (
            SELECT
                s.sale_id,
                DATE(MIN(s.date_time))                                          AS sale_date,
                SUM(CASE WHEN NOT COALESCE(s.voided,FALSE) THEN s.qty * s.unit_price ELSE 0 END) AS receipt_total,
                BOOL_OR(COALESCE(s.voided,FALSE))                               AS is_voided,
                {_oo_f}                                                         AS is_online
            FROM sales s {_oo_j}
            WHERE s.date_time >= :s AND s.date_time <= :e
            GROUP BY s.sale_id
        )
        SELECT
            sale_date::text,
            COALESCE(SUM(receipt_total) FILTER (WHERE is_online=1 AND NOT is_voided), 0) AS online_rev,
            COALESCE(SUM(receipt_total) FILTER (WHERE is_online=0 AND NOT is_voided), 0) AS instore_rev,
            COUNT(DISTINCT sale_id) FILTER (WHERE is_online=1 AND NOT is_voided)          AS online_count,
            COUNT(DISTINCT sale_id) FILTER (WHERE is_online=0 AND NOT is_voided)          AS instore_count
        FROM receipts
        GROUP BY sale_date
        ORDER BY sale_date
    """), {'s': start_dt, 'e': end_dt}).fetchall()

    delivery_rows = []
    if _has_oo:
        try:
            delivery_rows = db.session.execute(_text("""
                SELECT delivery_method, COUNT(*) AS cnt
                FROM online_orders
                WHERE created_at >= :s AND created_at <= :e AND status != 'cancelled'
                GROUP BY delivery_method
            """), {'s': start_dt, 'e': end_dt}).fetchall()
        except Exception:
            db.session.rollback()

    return jsonify({
        'daily': [{'date': r[0], 'online_rev': float(r[1]), 'instore_rev': float(r[2]),
                   'online_count': int(r[3]), 'instore_count': int(r[4])} for r in daily_rows],
        'delivery_methods': {r[0]: int(r[1]) for r in delivery_rows},
    })


# ── New drilldown: Customers ─────────────────────────────────────────────────

@bp.route('/api/stats/drilldown/customers')
def api_stats_drilldown_customers():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt, end_dt = _parse_range(request.args.get('start'), request.args.get('end'))
    from sqlalchemy import text as _text

    daily_rows = db.session.execute(_text("""
        WITH all_receipts AS (
            SELECT s.sale_id, s.customer_id, MIN(s.date_time) AS sale_at,
                BOOL_OR(COALESCE(s.voided,FALSE)) AS is_voided
            FROM sales s
            WHERE s.customer_id IS NOT NULL
            GROUP BY s.sale_id, s.customer_id
        ),
        first_purchase AS (
            SELECT customer_id, MIN(sale_at) AS first_sale_at
            FROM all_receipts WHERE NOT is_voided
            GROUP BY customer_id
        ),
        period_receipts AS (
            SELECT ar.*, DATE(ar.sale_at) AS sale_date,
                CASE WHEN fp.first_sale_at >= :s AND fp.first_sale_at <= :e THEN 'new' ELSE 'returning' END AS ctype
            FROM all_receipts ar
            JOIN first_purchase fp ON fp.customer_id = ar.customer_id
            WHERE ar.sale_at >= :s AND ar.sale_at <= :e AND NOT ar.is_voided
        )
        SELECT sale_date::text,
            COUNT(DISTINCT customer_id) FILTER (WHERE ctype='new')       AS new_c,
            COUNT(DISTINCT customer_id) FILTER (WHERE ctype='returning') AS returning_c
        FROM period_receipts
        GROUP BY sale_date ORDER BY sale_date
    """), {'s': start_dt, 'e': end_dt}).fetchall()

    freq_row = db.session.execute(_text("""
        WITH period_receipts AS (
            SELECT s.sale_id, s.customer_id, BOOL_OR(COALESCE(s.voided,FALSE)) AS is_voided
            FROM sales s
            WHERE s.date_time >= :s AND s.date_time <= :e AND s.customer_id IS NOT NULL
            GROUP BY s.sale_id, s.customer_id
        ),
        customer_counts AS (
            SELECT customer_id, COUNT(*) AS receipt_count
            FROM period_receipts WHERE NOT is_voided
            GROUP BY customer_id
        )
        SELECT
            COUNT(*) FILTER (WHERE receipt_count = 1)  AS once,
            COUNT(*) FILTER (WHERE receipt_count BETWEEN 2 AND 5) AS two_to_five,
            COUNT(*) FILTER (WHERE receipt_count > 5)  AS six_plus
        FROM customer_counts
    """), {'s': start_dt, 'e': end_dt}).fetchone()

    return jsonify({
        'new_vs_returning_daily': [{'date': r[0], 'new': int(r[1]), 'returning': int(r[2])} for r in daily_rows],
        'frequency_distribution': {
            'once':        int(freq_row[0] or 0),
            'two_to_five': int(freq_row[1] or 0),
            'six_plus':    int(freq_row[2] or 0),
        },
    })


@bp.route('/api/stats/drilldown/customer-list')
def api_stats_drilldown_customer_list():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    start_dt, end_dt  = _parse_range(request.args.get('start'), request.args.get('end'))
    ctype             = request.args.get('type', 'all')   # 'new' | 'returning' | 'all'
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)

    q = db.session.query(Sale).filter(
        Sale.date_time >= start_dt, Sale.date_time <= end_dt,
        Sale.voided == False, Sale.customer_id.isnot(None),
    )
    if user_id_filter:    q = q.filter(Sale.user_id    == user_id_filter)
    if product_id_filter: q = q.filter(Sale.product_id == product_id_filter)
    rows = q.all()

    cust_sale_map = defaultdict(set)
    cust_rev_map  = defaultdict(float)
    for r in rows:
        cust_sale_map[r.customer_id].add(r.sale_id)
        cust_rev_map[r.customer_id] += float(Decimal(str(r.qty)) * r.unit_price)

    cust_ids = list(cust_sale_map.keys())
    if not cust_ids:
        return jsonify({'customers': [], 'type': ctype})

    cust_objs = {c.id: c for c in Customer.query.filter(Customer.id.in_(cust_ids)).all()}
    fp_rows = db.session.query(Sale.customer_id, func.min(Sale.date_time).label('first_at')).filter(
        Sale.customer_id.in_(cust_ids), Sale.voided == False,
    ).group_by(Sale.customer_id).all()
    first_at_map = {r.customer_id: r.first_at for r in fp_rows}

    customers = []
    for cid in cust_ids:
        first_at = first_at_map.get(cid)
        is_new = bool(first_at and start_dt <= first_at <= end_dt)
        if ctype == 'new'       and not is_new: continue
        if ctype == 'returning' and     is_new: continue
        c = cust_objs.get(cid)
        customers.append({
            'customer_id':    cid,
            'name':           c.name if c else f'Customer #{cid}',
            'visits':         len(cust_sale_map[cid]),
            'revenue':        round(cust_rev_map[cid], 2),
            'first_purchase': first_at.isoformat() if first_at else None,
            'is_new':         is_new,
        })
    customers.sort(key=lambda x: x['revenue'], reverse=True)
    return jsonify({'customers': customers, 'type': ctype})

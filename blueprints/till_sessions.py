"""
Till session management — end-of-day cash-up / Z-report (ISSUE-33).

POST /api/till/sessions     — close the till (admin)
GET  /api/till/sessions     — list sessions with summary (admin)
GET  /api/till/sessions/summary — current-day summary for Close Till modal
"""
from decimal import Decimal
from datetime import datetime, date

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from sqlalchemy import case
from helpers import require_role, current_user, _parse_dt, get_setting
from models import db, Sale, TillSession, User

bp = Blueprint('till_sessions', __name__)


def _sum_split_cash(start_dt, end_dt):
    """Sum cash_tendered on split payment first-line rows (non-null cash_tendered)."""
    from models import Sale as _Sale
    r = db.session.query(func.coalesce(func.sum(_Sale.cash_tendered), 0)).filter(
        _Sale.date_time >= start_dt, _Sale.date_time <= end_dt,
        _Sale.voided == False, _Sale.payment_method == 'split',
        _Sale.cash_tendered.isnot(None),
    ).scalar()
    return Decimal(str(r))


def _sum_split_card(start_dt, end_dt):
    """Sum card_amount on split payment first-line rows."""
    from models import Sale as _Sale
    r = db.session.query(func.coalesce(func.sum(_Sale.card_amount), 0)).filter(
        _Sale.date_time >= start_dt, _Sale.date_time <= end_dt,
        _Sale.voided == False, _Sale.payment_method == 'split',
        _Sale.card_amount.isnot(None),
    ).scalar()
    return Decimal(str(r))


def _sum_cash_refunds(start_dt, end_dt):
    """Sum absolute value of return rows — cash actually paid out of the drawer."""
    r = db.session.query(func.coalesce(func.sum(Sale.qty * Sale.unit_price), 0)).filter(
        Sale.date_time >= start_dt,
        Sale.date_time <= end_dt,
        Sale.voided == False,
        Sale.payment_method == 'return',
    ).scalar()
    # qty is negative on return rows, so qty*price is negative; abs() for display
    return abs(Decimal(str(r)))


def _sum_sales(start_dt, end_dt, payment_method=None, voided=False):
    q = db.session.query(func.coalesce(func.sum(Sale.qty * Sale.unit_price), 0)).filter(
        Sale.date_time >= start_dt,
        Sale.date_time <= end_dt,
        Sale.voided == voided,
        db.or_(Sale.payment_method.is_(None), Sale.payment_method != 'return'),
    )
    if payment_method:
        q = q.filter(Sale.payment_method == payment_method)
    return Decimal(str(q.scalar()))


@bp.route('/api/till/sessions/summary', methods=['GET'])
def api_till_summary():
    """Return today's sales totals for the Close Till modal. Admin only."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    # Default period: from the last close (or midnight) to now
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    last = TillSession.query.order_by(TillSession.closed_at.desc()).first()
    period_start = last.closed_at if last and last.closed_at > today_start else today_start
    now = datetime.utcnow()

    cash_sales  = _sum_sales(period_start, now, payment_method='cash')
    card_sales  = _sum_sales(period_start, now, payment_method='card')
    qr_sales    = _sum_sales(period_start, now, payment_method='qr')
    split_cash   = _sum_split_cash(period_start, now)
    split_card   = _sum_split_card(period_start, now)
    total_sales  = _sum_sales(period_start, now)
    void_total   = _sum_sales(period_start, now, voided=True)
    cash_refunds = _sum_cash_refunds(period_start, now)

    opening_float = Decimal('0')
    if last:
        # Suggest opening float = last close's counted cash if it was within the last 24h
        if (now - last.closed_at).total_seconds() < 86400:
            opening_float = Decimal(str(last.counted_cash))

    vat_registered = get_setting('vat_registered', 'false') == 'true'
    vat_rate       = Decimal(str(get_setting('vat_rate', 15) or 15)) / 100
    vat_amount     = (total_sales * vat_rate / (1 + vat_rate)).quantize(Decimal('0.01')) if vat_registered else Decimal('0')

    return jsonify({
        'period_start': period_start.isoformat(),
        'period_end':   now.isoformat(),
        'cash_sales':   float(cash_sales + split_cash),
        'card_sales':   float(card_sales + split_card),
        'qr_sales':     float(qr_sales),
        'total_sales':  float(total_sales),
        'void_total':   float(void_total),
        'cash_refunds': float(cash_refunds),
        'vat_amount':   float(vat_amount),
        'vat_registered': vat_registered,
        'suggested_opening_float': float(opening_float),
        'last_close': last.closed_at.isoformat() if last else None,
    })


@bp.route('/api/till/sessions', methods=['POST'])
def api_till_close():
    """Close the till: record cash count and compute over/under. Admin only."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.json or {}
    try:
        counted_cash  = Decimal(str(data['counted_cash']))
        opening_float = Decimal(str(data.get('opening_float', 0)))
    except (KeyError, Exception):
        return jsonify({'error': 'counted_cash required (numeric)'}), 400

    opened_at_raw = data.get('opened_at')
    today_start   = datetime.combine(date.today(), datetime.min.time())
    opened_at     = _parse_dt(opened_at_raw) or today_start
    now           = datetime.utcnow()

    cash_sales  = _sum_sales(opened_at, now, payment_method='cash')
    card_sales  = _sum_sales(opened_at, now, payment_method='card')
    split_cash  = _sum_split_cash(opened_at, now)
    split_card  = _sum_split_card(opened_at, now)
    total_sales = _sum_sales(opened_at, now)
    void_total  = _sum_sales(opened_at, now, voided=True)
    # Cash refunds paid out: sum absolute value of return rows (negative qty × price)
    cash_refunds = _sum_cash_refunds(opened_at, now)

    total_cash = cash_sales + split_cash
    total_card = card_sales + split_card
    # expected_cash = float in drawer = opening float + cash sales - cash refunds paid out
    expected_cash = opening_float + total_cash - cash_refunds
    over_under    = counted_cash - expected_cash

    u = current_user()
    session_row = TillSession(
        opened_at=opened_at,
        closed_at=now,
        opened_by=u.id if u else None,
        closed_by=u.id if u else None,
        opening_float=opening_float,
        counted_cash=counted_cash,
        pos_cash_sales=total_cash,
        pos_card_sales=total_card,
        pos_total_sales=total_sales,
        expected_cash=expected_cash,
        over_under=over_under,
        void_total=void_total,
        cash_refunds=cash_refunds,
        notes=(data.get('notes') or '').strip() or None,
    )
    db.session.add(session_row)
    db.session.commit()

    return jsonify({
        'ok': True,
        'id': session_row.id,
        'cash_sales': float(total_cash),
        'card_sales': float(total_card),
        'total_sales': float(total_sales),
        'cash_refunds': float(cash_refunds),
        'expected_cash': float(expected_cash),
        'over_under': float(over_under),
        'void_total': float(void_total),
    })


@bp.route('/api/till/sessions', methods=['GET'])
def api_till_sessions_list():
    """List past sessions. Admin only."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    limit = min(int(request.args.get('limit', 30)), 200)
    rows  = TillSession.query.order_by(TillSession.closed_at.desc()).limit(limit).all()

    users = {u.id: u.username for u in User.query.all()} if rows else {}

    return jsonify([{
        'id':             r.id,
        'opened_at':      r.opened_at.isoformat(),
        'closed_at':      r.closed_at.isoformat(),
        'opened_by':      users.get(r.opened_by, ''),
        'closed_by':      users.get(r.closed_by, ''),
        'opening_float':  float(r.opening_float),
        'counted_cash':   float(r.counted_cash),
        'pos_cash_sales': float(r.pos_cash_sales),
        'pos_card_sales': float(r.pos_card_sales),
        'pos_total_sales':float(r.pos_total_sales),
        'expected_cash':  float(r.expected_cash),
        'over_under':     float(r.over_under),
        'void_total':     float(r.void_total),
        'cash_refunds':   float(r.cash_refunds or 0),
        'notes':          r.notes,
    } for r in rows])

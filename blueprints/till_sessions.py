"""
Till session management — end-of-day cash-up / Z-report (ISSUE-33).

POST /api/till/sessions     — close the till (admin)
GET  /api/till/sessions     — list sessions with summary (admin)
GET  /api/till/sessions/summary — current-day summary for Close Till modal
"""
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from sqlalchemy import case
from helpers import require_role, current_user, _parse_dt, get_setting, audit_event, audit_policy
from models import db, Sale, SaleHeader, TillSession, User

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
    """Sum cash actually paid out of the drawer for returns.

    Rev 5 P2-4: only the CASH portion of each refund counts — a card refund
    doesn't move expected cash, unlike the old behavior which summed every
    return row's full value as cash regardless of how the original sale was
    paid. Post-fix return rows carry their own cash_tendered (the return
    endpoint stamps it, prorated against the original tender). Pre-fix rows
    have cash_tendered=NULL — for those, fall back to the full refund value
    as cash, matching what this function always assumed before the fix. That
    fallback only ever applies to historical data, never to a new return.
    """
    rows = db.session.query(Sale.qty, Sale.unit_price, Sale.cash_tendered).filter(
        Sale.date_time >= start_dt,
        Sale.date_time <= end_dt,
        Sale.voided == False,
        Sale.payment_method == 'return',
    ).all()
    total = Decimal('0')
    for qty, unit_price, cash_tendered in rows:
        if cash_tendered is not None:
            total += Decimal(str(cash_tendered))
        else:
            total += abs(Decimal(str(qty)) * Decimal(str(unit_price)))
    return total


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


def _vat_summary(start_dt, end_dt):
    """VAT for the report window, read ONLY from the Rev 5 P1-1 sale_headers
    snapshot — never recomputed from current settings (that was the original
    defect: a later rate/registration change would retroactively alter a past
    Z-report). Voiding always voids every line sharing a sale_id (see
    api_transaction_void), so "any non-voided row in range" identifies the
    in-scope sale_ids; returns are excluded, matching _sum_sales's basis for
    total_sales (a return is its own transaction with no header of its own).

    A window can straddle the P1-1 cutover: some in-range sale_ids have a
    'per_line' header (real per-line VAT), others a 'legacy_flat' header (a
    frozen flat-rate approximation from scripts/backfill_vat_headers.py, using
    whatever rate was current when that script ran — not necessarily the rate
    actually in effect at sale time, since the old system never recorded it).
    These are methodologically different numbers. Per Rev 5 P1-1b, summing them
    into one commingled total would misrepresent both, so they are kept and
    reported separately; the caller decides how to present that split.
    """
    sale_id_rows = db.session.query(Sale.sale_id).filter(
        Sale.date_time >= start_dt, Sale.date_time <= end_dt,
        Sale.voided == False,
        db.or_(Sale.payment_method.is_(None), Sale.payment_method != 'return'),
    ).distinct().all()
    sale_ids = {r[0] for r in sale_id_rows}
    if not sale_ids:
        return {
            'vat_per_line': Decimal('0.00'), 'vat_legacy_flat': Decimal('0.00'),
            'per_line_count': 0, 'legacy_flat_count': 0, 'unrecorded_count': 0,
        }
    headers = SaleHeader.query.filter(SaleHeader.sale_id.in_(sale_ids)).all()
    by_id = {h.sale_id: h for h in headers}
    vat_per_line = Decimal('0.00')
    vat_legacy_flat = Decimal('0.00')
    per_line_count = 0
    legacy_flat_count = 0
    for sid in sale_ids:
        h = by_id.get(sid)
        if h is None:
            continue  # counted via unrecorded_count below
        if h.vat_method == 'per_line':
            vat_per_line += Decimal(str(h.total_vat))
            per_line_count += 1
        elif h.vat_method == 'legacy_flat':
            vat_legacy_flat += Decimal(str(h.total_vat))
            legacy_flat_count += 1
    unrecorded_count = len(sale_ids) - per_line_count - legacy_flat_count
    return {
        'vat_per_line': vat_per_line.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
        'vat_legacy_flat': vat_legacy_flat.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
        'per_line_count': per_line_count,
        'legacy_flat_count': legacy_flat_count,
        'unrecorded_count': unrecorded_count,
    }


@bp.route('/api/till/sessions/summary', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
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

    # VAT (Rev 5 P1-1) — read from the checkout-time sale_headers snapshot only,
    # never recomputed from current settings. vat_registered here is a display
    # toggle only (whether to show the VAT row at all before any sales exist in
    # the window) — it does not gate or alter any financial figure below it.
    vat_registered = get_setting('vat_registered', 'false') == 'true'
    vat = _vat_summary(period_start, now)
    vat_spans_cutover = vat['per_line_count'] > 0 and vat['legacy_flat_count'] > 0
    vat_note = None
    if vat_spans_cutover:
        vat_note = (
            f"Window includes {vat['legacy_flat_count']} pre-P1-1 sale(s) shown separately "
            "as 'VAT as originally recorded' — not summed with the per-line total."
        )
    elif vat['unrecorded_count']:
        vat_note = (
            f"{vat['unrecorded_count']} sale(s) in this window have no VAT record "
            "(not yet backfilled) and are excluded from both VAT totals."
        )

    return jsonify({
        'period_start': period_start.isoformat(),
        'period_end':   now.isoformat(),
        'cash_sales':   float(cash_sales + split_cash),
        'card_sales':   float(card_sales + split_card),
        'qr_sales':     float(qr_sales),
        'total_sales':  float(total_sales),
        'void_total':   float(void_total),
        'cash_refunds': float(cash_refunds),
        # Authoritative, real per-line VAT for this window — never includes a
        # legacy_flat contribution (see vat_amount_legacy_flat / vat_note below).
        'vat_amount':   float(vat['vat_per_line']),
        'vat_amount_legacy_flat': float(vat['vat_legacy_flat']),
        'vat_spans_cutover': vat_spans_cutover,
        'vat_unrecorded_count': vat['unrecorded_count'],
        'vat_note': vat_note,
        'vat_registered': vat_registered,
        'suggested_opening_float': float(opening_float),
        'last_close': last.closed_at.isoformat() if last else None,
    })


@bp.route('/api/till/sessions', methods=['POST'])
@audit_policy('AUDITED')
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
    db.session.flush()
    audit_event('till_closed', 'till_sessions', session_row.id, after={
        'opened_at': opened_at.isoformat(), 'closed_at': now.isoformat(),
        'opening_float': float(opening_float), 'counted_cash': float(counted_cash),
        'expected_cash': float(expected_cash), 'over_under': float(over_under),
        'pos_cash_sales': float(total_cash), 'pos_card_sales': float(total_card),
        'cash_refunds': float(cash_refunds), 'void_total': float(void_total),
    })
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
@audit_policy('NO_STATE_CHANGE')
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

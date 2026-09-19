import uuid
import json as _json
from decimal import Decimal
from datetime import datetime, date, timedelta
from collections import defaultdict

from flask import Blueprint, jsonify, request, Response
from sqlalchemy import func

from flask import current_app
from helpers import (
    require_login, require_role, current_user,
    consume_fifo, reverse_fifo, reverse_consignment_liabilities, _parse_dt,
    qty_bucket, get_stock_level, collect_kitchen_items, auto_produce_on_negative,
    get_setting, write_stock_movement, reverse_consignment_liabilities_partial,
    get_fifo_cost_per_unit, audit_event, audit_policy,
)
from decimal import ROUND_HALF_UP
from models import (
    db,
    Product, RecipeLine, StockBatch, StockConsumption, KitchenOrder,
    Sale, SaleHeader, Purchase, User, Category, PackagingUsage,
    ConsignmentLiability,
)

bp = Blueprint('transactions', __name__)


def _record_packaging_usage(sale_uuid):
    """After a successful checkout, record which packaging was used with which products.
    Runs in its own transaction so a failure here never blocks or rolls back the sale.
    """
    try:
        # Re-query from DB — learn only from what actually persisted
        sale_rows = Sale.query.filter_by(sale_id=sale_uuid, voided=False).all()
        if not sale_rows:
            return

        product_ids = list({r.product_id for r in sale_rows})
        products = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()}

        # Load packaging category IDs
        pkg_cat_ids = {c.id for c in Category.query.filter_by(is_packaging=True).all()}

        # Split into packaging and non-packaging
        pkg_pids = [pid for pid in product_ids if products.get(pid) and products[pid].category_id in pkg_cat_ids]
        if not pkg_pids:
            return  # no packaging in this sale — nothing to record

        # Sum qty per non-packaging product
        non_pkg_qty = {}
        for r in sale_rows:
            if r.product_id not in {p for p in pkg_pids}:
                non_pkg_qty[r.product_id] = non_pkg_qty.get(r.product_id, 0) + float(r.qty)

        from sqlalchemy import text
        upsert_sql = text("""
            INSERT INTO packaging_usage (product_id, qty_bucket, packaging_product_id, use_count, last_used_at)
            VALUES (:pid, :bucket, :pkg_pid, 1, NOW())
            ON CONFLICT (product_id, qty_bucket, packaging_product_id)
            DO UPDATE SET use_count = packaging_usage.use_count + 1, last_used_at = NOW()
        """)

        # Per-product pairings
        for pid, qty in non_pkg_qty.items():
            bucket = qty_bucket(qty)
            for pkg_pid in pkg_pids:
                db.session.execute(upsert_sql, {'pid': pid, 'bucket': bucket, 'pkg_pid': pkg_pid})

        # Cart-level pairings (product_id=0 sentinel)
        total_non_pkg_qty = sum(non_pkg_qty.values())
        cart_bucket = qty_bucket(total_non_pkg_qty)
        for pkg_pid in pkg_pids:
            db.session.execute(upsert_sql, {'pid': 0, 'bucket': cart_bucket, 'pkg_pid': pkg_pid})

        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('packaging_usage recording failed for sale %s', sale_uuid)


def _serialize_sale_rows(rows):
    """Snapshot Sale rows to JSON for the append-only audit trail (ISSUE-31).

    Rev 5 P3-1: includes voided/void_reason so a before/after pair actually
    differs for a void — omitting them made every void's after-snapshot
    identical to its before-snapshot, defeating the point of capturing both.
    """
    out = []
    for r in rows:
        out.append({
            'id': r.id, 'sale_id': r.sale_id,
            'date_time': r.date_time.isoformat() if r.date_time else None,
            'product_id': r.product_id, 'qty': str(r.qty), 'unit_price': str(r.unit_price),
            'user_id': r.user_id, 'customer_id': r.customer_id,
            'payment_method': r.payment_method, 'cash_tendered': (str(r.cash_tendered) if r.cash_tendered is not None else None),
            'discount_json': r.discount_json, 'sub_log': r.sub_log,
            'voided': r.voided, 'void_reason': r.void_reason,
        })
    return out


def _audit(event_type, target_id, before_rows, note=None, after_rows=None):
    # Rev 5 P3-1: delegates to the shared audit service (helpers.audit_event)
    # instead of writing AuditLog directly — same call in the same
    # transaction as before, now also stamped with correlation_id/store_id/
    # source, and able to carry an after-snapshot when the caller has one.
    audit_event(event_type, 'sales', target_id, before=before_rows, after=after_rows, reason=note)


@bp.route('/api/transactions', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_transactions_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    u           = current_user()
    limit_param = request.args.get('limit')
    start_param = request.args.get('start')
    end_param   = request.args.get('end')

    q = db.session.query(Sale).filter(
        Sale.voided == False,
        db.or_(Sale.payment_method.is_(None), Sale.payment_method != 'return'),
    )

    if not require_role('admin'):
        # Tellers see only their own last 5 transactions
        q = q.filter(Sale.user_id == u.id)

    if u.has_role('admin'):
        today = date.today()
        if start_param or end_param:
            start_dt = _parse_dt(start_param) or datetime(today.year, today.month, today.day)
            end_dt   = _parse_dt(end_param, is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)
        else:
            # Default: today only
            start_dt = datetime(today.year, today.month, today.day)
            end_dt   = datetime(today.year, today.month, today.day, 23, 59, 59)
        q = q.filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt)

    rows = q.order_by(Sale.id.desc()).limit(2000).all()

    _pids = {r.product_id for r in rows if r.product_id}
    product_names = {prod.id: prod.name for prod in Product.query.filter(Product.id.in_(_pids)).all()} if _pids else {}
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
    is_admin_req = u.has_role('admin')

    # Track per-ingredient COGS in a single pass (used for sale total and per-line attribution).
    # New rows have Sale.cogs stamped at checkout; old rows fall back to StockConsumption.
    cogs_by_sale       = defaultdict(Decimal)
    cogs_by_ingredient = defaultdict(lambda: defaultdict(Decimal))  # sale_id -> ingredient_id -> cost
    _new_cogs_ids = {r.sale_id for r in rows if r.cogs is not None}
    for r in rows:
        if r.cogs is not None:
            cogs_by_sale[r.sale_id] += Decimal(str(r.cogs))
            cogs_by_ingredient[r.sale_id][r.product_id] += Decimal(str(r.cogs))
    if sale_ids:
        for c in StockConsumption.query.filter(StockConsumption.sale_id.in_(sale_ids)).all():
            if c.sale_id in _new_cogs_ids:
                continue  # already handled via Sale.cogs above
            cost = Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit))
            cogs_by_sale[c.sale_id] += cost
            cogs_by_ingredient[c.sale_id][c.ingredient_id] += cost

    # For admin: fetch product types + recipe lines to compute per-line COGS
    product_types_map       = {}
    recipe_lines_by_product = defaultdict(list)  # recipe_product_id -> [(ingredient_id, qty_base)]
    if is_admin_req and rows:
        all_prod_ids = {r.product_id for r in rows}
        for pt in Product.query.filter(Product.id.in_(all_prod_ids)).with_entities(Product.id, Product.product_type).all():
            product_types_map[pt.id] = pt.product_type
        recipe_pids = {pid for pid, ptype in product_types_map.items() if ptype == 'recipe'}
        if recipe_pids:
            for rl in RecipeLine.query.filter(RecipeLine.product_id.in_(recipe_pids)).with_entities(RecipeLine.product_id, RecipeLine.ingredient_id, RecipeLine.qty_base).all():
                recipe_lines_by_product[rl.product_id].append((rl.ingredient_id, float(rl.qty_base)))

    result = []
    for sid in sorted(grouped.keys(), key=lambda k: max(x.id for x in grouped[k]), reverse=True):
        items, total = [], Decimal('0')
        sale_disc    = discounts_by_sale.get(sid, {})

        # Pre-compute per-recipe-product COGS attribution for this sale (proportional by expected qty)
        recipe_cogs_per_pid: dict = {}
        if is_admin_req:
            # Qty sold per product in this sale
            qty_by_pid: dict = defaultdict(Decimal)
            for ln in grouped[sid]:
                qty_by_pid[ln.product_id] += Decimal(str(ln.qty))
            # For each ingredient consumed, find which recipe products in this sale use it and split proportionally
            ingredient_attributions: dict = defaultdict(list)  # ingredient_id -> [(recipe_pid, expected_qty)]
            for pid in qty_by_pid:
                if product_types_map.get(pid) == 'recipe':
                    for ing_id, ing_qty_per in recipe_lines_by_product.get(pid, []):
                        ingredient_attributions[ing_id].append((pid, float(qty_by_pid[pid]) * ing_qty_per))
            recipe_cogs_acc: dict = defaultdict(Decimal)
            for ing_id, attributions in ingredient_attributions.items():
                total_cost = cogs_by_ingredient[sid].get(ing_id, Decimal('0'))
                total_exp  = sum(exp for _, exp in attributions)
                for recipe_pid, exp in attributions:
                    recipe_cogs_acc[recipe_pid] += (total_cost * Decimal(str(exp)) / Decimal(str(total_exp))) if total_exp > 0 else (total_cost / len(attributions))
            recipe_cogs_per_pid = dict(recipe_cogs_acc)

        for ln in grouped[sid]:
            subtotal = Decimal(str(ln.qty)) * ln.unit_price
            total   += subtotal
            line     = {'product_id': ln.product_id, 'name': product_names.get(ln.product_id) or ln.product_name or f'Product {ln.product_id}', 'qty': float(ln.qty), 'unit_price': float(ln.unit_price), 'subtotal': float(subtotal)}
            if ln.discount_json:
                try: line['discount'] = _json.loads(ln.discount_json)
                except Exception: pass
            if is_admin_req:
                if ln.cogs is not None:
                    line_cogs = Decimal(str(ln.cogs))
                else:
                    ptype = product_types_map.get(ln.product_id, 'stock_item')
                    if ptype == 'stock_item':
                        line_cogs = cogs_by_ingredient[sid].get(ln.product_id, Decimal('0'))
                    elif ptype == 'recipe':
                        line_cogs = recipe_cogs_per_pid.get(ln.product_id, Decimal('0'))
                        # Batch-produced recipes consume their own finished-goods batch (ingredient_id == product_id)
                        if line_cogs == Decimal('0'):
                            line_cogs = cogs_by_ingredient[sid].get(ln.product_id, Decimal('0'))
                    else:
                        line_cogs = Decimal('0')
                line_cogs_f = float(round(line_cogs, 4))
                if line_cogs_f > 0:
                    sub_f = float(subtotal)
                    line['cogs']   = line_cogs_f
                    line['margin'] = round((sub_f - line_cogs_f) / sub_f * 100, 1) if sub_f > 0 else None
            items.append(line)

        cogs    = float(round(cogs_by_sale.get(sid, Decimal('0')), 4))
        total_f = float(round(total, 2))
        margin  = round((total_f - cogs) / total_f * 100, 1) if total_f > 0 and cogs > 0 else None
        result.append({'id': sid, 'date_time': dates[sid].isoformat(), 'total': total_f, 'lines': items, 'teller': users_by_sale.get(sid, ''), 'cogs': cogs if cogs > 0 else None, 'margin_pct': margin, 'flagged': flags_by_sale.get(sid, {}).get('flagged', False), 'flag_note': flags_by_sale.get(sid, {}).get('flag_note'), 'flag_resolved': flags_by_sale.get(sid, {}).get('flag_resolved', False), 'discount_by': sale_disc.get('discount_by', '')})

    if not u.has_role('admin'):
        result = result[:5]
    elif limit_param:
        try: result = result[:int(limit_param)]
        except Exception: pass
    return jsonify(result)


@bp.route('/api/transactions', methods=['POST'])
@audit_policy('EXPLICITLY_EXEMPT', reason='routine sale creation — the immutable Sale row IS '
              'the record; the audit trail covers corrections/reversals of it (void/edit/return), '
              'not its creation')
def api_transactions_post():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    cart = data.get('cart', [])
    if not cart:
        return jsonify({'error': 'Empty cart'}), 400
    if len(cart) > 100:
        return jsonify({'error': 'Cart too large (max 100 items)'}), 400

    sale_uuid    = str(uuid.uuid4())
    now          = datetime.utcnow()
    u            = current_user()
    customer_id  = data.get('customer_id')
    # Tender info (ISSUE-29): the teller's cash/card choice for this whole transaction.
    # Applies to every line of the sale (they share sale_id). Normalised + validated.
    pm_raw       = (data.get('payment_method') or '').strip().lower()
    if pm_raw not in ('cash', 'card', 'qr', 'split'):
        return jsonify({'error': 'payment_method required (cash/card/qr/split)'}), 400
    payment_method = pm_raw
    cash_tendered = None
    if data.get('cash_tendered') not in (None, ''):
        try:
            cash_tendered = Decimal(str(data.get('cash_tendered')))
        except Exception:
            cash_tendered = None
    card_amount = None
    if data.get('card_amount') not in (None, ''):
        try:
            card_amount = Decimal(str(data.get('card_amount')))
        except Exception:
            card_amount = None
    cart_discount        = data.get('cart_discount')
    draft_order_id       = str(data.get('draft_order_id') or '').strip() or None
    kitchen_already_sent = {str(k): Decimal(str(v)) for k, v in (data.get('kitchen_already_sent') or {}).items()}
    has_discount  = cart_discount or any(i.get('item_discount') or i.get('special_name') for i in cart)
    discount_by_id = (u.id if u else None) if has_discount else None

    force = bool(data.get('force', False))

    # ── Pre-flight policy check (read-only, before any DB writes) ──
    # Aggregate cart quantities per product first so duplicate lines don't bypass thresholds.
    _required = {}  # pid -> total Decimal qty in this cart
    for _item in cart:
        _pid = int(_item['product_id'])
        _required[_pid] = _required.get(_pid, Decimal('0')) + Decimal(str(_item.get('qty', 1)))

    _pre_stock   = {}   # pid -> Decimal stock level (for phantom batch calc later)
    _sale_blocks = []
    _sale_warns  = []
    for _pid, _total_qty in _required.items():
        _pc  = db.session.get(Product, _pid)
        if not _pc:
            continue
        _pol = getattr(_pc, 'inventory_policy', None) or 'ALLOW_NEGATIVE'
        if _pc.product_type == 'stock_item' or (_pc.product_type == 'recipe' and _pc.is_produced):
            _stk = Decimal(str(get_stock_level(_pid)))
            _pre_stock[_pid] = _stk
            if _pol != 'ALLOW_NEGATIVE' and _stk < _total_qty:
                _info = {'product_id': _pid, 'name': _pc.name,
                         'available': float(_stk), 'needed': float(_total_qty)}
                if _pol == 'STRICT':
                    _sale_blocks.append(_info)
                elif _pol == 'WARN':
                    _sale_warns.append(_info)
    if _sale_blocks:
        # Admins may override STRICT with force=True; tellers cannot.
        if force and u and u.has_role('admin'):
            _sale_blocks = []
        else:
            return jsonify({'error': 'Sale blocked: insufficient stock.',
                            'blocked': _sale_blocks}), 409
    if _sale_warns and not force:
        return jsonify({'warn': True,
                        'message': 'Some items are out of stock. Confirm to sell anyway.',
                        'warnings': _sale_warns}), 409

    # Auto-produce any produced recipes that would go negative so consume_fifo
    # finds stock and correctly records COGS (no negative placeholder needed).
    for _pid, _total_qty in _required.items():
        _pc = db.session.get(Product, _pid)
        if not (_pc and _pc.product_type == 'recipe' and _pc.is_produced):
            continue
        _pol = getattr(_pc, 'inventory_policy', None) or 'ALLOW_NEGATIVE'
        if _pol not in ('ALLOW_NEGATIVE', 'WARN'):
            continue
        _stk = _pre_stock.get(_pid, Decimal('0'))
        if _stk < _total_qty:
            _shortfall = _total_qty - max(Decimal('0'), _stk)
            auto_produce_on_negative(_pid, _shortfall, now, u)
            # Refresh pre_stock so the negative-placeholder logic below sees the new level
            _pre_stock[_pid] = Decimal(str(get_stock_level(_pid)))

    # VAT snapshot (Rev 5 P1-1) — read ONCE per transaction, not per line, then stamped
    # onto every line as it's created. Captures what was actually in effect at checkout
    # so a later settings change can never retroactively alter a past sale's VAT.
    _vat_registered = get_setting('vat_registered', 'false') == 'true'
    _vat_rate = Decimal(str(get_setting('vat_rate', 15) or 15))
    _vat_bucket_totals = {'standard': Decimal('0.00'), 'zero_rated': Decimal('0.00'), 'exempt': Decimal('0.00')}
    _header_total_vat = Decimal('0.00')

    for item in cart:
        pid        = int(item['product_id'])
        qty        = Decimal(str(item.get('qty', 1)))
        subs_raw   = item.get('subs', {})
        # Use the server-side price as the source of truth.
        _prod_price = Product.query.with_entities(
            Product.price, Product.price_per_unit, Product.sold_by_weight, Product.vat_type
        ).filter_by(id=pid).first()
        if _prod_price is None:
            return jsonify({'error': f'Product {pid} not found'}), 404
        # sold_by_weight items bill per base unit (price_per_unit); all others use price
        if _prod_price.sold_by_weight and _prod_price.price_per_unit:
            unit_price = Decimal(str(_prod_price.price_per_unit))
        else:
            unit_price = Decimal(str(_prod_price.price or 0))
        # Admin-authorized discounts: trust the client's discounted unit_price.
        # Capped at the server price so it can only go down, never up.
        has_disc = item.get('item_discount') or item.get('special_name') or cart_discount
        if u and u.has_role('admin') and has_disc and item.get('unit_price') is not None:
            try:
                client_price = Decimal(str(item['unit_price']))
                if Decimal('0') <= client_price <= unit_price:
                    unit_price = client_price
            except Exception:
                pass
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
        # payment_method on every line (they share sale_id); cash_tendered only on the
        # first line so it's recorded once per transaction, not double-counted per line.
        _first_line = (item is cart[0])

        # Per-line VAT (Rev 5 P1-1) — rounded to the cent HERE, independently per line,
        # never as a share of a basket-level total (that's what INV-7 checks). amount_excl
        # + vat_amount == amount_incl always holds by construction, not by two separate
        # formulas that could disagree by a cent.
        _vat_classification = (_prod_price.vat_type or 'standard')
        if _vat_classification not in _vat_bucket_totals:  # unrecognized -> Product's own column default
            _vat_classification = 'standard'
        _line_incl = (qty * unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if _vat_registered and _vat_classification == 'standard':
            _line_excl = (_line_incl / (Decimal('1') + _vat_rate / Decimal('100'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            _line_vat = _line_incl - _line_excl
            _line_rate = _vat_rate
        else:
            _line_excl = _line_incl
            _line_vat = Decimal('0.00')
            _line_rate = Decimal('0')
        _vat_bucket_totals[_vat_classification] += _line_excl
        _header_total_vat += _line_vat

        sale_row = Sale(sale_id=sale_uuid, date_time=now, product_id=pid, qty=qty, unit_price=unit_price, user_id=u.id if u else None, customer_id=customer_id, sub_log=sub_log_val, discount_json=discount_val, discount_by=discount_by_id, payment_method=payment_method, cash_tendered=(cash_tendered if _first_line else None), card_amount=(card_amount if _first_line else None), vat_classification=_vat_classification, vat_rate=_line_rate, amount_excl=_line_excl, vat_amount=_line_vat, amount_incl=_line_incl)
        db.session.add(sale_row)
        p = db.session.get(Product, pid, with_for_update=True)
        if not p: continue
        if p.product_type == 'stock_item' or (p.product_type == 'recipe' and p.is_produced):
            # Rev 5 P2-1: consume_fifo posts any shortfall to the negative placeholder
            # itself now — no separate caller-side shortfall/placeholder logic needed
            # (and keeping it would double-count the debt against consume_fifo's own).
            sale_row.cogs = consume_fifo(pid, qty, sale_uuid, now, sale_unit_price=unit_price)
        elif p.product_type == 'recipe':
            # Made-to-order: consume ingredients at point of sale
            line_cogs = Decimal('0')
            for rl in RecipeLine.query.filter_by(product_id=pid).all():
                actual_id = subs.get(rl.ingredient_id, rl.ingredient_id)
                if actual_id == -1: continue
                line_cogs += consume_fifo(actual_id, Decimal(str(rl.qty_base)) * qty, sale_uuid, now)
            for ex in extras:
                ex_id = int(ex.get('ingredient_id', 0)); ex_qty = Decimal(str(ex.get('qty_base', 0)))
                if ex_id and ex_qty > 0: line_cogs += consume_fifo(ex_id, ex_qty * qty, sale_uuid, now)
            sale_row.cogs = line_cogs
        else:
            sale_row.cogs = Decimal('0')
            # Consignment liability for simple products (no FIFO batches).
            # stock_item products get their liability inside consume_fifo.
            _csup = getattr(p, 'consignment_supplier_id', None)
            if p.is_consignment and _csup:
                _basis = getattr(p, 'settlement_basis', 'FIXED_COST')
                _sale_snap = None; _pct_snap = None
                if _basis == 'PCT_OF_SALE':
                    _pct = Decimal(str(p.consignment_pct or 0)) / Decimal('100')
                    _unit_cost = (unit_price * _pct).quantize(Decimal('0.000001'))
                    _sale_snap = float(unit_price)
                    _pct_snap  = float(p.consignment_pct or 0)
                else:
                    # FIXED_COST: manually-set price. UNIT_COST: auto from batch (no shipping).
                    # Simple products have no batch, so both use consignment_cost_per_unit.
                    _cuc = getattr(p, 'consignment_cost_per_unit', None)
                    _unit_cost = Decimal(str(_cuc)) if _cuc else Decimal('0')
                if _unit_cost > 0:
                    _amount = (qty * _unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    db.session.add(ConsignmentLiability(
                        supplier_id=_csup,
                        product_id=pid,
                        batch_id=None,
                        sale_id=sale_uuid,
                        qty_consumed=float(qty),
                        unit_cost=float(_unit_cost),
                        amount_owed=float(_amount),
                        sale_price_at_time=_sale_snap,
                        settlement_percent_at_time=_pct_snap,
                    ))

    # One sale_headers row per transaction (Rev 5 P1-1) — sums of the already-rounded
    # per-line amounts above, never a separate recomputation from a basket total.
    _header_total_excl = sum(_vat_bucket_totals.values(), Decimal('0.00'))
    db.session.add(SaleHeader(
        sale_id=sale_uuid,
        standard_rated_subtotal=_vat_bucket_totals['standard'],
        zero_rated_subtotal=_vat_bucket_totals['zero_rated'],
        exempt_subtotal=_vat_bucket_totals['exempt'],
        total_excl_vat=_header_total_excl,
        total_vat=_header_total_vat,
        total_incl_vat=_header_total_excl + _header_total_vat,
        vat_rate_snapshot=_vat_rate,
        vat_registered_snapshot=_vat_registered,
        vat_method='per_line',
        cash_tendered=cash_tendered,
        card_amount=card_amount,
        payment_method=payment_method,
    ))

    max_sort = db.session.query(func.max(KitchenOrder.sort_order)).filter_by(status='pending').scalar() or 0

    # Build kitchen orders — compute delta to skip items already sent via draft
    all_kitchen = []
    for item in cart:
        pid       = int(item['product_id'])
        total_qty = Decimal(str(item.get('qty', 1)))
        sent_qty  = kitchen_already_sent.get(str(pid), Decimal('0'))
        delta_qty = total_qty - sent_qty
        if delta_qty > 0:
            subs_m = {int(k): int(v) for k, v in item.get('subs', {}).items()}
            exts   = item.get('extras', [])
            all_kitchen.extend(collect_kitchen_items(pid, delta_qty, subs=subs_m, extras=exts))
    for pos, (ko_product, ko_qty, ko_ingredients) in enumerate(all_kitchen):
        db.session.add(KitchenOrder(sale_id=sale_uuid, product_id=ko_product.id, product_name=ko_product.name, qty=ko_qty, ingredients=_json.dumps(ko_ingredients), status='pending', sort_order=max_sort + pos + 1, queued_at=now, teller_id=u.id if u else None))

    # Link pre-sent draft kitchen orders to the real sale UUID
    if draft_order_id:
        for dko in KitchenOrder.query.filter_by(draft_order_id=draft_order_id).all():
            dko.sale_id = sale_uuid

    db.session.commit()
    return jsonify({'ok': True, 'transaction_id': sale_uuid, 'kitchen_orders': len(all_kitchen)})


def _vat_display(sale_id):
    """Single source of truth for how a sale's VAT is shown — every receipt surface
    (JSON receipt, thermal print, browser print) and the Z-report's per-sale detail
    all call this instead of independently recomputing (Rev 5 P1-1).

    Reads ONLY the checkout-time snapshot in sale_headers. Never recomputes from
    current settings — that was the original defect: a later VAT-rate or
    registration change would silently alter every past receipt's shown VAT.

    A legacy_flat header (scripts/backfill_vat_headers.py) is presented as "VAT as
    originally recorded", never as a recomputed or verified figure (Rev 5 P1-1b).
    A sale_id with no header at all (a return — returns don't create their own
    header — or pre-migration history the backfill hasn't reached yet) is reported
    as 'unrecorded' rather than silently falling back to the old live-recompute
    formula, which would reintroduce the exact bug this fixes.
    """
    header = SaleHeader.query.filter_by(sale_id=sale_id).first()
    if header is None:
        return {
            'vat_registered': False, 'vat_rate': 0.0, 'vat_amount': 0.0,
            'vat_method': 'unrecorded',
            'vat_note': 'No VAT record for this transaction (return, or pre-migration sale not yet backfilled).',
        }
    note = None
    if header.vat_method == 'legacy_flat':
        note = 'VAT as originally recorded (pre-P1-1 flat-rate calculation, not a verified per-line breakdown).'
    return {
        'vat_registered': bool(header.vat_registered_snapshot),
        'vat_rate':       float(header.vat_rate_snapshot or 0),
        'vat_amount':     float(header.total_vat),
        'vat_method':     header.vat_method,
        'vat_note':       note,
    }


def _receipt_lines(rows, product_map):
    return [{'name': product_map.get(r.product_id, f'Product {r.product_id}'),
              'qty': float(r.qty), 'unit_price': float(r.unit_price),
              'subtotal': float(Decimal(str(r.qty)) * r.unit_price),
              'vat_classification': r.vat_classification,
              'vat_amount': float(r.vat_amount) if r.vat_amount is not None else None} for r in rows]


@bp.route('/api/transactions/<sale_id>/receipt', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_transaction_receipt(sale_id):
    """Return receipt data for a sale. Used by the print receipt button."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    from helpers import get_setting
    rows = Sale.query.filter_by(sale_id=sale_id, voided=False).all()
    if not rows:
        return jsonify({'error': 'Transaction not found'}), 404
    product_map = {p.id: p.name for p in Product.query.filter(
        Product.id.in_({r.product_id for r in rows})).all()}
    lines = _receipt_lines(rows, product_map)
    total = sum(ln['subtotal'] for ln in lines)
    vat = _vat_display(sale_id)
    u = current_user()
    return jsonify({
        'sale_id':       sale_id,
        'date_time':     rows[0].date_time.isoformat(),
        'lines':         lines,
        'total':         round(total, 2),
        'payment_method': rows[0].payment_method,
        'cash_tendered': float(rows[0].cash_tendered) if rows[0].cash_tendered else None,
        'change':        round(float(rows[0].cash_tendered or 0) - total, 2) if rows[0].cash_tendered else None,
        'vat_registered': vat['vat_registered'],
        'vat_rate':      vat['vat_rate'],
        'vat_amount':    vat['vat_amount'],
        'vat_method':    vat['vat_method'],
        'vat_note':      vat['vat_note'],
        'store_name':    get_setting('branding_store_name', ''),
        'store_legal':   get_setting('branding_invoice_legal', ''),
        'vat_number':    get_setting('vat_number', ''),
        'footer':        get_setting('branding_invoice_footer', ''),
    })


@bp.route('/api/transactions/<sale_id>/print-receipt', methods=['POST'])
@audit_policy('NO_STATE_CHANGE')  # sends to a physical printer; no persisted state change
def api_transaction_print_receipt(sale_id):
    """Render a receipt image and send directly to the thermal printer via TSPL2."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    from helpers import get_setting
    from services.receipt_service import ReceiptRenderService
    from services.label_service import PrintDispatchService

    data       = request.json or {}
    printer_id = data.get('printer_id')

    rows = Sale.query.filter_by(sale_id=sale_id, voided=False).all()
    if not rows:
        return jsonify({'error': 'Transaction not found'}), 404

    product_map = {p.id: p.name for p in Product.query.filter(
        Product.id.in_({r.product_id for r in rows})).all()}
    lines = _receipt_lines(rows, product_map)
    total = sum(ln['subtotal'] for ln in lines)
    vat = _vat_display(sale_id)

    receipt_data = {
        'sale_id':        sale_id,
        'date_time':      rows[0].date_time.isoformat(),
        'lines':          lines,
        'total':          round(total, 2),
        'payment_method': rows[0].payment_method,
        'cash_tendered':  float(rows[0].cash_tendered) if rows[0].cash_tendered else None,
        'change':         round(float(rows[0].cash_tendered or 0) - total, 2) if rows[0].cash_tendered else None,
        'vat_registered': vat['vat_registered'],
        'vat_rate':       vat['vat_rate'],
        'vat_amount':     vat['vat_amount'],
        'vat_method':     vat['vat_method'],
        'vat_note':       vat['vat_note'],
        'store_name':     get_setting('branding_store_name', ''),
        'store_legal':    get_setting('branding_invoice_legal', ''),
        'vat_number':     get_setting('vat_number', ''),
        'footer':         get_setting('branding_invoice_footer', ''),
        'logo_file':      get_setting('branding_logo_file', ''),
    }

    width_mm = float(get_setting('receipt_width_mm', '72') or '72')

    try:
        svc      = ReceiptRenderService()
        img, h   = svc.render(receipt_data, width_mm=width_mm)
        dispatch = PrintDispatchService()
        result   = dispatch.send(img, printer_id=printer_id,
                                 width_mm=width_mm, height_mm=h, gap_mm=0)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'status': result.get('status'), 'notes': result.get('notes')})


@bp.route('/api/transactions/<sale_id>/browser-print-receipt', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_transaction_browser_print_receipt(sale_id):
    """Return a self-printing HTML receipt page — browser handles the printer protocol."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    from helpers import get_setting

    rows = Sale.query.filter_by(sale_id=sale_id, voided=False).all()
    if not rows:
        return jsonify({'error': 'Transaction not found'}), 404

    product_map = {p.id: p.name for p in Product.query.filter(
        Product.id.in_({r.product_id for r in rows})).all()}

    lines = _receipt_lines(rows, product_map)
    total = sum(ln['subtotal'] for ln in lines)

    vat = _vat_display(sale_id)
    vat_registered = vat['vat_registered']
    vat_rate_pct   = vat['vat_rate']
    vat_amount     = vat['vat_amount']
    vat_method     = vat['vat_method']
    vat_note       = vat['vat_note']

    store_name  = get_setting('branding_store_name', '') or 'Farm Stall'
    store_legal = get_setting('branding_invoice_legal', '') or ''
    vat_number  = get_setting('vat_number', '') or ''
    footer      = get_setting('branding_invoice_footer', '') or 'Thank you for your purchase!'
    width_mm    = float(get_setting('receipt_width_mm', '72') or '72')

    try:
        dt = datetime.fromisoformat(rows[0].date_time.isoformat()).strftime('%Y-%m-%d %H:%M')
    except Exception:
        dt = str(rows[0].date_time)[:16]

    def esc(s):
        return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    items_html = ''
    for ln in lines:
        items_html += f'''
        <tr>
          <td class="name">{esc(ln["name"])}</td>
          <td class="price">R{ln["subtotal"]:.2f}</td>
        </tr>'''
        if abs(ln['qty'] - 1.0) > 0.001:
            items_html += f'<tr><td class="detail" colspan="2">{ln["qty"]:.3f} &times; R{ln["unit_price"]:.2f}</td></tr>'

    pm = (rows[0].payment_method or '').upper()
    cash_tendered = float(rows[0].cash_tendered) if rows[0].cash_tendered else None
    change = round(cash_tendered - total, 2) if cash_tendered else None

    totals_html = f'<tr><td class="total-label"><b>TOTAL</b></td><td class="total-amount"><b>R{total:.2f}</b></td></tr>'
    if vat_registered:
        vat_label = f'VAT ({vat_rate_pct:.0f}%)' if vat_method != 'legacy_flat' else 'VAT (as recorded)'
        totals_html += f'<tr><td>{vat_label}</td><td>R{vat_amount:.2f}</td></tr>'
        if vat_note:
            totals_html += f'<tr><td class="detail" colspan="2">{esc(vat_note)}</td></tr>'
    if pm:
        totals_html += f'<tr><td>Payment</td><td>{esc(pm)}</td></tr>'
    if cash_tendered:
        totals_html += f'<tr><td>Tendered</td><td>R{cash_tendered:.2f}</td></tr>'
    if change and change > 0:
        totals_html += f'<tr><td>Change</td><td>R{change:.2f}</td></tr>'

    legal_html   = f'<div class="sub">{esc(store_legal)}</div>' if store_legal and store_legal != store_name else ''
    vat_num_html = f'<div class="sub">VAT No: {esc(vat_number)}</div>' if vat_registered and vat_number else ''

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{ size: {width_mm}mm auto; margin: 4mm 3mm; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Courier New', Courier, monospace; font-size: 10pt;
          width: {width_mm - 6}mm; }}
  .header {{ text-align: center; margin-bottom: 4pt; }}
  .header .store {{ font-size: 12pt; font-weight: bold; }}
  .header .sub {{ font-size: 9pt; }}
  .meta {{ font-size: 9pt; margin-bottom: 4pt; }}
  hr {{ border: none; border-top: 1px dashed #000; margin: 4pt 0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 9.5pt; }}
  td {{ vertical-align: top; padding: 1pt 0; }}
  td.name {{ width: 70%; word-break: break-word; }}
  td.price {{ width: 30%; text-align: right; white-space: nowrap; }}
  td.detail {{ font-size: 8.5pt; color: #444; padding-left: 8pt; }}
  td.total-label {{ font-size: 10.5pt; }}
  td.total-amount {{ font-size: 10.5pt; text-align: right; }}
  .footer {{ text-align: center; font-size: 8.5pt; margin-top: 6pt; }}
</style>
</head>
<body>
  <div class="header">
    <div class="store">{esc(store_name)}</div>
    {legal_html}
    {vat_num_html}
  </div>
  <div class="meta">
    <div>{esc(dt)}</div>
    <div>Receipt: #{sale_id[:8]}</div>
  </div>
  <hr>
  <table>{items_html}</table>
  <hr>
  <table>{totals_html}</table>
  <hr>
  <div class="footer">{esc(footer)}</div>
<script>window.onload = function() {{ window.print(); }};</script>
</body>
</html>"""

    return Response(html, mimetype='text/html')


@bp.route('/api/transactions/<sale_id>/flag', methods=['POST'])
@audit_policy('EXPLICITLY_EXEMPT', reason='flag state is visible directly on the Sale row and '
              'transactions list; not yet wired to the audit service')
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


@bp.route('/api/transactions/<sale_id>/return', methods=['POST'])
@audit_policy('AUDITED')
def api_transaction_return(sale_id):
    """Post-session return: partial or full reversal with FIFO stock restore.

    Accepts a list of {product_id, qty} to return. Creates a negative-qty Sale
    row (return_of=<sale_id>) so the original remains intact for SARS audit.
    Restores stock to FIFO batches via reverse_fifo on the new return_id.
    """
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    u    = current_user()
    data = request.json or {}
    lines = data.get('lines', [])  # [{product_id, qty}]
    reason = (data.get('reason') or '').strip()
    if not lines:
        return jsonify({'error': 'lines required'}), 400
    if not reason:
        return jsonify({'error': 'reason required'}), 400

    # Load the original sale to validate product_ids and qtys
    orig_rows = Sale.query.filter_by(sale_id=sale_id, voided=False).all()
    if not orig_rows:
        return jsonify({'error': 'Transaction not found or already voided'}), 404

    orig_by_pid = {}
    for r in orig_rows:
        orig_by_pid.setdefault(r.product_id, Decimal('0'))
        orig_by_pid[r.product_id] += Decimal(str(r.qty))

    # Subtract quantities already returned against this sale_id (prevent double-return)
    # Use original_sale_id column; fall back to void_reason pattern for legacy rows
    already_returned = Sale.query.filter(
        db.or_(
            Sale.original_sale_id == sale_id,
            Sale.void_reason.like(f'return:{sale_id}:%'),
        ),
        Sale.voided == False,
        Sale.payment_method == 'return',
    ).all()
    already_by_pid = {}
    for r in already_returned:
        already_by_pid.setdefault(r.product_id, Decimal('0'))
        already_by_pid[r.product_id] += abs(Decimal(str(r.qty)))

    for pid, returned_qty in already_by_pid.items():
        if pid in orig_by_pid:
            orig_by_pid[pid] = max(Decimal('0'), orig_by_pid[pid] - returned_qty)

    now = datetime.utcnow()
    return_uuid = str(uuid.uuid4())

    returned_lines = []
    _return_rows = []
    for item in lines:
        pid = int(item['product_id'])
        qty = Decimal(str(item['qty']))
        if qty <= 0:
            continue
        orig_qty = orig_by_pid.get(pid, Decimal('0'))
        if qty > orig_qty:
            return jsonify({'error': f'Return qty {qty} exceeds original {orig_qty} for product {pid}'}), 400

        orig_row = next((r for r in orig_rows if r.product_id == pid), None)
        unit_price = orig_row.unit_price if orig_row else Decimal('0')

        # Stamp proportional COGS on the return row so credit calculations don't need StockConsumption.
        return_cogs = None
        if orig_row and orig_row.cogs is not None and abs(Decimal(str(orig_row.qty))) > 0:
            return_cogs = Decimal(str(orig_row.cogs)) * (qty / abs(Decimal(str(orig_row.qty))))

        # Rev 5 P2-4: carry the refund tender forward instead of assuming cash.
        # TillSession.cash_refunds used to sum every return row as cash out of
        # the drawer regardless of how the original sale was paid, so refunding
        # a card sale still reduced expected cash as if physical notes had left
        # the till. Split against the ORIGINAL sale's own cash/card tender
        # (prorated for a split-tender original) so a partial return of one
        # product from a mixed-tender basket still refunds accurately.
        refund_amount = (qty * unit_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        _orig_pm = (orig_row.payment_method if orig_row else None) or 'cash'
        if _orig_pm == 'split':
            _orig_cash  = Decimal(str(orig_row.cash_tendered or 0)) if orig_row else Decimal('0')
            _orig_card  = Decimal(str(orig_row.card_amount or 0)) if orig_row else Decimal('0')
            _orig_total = _orig_cash + _orig_card
            if _orig_total > 0:
                refund_cash = (refund_amount * _orig_cash / _orig_total).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                refund_card = refund_amount - refund_cash
            else:
                refund_cash, refund_card = refund_amount, Decimal('0')
        elif _orig_pm == 'card':
            refund_cash, refund_card = Decimal('0'), refund_amount
        else:  # cash, qr, or unset -> treated as cash out of the drawer
            refund_cash, refund_card = refund_amount, Decimal('0')

        _return_row = Sale(
            sale_id=return_uuid,
            date_time=now,
            product_id=pid,
            qty=-qty,
            unit_price=unit_price,
            user_id=u.id if u else None,
            original_sale_id=sale_id,
            void_reason=f'return:{sale_id}:{reason}',
            payment_method='return',
            cogs=return_cogs,
            cash_tendered=refund_cash,
            card_amount=refund_card,
        )
        db.session.add(_return_row)
        _return_rows.append(_return_row)

        p = db.session.get(Product, pid, with_for_update=True)
        if not p:
            pass
        elif p.product_type == 'stock_item' or (p.product_type == 'recipe' and p.is_produced):
            # Restore: look up FIFO cost from original sale consumptions
            consumptions = StockConsumption.query.filter_by(
                sale_id=sale_id, ingredient_id=pid).all()
            orig_batch_cost = Decimal('0')
            if consumptions:
                total_consumed = sum(Decimal(str(c.qty_consumed_base)) for c in consumptions)
                if total_consumed > 0:
                    orig_batch_cost = sum(
                        Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit))
                        for c in consumptions
                    ) / total_consumed
            if orig_batch_cost <= 0:
                orig_batch_cost = unit_price
            _ret_batch = StockBatch(
                product_id=pid,
                qty_purchased_base=qty,
                qty_remaining_base=qty,
                cost_per_base_unit=orig_batch_cost,
                purchased_at=now,
                user_id=u.id if u else None,
            )
            db.session.add(_ret_batch)
            db.session.flush()
            write_stock_movement(
                _ret_batch, movement_type='RETURN_SALEABLE', qty_delta=qty,
                unit_cost=orig_batch_cost, source_type='return',
                source_id=return_uuid, when=now,
            )
            # Rev 5 P2-2: reverse only the liability the returned qty is worth,
            # pro-rata — a no-op when this product carries no outstanding
            # consignment liability for this sale.
            reverse_consignment_liabilities_partial(sale_id, pid, qty)
        elif p.product_type == 'recipe':
            # Made-to-order recipe (Rev 5 P2-2): restore each ingredient by the
            # RECIPE'S OWN formula (qty_base * qty returned), not by trying to
            # locate and prorate the original StockConsumption rows. The old
            # approach queried consumption by (sale_id, ingredient_id) alone,
            # which is ambiguous the moment two recipes in the same sale share
            # an ingredient — restoring one over-restores the other. It also
            # computed its ratio against orig_by_pid, which is the REMAINING
            # returnable qty (already reduced by prior partial returns on this
            # sale), not the true original qty — over-restoring on any partial
            # return after the first. Creating a fresh batch per ingredient (at
            # its current FIFO cost, like the stock_item branch's own new-batch
            # pattern above) sidesteps both: no shared history to disambiguate,
            # no ratio/denominator to get wrong.
            for rl in RecipeLine.query.filter_by(product_id=pid).all():
                restore_qty = Decimal(str(rl.qty_base)) * qty
                if restore_qty <= 0:
                    continue
                restore_cost = Decimal(str(get_fifo_cost_per_unit(rl.ingredient_id)))
                _ing_batch = StockBatch(
                    product_id=rl.ingredient_id,
                    qty_purchased_base=restore_qty,
                    qty_remaining_base=restore_qty,
                    cost_per_base_unit=restore_cost,
                    purchased_at=now,
                    user_id=u.id if u else None,
                )
                db.session.add(_ing_batch)
                db.session.flush()
                write_stock_movement(
                    _ing_batch, movement_type='RETURN_SALEABLE', qty_delta=restore_qty,
                    unit_cost=restore_cost, source_type='return',
                    source_id=return_uuid, source_line_id=str(rl.ingredient_id), when=now,
                )
                reverse_consignment_liabilities_partial(sale_id, rl.ingredient_id, restore_qty)

        returned_lines.append({'product_id': pid, 'qty': float(qty)})

    db.session.flush()
    _audit('sale_return', sale_id, _serialize_sale_rows(orig_rows),
           note=f'return_id={return_uuid} reason={reason}', after_rows=_serialize_sale_rows(_return_rows))
    db.session.commit()
    return jsonify({'ok': True, 'return_id': return_uuid, 'lines': returned_lines})


@bp.route('/api/transactions/<sale_id>/void', methods=['POST'])
@audit_policy('AUDITED')
def api_transaction_void(sale_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data   = request.json or {}
    reason = data.get('reason', '').strip()
    # Rev 5 P2-4: a blank void reason is the classic till-fraud signature —
    # return already requires one (transactions.py's return endpoint), void
    # didn't.
    if not reason:
        return jsonify({'error': 'reason required'}), 400
    rows   = Sale.query.filter_by(sale_id=sale_id, voided=False).with_for_update().all()
    if not rows: return jsonify({'error': 'Transaction not found or already voided'}), 404
    u = current_user(); now = datetime.utcnow()
    before_snapshot = _serialize_sale_rows(rows)
    for row in rows:
        row.voided = True; row.voided_by = u.id if u else None; row.voided_at = now; row.void_reason = reason
    reverse_fifo(sale_id)
    reverse_consignment_liabilities(sale_id)
    _audit('sale_void', sale_id, before_snapshot, note=reason, after_rows=_serialize_sale_rows(rows))
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/transactions/<sale_id>/edit', methods=['POST'])
@audit_policy('AUDITED')
def api_transaction_edit(sale_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data   = request.json or {}
    lines  = data.get('lines', [])
    reason = (data.get('reason') or '').strip()
    force  = bool(data.get('force'))
    if not lines: return jsonify({'error': 'lines required'}), 400
    # Rev 5 P2-4: edit rewrote a sale to arbitrary products with no reason
    # required and no inventory-policy check, bypassing STRICT entirely —
    # return already requires a reason; edit now does too.
    if not reason: return jsonify({'error': 'reason required'}), 400
    rows = Sale.query.filter_by(sale_id=sale_id, voided=False).with_for_update().all()
    if not rows: return jsonify({'error': 'Transaction not found or voided'}), 404

    # Pre-flight STRICT policy check (read-only, before any DB writes) — the
    # same check checkout runs. "Available after this edit" = current stock +
    # whatever THIS sale originally consumed, since editing reverses that
    # consumption before re-consuming for the new line composition.
    _orig_qty_by_pid = {}
    for r in rows:
        _orig_qty_by_pid[r.product_id] = _orig_qty_by_pid.get(r.product_id, Decimal('0')) + Decimal(str(r.qty))
    _edit_required = {}
    for item in lines:
        _pid = int(item['product_id'])
        _edit_required[_pid] = _edit_required.get(_pid, Decimal('0')) + Decimal(str(item.get('qty', 1)))
    _edit_blocks = []
    for _pid, _total_qty in _edit_required.items():
        _pc = db.session.get(Product, _pid)
        if not _pc: continue
        _pol = getattr(_pc, 'inventory_policy', None) or 'ALLOW_NEGATIVE'
        if _pol != 'STRICT': continue
        if not (_pc.product_type == 'stock_item' or (_pc.product_type == 'recipe' and _pc.is_produced)): continue
        _available = Decimal(str(get_stock_level(_pid))) + _orig_qty_by_pid.get(_pid, Decimal('0'))
        if _available < _total_qty:
            _edit_blocks.append({'product_id': _pid, 'name': _pc.name,
                                  'available': float(_available), 'needed': float(_total_qty)})
    if _edit_blocks and not force:
        return jsonify({'error': 'Edit blocked: insufficient stock.', 'blocked': _edit_blocks}), 409

    orig_date = rows[0].date_time
    # Preserve original payment_method and tender on replacement rows so Z-report stays correct
    orig_payment_method = rows[0].payment_method
    orig_cash_tendered  = rows[0].cash_tendered
    orig_card_amount    = rows[0].card_amount
    u = current_user()
    now_wall = datetime.utcnow()  # use wall-clock for consume_fifo so batch eligibility isn't capped at the original sale date
    before_snapshot = _serialize_sale_rows(rows)
    for row in rows:
        row.voided = True; row.voided_by = (u.id if u else None); row.voided_at = now_wall
        row.void_reason = f'superseded by edit: {reason}'
    reverse_fifo(sale_id)
    reverse_consignment_liabilities(sale_id)
    _new_sale_rows = []
    for idx, item in enumerate(lines):
        pid       = int(item['product_id'])
        qty       = Decimal(str(item.get('qty', 1)))
        subs_raw  = item.get('subs', {})
        subs_edit = {int(k): int(v) for k, v in subs_raw.items()} if subs_raw else {}
        if qty <= 0: continue
        # Always use server-side price on edit (same rule as checkout).
        _ep = Product.query.with_entities(
            Product.price, Product.price_per_unit, Product.sold_by_weight
        ).filter_by(id=pid).first()
        if _ep and _ep.sold_by_weight and _ep.price_per_unit:
            unit_price = Decimal(str(_ep.price_per_unit))
        else:
            unit_price = Decimal(str((_ep.price if _ep else None) or 0))
        # payment_method preserved from original; cash/card tender only on first new line
        _first = (idx == 0)
        sale_row = Sale(sale_id=sale_id, date_time=orig_date, product_id=pid, qty=qty,
                        unit_price=unit_price, user_id=u.id if u else None,
                        payment_method=orig_payment_method,
                        cash_tendered=(orig_cash_tendered if _first else None),
                        card_amount=(orig_card_amount if _first else None))
        db.session.add(sale_row)
        _new_sale_rows.append(sale_row)
        p = db.session.get(Product, pid, with_for_update=True)
        if not p: continue
        if p.product_type == 'stock_item' or (p.product_type == 'recipe' and p.is_produced):
            sale_row.cogs = consume_fifo(pid, qty, sale_id, now_wall, sale_unit_price=unit_price)
        elif p.product_type == 'recipe':
            line_cogs = Decimal('0')
            for rl in RecipeLine.query.filter_by(product_id=pid).all():
                actual_id = subs_edit.get(rl.ingredient_id, rl.ingredient_id)
                if actual_id == -1: continue
                line_cogs += consume_fifo(actual_id, Decimal(str(rl.qty_base)) * qty, sale_id, now_wall)
            sale_row.cogs = line_cogs
        else:
            sale_row.cogs = Decimal('0')
            _csup = getattr(p, 'consignment_supplier_id', None)
            if p.is_consignment and _csup:
                _basis = getattr(p, 'settlement_basis', 'FIXED_COST')
                _sale_snap = None; _pct_snap = None
                if _basis == 'PCT_OF_SALE':
                    _pct = Decimal(str(p.consignment_pct or 0)) / Decimal('100')
                    _unit_cost = (unit_price * _pct).quantize(Decimal('0.000001'))
                    _sale_snap = float(unit_price)
                    _pct_snap  = float(p.consignment_pct or 0)
                else:
                    # FIXED_COST: manually-set price. UNIT_COST: auto from batch (no shipping).
                    # Simple products have no batch, so both use consignment_cost_per_unit.
                    _cuc = getattr(p, 'consignment_cost_per_unit', None)
                    _unit_cost = Decimal(str(_cuc)) if _cuc else Decimal('0')
                if _unit_cost > 0:
                    _amount = (qty * _unit_cost).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    db.session.add(ConsignmentLiability(
                        supplier_id=_csup, product_id=pid, batch_id=None,
                        sale_id=sale_id, qty_consumed=float(qty),
                        unit_cost=float(_unit_cost), amount_owed=float(_amount),
                        sale_price_at_time=_sale_snap, settlement_percent_at_time=_pct_snap,
                    ))
    db.session.flush()
    _audit('sale_edit', sale_id, before_snapshot, note=reason, after_rows=_serialize_sale_rows(_new_sale_rows))
    db.session.commit()
    return jsonify({'ok': True})

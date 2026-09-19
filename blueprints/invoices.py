import uuid
import json as _json
from decimal import Decimal
from datetime import datetime

from flask import Blueprint, jsonify, request, render_template
from sqlalchemy import text

from helpers import (
    require_login, require_role, current_user, get_online_user_id, consume_fifo,
    get_setting, set_setting, reverse_fifo, reverse_consignment_liabilities, audit_event,
    audit_policy,
)
from models import db, Invoice, Customer, Product, RecipeLine, Sale

bp = Blueprint('invoices', __name__)

_UNIT_TO_BASE = {'weight': {'g': 1, 'kg': 1000}, 'volume': {'ml': 1, 'L': 1000}, 'count': {'unit': 1}}

# Online-shop shipping fees (ZAR). Stored in the shared `settings` table so the
# Lady Coleen website reads the same values. Defaults match the launch prices.
_SHIPPING_METHODS = (
    ('collection', 'Collect from farm stall', 0.0),
    ('pudo',       'Pudo locker-to-locker',   69.0),
    ('delivery',   'Deliver to my address',   99.0),
)


@bp.route('/api/invoices/shipping-fees', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_shipping_fees_get():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    return jsonify({
        'fees': [
            {'method': m, 'label': label,
             'fee': float(get_setting(f'shipping_fee_{m}', default) or default)}
            for m, label, default in _SHIPPING_METHODS
        ]
    })


@bp.route('/api/invoices/shipping-fees', methods=['POST'])
@audit_policy('AUDITED')
def api_shipping_fees_update():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    fees = data.get('fees') or {}
    before = {m: float(get_setting(f'shipping_fee_{m}', d) or d) for m, _label, d in _SHIPPING_METHODS}
    saved = {}
    for m, _label, _default in _SHIPPING_METHODS:
        if m in fees:
            try:
                val = round(float(fees[m]), 2)
            except (TypeError, ValueError):
                return jsonify({'error': f'Invalid fee for {m}'}), 400
            if val < 0:
                return jsonify({'error': f'Fee for {m} cannot be negative'}), 400
            set_setting(f'shipping_fee_{m}', val)
            saved[m] = val
    if saved:
        audit_event('shipping_fees_updated', 'settings', None, before=before, after=saved)
    return jsonify({'ok': True, 'saved': saved})


def _next_invoice_number():
    # Use a PostgreSQL sequence so concurrent creates never collide on the same number.
    try:
        num = db.session.execute(text("SELECT nextval('invoice_number_seq')")).scalar()
    except Exception:
        db.session.rollback()
        # Sequence doesn't exist yet (first run before migration) — fall back to MAX+1
        last = db.session.query(Invoice).order_by(Invoice.id.desc()).first()
        if last:
            try: num = int(last.invoice_number.split('-')[-1]) + 1
            except Exception: num = last.id + 1
        else:
            num = 1
    return f'INV-{num:04d}'


def _resolve_online_customer(email, name, phone):
    email_clean = (email or '').strip().lower()
    if email_clean:
        row = db.session.execute(text("SELECT id FROM customers WHERE LOWER(TRIM(email)) = :e AND active = true LIMIT 1"), {'e': email_clean}).fetchone()
        if row:
            c = db.session.get(Customer, row[0])
            if not c.is_online_customer: c.is_online_customer = True; db.session.commit()
            return c, False
    u = current_user()
    c = Customer(name=(name or '').strip() or None, phone=(phone or '').strip() or None, email=email_clean or None, enrolled_by=u.id if u else None, is_online_customer=True, is_pos_customer=False)
    db.session.add(c); db.session.commit()
    return c, True


@bp.route('/api/invoices', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_invoices_list():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    invs = db.session.query(Invoice).order_by(Invoice.created_at.desc()).all()
    return jsonify([{'id': i.id, 'invoice_number': i.invoice_number, 'created_at': i.created_at.isoformat() if i.created_at else None, 'due_date': i.due_date, 'customer_name': i.customer_name, 'customer_phone': i.customer_phone, 'customer_email': i.customer_email, 'total': float(i.total), 'status': i.status, 'customer_id': i.customer_id} for i in invs])


@bp.route('/api/invoices', methods=['POST'])
@audit_policy('AUDITED')
def api_invoices_create():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}; lines = data.get('lines', [])
    subtotal = sum(float(l.get('subtotal', 0)) for l in lines)
    disc = float(data.get('discount_pct') or 0)
    total = subtotal * (1 - disc / 100) if disc else subtotal
    cust_id = data.get('customer_id') or None
    is_online_inv = bool(data.get('notes') and '[ONLINE' in (data.get('notes') or ''))
    if not cust_id and is_online_inv:
        cust, _ = _resolve_online_customer((data.get('customer_email') or '').strip(), (data.get('customer_name') or '').strip(), (data.get('customer_phone') or '').strip())
        cust_id = cust.id
    inv = Invoice(invoice_number=_next_invoice_number(), due_date=data.get('due_date') or None, customer_name=data.get('customer_name') or None, customer_phone=data.get('customer_phone') or None, customer_email=data.get('customer_email') or None, customer_address=data.get('customer_address') or None, notes=data.get('notes') or None, bank_details=data.get('bank_details') or None, lines_json=_json.dumps(lines), subtotal=round(subtotal, 2), discount_pct=disc or None, total=round(total, 2), status='draft', created_by=current_user().id if current_user() else None, customer_id=cust_id)
    db.session.add(inv); db.session.flush()
    audit_event('invoice_created', 'invoices', inv.id, after={
        'invoice_number': inv.invoice_number, 'customer_id': inv.customer_id,
        'subtotal': float(inv.subtotal), 'total': float(inv.total), 'status': inv.status,
    })
    db.session.commit()
    return jsonify({'id': inv.id, 'invoice_number': inv.invoice_number, 'customer_id': inv.customer_id})


@bp.route('/api/invoices/<int:inv_id>', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_invoices_get(inv_id):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv: return jsonify({'error': 'Not found'}), 404
    return jsonify({'id': inv.id, 'invoice_number': inv.invoice_number, 'created_at': inv.created_at.isoformat() if inv.created_at else None, 'due_date': inv.due_date, 'customer_name': inv.customer_name, 'customer_phone': inv.customer_phone, 'customer_email': inv.customer_email, 'customer_address': inv.customer_address, 'notes': inv.notes, 'bank_details': inv.bank_details, 'lines': _json.loads(inv.lines_json or '[]'), 'subtotal': float(inv.subtotal), 'discount_pct': float(inv.discount_pct or 0), 'total': float(inv.total), 'status': inv.status, 'sale_id': inv.sale_id, 'customer_id': inv.customer_id})


@bp.route('/api/invoices/<int:inv_id>', methods=['POST'])
@audit_policy('AUDITED')
def api_invoices_update(inv_id):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv: return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    allowed_fields = ('due_date', 'customer_name', 'customer_phone', 'customer_email', 'customer_address', 'notes', 'bank_details', 'status')
    before = {f: getattr(inv, f) for f in allowed_fields}
    before['subtotal'] = float(inv.subtotal); before['total'] = float(inv.total)
    if inv.sale_id:
        for field in allowed_fields:
            if field in data: setattr(inv, field, data[field] or None)
        audit_event('invoice_updated', 'invoices', inv.id, before=before,
                    after={f: getattr(inv, f) for f in allowed_fields} | {'subtotal': float(inv.subtotal), 'total': float(inv.total)})
        db.session.commit(); return jsonify({'ok': True})
    for field in allowed_fields:
        if field in data: setattr(inv, field, data[field] or None)
    if 'lines' in data:
        lines = data['lines']; subtotal = sum(float(l.get('subtotal', 0)) for l in lines)
        disc = float(data.get('discount_pct') or inv.discount_pct or 0); total = subtotal * (1 - disc / 100) if disc else subtotal
        inv.lines_json = _json.dumps(lines); inv.subtotal = round(subtotal, 2); inv.discount_pct = disc or None; inv.total = round(total, 2)
    audit_event('invoice_updated', 'invoices', inv.id, before=before,
                after={f: getattr(inv, f) for f in allowed_fields} | {'subtotal': float(inv.subtotal), 'total': float(inv.total)})
    db.session.commit(); return jsonify({'ok': True})


@bp.route('/api/invoices/<int:inv_id>/delete', methods=['POST'])
@audit_policy('AUDITED')
def api_invoices_delete(inv_id):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv: return jsonify({'error': 'Not found'}), 404
    if inv.status == 'finalised':
        return jsonify({'error': 'Cannot delete a finalised invoice. Use Undo to reverse it first.'}), 400
    before = {'invoice_number': inv.invoice_number, 'status': inv.status,
              'total': float(inv.total), 'customer_id': inv.customer_id}
    audit_event('invoice_deleted', 'invoices', inv.id, before=before, after=None)
    db.session.delete(inv); db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/invoices/<int:inv_id>/copy', methods=['POST'])
@audit_policy('AUDITED')
def api_invoices_copy(inv_id):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    src = db.session.get(Invoice, inv_id)
    if not src: return jsonify({'error': 'Not found'}), 404
    bank = get_setting('invoice_bank_details') or src.bank_details
    copy = Invoice(
        invoice_number=_next_invoice_number(),
        created_at=datetime.utcnow(),
        due_date=None,
        customer_name=src.customer_name,
        customer_phone=src.customer_phone,
        customer_email=src.customer_email,
        customer_address=src.customer_address,
        notes=src.notes,
        bank_details=bank,
        lines_json=src.lines_json,
        subtotal=src.subtotal,
        discount_pct=src.discount_pct,
        total=src.total,
        status='draft',
        created_by=current_user().id if current_user() else None,
        customer_id=src.customer_id,
    )
    db.session.add(copy); db.session.flush()
    audit_event('invoice_copied', 'invoices', copy.id, before={'source_invoice_id': src.id},
                after={'invoice_number': copy.invoice_number, 'total': float(copy.total)})
    db.session.commit()
    return jsonify({'id': copy.id, 'invoice_number': copy.invoice_number})


@bp.route('/api/invoices/<int:inv_id>/finalise', methods=['POST'])
@audit_policy('AUDITED')
def api_invoices_finalise(inv_id):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv: return jsonify({'error': 'Not found'}), 404
    if inv.sale_id and inv.status == 'finalised': return jsonify({'ok': True, 'sale_id': inv.sale_id})
    is_online = bool(inv.notes and '[ONLINE' in inv.notes)
    if not inv.customer_id:
        email = (inv.customer_email or '').strip(); name = (inv.customer_name or '').strip(); phone = (inv.customer_phone or '').strip()
        if is_online and (email or name or phone):
            cust, _ = _resolve_online_customer(email, name, phone); inv.customer_id = cust.id
    if inv.customer_id:
        cust = db.session.get(Customer, inv.customer_id)
        if cust:
            if is_online and not cust.is_online_customer: cust.is_online_customer = True
            if not is_online and not cust.is_pos_customer: cust.is_pos_customer = True
    if inv.sale_id:
        before_status = inv.status
        inv.status = 'finalised'
        audit_event('invoice_finalised', 'invoices', inv.id,
                    before={'status': before_status}, after={'status': 'finalised', 'sale_id': inv.sale_id})
        db.session.commit(); return jsonify({'ok': True, 'sale_id': inv.sale_id})
    lines = _json.loads(inv.lines_json or '[]')
    if not lines: return jsonify({'error': 'Invoice has no items'}), 400
    if float(inv.total or 0) <= 0:
        return jsonify({'error': 'Invoice total must be positive'}), 400
    sale_uuid = str(uuid.uuid4()); now = datetime.utcnow(); u = current_user()
    sale_user_id = get_online_user_id() if is_online else (u.id if u else None)
    inv_payment_method = 'card' if is_online else 'invoice'
    for line in lines:
        name = (line.get('name') or '').strip(); qty_disp = Decimal(str(line.get('qty', 1))); unit_price = Decimal(str(line.get('unit_price', 0))); unit = line.get('unit', 'unit')
        # Prefer stored product_id; fall back to name match for legacy invoices
        pid_stored = line.get('product_id')
        if pid_stored:
            p = db.session.get(Product, int(pid_stored))
        else:
            base_name = name.split('(')[0].strip() if '(' in name else name
            p = Product.query.filter(Product.name.ilike(base_name), Product.is_archived == False).first()
        if p:
            line_cogs = Decimal('0')
            if p.product_type == 'stock_item':
                conv = _UNIT_TO_BASE.get(p.unit_type, {}).get(unit, 1) if (unit and p.unit_type in ('weight', 'volume')) else 1
                line_cogs = consume_fifo(p.id, qty_disp * Decimal(str(conv)), sale_uuid, now)
            elif p.product_type == 'simple':
                p.stock_qty = max(0, (p.stock_qty or 0) - int(qty_disp))
            elif p.product_type == 'recipe':
                for rl in RecipeLine.query.filter_by(product_id=p.id).all():
                    line_cogs += consume_fifo(rl.ingredient_id, Decimal(str(rl.qty_base)) * qty_disp, sale_uuid, now)
            db.session.add(Sale(sale_id=sale_uuid, date_time=now, product_id=p.id, qty=qty_disp,
                                unit_price=unit_price, user_id=sale_user_id,
                                payment_method=inv_payment_method, cogs=line_cogs))
    inv.sale_id = sale_uuid; inv.status = 'finalised'
    audit_event('invoice_finalised', 'invoices', inv.id,
                before={'status': 'draft', 'sale_id': None},
                after={'status': 'finalised', 'sale_id': sale_uuid, 'total': float(inv.total)})
    db.session.commit()
    return jsonify({'ok': True, 'sale_id': sale_uuid})


@bp.route('/api/invoices/<int:inv_id>/undo', methods=['POST'])
@audit_policy('AUDITED')
def api_invoices_undo(inv_id):
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv: return jsonify({'error': 'Not found'}), 404
    if not inv.sale_id: return jsonify({'ok': True})
    sale_uuid = inv.sale_id; u = current_user(); now = datetime.utcnow()
    stamp = f"[UNDO] Sale {sale_uuid[:8]} reversed by {u.username if u else '?'} @ {now.strftime('%Y-%m-%d %H:%M')} UTC"
    _undo_rows = Sale.query.filter_by(sale_id=sale_uuid, voided=False).all()
    _before_snapshot = [{'id': s.id, 'sale_id': s.sale_id, 'product_id': s.product_id, 'qty': str(s.qty),
                          'unit_price': str(s.unit_price), 'voided': s.voided} for s in _undo_rows]
    for s in _undo_rows:
        s.voided = True; s.voided_by = u.id if u else None; s.voided_at = now; s.void_reason = f'Invoice {inv.invoice_number} undone'
        p = db.session.get(Product, s.product_id) if s.product_id else None
        if p and p.product_type == 'simple': p.stock_qty = (p.stock_qty or 0) + int(s.qty)
    # Rev 5 P2-2b: this used to hand-roll its own FIFO reversal (restore
    # qty_remaining_base from StockConsumption, then delete the rows) - a
    # fourth copy of what reverse_fifo already does, minus the with_for_update
    # lock reverse_fifo takes and the stock_movements dual-write it now writes.
    # It also skipped consignment liability reversal entirely, leaving the
    # supplier shown as owed for a sale that no longer exists. Routing through
    # the shared functions fixes both - there is exactly one reversal
    # implementation now, not four.
    reverse_fifo(sale_uuid)
    reverse_consignment_liabilities(sale_uuid)
    inv.notes = ((inv.notes or '') + ' ' + stamp).strip(); inv.sale_id = None; inv.status = 'draft'
    audit_event('invoice_undo', 'sales', sale_uuid, before=_before_snapshot,
                after=[{**row, 'voided': True} for row in _before_snapshot],
                reason=f'Invoice {inv.invoice_number} undone')
    db.session.commit(); return jsonify({'ok': True})


@bp.route('/invoices/<int:inv_id>/print')
@audit_policy('NO_STATE_CHANGE')
def invoice_print(inv_id):
    if not require_login(): return 'Unauthorized', 401
    inv = db.session.get(Invoice, inv_id)
    if not inv: return 'Not found', 404
    lines = _json.loads(inv.lines_json or '[]')
    # Enrich lines missing a unit by looking up the product name
    names = {l['name'] for l in lines if not l.get('unit') or l.get('unit') == 'unit'}
    if names:
        prods = {p.name: p for p in Product.query.filter(Product.name.in_(names)).all()}
        for l in lines:
            if not l.get('unit') or l.get('unit') == 'unit':
                p = prods.get(l.get('name', ''))
                if p:
                    if p.sold_by_weight or p.unit_type == 'weight':
                        l['unit'] = 'kg'
                    elif p.unit_type == 'volume':
                        l['unit'] = 'L'
    return render_template('invoice.html', inv=inv, lines=lines)

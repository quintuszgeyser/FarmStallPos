import io
import json as _json
import os
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request, current_app, send_from_directory, abort
from sqlalchemy import func

from helpers import require_login, require_role, current_user, _gen_barcode
from models import db, Supplier, StockBatch, StockConsumption, Purchase, Product, SupplierDocument, SupplierInvoice

bp = Blueprint('suppliers', __name__)


# ── Invoice OCR / parsing helpers ────────────────────────────────────────────

_INV_NUM_PATTERNS = [
    r'(?:invoice\s*(?:number|no|#)\s*[:\s#]+\s*)([\w][\w/-]*)',
    r'(?:document\s*no\s*[:\s]+\s*)(\w[\w/-]*)',
    r'(?:number\s*:\s*)([\w][\w/-]*)',
    r'(?:#\s*)(inv[-\w]+)',
    r'(?:invoice\s+)([\d]+)',
    r'\binv[-\s]?([\w\d/-]{3,})',
    r'(?:order\s+)([\d][\d/-]*)',
]

_DATE_PATTERNS = [
    (r'(?:invoice\s*date|order\s*date)[:\s]+(\w+\s+\d{1,2},?\s*\d{4})', '%B %d %Y'),
    (r'(?:invoice\s*date|order\s*date)[:\s]+(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})', '%d/%m/%Y'),
    (r'(?:invoice\s*date|order\s*date)[:\s]+(\d{4}[/.-]\d{2}[/.-]\d{2})', '%Y/%m/%d'),
    (r'(?:invoice\s*date|order\s*date)[:\s]+(\d{1,2}\s+\w+\s+\d{4})', '%d %b %Y'),
    (r'(?<!\w)date[:\s]+(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})', '%d/%m/%Y'),
    (r'(?<!\w)date[:\s]+(\d{4}[/.-]\d{2}[/.-]\d{2})', '%Y/%m/%d'),
    (r'(?<!\w)date[:\s]+(\d{1,2}\s+\w+\s+\d{4})', '%d %b %Y'),
    (r'(\d{4}/\d{2}/\d{2})', '%Y/%m/%d'),
    (r'(\d{1,2}/\d{1,2}/\d{4})', '%d/%m/%Y'),
    (r'(\d{1,2}/\d{1,2}/\d{2})(?!\d)', '%d/%m/%y'),
]

_SKIP_RE = re.compile(
    r'\b(total|subtotal|sub.total|balance\s+due|vat|tax\s+summary|payment|paid|discount'
    r'|thank\s+you|powered\s+by|page\s+\d|terms|conditions|banking\s+details|account\s+holder'
    r'|branch\s+code|swift|bill\s+to|ship\s+to|sold\s+to|delivery\s+details'
    r'|invoice\s+date|due\s+date|order\s+date|customer\s+order|customer\s+vat'
    r'|signature|stock\s+controller|produced\s+by|postnet\s+suite|private\s+bag'
    r'|account\s+number|account\s+no|branch\s+name|account\s+type|account\s+name'
    r'|reg\s+number|registration\s+number|company\s+id|vat\s+reg|vat\s+no'
    r'|tel:|fax:|www\.|email:|south\s+africa|western\s+cape|gauteng|kwazulu'
    r'|eikeboom|hermon|cape\s+town|durbanville|sandton|melrose)\b',
    re.IGNORECASE,
)

_HEADER_RE = re.compile(
    r'^(description|item|qty|quantity|unit\s+price|rate|amount|price|code|ext\.?\s*price'
    r'|disc\s*%|vat\s*%|excl\.|incl\.|line|date|activity|nett\s+price|no\.?\s+exclusive)\b',
    re.IGNORECASE,
)

_SHIPPING_RE = re.compile(r'\b(shipping|courier|freight|transport|delivery|postage|carriage)\b', re.IGNORECASE)


def _extract_invoice_number(text):
    for pat in _INV_NUM_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip('.,')
            if len(val) >= 2:
                return val
    return None


def _extract_date(text):
    import calendar
    month_abbrevs = {m.lower(): i+1 for i, m in enumerate(calendar.month_abbr) if m}
    month_names   = {m.lower(): i+1 for i, m in enumerate(calendar.month_name) if m}

    for pat, fmt in _DATE_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw = m.group(1).strip().replace('-', '/').replace('.', '/')
            raw = re.sub(r',', '', raw)
            # Normalise month names
            for name, num in {**month_names, **month_abbrevs}.items():
                raw = re.sub(r'\b' + re.escape(name) + r'\b', str(num), raw, flags=re.IGNORECASE)
            raw = re.sub(r'\s+', '/', raw)
            raw = re.sub(r'/+', '/', raw)
            for tfmt in ('%d/%m/%Y', '%Y/%m/%d', '%d/%m/%y', '%m/%d/%Y'):
                try:
                    d = datetime.strptime(raw, tfmt).date()
                    if 2020 <= d.year <= 2035:
                        return d.isoformat()
                except Exception:
                    pass
    return None


def _clean_num(s):
    s = re.sub(r'[R\s,]', '', str(s))
    try:
        return float(s)
    except Exception:
        return None


def _try_parse_line(line):
    """Try to extract (description, qty, total_price) from a text line."""
    original = line.strip()
    if not original or len(original) < 8:
        return None

    # Skip header/footer lines
    if _SKIP_RE.search(original):
        return None
    if _HEADER_RE.match(original):
        return None

    # Strip leading item code (all-caps alphanum, e.g. "TILES001BLUE", "1HALM", "RBN001 -")
    line = re.sub(r'^[A-Z][A-Z0-9]{3,}\s*[-–]?\s*', '', original, count=1).strip()
    # Strip leading date (YYYY/MM/DD or DD/MM/YYYY)
    line = re.sub(r'^\d{4}[/.-]\d{2}[/.-]\d{2}\s+', '', line).strip()
    line = re.sub(r'^\d{1,2}[/.-]\d{2}[/.-]\d{2,4}\s+', '', line).strip()
    # Strip leading line number (a bare integer followed by space)
    line = re.sub(r'^\d+\s+', '', line, count=1).strip()

    if len(line) < 4:
        return None

    # Collect all price-like tokens: optional R, digits with optional comma-thousands, decimal
    # Exclude percentages (numbers immediately followed by %)
    num_pat = r'R?\s*(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2}|\d{1,5})(?!\s*%)'
    tokens = []
    for m in re.finditer(num_pat, line):
        val = _clean_num(m.group(1))
        if val is not None and val >= 0:
            tokens.append((m.start(), m.end(), val))

    if len(tokens) < 2:
        return None

    # Skip lines that are entirely percentages or pure-number lines
    non_num = re.sub(num_pat, '', line).strip()
    if not non_num or re.match(r'^[\s%.,]+$', non_num):
        return None

    # Last token = total price
    total = tokens[-1][2]
    if total <= 0:
        return None

    # Find qty: look for a small integer (1-9999) among the tokens
    qty = 1.0
    unit_price_candidate = None

    if len(tokens) >= 3:
        # Pattern: ... qty unit_price total
        # unit_price × qty ≈ total
        for i in range(len(tokens) - 2, 0, -1):
            up = tokens[i][2]
            q_raw = total / up if up > 0 else 0
            q_round = round(q_raw)
            if up > 0 and 1 <= q_round <= 500 and abs(q_raw - q_round) < 0.05:
                qty = float(q_round)
                unit_price_candidate = up
                break

    # Description = text before the first number token
    desc_end = tokens[0][0]
    desc = line[:desc_end].strip().strip('.,- ')

    # If desc is empty (line started with a number), use the original stripped line up to second token
    if not desc and len(tokens) >= 2:
        desc_end = tokens[1][0]
        desc = line[:desc_end].strip().strip('.,- ')

    # Remove trailing units/tags like "ea", "PACK", "Standard" that got left in desc
    desc = re.sub(r'\s+\b(ea|PACK|Pack|unit|Unit|Standard|STD|PK)\b\s*$', '', desc, flags=re.IGNORECASE).strip()

    if not desc or len(desc) < 3:
        return None
    # Description mustn't be purely numeric
    if re.match(r'^[\d\s.,R%]+$', desc):
        return None
    # Skip if description ends in colon (header field like "Account:", "Branch:")
    if desc.rstrip().endswith(':'):
        return None
    # Skip single short words that are clearly not products (phone numbers, codes, etc.)
    if re.match(r'^[\w.-]{1,10}:?$', desc) and not any(c.isalpha() for c in desc[2:]):
        return None
    # Skip if description looks like address fragments (short + postal code pattern)
    if re.match(r'^[\w\s]{2,25}\s+\d{4,5}$', desc):
        return None

    return {
        'description': desc,
        'qty':         qty,
        'unit':        'unit',
        'total_price': round(total, 2),
    }


def _parse_invoice_pdf(content):
    """Extract structured invoice data from PDF bytes using pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError('pdfplumber is not installed on this server')

    full_text_parts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ''
            full_text_parts.append(t)

    full_text = '\n'.join(full_text_parts)
    if not full_text.strip():
        raise RuntimeError('No text could be extracted from this PDF — it may be a scanned image')

    invoice_number = _extract_invoice_number(full_text)
    invoice_date   = _extract_date(full_text)

    lines    = []
    shipping = 0.0

    for raw in full_text.split('\n'):
        raw = raw.strip()
        if not raw:
            continue
        if _SHIPPING_RE.search(raw) and not _SKIP_RE.search(raw):
            # Try to pull out a shipping cost
            nums = re.findall(r'R?\s*(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2})', raw)
            if nums:
                val = _clean_num(nums[-1])
                if val and val > 0:
                    shipping = round(shipping + val, 2)
            continue
        item = _try_parse_line(raw)
        if item:
            lines.append(item)

    # De-duplicate lines that appear twice (some PDFs repeat description in adjacent columns)
    seen = set()
    deduped = []
    for item in lines:
        key = (item['description'][:30].lower(), item['total_price'])
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return {
        'invoice_number': invoice_number,
        'date':           invoice_date,
        'lines':          deduped,
        'shipping':       shipping if shipping > 0 else None,
        'raw_line_count': len(full_text.split('\n')),
    }


@bp.route('/api/suppliers/<int:sid>/invoices/parse', methods=['POST'])
def api_supplier_invoice_parse(sid):
    """Parse an uploaded invoice PDF and return structured delivery data."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if not db.session.get(Supplier, sid):
        return jsonify({'error': 'Not found'}), 404

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file provided'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext != '.pdf':
        return jsonify({'error': 'Only PDF files are supported for invoice scanning'}), 400

    content = f.read()
    if len(content) > 20 * 1024 * 1024:
        return jsonify({'error': 'File too large (max 20 MB)'}), 400

    try:
        result = _parse_invoice_pdf(content)
        return jsonify(result)
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 422
    except Exception as e:
        current_app.logger.error(f'Invoice parse error: {e}')
        return jsonify({'error': 'Failed to parse invoice'}), 422


def _parse_addl_costs(raw, source='manual_edit', source_id=None):
    """Validate and normalize additional_costs list from request.
    Returns list of dicts with Decimal-safe float amounts."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError('additional_costs must be a list')
    result = []
    for i, entry in enumerate(raw):
        label      = str(entry.get('label') or '').strip()
        ctype      = str(entry.get('type') or 'other').strip() or 'other'
        amount_raw = entry.get('amount')
        if not label:
            raise ValueError(f'additional_costs[{i}].label is required')
        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, TypeError):
            raise ValueError(f'additional_costs[{i}].amount is invalid')
        result.append({
            'label':       label,
            'type':        ctype,
            'amount':      float(amount.quantize(Decimal('0.01'))),
            'source':      source,
            'source_id':   source_id,
            'invoice_ref': str(entry.get('invoice_ref') or '').strip() or None,
        })
    return result


def _split_costs(line_totals, total_addl):
    """Proportional split of total_addl across lines by their base cost.
    Returns list of Decimal shares in the same order as line_totals.
    Last item absorbs rounding remainder so sum(shares) == total_addl exactly."""
    if not line_totals or total_addl == Decimal('0'):
        return [Decimal('0')] * len(line_totals)
    grand = sum(line_totals)
    if grand == Decimal('0'):
        # Equal split when all line totals are zero
        per = (total_addl / len(line_totals)).quantize(Decimal('0.01'))
        shares = [per] * len(line_totals)
        shares[-1] += total_addl - sum(shares)
        return shares
    shares = []
    running = Decimal('0')
    for i, lt in enumerate(line_totals):
        if i == len(line_totals) - 1:
            shares.append(total_addl - running)
        else:
            s = (lt / grand * total_addl).quantize(Decimal('0.01'))
            shares.append(s)
            running += s
    return shares


_UNIT_CONVERSIONS = {
    'g': 1, 'kg': 1000,
    'ml': 1, 'L': 1000,
    'unit': 1, 'dozen': 12,
}


@bp.route('/api/suppliers', methods=['GET'])
def api_suppliers_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    suppliers = Supplier.query.order_by(Supplier.name.asc()).all()
    return jsonify([{
        'id': s.id, 'name': s.name, 'phone': s.phone,
        'email': s.email, 'website': s.website, 'notes': s.notes,
        'last_run_costs': s.last_run_costs,
    } for s in suppliers])


@bp.route('/api/suppliers', methods=['POST'])
def api_suppliers_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data    = request.json or {}
    name    = data.get('name', '').strip()
    phone   = data.get('phone',   '').strip() or None
    email   = data.get('email',   '').strip() or None
    website = data.get('website', '').strip() or None
    notes   = data.get('notes',   '').strip() or None
    if not name:
        return jsonify({'error': 'name required'}), 400
    if Supplier.query.filter_by(name=name).first():
        return jsonify({'error': 'Supplier already exists'}), 409
    s = Supplier(name=name, phone=phone, email=email, website=website, notes=notes)
    db.session.add(s)
    db.session.commit()
    return jsonify({'ok': True, 'id': s.id})


@bp.route('/api/suppliers/<int:sid>', methods=['POST'])
def api_suppliers_update(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'name' in data:
        name  = data['name'].strip()
        clash = Supplier.query.filter(Supplier.id != sid, Supplier.name == name).first()
        if clash:
            return jsonify({'error': 'Supplier name already exists'}), 409
        s.name = name
    if 'phone'   in data: s.phone   = data['phone'].strip()   or None
    if 'email'   in data: s.email   = data['email'].strip()   or None
    if 'website' in data: s.website = data['website'].strip() or None
    if 'notes'   in data: s.notes   = data['notes'].strip()   or None
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/suppliers/<int:sid>', methods=['DELETE'])
def api_suppliers_delete(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    StockBatch.query.filter_by(supplier_id=sid).update({'supplier_id': None})
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/suppliers/<int:sid>/products', methods=['GET'])
def api_suppliers_products(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    batches = (db.session.query(
                   StockBatch.product_id,
                   func.max(StockBatch.purchased_at).label('last_received'),
               )
               .filter_by(supplier_id=sid)
               .group_by(StockBatch.product_id)
               .all())
    result = []
    for prod_id, last_received in batches:
        p = db.session.get(Product, prod_id)
        if p:
            result.append({
                'id': p.id,
                'name': p.name,
                'product_type': p.product_type,
                'last_received': last_received.date().isoformat() if last_received else None,
            })
    result.sort(key=lambda x: x['name'])
    return jsonify(result)


@bp.route('/api/suppliers/<int:sid>/purchase_run', methods=['POST'])
def api_suppliers_purchase_run(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404

    data            = request.json or {}
    lines           = data.get('lines', [])
    date_str        = data.get('date')
    addl_costs_raw  = data.get('additional_costs', [])
    invoice_ref     = str(data.get('invoice_ref') or '').strip() or None
    invoice_addl_total = data.get('invoice_additional_total')

    if not lines:
        return jsonify({'error': 'No lines provided'}), 400

    from datetime import date as _date
    purchase_date = datetime.now()
    run_date      = _date.today()
    if date_str:
        try:
            parts = date_str.split('-')
            run_date      = _date(int(parts[0]), int(parts[1]), int(parts[2]))
            purchase_date = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            return jsonify({'error': 'Invalid date format'}), 400

    # Validate and normalize additional costs
    try:
        addl_costs = _parse_addl_costs(addl_costs_raw, source='supplier_run')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    u = current_user()
    created_products = []
    batches_created  = 0

    # Compute subtotal from lines (after prepared_lines is built below)
    # Create SupplierInvoice record — flush to get ID before creating batches
    inv = SupplierInvoice(
        supplier_id=sid,
        date=run_date,
        invoice_number=invoice_ref,
        status='posted',
        source='purchase_run',
        notes=data.get('notes') or None,
        created_at=datetime.utcnow(),
        created_by=u.id if u else None,
    )
    db.session.add(inv)
    db.session.flush()
    run_id = inv.id

    # First pass: build line items with base costs for proportional split
    prepared_lines = []
    for line in lines:
        pid      = line.get('product_id')
        new_prod = line.get('new_product')
        qty      = line.get('qty')
        unit     = line.get('unit', 'unit')
        total_price = line.get('total_price')

        try:
            qty         = float(qty)
            total_price = float(total_price)
        except Exception:
            return jsonify({'error': 'Invalid qty or total_price'}), 400

        if new_prod:
            name = new_prod.get('name', '').strip()
            if not name:
                return jsonify({'error': 'new_product.name required'}), 400
            if Product.query.filter_by(name=name).first():
                return jsonify({'error': f'Product name "{name}" already exists'}), 409
            next_id      = (db.session.query(func.max(Product.id)).scalar() or 0) + 1
            barcode      = _gen_barcode(next_id)
            price        = new_prod.get('price')
            product_type = 'stock_item'
            base_unit    = new_prod.get('base_unit') or None
            unit_type    = new_prod.get('unit_type') or None
            try:
                price = float(price) if price is not None else None
            except Exception:
                return jsonify({'error': 'Invalid price'}), 400
            p = Product(
                name=name, barcode=barcode, stock_qty=0,
                price=price, product_type=product_type,
                unit_type=unit_type, base_unit=base_unit,
            )
            db.session.add(p)
            db.session.flush()
            pid = p.id
            created_products.append({'id': p.id, 'name': p.name})
        else:
            try:
                pid = int(pid)
            except Exception:
                return jsonify({'error': 'product_id required'}), 400

        p = db.session.get(Product, pid)
        if not p:
            return jsonify({'error': f'Product id {pid} not found'}), 404

        if p.product_type == 'stock_item':
            conversion = _UNIT_CONVERSIONS.get(unit, 1)
            qty_base   = qty * conversion
            if qty_base == 0:
                return jsonify({'error': f'qty converts to 0 base units for product {pid}'}), 400
            prepared_lines.append({
                'pid': pid, 'qty_base': qty_base,
                'base_cost_total': Decimal(str(total_price)),
            })

    # Proportional split of additional costs across lines (by base_cost_total)
    total_addl = sum(Decimal(str(c['amount'])) for c in addl_costs)
    shares = _split_costs([l['base_cost_total'] for l in prepared_lines], total_addl)

    for i, pl in enumerate(prepared_lines):
        share = shares[i]
        # Build per-batch additional_costs with the allocated share
        batch_addl = []
        if share != Decimal('0') and addl_costs:
            if len(addl_costs) == 1:
                batch_addl = [{**addl_costs[0], 'amount': float(share.quantize(Decimal('0.01')))}]
            else:
                # Scale each entry proportionally to their fraction of total_addl
                if total_addl != Decimal('0'):
                    batch_addl = []
                    running = Decimal('0')
                    for j, ac in enumerate(addl_costs):
                        if j == len(addl_costs) - 1:
                            entry_share = share - running
                        else:
                            entry_share = (Decimal(str(ac['amount'])) / total_addl * share).quantize(Decimal('0.01'))
                            running += entry_share
                        batch_addl.append({**ac, 'amount': float(entry_share)})

        cost_per_base = (pl['base_cost_total'] + share) / Decimal(str(pl['qty_base']))
        db.session.add(StockBatch(
            product_id=pl['pid'],
            qty_purchased_base=pl['qty_base'],
            qty_remaining_base=pl['qty_base'],
            cost_per_base_unit=cost_per_base,
            base_cost_total=pl['base_cost_total'],
            additional_costs=_json.dumps(batch_addl) if batch_addl else None,
            supplier_id=sid,
            user_id=u.id if u else None,
            purchased_at=purchase_date,
            invoice_id=run_id,
        ))
        batches_created += 1

    # Stamp invoice-level totals
    subtotal = sum(pl['base_cost_total'] for pl in prepared_lines)
    inv.subtotal = float(subtotal)
    inv.additional_costs_json = _json.dumps([{'label': c['label'], 'type': c['type'], 'amount': c['amount']} for c in addl_costs]) if addl_costs else None
    inv.additional_costs_total = float(total_addl)
    inv.total = float(subtotal + total_addl)

    # Update supplier's last_run_costs for pre-population next time
    if addl_costs and batches_created > 0:
        run_level = [{'label': c['label'], 'type': c['type'], 'amount': float(Decimal(str(c['amount'])).quantize(Decimal('0.01')))} for c in addl_costs]
        s.last_run_costs = _json.dumps(run_level)

    db.session.commit()
    return jsonify({
        'ok': True,
        'created_products': created_products,
        'batches_created':  batches_created,
        'invoice_id':       run_id,
        'invoice_number':   invoice_ref,
    })


_ALLOWED_DOC_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.doc', '.docx', '.xls', '.xlsx', '.csv', '.txt'}
_MAX_DOC_SIZE = 20 * 1024 * 1024  # 20 MB


def _doc_dir():
    d = os.path.join(current_app.static_folder, 'supplier_docs')
    os.makedirs(d, exist_ok=True)
    return d


@bp.route('/api/suppliers/<int:sid>/documents', methods=['GET'])
def api_supplier_docs_list(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    docs = SupplierDocument.query.filter_by(supplier_id=sid).order_by(SupplierDocument.uploaded_at.desc()).all()
    return jsonify([{
        'id': d.id,
        'original_name': d.original_name,
        'filename': d.filename,
        'uploaded_at': d.uploaded_at.date().isoformat() if d.uploaded_at else None,
    } for d in docs])


@bp.route('/api/suppliers/<int:sid>/documents', methods=['POST'])
def api_supplier_docs_upload(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file provided'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in _ALLOWED_DOC_EXTENSIONS:
        return jsonify({'error': f'File type {ext} not allowed'}), 400
    content = f.read()
    if len(content) > _MAX_DOC_SIZE:
        return jsonify({'error': 'File too large (max 20 MB)'}), 400
    stored_name = f'{uuid.uuid4().hex}{ext}'
    path = os.path.join(_doc_dir(), stored_name)
    with open(path, 'wb') as fh:
        fh.write(content)
    u = current_user()
    invoice_id = request.form.get('invoice_id', type=int) or None
    doc = SupplierDocument(
        supplier_id=sid,
        invoice_id=invoice_id,
        filename=stored_name,
        original_name=f.filename,
        uploaded_by=u.id if u else None,
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify({'ok': True, 'id': doc.id, 'original_name': doc.original_name,
                    'filename': doc.filename, 'uploaded_at': doc.uploaded_at.date().isoformat()})


@bp.route('/api/suppliers/<int:sid>/documents/<int:did>/download', methods=['GET'])
def api_supplier_docs_download(sid, did):
    if not require_role('admin'):
        abort(403)
    doc = db.session.get(SupplierDocument, did)
    if not doc or doc.supplier_id != sid:
        abort(404)
    return send_from_directory(_doc_dir(), doc.filename, as_attachment=True,
                               download_name=doc.original_name)


@bp.route('/api/suppliers/<int:sid>/documents/<int:did>', methods=['DELETE'])
def api_supplier_docs_delete(sid, did):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    doc = db.session.get(SupplierDocument, did)
    if not doc or doc.supplier_id != sid:
        return jsonify({'error': 'Not found'}), 404
    path = os.path.join(_doc_dir(), doc.filename)
    try:
        os.remove(path)
    except OSError:
        pass
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/suppliers/<int:sid>/invoices', methods=['GET'])
def api_supplier_invoices(sid):
    """Return supplier invoices with nested batches and documents."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(Supplier, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404

    # Fetch all invoices for this supplier (most recent first)
    invoices = (SupplierInvoice.query
                .filter_by(supplier_id=sid)
                .order_by(SupplierInvoice.date.desc(), SupplierInvoice.id.desc())
                .limit(50)
                .all())
    inv_ids = {inv.id for inv in invoices}

    # Fetch all batches linked to these invoices (+ unlinked batches for this supplier)
    all_batches = (StockBatch.query
                   .filter_by(supplier_id=sid)
                   .order_by(StockBatch.purchased_at.desc())
                   .all())

    pids = {b.product_id for b in all_batches}
    prod_map = {p.id: p for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}

    def _batch_dict(b):
        p = prod_map.get(b.product_id)
        qty_purchased = float(b.qty_purchased_base)
        qty_remaining = float(b.qty_remaining_base)
        consumed_pct  = round((1 - qty_remaining / qty_purchased) * 100, 1) if qty_purchased > 0 else 0
        return {
            'id':                 b.id,
            'product_id':         b.product_id,
            'product_name':       p.name if p else str(b.product_id),
            'base_unit':          p.base_unit if p else None,
            'unit_type':          p.unit_type if p else None,
            'purchased_at':       b.purchased_at.isoformat(),
            'qty_purchased_base': qty_purchased,
            'qty_remaining_base': qty_remaining,
            'consumed_pct':       consumed_pct,
            'cost_per_base_unit': float(b.cost_per_base_unit),
            'base_cost_total':    float(b.base_cost_total) if b.base_cost_total is not None else None,
            'additional_costs':   b.additional_costs,
            'updated_at':         b.updated_at.isoformat() if b.updated_at else None,
        }

    # Group batches by invoice_id
    from collections import defaultdict
    batches_by_invoice = defaultdict(list)
    unlinked_batches   = []
    for b in all_batches:
        if b.invoice_id and b.invoice_id in inv_ids:
            batches_by_invoice[b.invoice_id].append(_batch_dict(b))
        elif not b.invoice_id:
            unlinked_batches.append(_batch_dict(b))

    # Fetch documents grouped by invoice_id
    docs_by_invoice = defaultdict(list)
    all_docs = SupplierDocument.query.filter_by(supplier_id=sid).all()
    for d in all_docs:
        if d.invoice_id and d.invoice_id in inv_ids:
            docs_by_invoice[d.invoice_id].append({
                'id': d.id, 'original_name': d.original_name,
                'filename': d.filename,
                'uploaded_at': d.uploaded_at.date().isoformat() if d.uploaded_at else None,
            })

    result = []
    for inv in invoices:
        batches = batches_by_invoice.get(inv.id, [])
        docs    = docs_by_invoice.get(inv.id, [])
        result.append({
            'id':                     inv.id,
            'invoice_number':         inv.invoice_number,
            'date':                   inv.date.isoformat() if inv.date else None,
            'status':                 inv.status,
            'source':                 inv.source,
            'subtotal':               float(inv.subtotal) if inv.subtotal is not None else None,
            'additional_costs_json':  inv.additional_costs_json,
            'additional_costs_total': float(inv.additional_costs_total) if inv.additional_costs_total is not None else None,
            'total':                  float(inv.total) if inv.total is not None else None,
            'notes':                  inv.notes,
            'batch_count':            len(batches),
            'batches':                batches,
            'documents':              docs,
        })

    # Append unlinked receives as a virtual group at the bottom
    if unlinked_batches:
        result.append({
            'id':            None,
            'invoice_number': None,
            'date':           unlinked_batches[0]['purchased_at'][:10] if unlinked_batches else None,
            'status':        'unlinked',
            'source':        'quick_receive',
            'subtotal':      sum(b['base_cost_total'] or 0 for b in unlinked_batches),
            'additional_costs_json': None,
            'additional_costs_total': None,
            'total':         sum(b['base_cost_total'] or 0 for b in unlinked_batches),
            'batch_count':   len(unlinked_batches),
            'batches':       unlinked_batches,
            'documents':     [],
        })

    return jsonify(result)


@bp.route('/api/suppliers/<int:sid>/invoices/<int:inv_id>', methods=['PUT'])
def api_supplier_invoice_update(sid, inv_id):
    """Replace all batches on an invoice with new lines (only if no stock consumed)."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(SupplierInvoice, inv_id)
    if not inv or inv.supplier_id != sid:
        return jsonify({'error': 'Invoice not found'}), 404

    batches = StockBatch.query.filter_by(invoice_id=inv_id).all()
    for b in batches:
        if StockConsumption.query.filter_by(batch_id=b.id).first():
            return jsonify({'error': f'Cannot edit — stock from batch #{b.id} has already been used in sales'}), 400
        if Decimal(str(b.qty_remaining_base)) != Decimal(str(b.qty_purchased_base)):
            return jsonify({'error': f'Cannot edit — some stock from batch #{b.id} has already been consumed'}), 400

    data           = request.json or {}
    lines          = data.get('lines', [])
    date_str       = data.get('date')
    addl_costs_raw = data.get('additional_costs', [])
    invoice_ref    = str(data.get('invoice_ref') or '').strip() or None

    if not lines:
        return jsonify({'error': 'No lines provided'}), 400

    from datetime import date as _date
    run_date      = _date.today()
    purchase_date = datetime.now()
    if date_str:
        try:
            parts         = date_str.split('-')
            run_date      = _date(int(parts[0]), int(parts[1]), int(parts[2]))
            purchase_date = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        except Exception:
            return jsonify({'error': 'Invalid date format'}), 400

    try:
        addl_costs = _parse_addl_costs(addl_costs_raw, source='supplier_run')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    u = current_user()

    # Delete existing batches
    for b in batches:
        db.session.delete(b)

    # Update invoice metadata
    inv.invoice_number = invoice_ref
    inv.date           = run_date

    # Re-create batches with same split logic as purchase_run
    prepared_lines = []
    for line in lines:
        pid         = line.get('product_id')
        qty         = line.get('qty')
        unit        = line.get('unit', 'unit')
        total_price = line.get('total_price')
        try:
            pid         = int(pid)
            qty         = float(qty)
            total_price = float(total_price)
        except Exception:
            return jsonify({'error': 'Invalid product_id, qty, or total_price'}), 400
        p = db.session.get(Product, pid)
        if not p:
            return jsonify({'error': f'Product id {pid} not found'}), 404
        conversion = _UNIT_CONVERSIONS.get(unit, 1)
        qty_base   = qty * conversion
        if qty_base == 0:
            return jsonify({'error': f'qty converts to 0 base units for product {pid}'}), 400
        prepared_lines.append({'pid': pid, 'qty_base': qty_base, 'base_cost_total': Decimal(str(total_price))})

    total_addl = sum(Decimal(str(c['amount'])) for c in addl_costs)
    shares     = _split_costs([l['base_cost_total'] for l in prepared_lines], total_addl)
    batches_created = 0

    for i, pl in enumerate(prepared_lines):
        share      = shares[i]
        batch_addl = []
        if share != Decimal('0') and addl_costs:
            if len(addl_costs) == 1:
                batch_addl = [{**addl_costs[0], 'amount': float(share.quantize(Decimal('0.01')))}]
            else:
                if total_addl != Decimal('0'):
                    running = Decimal('0')
                    for j, ac in enumerate(addl_costs):
                        if j == len(addl_costs) - 1:
                            entry_share = share - running
                        else:
                            entry_share = (Decimal(str(ac['amount'])) / total_addl * share).quantize(Decimal('0.01'))
                            running += entry_share
                        batch_addl.append({**ac, 'amount': float(entry_share)})
        cost_per_base = (pl['base_cost_total'] + share) / Decimal(str(pl['qty_base']))
        db.session.add(StockBatch(
            product_id=pl['pid'],
            qty_purchased_base=pl['qty_base'],
            qty_remaining_base=pl['qty_base'],
            cost_per_base_unit=cost_per_base,
            base_cost_total=pl['base_cost_total'],
            additional_costs=_json.dumps(batch_addl) if batch_addl else None,
            supplier_id=sid,
            user_id=u.id if u else None,
            purchased_at=purchase_date,
            invoice_id=inv_id,
        ))
        batches_created += 1

    subtotal = sum(pl['base_cost_total'] for pl in prepared_lines)
    inv.subtotal               = float(subtotal)
    inv.additional_costs_json  = _json.dumps([{'label': c['label'], 'type': c['type'], 'amount': c['amount']} for c in addl_costs]) if addl_costs else None
    inv.additional_costs_total = float(total_addl)
    inv.total                  = float(subtotal + total_addl)

    db.session.commit()
    return jsonify({'ok': True, 'batches_created': batches_created, 'invoice_id': inv_id, 'invoice_number': invoice_ref})


@bp.route('/api/suppliers/<int:sid>/invoices/<int:inv_id>', methods=['DELETE'])
def api_supplier_invoice_delete(sid, inv_id):
    """Delete an invoice and all its batches (only if no stock has been consumed)."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(SupplierInvoice, inv_id)
    if not inv or inv.supplier_id != sid:
        return jsonify({'error': 'Invoice not found'}), 404

    batches = StockBatch.query.filter_by(invoice_id=inv_id).all()
    for b in batches:
        if StockConsumption.query.filter_by(batch_id=b.id).first():
            return jsonify({'error': f'Cannot delete — stock from batch #{b.id} has already been used in sales'}), 400
        if Decimal(str(b.qty_remaining_base)) != Decimal(str(b.qty_purchased_base)):
            return jsonify({'error': f'Cannot delete — some stock from batch #{b.id} has already been consumed'}), 400

    # Delete linked documents from disk
    for doc in SupplierDocument.query.filter_by(invoice_id=inv_id).all():
        try:
            path = os.path.join(_doc_dir(), doc.filename)
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        db.session.delete(doc)

    for b in batches:
        db.session.delete(b)
    db.session.delete(inv)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/suppliers/<int:sid>/batches', methods=['GET'])
def api_supplier_batches(sid):
    """Legacy alias — returns flat batch list for the apply-costs selector."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    batches = (StockBatch.query
               .filter_by(supplier_id=sid)
               .order_by(StockBatch.purchased_at.desc(), StockBatch.id.desc())
               .limit(100).all())
    pids = {b.product_id for b in batches}
    prod_map = {p.id: p for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    result = []
    for b in batches:
        p = prod_map.get(b.product_id)
        qty_purchased = float(b.qty_purchased_base)
        qty_remaining = float(b.qty_remaining_base)
        consumed_pct  = round((1 - qty_remaining / qty_purchased) * 100, 1) if qty_purchased > 0 else 0
        result.append({
            'id': b.id, 'product_id': b.product_id,
            'product_name': p.name if p else str(b.product_id),
            'purchased_at': b.purchased_at.isoformat(),
            'qty_purchased_base': qty_purchased, 'qty_remaining_base': qty_remaining,
            'consumed_pct': consumed_pct,
            'cost_per_base_unit': float(b.cost_per_base_unit),
            'base_cost_total': float(b.base_cost_total) if b.base_cost_total is not None else None,
            'additional_costs': b.additional_costs,
            'invoice_id': b.invoice_id,
        })
    return jsonify(result)

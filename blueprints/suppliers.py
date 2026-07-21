import hashlib
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
from models import (db, Supplier, StockBatch, StockConsumption, Purchase, Product,
                    SupplierDocument, SupplierInvoice,
                    SupplierInvoiceTemplate, SupplierProductMapping,
                    SupplierInvoiceLearningEvent)

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
    # Word-boundary patterns (keyword must appear as a whole word)
    r'\b(?:total|subtotal|sub.total|balance\s+due|vat|tax\s+summary|payment|paid|discount'
    r'|thank\s+you|powered\s+by|page\s+\d|terms|conditions|banking\s+details|account\s+holder'
    r'|branch\s+code|swift|bill\s+to|ship\s+to|sold\s+to|delivery\s+details'
    r'|invoice\s+date|due\s+date|order\s+date|customer\s+order|customer\s+vat'
    r'|signature|stock\s+controller|produced\s+by|postnet\s+suite|private\s+bag'
    r'|account\s+number|account\s+no|branch\s+name|account\s+type|account\s+name'
    r'|reg\s+number|registration\s+number|company\s+id|vat\s+reg|vat\s+no'
    r'|document\s+no|ref\s+no|purchase\s+order|bill\s+from|remit\s+to|prepared\s+by'
    r'|south\s+africa|western\s+cape|gauteng|kwazulu'
    r'|eikeboom|hermon|cape\s+town|durbanville|sandton|melrose|boston)\b'
    # Non-word-boundary patterns (end in colon, period, or slash — \b fails after them)
    r'|(?:number|reference|document|invoice\s+no|contact|attn|attention|sold\s+by)\s*:'
    r'|(?:tel|fax|email|www)\s*[.:]'
    r'|p\.o\.|s\.o\.'
    # Invoice/order header lines like "Order 015832", "Invoice 12345"
    r'|\border\s+\d{5,}'
    # Contact footer lines: "Name, 082 XXXXXXX, email@domain" — phone number pattern
    r'|\b0[678]\d\s*\d{3}\s*\d{4}\b'
    # Lines containing an email address
    r'|[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE,
)

_HEADER_RE = re.compile(
    r'^(description|item|qty|quantity|unit\s+price|rate|amount|price|code|ext\.?\s*price'
    r'|disc\s*%|vat\s*%|excl\.|incl\.|line|date|activity|nett\s+price|no\.?\s+exclusive)\b',
    re.IGNORECASE,
)

_SHIPPING_RE = re.compile(r'\b(shipping|courier|freight|transport|delivery|postage|carriage)\b', re.IGNORECASE)

# Patterns that unambiguously start a new product line in a merged description cell
_PRODUCT_START_RE = re.compile(
    r'^\d+\s*[xX]\s+'               # "20 x 50g", "6 x"
    r'|^\d+\s*(?:g|ml|kg|l)\b'      # "500g Honey", "250ml Sauce"
    r'|^(?:Jar|Box|Pack|Bag|Bottle|Tin|Tub|Can|Tube|Cup|Sachet|Dr\.?)\s',
    re.IGNORECASE,
)


def _split_desc_lines(lines, n):
    """Split merged description lines into n product groups using definite-start boundaries."""
    lines = [l for l in lines if l]
    if not lines:
        return [''] * n
    if len(lines) <= n:
        return lines + [''] * (n - len(lines))

    # Find lines that unambiguously start a new product
    start_indices = [
        i for i, ln in enumerate(lines)
        if _PRODUCT_START_RE.match(ln) or _SHIPPING_RE.search(ln)
    ]

    if len(start_indices) == n:
        groups = []
        for k, si in enumerate(start_indices):
            end = start_indices[k + 1] if k + 1 < n else len(lines)
            groups.append(' '.join(lines[si:end]))
        return groups

    # Fallback: equal chunks, first line of each chunk
    chunk = max(1, (len(lines) + n - 1) // n)
    return [lines[k * chunk] for k in range(min(n, (len(lines) + chunk - 1) // chunk))]


def _extract_from_tables(pdf):
    """Extract line items from pdfplumber PDF tables, handling merged multi-value cells."""
    results = []
    shipping = 0.0

    for page in pdf.pages:
        for table in (page.extract_tables() or []):
            if not table or len(table) < 2:
                continue
            header = [str(c or '').lower().strip() for c in (table[0] or [])]
            if not header:
                continue

            desc_col  = next((i for i, h in enumerate(header) if 'desc' in h), None)
            if desc_col is None:
                desc_col = next((i for i, h in enumerate(header) if 'item' in h and 'no' not in h), None)
            qty_col   = next((i for i, h in enumerate(header) if h in ('qty', 'quantity') or h.startswith('qty')), None)
            total_col = next((i for i, h in enumerate(header) if 'total' in h or 'amount' in h or 'nett' in h), None)

            if desc_col is None or total_col is None:
                continue

            for row in table[1:]:
                if not any(row):
                    continue

                def _cell(idx):
                    if idx is None or idx >= len(row):
                        return ''
                    return str(row[idx] or '').strip()

                total_cell = _cell(total_col)
                qty_cell   = _cell(qty_col)
                desc_cell  = _cell(desc_col)

                total_lines = [l.strip() for l in total_cell.split('\n') if l.strip()]
                qty_lines   = [l.strip() for l in qty_cell.split('\n') if l.strip()]
                desc_lines  = [l.strip() for l in desc_cell.split('\n') if l.strip()]

                if not total_lines:
                    continue

                n = len(total_lines)
                desc_groups = _split_desc_lines(desc_lines, n)
                qty_groups  = qty_lines[:n] if len(qty_lines) >= n else ([qty_lines[0]] * n if qty_lines else ['1'] * n)

                for k in range(n):
                    price = _clean_num(total_lines[k])
                    if not price or price <= 0:
                        continue
                    desc = desc_groups[k] if k < len(desc_groups) else ''
                    if not desc or _SKIP_RE.search(desc) or _HEADER_RE.match(desc):
                        continue
                    desc = _deduplicate_description(desc)
                    qty = _clean_num(qty_groups[k]) if k < len(qty_groups) else 1.0
                    if _SHIPPING_RE.search(desc):
                        shipping = round(shipping + price, 2)
                    else:
                        results.append({'description': desc, 'qty': qty or 1.0, 'unit': 'unit', 'total_price': round(price, 2)})

    return results, shipping


def _undouble_chars(line):
    """Fix PDFs where every char is rendered twice: 'TTaaxx IInnvv' → 'Tax Inv'."""
    stripped = line.replace(' ', '')
    if len(stripped) < 6:
        return line
    pairs = sum(1 for i in range(0, len(stripped) - 1, 2) if stripped[i] == stripped[i + 1])
    if pairs / max(1, len(stripped) // 2) < 0.70:
        return line
    result, i = [], 0
    while i < len(line):
        c = line[i]
        if c != ' ' and i + 1 < len(line) and line[i + 1] == c:
            result.append(c); i += 2
        else:
            result.append(c); i += 1
    return ''.join(result)


def _deduplicate_description(desc):
    """Remove repeated phrase prefix from merged descriptions.
    Handles word-level ('Foo Bar Foo Bar Baz' → 'Foo Bar Baz') and
    char-level joins with no space ('Foo BarFoo Bar Baz' → 'Foo Bar Baz').
    """
    words = desc.split()
    n = len(words)
    for i in range(max(2, n // 5), n // 2 + 1):
        if words[:i] == words[i:2 * i]:
            return ' '.join(words[i:])
    # Char-level: handles 'Milk Tart and White ChocolateMilk Tart and White Chocolate Layered...'
    # Find longest prefix desc[:k] such that desc[k:] starts with desc[:k]
    for k in range(max(4, len(desc) // 5), len(desc) // 2 + 1):
        if desc[k:].startswith(desc[:k]):
            return desc[k:]
    return desc


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
    s = str(s).strip().lstrip('R').strip()
    # Remove space-thousands separators first: "1 147" → "1147"
    s = re.sub(r'(\d) (\d{3})(?=,\d{1,2}$|$)', r'\1\2', s)
    s = re.sub(r'(\d) (\d{3})', r'\1\2', s)
    # SA comma-decimal: digits,2digits at end → comma is decimal separator
    if re.search(r'^\d+,\d{1,2}$', s):
        s = s.replace(',', '.')
    else:
        s = s.replace(',', '')  # comma is thousands separator
    try:
        return float(s)
    except Exception:
        return None


def _try_parse_line(line):
    """Try to extract (description, qty, total_price) from a text line."""
    original = _undouble_chars(line.strip())
    if not original or len(original) < 8:
        return None

    # Skip header/footer lines
    if _SKIP_RE.search(original):
        return None
    if _HEADER_RE.match(original):
        return None

    # Skip customer name lines like "Nicolene Geyser DATE 15/07/2026"
    if re.search(r'\bDATE\b', original, re.IGNORECASE) and not re.search(r'\binvoice\b|\border\b', original, re.IGNORECASE):
        return None

    # Strip leading item code(s) (all-caps alphanum, e.g. "TILES001BLUE", "LEO03 BROWNSLIM")
    # Using + to handle consecutive codes on the same line
    line = re.sub(r'^(?:[A-Z][A-Z0-9]{3,}\s*[-–]?\s*)+', '', original).strip()
    # Strip leading date (YYYY/MM/DD or DD/MM/YYYY)
    line = re.sub(r'^\d{4}[/.-]\d{2}[/.-]\d{2}\s+', '', line).strip()
    line = re.sub(r'^\d{1,2}[/.-]\d{2}[/.-]\d{2,4}\s+', '', line).strip()
    # Strip leading line number (a bare integer followed by space)
    line = re.sub(r'^\d+\s+', '', line, count=1).strip()

    if len(line) < 4:
        return None

    # Strip embedded dates (mid-line "due date" columns like "2026/07/08")
    line = re.sub(r'\b\d{4}[/.-]\d{2}[/.-]\d{2}\b', '', line)
    line = re.sub(r'\b\d{1,2}[/.-]\d{2}[/.-]\d{4}\b', '', line)
    # Capture pack size spec before stripping — preserve in description for _detect_pack_multiplier
    # e.g. "(12x60g)" → pack_multiplier=12; this MUST be saved before stripping numbers
    _psm = re.search(r'\(\d+\s*[xX]\s*\d+\s*[gmlk][gl]?\)', line, re.IGNORECASE)
    _captured_size_spec = _psm.group(0) if _psm else ''
    # Strip product size specs in parentheses: (12x60g), (480g), (480g Tub) — prevents false qty/price tokens
    line = re.sub(r'\(\d+\s*[xX]\s*\d+\s*[gmlk][gl]?\)', '', line)
    line = re.sub(r'\(\d+\s*[gmlk][gl]?(?:\s+\w+)?\)', '', line)
    # Strip percentage column values like "0.00%", "15.00%" before they produce false qty tokens
    line = re.sub(r'\b\d+\.?\d*\s*%', '', line)
    line = re.sub(r'\s{2,}', ' ', line).strip()

    # Collect all price-like tokens: optional R, digits with optional separators/decimal
    # Handles SA format: space-thousands + comma-decimal (e.g. "1 147,83" = 1147.83, "47,83" = 47.83)
    # Alt order: longest/most-specific first so "1 147,83" beats "1 147" and "47,83" beats "47"
    num_pat = (
        r'R?\s*('
        r'\d{1,3}(?:\s\d{3})+,\d{1,2}'        # SA space-thousands + comma-decimal: "1 147,83"
        r'|\d{1,3}(?:\s\d{3})+(?:\.\d{1,2})?'  # space-thousands (+ optional dot-decimal): "1 147"
        r'|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?'   # US comma-thousands: "1,147"
        r'|\d{1,3},\d{1,2}'                     # SA comma-decimal only: "47,83"
        r'|\d+\.\d{1,2}'                        # dot-decimal: "47.83"
        r'|\d{1,5}'                             # plain integer
        r')(?!\s*%)'
    )
    tokens = []
    for m in re.finditer(num_pat, line):
        # Skip pack-size specifiers like "x6", "X12" — but only simple integers, not space-thousands prices
        if m.start() > 0 and line[m.start() - 1].lower() == 'x':
            raw_tok = m.group(1)
            if ' ' not in raw_tok and ',' not in raw_tok:
                continue
        # Skip numbers that are part of size descriptors: "125ml", "500g", "1.5kg", "250ml"
        rest = line[m.end():m.end() + 4].lower()
        if re.match(r'(?:ml|kg|g(?!b)|l(?!b))\b', rest):
            continue
        val = _clean_num(m.group(1))
        if val is not None and val >= 0:
            tokens.append((m.start(), m.end(), val))

    if len(tokens) < 2:
        return None

    # Skip lines that are entirely percentages or pure-number lines
    non_num = re.sub(num_pat, '', line).strip()
    if not non_num or re.match(r'^[\s%.,]+$', non_num):
        return None

    # Last token = total price; detect VAT pair (last two tokens in 1.15 ratio → use excl-VAT)
    total = tokens[-1][2]
    qty_tokens = tokens
    if len(tokens) >= 2 and tokens[-2][2] > 0:
        ratio = tokens[-1][2] / tokens[-2][2]
        if abs(ratio - 1.15) < 0.006:  # SA 15% VAT
            total = tokens[-2][2]
            qty_tokens = tokens[:-1]
    if total <= 0:
        return None

    # Find qty: look for a small integer (1-9999) among the tokens
    qty = 1.0
    unit_price_candidate = None

    if len(qty_tokens) >= 3:
        # Pattern: ... qty unit_price total
        # unit_price × qty ≈ total
        for i in range(len(qty_tokens) - 2, 0, -1):
            up = qty_tokens[i][2]
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
    # Remove trailing pack-size suffix like "x6", "x 6", "x" from product names
    desc = re.sub(r'\s+[xX]\s*\d*\s*$', '', desc).strip()
    # De-duplicate repeated phrase prefix (Nutri-Go activity+description columns merged)
    desc = _deduplicate_description(desc)
    # Re-append pack size spec so _detect_pack_multiplier can extract e.g. 12 from (12x60g)
    if _captured_size_spec and _captured_size_spec not in desc:
        desc = f'{desc} {_captured_size_spec}'.strip()

    if not desc or len(desc) < 3:
        return None
    # Description mustn't be purely numeric
    if re.match(r'^[\d\s.,R%]+$', desc):
        return None
    # Skip if starts with # (invoice/document number line like "# Inv-000368")
    if desc.startswith('#'):
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


# ── Supplier learning helpers ─────────────────────────────────────────────────

_DOC_TYPE_PATTERNS = [
    (r'\btax\s+invoice\b',                   'tax_invoice'),
    (r'\bproforma\b|\bpro[\s.\-]forma\b',    'proforma'),
    (r'\bsales\s+order\b',                   'sales_order'),
    (r'\bquotation\b|\bquote\b',             'quote'),
    (r'\bcredit\s+note\b|\bcredit\s+memo\b', 'credit_note'),
    (r'\bstatement\b',                       'statement'),
    (r'\binvoice\b',                         'invoice'),
]

_CONFIDENT_MULT_RE = re.compile(
    r'\b(\d+)\s*[xX]\s*\d',  # "12x60g", "12 x 60"
    re.IGNORECASE,
)
_POSSIBLE_MULT_RE = re.compile(
    r'\b(\d+)\s+per\s+(?:pack|packet|box|case|carton)\b'
    r'|\b(?:case|carton|box)\s+of\s+(\d+)\b',
    re.IGNORECASE,
)


def _detect_document_type(text):
    sample = text[:3000]
    for pattern, doc_type in _DOC_TYPE_PATTERNS:
        if re.search(pattern, sample, re.IGNORECASE):
            return doc_type
    return 'unknown'


def _normalize_desc(text):
    """Lowercase, collapse whitespace, strip non-word chars for matching."""
    if not text:
        return ''
    text = text.lower().strip()
    text = re.sub(r'[^\w\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _desc_hash(normalized):
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]


def _token_similarity(a, b):
    a_tok = set(_normalize_desc(a).split())
    b_tok = set(_normalize_desc(b).split())
    if not a_tok or not b_tok:
        return 0.0
    return len(a_tok & b_tok) / len(a_tok | b_tok)


def _match_scan_rows(scan_lines, confirmed_lines):
    """
    Match confirmed purchase lines to original scan lines using composite score.
    Score: 40% amount, 25% description similarity, 15% qty, 10% row order, 10% SKU.
    Returns: [(confirmed_line, scan_line_or_None, score), ...]
    """
    n = len(scan_lines)
    used = set()
    matches = []

    for ci_idx, ci in enumerate(confirmed_lines):
        ci_total = float(ci.get('total_price', 0))
        ci_qty   = float(ci.get('qty', 1))
        ci_name  = ci.get('product_name', '')

        best_score   = 0.0
        best_si_idx  = None

        for si_idx, si in enumerate(scan_lines):
            if si_idx in used:
                continue

            si_total = float(si.get('total_price', 0))
            si_qty   = float(si.get('qty', 1))
            si_desc  = si.get('description', '')

            score = 0.0

            # 40% amount similarity
            if si_total > 0:
                diff_pct = abs(si_total - ci_total) / si_total
                if diff_pct < 0.001:
                    score += 0.40
                elif diff_pct < 0.01:
                    score += 0.28
                elif diff_pct < 0.05:
                    score += 0.12

            # 25% description token overlap
            score += _token_similarity(si_desc, ci_name) * 0.25

            # 15% qty similarity (direct or via pack multiplier)
            if abs(si_qty - ci_qty) < 0.01:
                score += 0.15
            elif si_qty > 0:
                mult = round(ci_qty / si_qty)
                if mult >= 2 and abs(si_qty * mult - ci_qty) < 0.01:
                    score += 0.10

            # 10% row order proximity
            score += (1.0 - abs(si_idx - ci_idx) / max(n, 1)) * 0.10

            # 10% SKU match
            if si.get('sku') and ci.get('supplier_sku') and si['sku'] == ci['supplier_sku']:
                score += 0.10

            if score > best_score:
                best_score  = score
                best_si_idx = si_idx

        if best_si_idx is not None and best_score >= 0.30:
            matches.append((ci, scan_lines[best_si_idx], best_score))
            used.add(best_si_idx)
        else:
            matches.append((ci, None, 0.0))

    return matches


def _detect_pack_multiplier(raw_desc, invoice_qty, stock_qty):
    """Return (multiplier, confidence). Product size strings like '190g' are not multipliers."""
    if abs(invoice_qty) < 0.001:
        return 1, 1.0

    ratio = stock_qty / invoice_qty
    mult  = round(ratio)

    if mult <= 1 or abs(ratio - mult) > 0.05:
        return 1, 1.0

    m = _CONFIDENT_MULT_RE.search(raw_desc)
    if m and int(m.group(1)) == mult:
        return mult, 0.95

    m = _POSSIBLE_MULT_RE.search(raw_desc)
    if m:
        val = int(m.group(1) or m.group(2))
        if val == mult:
            return mult, 0.70

    # Multiplier detected from qty difference but not validated in description
    return mult, 0.50


def _normalize_invoice_number(inv_num):
    """Normalise invoice ref for duplicate detection: 'INV-3388' → '3388'."""
    if not inv_num:
        return None
    digits = re.sub(r'[^0-9]', '', str(inv_num))
    return digits if digits else str(inv_num).strip().lower()


def _check_cost_sanity(product_id, inv_qty, line_total, detected_multiplier):
    """Compare parsed unit cost against historical median from stock_batches.
    Returns a warning dict when pack multiplier is likely missing, else None.
    Requires ≥2 historical batches; only fires when detected_multiplier > 1."""
    if not product_id or not detected_multiplier or detected_multiplier <= 1:
        return None
    if inv_qty <= 0 or line_total <= 0:
        return None

    batches = (StockBatch.query
               .filter_by(product_id=product_id)
               .filter(StockBatch.cost_per_base_unit.isnot(None),
                       StockBatch.cost_per_base_unit > 0)
               .order_by(StockBatch.purchased_at.desc())
               .limit(8).all())

    if len(batches) < 2:
        return None

    prod = db.session.get(Product, product_id)
    if not prod:
        return None

    # Convert cost_per_base_unit to per-item cost
    # unit-type products: direct; weight/volume products: × package_size
    pkg_size = float(getattr(prod, 'package_size', None) or 1) or 1.0
    costs = sorted(float(b.cost_per_base_unit) * pkg_size for b in batches)
    n = len(costs)
    hist_median = costs[n // 2] if n % 2 else (costs[n//2 - 1] + costs[n//2]) / 2

    if hist_median <= 0:
        return None

    current_cost = line_total / inv_qty
    pack_cost    = line_total / (inv_qty * detected_multiplier)

    # Flag when: current_cost is abnormally high (>3× hist) AND pack-adjusted is close (within 50%)
    if (current_cost > hist_median * 3
            and abs(pack_cost - hist_median) / hist_median < 0.50):
        return {
            'detected_multiplier': detected_multiplier,
            'current_unit_cost':   round(current_cost, 2),
            'pack_unit_cost':      round(pack_cost, 2),
            'historical_median':   round(hist_median, 2),
            'sample_count':        n,
        }
    return None


def _apply_mappings(sid, lines, document_type='unknown'):
    """Apply learned mappings to parsed scan lines.
    Each line gets: suggested_product_id, suggested_product_name, pack_multiplier,
    confidence_tier (auto/review/suggest), mapping_id, mapping_state."""
    if not lines or not sid:
        return lines

    enriched = []
    for line in lines:
        raw_desc = line.get('description', '')
        if not raw_desc:
            enriched.append(line)
            continue

        norm_desc = _normalize_desc(raw_desc)
        desc_hash = _desc_hash(norm_desc)
        sku       = line.get('sku') or ''
        mapping   = None

        # 1. SKU match — strongest signal
        if sku:
            mapping = (SupplierProductMapping.query
                       .filter_by(supplier_id=sid, supplier_sku=sku)
                       .filter(SupplierProductMapping.mapping_state.notin_(['REJECTED', 'IGNORED']))
                       .first())

        # 2. Hash + document_type
        if not mapping:
            mapping = (SupplierProductMapping.query
                       .filter_by(supplier_id=sid, raw_description_hash=desc_hash, document_type=document_type)
                       .filter(SupplierProductMapping.mapping_state.notin_(['REJECTED', 'IGNORED']))
                       .first())

        # 3. Hash + 'unknown' fallback
        if not mapping and document_type != 'unknown':
            mapping = (SupplierProductMapping.query
                       .filter_by(supplier_id=sid, raw_description_hash=desc_hash, document_type='unknown')
                       .filter(SupplierProductMapping.mapping_state.notin_(['REJECTED', 'IGNORED']))
                       .first())

        if not mapping:
            # No DB mapping yet — try description-based pack detection as a first-scan suggestion
            dm = _CONFIDENT_MULT_RE.search(raw_desc)
            if dm:
                desc_mult = int(dm.group(1))
                if desc_mult >= 2:
                    enriched_line = dict(line)
                    enriched_line['pack_multiplier']  = desc_mult
                    enriched_line['confidence_tier']  = 'suggest'
                    enriched_line['confidence']       = 0.70
                    enriched.append(enriched_line)
                    continue
            enriched.append(line)
            continue

        conf  = float(mapping.confidence)
        state = mapping.mapping_state

        if state == 'CONFIRMED' or conf >= 0.85:
            tier = 'auto'
        elif conf >= 0.60:
            tier = 'review'
        else:
            tier = 'suggest'

        prod = db.session.get(Product, mapping.product_id) if mapping.product_id else None

        enriched_line = dict(line)
        enriched_line['mapping_id']      = mapping.id
        enriched_line['mapping_state']   = state
        enriched_line['confidence_tier'] = tier
        enriched_line['confidence']      = conf

        if mapping.product_id and mapping.line_type not in ('SHIPPING', 'UNKNOWN'):
            enriched_line['suggested_product_id']   = mapping.product_id
            enriched_line['suggested_product_name'] = prod.name if prod else ''
            enriched_line['pack_multiplier']        = float(mapping.pack_multiplier)

            # Cost sanity: compare unit cost vs historical median (catches forgotten multiplier)
            pm = float(mapping.pack_multiplier)
            if pm > 1:
                cw = _check_cost_sanity(
                    mapping.product_id,
                    float(line.get('qty', 1)),
                    float(line.get('total_price', 0)),
                    pm,
                )
                if cw:
                    enriched_line['cost_warning'] = cw

        enriched.append(enriched_line)

    return enriched


def _run_learning(sid, scan_result, confirmed_lines, invoice_id=None):
    """
    After a purchase run is confirmed/received, compare confirmed lines to the
    original scan result and update supplier_product_mappings.
    New mappings are created as SUGGESTED; learning events are written for audit.

    confirmed_lines: [{product_id, product_name, qty, total_price, supplier_sku?}]
    Returns list of learned-mapping dicts for UI display.
    """
    if not scan_result or not confirmed_lines:
        return []

    scan_lines = scan_result.get('lines') or []
    doc_type   = scan_result.get('document_type', 'unknown')
    if not scan_lines:
        return []

    matches = _match_scan_rows(scan_lines, confirmed_lines)
    learned = []
    now     = datetime.utcnow()

    for ci, si, score in matches:
        if si is None:
            continue

        raw_desc  = si.get('description', '')
        if not raw_desc:
            continue

        norm_desc  = _normalize_desc(raw_desc)
        desc_hash  = _desc_hash(norm_desc)
        tokens_str = ' '.join(sorted(set(re.sub(r'[^a-z0-9]', ' ', norm_desc).split())))

        product_id = ci.get('product_id')
        stock_qty  = float(ci.get('qty', 1))
        inv_qty    = float(si.get('qty', 1))

        # Classify line type
        if _SHIPPING_RE.search(raw_desc):
            line_type  = 'SHIPPING'
            product_id = None
        elif product_id:
            line_type = 'STOCK_ITEM'
        else:
            line_type = 'UNKNOWN'

        pack_mult, mult_conf = _detect_pack_multiplier(raw_desc, inv_qty, stock_qty)

        # Lookup: doc_type-specific first, then 'unknown' fallback
        existing = SupplierProductMapping.query.filter_by(
            supplier_id=sid, raw_description_hash=desc_hash, document_type=doc_type,
        ).first()
        if not existing:
            existing = SupplierProductMapping.query.filter_by(
                supplier_id=sid, raw_description_hash=desc_hash, document_type='unknown',
            ).first()

        if existing:
            # Never modify REJECTED or IGNORED — user explicitly set those
            if existing.mapping_state in ('REJECTED', 'IGNORED'):
                continue

            old_conf    = float(existing.confidence)
            old_prod_id = existing.product_id
            old_state   = existing.mapping_state
            prod_changed = product_id and existing.product_id != product_id
            mult_changed = abs(float(existing.pack_multiplier) - pack_mult) > 0.01
            new_conf     = old_conf

            if prod_changed:
                existing.product_id = product_id
                new_conf = max(0.10, new_conf - 0.30)
            if mult_changed:
                existing.pack_multiplier = pack_mult
                new_conf = max(0.10, new_conf - 0.20)
            if not prod_changed and not mult_changed:
                new_conf = min(0.95, new_conf + 0.10)

            existing.line_type            = line_type
            existing.confidence           = new_conf
            existing.correction_count    += 1
            existing.raw_description_tokens = tokens_str
            existing.last_used_at         = now
            existing.updated_at           = now
            if doc_type != 'unknown':
                existing.document_type = doc_type

            db.session.flush()
            db.session.add(SupplierInvoiceLearningEvent(
                invoice_id=invoice_id, supplier_id=sid, mapping_id=existing.id,
                raw_description=raw_desc, matched_product_id=product_id,
                action='updated', old_confidence=old_conf, new_confidence=new_conf,
                old_product_id=old_prod_id, new_product_id=product_id,
                old_state=old_state, new_state=existing.mapping_state,
                match_score=score, created_at=now,
            ))

            learned.append({
                'action': 'updated', 'mapping_id': existing.id, 'mapping_state': existing.mapping_state,
                'raw_description': raw_desc,
                'product_id': existing.product_id, 'product_name': ci.get('product_name', ''),
                'pack_multiplier': pack_mult, 'invoice_qty': inv_qty, 'stock_qty': stock_qty,
                'line_type': line_type, 'confidence': float(existing.confidence),
                'match_score': round(score, 2),
            })
        else:
            init_conf = min(0.75, 0.50 + score * 0.30)
            mapping = SupplierProductMapping(
                supplier_id=sid,
                raw_description_original=raw_desc,
                raw_description_normalized=norm_desc,
                raw_description_hash=desc_hash,
                raw_description_tokens=tokens_str,
                document_type=doc_type,
                supplier_sku=si.get('sku') or None,
                product_id=product_id if line_type == 'STOCK_ITEM' else None,
                line_type=line_type,
                pack_multiplier=pack_mult,
                invoice_unit=si.get('unit', 'unit'),
                mapping_state='SUGGESTED',
                correction_count=1,
                confidence=init_conf,
                first_learned_at=now,
                last_used_at=now,
                created_at=now,
                updated_at=now,
            )
            db.session.add(mapping)
            db.session.flush()  # get mapping.id before writing event

            db.session.add(SupplierInvoiceLearningEvent(
                invoice_id=invoice_id, supplier_id=sid, mapping_id=mapping.id,
                raw_description=raw_desc, matched_product_id=product_id,
                action='created', old_confidence=None, new_confidence=init_conf,
                old_product_id=None, new_product_id=product_id,
                old_state=None, new_state='SUGGESTED',
                match_score=score, created_at=now,
            ))

            learned.append({
                'action': 'created', 'mapping_id': mapping.id, 'mapping_state': 'SUGGESTED',
                'raw_description': raw_desc,
                'product_id': product_id, 'product_name': ci.get('product_name', ''),
                'pack_multiplier': pack_mult, 'invoice_qty': inv_qty, 'stock_qty': stock_qty,
                'line_type': line_type, 'confidence': init_conf,
                'match_score': round(score, 2),
            })

    # Update/create supplier invoice template with detected document type
    layout_type = scan_result.get('layout_type', 'unknown')
    tmpl = SupplierInvoiceTemplate.query.filter_by(supplier_id=sid, document_type=doc_type).first()
    if not tmpl:
        tmpl = SupplierInvoiceTemplate.query.filter_by(supplier_id=sid, active=True).first()
    if not tmpl:
        tmpl = SupplierInvoiceTemplate(
            supplier_id=sid, document_type=doc_type, layout_type=layout_type,
            column_hints='{}', totals_rules='{}', vat_rules='{}',
            line_classifier_rules='{}', confidence=0.30,
            created_at=now, updated_at=now,
        )
        db.session.add(tmpl)
    else:
        if doc_type != 'unknown':
            tmpl.document_type = doc_type
        tmpl.last_successful_parse_at = now
        tmpl.updated_at = now

    db.session.flush()
    return learned


def _extract_invoice_totals(full_text):
    """Extract VAT trio from invoice footer using the math relationship:
    total_incl = total_excl + total_vat, total_vat ≈ total_excl × 0.15 (SA VAT).
    Searches the last ~30% of text lines for consecutive amount triples.
    Returns {'total_excl', 'total_vat', 'total_incl', 'discount_total'}.
    """
    lines_text = full_text.split('\n')
    footer_start = max(0, len(lines_text) - max(25, len(lines_text) // 3))
    footer_lines = lines_text[footer_start:]

    amounts = []
    discount_total = 0.0

    for line in footer_lines:
        line = line.strip()
        if not line:
            continue
        # Extract amounts with two decimal places (money values)
        # Handles both dot-decimal (1147.83) and SA comma-decimal (1 147,83 / 47,83 / R3 624,00)
        nums = [_clean_num(m) for m in re.findall(
            r'R?\s*('
            r'\d{1,3}(?:\s\d{3})+,\d{2}'   # SA: "3 151,30", "R3 624,00" (space-thousands + comma-decimal)
            r'|\d{1,3}(?:\s\d{3})+\.\d{2}'  # space-thousands + dot-decimal: "3 151.30"
            r'|\d{1,3}(?:,\d{3})+\.\d{2}'   # US comma-thousands + dot-decimal: "3,151.30"
            r'|\d{1,5},\d{2}'               # SA comma-decimal only: "472,70"
            r'|\d{1,7}\.\d{2}'              # plain dot-decimal: "3151.30"
            r')', line
        )]
        nums = [v for v in nums if v is not None and v > 0]
        if not nums:
            continue
        if re.search(r'\bdiscount\b', line, re.IGNORECASE):
            discount_total = nums[-1]
            continue
        amounts.append(nums[-1])

    total_excl = total_vat = total_incl = None

    # Sliding window: find A, B, C where B/A ≈ 0.15 and C ≈ A + B
    for i in range(len(amounts) - 2):
        for j in range(i + 1, min(i + 4, len(amounts) - 1)):
            for k in range(j + 1, min(j + 4, len(amounts))):
                a, b, c = amounts[i], amounts[j], amounts[k]
                if a > 0 and b > 0:
                    if abs(b / a - 0.15) < 0.03 and abs(c - (a + b)) < max(2.0, c * 0.005):
                        total_excl, total_vat, total_incl = a, b, c
                        break
            if total_excl is not None:
                break
        if total_excl is not None:
            break

    # Fallback: adjacent pair in 1.15 ratio
    if total_vat is None:
        for i in range(len(amounts) - 1):
            a, b = amounts[i], amounts[i + 1]
            if a > 0 and abs(b / a - 1.15) < 0.025:
                total_excl = a
                total_incl = b
                total_vat  = round(b - a, 2)
                break

    return {
        'total_excl':     total_excl,
        'total_vat':      total_vat,
        'total_incl':     total_incl,
        'discount_total': discount_total,
    }


def _detect_vat_treatment(all_lines_sum, footer):
    """Compare all_lines_sum (product lines + shipping combined) against footer totals.
    Returns (vat_treatment, accounting_balanced).
    vat_treatment: 'lines_excl_vat' | 'lines_incl_vat' | 'unknown'
    """
    if not all_lines_sum or all_lines_sum <= 0:
        return 'unknown', False
    TOLERANCE = 2.0
    total_excl = footer.get('total_excl')
    total_vat  = footer.get('total_vat')
    total_incl = footer.get('total_incl')
    discount   = footer.get('discount_total', 0.0) or 0.0

    if total_excl and abs(all_lines_sum - total_excl) <= TOLERANCE:
        balanced = (total_incl is not None and total_vat is not None and
                    abs((total_excl + total_vat) - total_incl) <= TOLERANCE)
        return 'lines_excl_vat', balanced

    if total_incl and abs(all_lines_sum - total_incl) <= TOLERANCE:
        return 'lines_incl_vat', True

    if total_excl and discount and abs(all_lines_sum - (total_excl + discount)) <= TOLERANCE:
        balanced = (total_incl is not None and total_vat is not None and
                    abs((total_excl + total_vat) - total_incl) <= TOLERANCE)
        return 'lines_excl_vat', balanced

    if total_incl and discount and abs(all_lines_sum - (total_incl + discount)) <= TOLERANCE:
        return 'lines_incl_vat', True

    return 'unknown', False


def _parse_invoice_pdf(content):
    """Extract structured invoice data from PDF bytes using pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError('pdfplumber is not installed on this server')

    full_text_parts = []
    table_results   = []
    table_shipping  = 0.0

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        table_results, table_shipping = _extract_from_tables(pdf)
        for page in pdf.pages:
            t = page.extract_text() or ''
            full_text_parts.append(t)

    full_text = '\n'.join(full_text_parts)
    if not full_text.strip():
        raise RuntimeError(
            'This PDF is a scanned image — no text could be extracted. '
            'Please fill in the delivery details manually.'
        )

    invoice_number = _extract_invoice_number(full_text)
    invoice_date   = _extract_date(full_text)
    document_type  = _detect_document_type(full_text)

    # Prefer structured table extraction when it found items (handles merged cells)
    if table_results:
        footer = _extract_invoice_totals(full_text)
        all_lines_sum = sum(r['total_price'] for r in table_results) + table_shipping
        vat_treatment, accounting_balanced = _detect_vat_treatment(all_lines_sum, footer)
        return {
            'invoice_number':      invoice_number,
            'date':                invoice_date,
            'lines':               table_results,
            'shipping':            table_shipping if table_shipping > 0 else None,
            'raw_line_count':      len(full_text.split('\n')),
            'document_type':       document_type,
            'layout_type':         'table',
            'vat_total':           footer.get('total_vat'),
            'vat_treatment':       vat_treatment,
            'accounting_balanced': accounting_balanced,
            'invoice_total_excl':  footer.get('total_excl'),
            'invoice_total_incl':  footer.get('total_incl'),
            'discount_total':      footer.get('discount_total') or 0,
        }

    # Fall back to line-by-line text parsing
    lines    = []
    shipping = 0.0

    for raw in full_text.split('\n'):
        raw = raw.strip()
        if not raw:
            continue
        if _SHIPPING_RE.search(raw) and not _SKIP_RE.search(raw):
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

    footer = _extract_invoice_totals(full_text)
    all_lines_sum = sum(r['total_price'] for r in deduped) + shipping
    vat_treatment, accounting_balanced = _detect_vat_treatment(all_lines_sum, footer)
    return {
        'invoice_number':      invoice_number,
        'date':                invoice_date,
        'lines':               deduped,
        'shipping':            shipping if shipping > 0 else None,
        'raw_line_count':      len(full_text.split('\n')),
        'document_type':       document_type,
        'layout_type':         'text',
        'vat_total':           footer.get('total_vat'),
        'vat_treatment':       vat_treatment,
        'accounting_balanced': accounting_balanced,
        'invoice_total_excl':  footer.get('total_excl'),
        'invoice_total_incl':  footer.get('total_incl'),
        'discount_total':      footer.get('discount_total') or 0,
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
        # Enrich lines with learned mappings for this supplier
        result['lines'] = _apply_mappings(sid, result.get('lines', []), result.get('document_type', 'unknown'))
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

    data               = request.json or {}
    lines              = data.get('lines', [])
    date_str           = data.get('date')
    addl_costs_raw     = data.get('additional_costs', [])
    invoice_ref        = str(data.get('invoice_ref') or '').strip() or None
    invoice_addl_total = data.get('invoice_additional_total')
    scan_result        = data.get('scan_result')   # original parser output — triggers learning
    bypass_dup         = bool(data.get('bypass_duplicate_check', False))
    # VAT fields from invoice scan — retained for reporting, VAT included in COGS
    vat_total_req      = data.get('vat_total')
    vat_total          = Decimal(str(vat_total_req)) if vat_total_req is not None else Decimal('0')
    discount_total_req = data.get('discount_total')
    discount_total_inv = Decimal(str(discount_total_req)) if discount_total_req is not None else Decimal('0')
    vat_treatment      = str(data.get('vat_treatment') or 'unknown')
    accounting_balanced = bool(data.get('accounting_balanced', False))

    if not lines:
        return jsonify({'error': 'No lines provided'}), 400

    # Duplicate invoice detection — soft block; caller re-submits with bypass_duplicate_check=true
    if invoice_ref and not bypass_dup:
        norm_ref = _normalize_invoice_number(invoice_ref)
        if norm_ref:
            for existing_inv in SupplierInvoice.query.filter_by(supplier_id=sid).all():
                if _normalize_invoice_number(existing_inv.invoice_number) == norm_ref:
                    return jsonify({
                        'duplicate_warning':      True,
                        'existing_invoice_id':    existing_inv.id,
                        'existing_invoice_number': existing_inv.invoice_number,
                        'existing_date':          existing_inv.date.isoformat() if existing_inv.date else None,
                        'existing_total':         float(existing_inv.total or 0),
                    })

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
        scan_raw_json=_json.dumps(scan_result) if scan_result else None,
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
            consignment_unit_cost_raw = line.get('consignment_unit_cost')
            prepared_lines.append({
                'pid': pid, 'qty_base': qty_base,
                'base_cost_total': Decimal(str(total_price)),
                'is_consignment': bool(getattr(p, 'is_consignment', False)),
                'consignment_unit_cost': float(consignment_unit_cost_raw) if consignment_unit_cost_raw is not None else None,
            })

    # Step 1: proportional VAT allocation across product lines (ex-VAT base)
    # VAT is allocated to products only (not shipping); retained per-batch for reporting.
    # COGS = base_ex_vat + vat_amount + overhead_share  (VAT is included in inventory value)
    subtotal = sum(pl['base_cost_total'] for pl in prepared_lines)
    vat_shares = _split_costs([l['base_cost_total'] for l in prepared_lines], vat_total)

    # Step 2: proportional split of additional costs (shipping/overhead) across lines
    # Overhead is applied on top of the incl-VAT base
    total_addl = sum(Decimal(str(c['amount'])) for c in addl_costs)
    incl_vat_bases = [pl['base_cost_total'] + vat_shares[i] for i, pl in enumerate(prepared_lines)]
    shares = _split_costs(incl_vat_bases, total_addl)

    for i, pl in enumerate(prepared_lines):
        vat_share  = vat_shares[i]
        share      = shares[i]
        batch_addl = []
        if share != Decimal('0') and addl_costs:
            if len(addl_costs) == 1:
                batch_addl = [{**addl_costs[0], 'amount': float(share.quantize(Decimal('0.01')))}]
            else:
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

        # Allocation: ex_vat + vat = incl_vat → + overheads = final_cost
        base_incl_vat    = pl['base_cost_total'] + vat_share
        shipping_alloc   = sum(Decimal(str(e['amount'])) for e in batch_addl
                               if e.get('type') == 'shipping') if batch_addl else Decimal('0')
        final_cost       = base_incl_vat + share
        cost_per_base    = final_cost / Decimal(str(pl['qty_base']))
        _ownership  = 'CONSIGNMENT' if pl.get('is_consignment') else 'NORMAL'
        _cuc        = pl.get('consignment_unit_cost')
        db.session.add(StockBatch(
            product_id=pl['pid'],
            qty_purchased_base=pl['qty_base'],
            qty_remaining_base=pl['qty_base'],
            cost_per_base_unit=cost_per_base,
            ownership_type=_ownership,
            consignment_unit_cost=_cuc,
            base_cost_total=pl['base_cost_total'],
            vat_amount=float(vat_share.quantize(Decimal('0.0001'))) if vat_total > 0 else None,
            base_cost_incl_vat=float(base_incl_vat),
            allocated_shipping=float(shipping_alloc) if shipping_alloc > 0 else None,
            final_cost_incl_vat=float(final_cost),
            additional_costs=_json.dumps(batch_addl) if batch_addl else None,
            supplier_id=sid,
            user_id=u.id if u else None,
            purchased_at=purchase_date,
            invoice_id=run_id,
        ))
        batches_created += 1

    # Stamp invoice-level totals
    inv.subtotal               = float(subtotal)
    inv.additional_costs_json  = _json.dumps([{'label': c['label'], 'type': c['type'], 'amount': c['amount']} for c in addl_costs]) if addl_costs else None
    inv.additional_costs_total = float(total_addl)
    inv.vat_total              = float(vat_total) if vat_total > 0 else None
    inv.discount_total         = float(discount_total_inv) if discount_total_inv > 0 else None
    inv.vat_treatment          = vat_treatment
    inv.accounting_balanced    = accounting_balanced
    inv.total                  = float(subtotal + vat_total + total_addl)

    # VAT reconciliation check — warn if float storage drifts from computed Decimal totals
    if vat_total > 0 and prepared_lines:
        sum_vat_shares = sum(vat_shares)
        # Σ(vat_shares) must equal vat_total exactly (_split_costs guarantees this)
        if abs(sum_vat_shares - vat_total) > Decimal('0.02'):
            current_app.logger.warning(
                f'[purchase_run] VAT reconciliation mismatch for invoice {run_id}: '
                f'sum_vat_shares={sum_vat_shares} != vat_total={vat_total}'
            )
        # Σ(base_ex_vat) + Σ(vat_shares) == Σ(base_incl_vat) — waterfall integrity
        sum_ex   = sum(pl['base_cost_total'] for pl in prepared_lines)
        sum_incl = sum(pl['base_cost_total'] + vat_shares[i] for i, pl in enumerate(prepared_lines))
        if abs((sum_ex + sum_vat_shares) - sum_incl) > Decimal('0.01'):
            current_app.logger.warning(
                f'[purchase_run] VAT waterfall mismatch for invoice {run_id}: '
                f'sum_ex={sum_ex} + sum_vat={sum_vat_shares} != sum_incl={sum_incl}'
            )

    # Update supplier's last_run_costs for pre-population next time
    if addl_costs and batches_created > 0:
        run_level = [{'label': c['label'], 'type': c['type'], 'amount': float(Decimal(str(c['amount'])).quantize(Decimal('0.01')))} for c in addl_costs]
        s.last_run_costs = _json.dumps(run_level)

    # Run learning step — compare confirmed lines to original scan result
    # Build confirmed_lines list enriched with product names for description matching
    confirmed_for_learning = []
    for line in lines:
        pid = line.get('product_id')
        if pid:
            try:
                pid = int(pid)
            except Exception:
                pid = None
        p_name = ''
        if pid:
            prod = db.session.get(Product, pid)
            p_name = prod.name if prod else ''
        confirmed_for_learning.append({
            'product_id':   pid,
            'product_name': p_name,
            'qty':          line.get('qty', 1),
            'total_price':  line.get('total_price', 0),
            'supplier_sku': line.get('supplier_sku', ''),
        })

    learned = []
    if scan_result:
        try:
            learned = _run_learning(sid, scan_result, confirmed_for_learning, invoice_id=run_id)
        except Exception as _le:
            current_app.logger.warning(f'Learning step failed for supplier {sid}: {_le}')

    db.session.commit()
    return jsonify({
        'ok': True,
        'created_products': created_products,
        'batches_created':  batches_created,
        'invoice_id':       run_id,
        'invoice_number':   invoice_ref,
        'learned':          learned,
    })


@bp.route('/api/suppliers/<int:sid>/product-mappings', methods=['GET'])
def api_supplier_product_mappings(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if not db.session.get(Supplier, sid):
        return jsonify({'error': 'Not found'}), 404
    mappings = (SupplierProductMapping.query
                .filter_by(supplier_id=sid)
                .order_by(SupplierProductMapping.confidence.desc())
                .all())
    result = []
    for m in mappings:
        prod = db.session.get(Product, m.product_id) if m.product_id else None
        result.append({
            'id':                  m.id,
            'raw_description':     m.raw_description_original,
            'supplier_sku':        m.supplier_sku,
            'product_id':          m.product_id,
            'product_name':        prod.name if prod else None,
            'line_type':           m.line_type,
            'pack_multiplier':     float(m.pack_multiplier),
            'invoice_unit':        m.invoice_unit,
            'confidence':          float(m.confidence),
            'correction_count':    m.correction_count,
            'mapping_state':       getattr(m, 'mapping_state', 'SUGGESTED'),
            'document_type':       getattr(m, 'document_type', 'unknown'),
            'first_learned_at':    m.first_learned_at.date().isoformat() if getattr(m, 'first_learned_at', None) else None,
            'last_used_at':        m.last_used_at.date().isoformat() if m.last_used_at else None,
        })
    return jsonify(result)


@bp.route('/api/suppliers/<int:sid>/product-mappings/<int:mid>', methods=['DELETE'])
def api_supplier_product_mapping_delete(sid, mid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    m = SupplierProductMapping.query.filter_by(id=mid, supplier_id=sid).first()
    if not m:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(m)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/suppliers/<int:sid>/product-mappings/<int:mid>', methods=['PATCH'])
def api_supplier_product_mapping_patch(sid, mid):
    """Update mapping_state: SUGGESTED | CONFIRMED | REJECTED | IGNORED."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    m = SupplierProductMapping.query.filter_by(id=mid, supplier_id=sid).first()
    if not m:
        return jsonify({'error': 'Not found'}), 404
    data      = request.json or {}
    new_state = data.get('mapping_state')
    if new_state not in ('SUGGESTED', 'CONFIRMED', 'REJECTED', 'IGNORED'):
        return jsonify({'error': 'mapping_state must be SUGGESTED, CONFIRMED, REJECTED, or IGNORED'}), 400
    old_state = getattr(m, 'mapping_state', 'SUGGESTED')
    m.mapping_state = new_state
    m.updated_at    = datetime.utcnow()
    db.session.add(SupplierInvoiceLearningEvent(
        supplier_id=sid, mapping_id=m.id,
        raw_description=m.raw_description_original, matched_product_id=m.product_id,
        action='state_changed', old_state=old_state, new_state=new_state,
        created_at=datetime.utcnow(),
    ))
    db.session.commit()
    return jsonify({'ok': True, 'mapping_state': new_state})


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
            'vat_total':              float(inv.vat_total) if getattr(inv, 'vat_total', None) is not None else None,
            'discount_total':         float(inv.discount_total) if getattr(inv, 'discount_total', None) is not None else None,
            'vat_treatment':          getattr(inv, 'vat_treatment', None),
            'accounting_balanced':    getattr(inv, 'accounting_balanced', None),
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
    vat_total_req  = data.get('vat_total')
    vat_total_upd  = Decimal(str(vat_total_req)) if vat_total_req is not None else Decimal('0')
    vat_treatment_upd    = str(data.get('vat_treatment') or getattr(inv, 'vat_treatment', 'unknown') or 'unknown')
    accounting_bal_upd   = bool(data.get('accounting_balanced', getattr(inv, 'accounting_balanced', False) or False))

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

    # Re-create batches with same VAT-first allocation logic as purchase_run
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

    subtotal_upd = sum(pl['base_cost_total'] for pl in prepared_lines)
    vat_shares_upd   = _split_costs([l['base_cost_total'] for l in prepared_lines], vat_total_upd)
    total_addl       = sum(Decimal(str(c['amount'])) for c in addl_costs)
    incl_vat_bases   = [pl['base_cost_total'] + vat_shares_upd[i] for i, pl in enumerate(prepared_lines)]
    shares           = _split_costs(incl_vat_bases, total_addl)
    batches_created  = 0

    for i, pl in enumerate(prepared_lines):
        vat_share  = vat_shares_upd[i]
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
        base_incl_vat_upd   = pl['base_cost_total'] + vat_share
        shipping_alloc_upd  = sum(Decimal(str(e['amount'])) for e in batch_addl
                                  if e.get('type') == 'shipping') if batch_addl else Decimal('0')
        final_cost_upd      = base_incl_vat_upd + share
        cost_per_base       = final_cost_upd / Decimal(str(pl['qty_base']))
        db.session.add(StockBatch(
            product_id=pl['pid'],
            qty_purchased_base=pl['qty_base'],
            qty_remaining_base=pl['qty_base'],
            cost_per_base_unit=cost_per_base,
            base_cost_total=pl['base_cost_total'],
            vat_amount=float(vat_share.quantize(Decimal('0.0001'))) if vat_total_upd > 0 else None,
            base_cost_incl_vat=float(base_incl_vat_upd),
            allocated_shipping=float(shipping_alloc_upd) if shipping_alloc_upd > 0 else None,
            final_cost_incl_vat=float(final_cost_upd),
            additional_costs=_json.dumps(batch_addl) if batch_addl else None,
            supplier_id=sid,
            user_id=u.id if u else None,
            purchased_at=purchase_date,
            invoice_id=inv_id,
        ))
        batches_created += 1

    inv.subtotal               = float(subtotal_upd)
    inv.additional_costs_json  = _json.dumps([{'label': c['label'], 'type': c['type'], 'amount': c['amount']} for c in addl_costs]) if addl_costs else None
    inv.additional_costs_total = float(total_addl)
    inv.vat_total              = float(vat_total_upd) if vat_total_upd > 0 else None
    inv.vat_treatment          = vat_treatment_upd
    inv.accounting_balanced    = accounting_bal_upd
    inv.total                  = float(subtotal_upd + vat_total_upd + total_addl)

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

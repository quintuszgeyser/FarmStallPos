"""
CSV Bulk Product Import

POST /api/products/import?mode=preview|import|strict&allow_name_match=false
GET  /api/products/import-template

CSV template version=1. Idempotent - re-running same CSV produces identical state.
Match priority: product_code (primary) > name (fallback, only if allow_name_match=true).
"""
import csv
import hashlib
import io
import time as _time
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request, Response

from helpers import (
    require_role, current_user,
    _assign_product_code, _gen_barcode_from_code, _plu_range,
    get_or_create_category,
)

# Unit conversion (grams/ml base)
_UNIT_CONV = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1}
from models import db, Product, ProductImportRun, SubCategory, ProductFamily

bp = Blueprint('imports', __name__)

# CSV version - bump when column schema changes
CSV_VERSION = 1

REQUIRED_COLS = {'name', 'product_type'}

# Column order determines template header row order.
# New optional columns appended at the end — existing CSVs without them still parse correctly.
ALL_COLS = [
    # ── Core (mandatory) ──────────────────────────────────────────────────────
    'name',
    'product_type',
    'unit_type',
    # ── Pricing (conditional mandatory — see template comments) ───────────────
    'price',
    'price_per_unit',
    # ── Identity ─────────────────────────────────────────────────────────────
    'product_code',
    'barcode',
    # ── Stock & sales ─────────────────────────────────────────────────────────
    'stock_qty',
    'is_for_sale',
    'low_stock_threshold',
    # ── Metadata ──────────────────────────────────────────────────────────────
    'description',
    'margin_pct',
    # ── Classification ────────────────────────────────────────────────────────
    'category',
    'sub_category',
    'family_name',
    'is_default_variant',
    # ── Online shop & kitchen ─────────────────────────────────────────────────
    'is_available_online',
    'is_prepared',
    # ── Scale ─────────────────────────────────────────────────────────────────
    'sync_to_scale',
    'scale_tare',
    'scale_shelf_life',
    'scale_open_price',
    'scale_msg1',
    'scale_msg2',
    # ── Consignment ───────────────────────────────────────────────────────────
    'is_consignment',
    'settlement_basis',
    'consignment_pct',
    # ── Packaging ─────────────────────────────────────────────────────────────
    'package_size',
    'package_size_unit',
    'package_unit',
]

SCALE_RELEVANT = {'name', 'price', 'price_per_unit', 'scale_tare', 'scale_shelf_life',
                  'scale_open_price', 'scale_msg1', 'scale_msg2', 'sold_by_weight'}

_VALID_PACKAGE_UNITS = {'g', 'kg', 'ml', 'l', 'unit'}


def _parse_bool(val, default=True):
    if val is None or val == '':
        return default
    return str(val).strip().lower() in ('true', '1', 'yes')


def _parse_decimal(val):
    if val is None or str(val).strip() == '':
        return None
    try:
        return Decimal(str(val).strip())
    except InvalidOperation:
        return None


def _parse_int(val):
    if val is None or str(val).strip() == '':
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def _normalize_name(name):
    """Collapse whitespace, preserve original case."""
    return ' '.join(name.split())


def _resolve_family_import(name):
    """Find or create a ProductFamily by name. Returns the family id, or None if name is blank.
    Does NOT commit — caller owns the transaction."""
    import re
    name = ' '.join(name.split())  # normalize whitespace
    if not name:
        return None
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    fam = ProductFamily.query.filter_by(slug=slug).first()
    if not fam:
        # Also try exact name match in case slug collides with a differently-named family
        fam = ProductFamily.query.filter(
            db.func.lower(ProductFamily.name) == name.lower()
        ).first()
    if fam:
        return fam.id
    # Ensure slug is unique
    base, n = slug, 2
    while ProductFamily.query.filter_by(slug=slug).first():
        slug = f'{base}-{n}'; n += 1
    fam = ProductFamily(name=name, slug=slug)
    db.session.add(fam)
    db.session.flush()
    return fam.id


def _validate_row(row, idx):
    """Returns list of (error_msg, error_code) tuples. Empty = valid."""
    errors = []
    warnings = []
    ptype = row.get('product_type', '').strip()
    utype = row.get('unit_type', '').strip()
    name  = row.get('name', '').strip()

    if not name:
        errors.append(('name is required', 'MISSING_FIELD'))

    if ptype not in ('stock_item', 'recipe'):
        errors.append((f"product_type '{ptype}' invalid (stock_item/recipe)", 'INVALID_TYPE'))

    sold_by_weight = ptype == 'stock_item' and utype in ('weight', 'volume')

    price     = _parse_decimal(row.get('price'))
    ppu       = _parse_decimal(row.get('price_per_unit'))

    if ptype != 'recipe':
        if sold_by_weight:
            if not ppu or ppu <= 0:
                errors.append(('price_per_unit required and > 0 for weight/volume items', 'MISSING_FIELD'))
            if price is not None:
                errors.append(('price must be blank for weight/volume items (use price_per_unit)', 'VALIDATION_FAILED'))
        else:
            if ptype != 'recipe' and (not price or price <= 0):
                errors.append(('price required and > 0 for fixed-price items', 'MISSING_FIELD'))
            if ppu is not None:
                errors.append(('price_per_unit must be blank for fixed items (use price)', 'VALIDATION_FAILED'))

    tare = _parse_decimal(row.get('scale_tare'))
    if tare is not None and tare < 0:
        errors.append(('scale_tare must be >= 0', 'VALIDATION_FAILED'))

    shelf = _parse_int(row.get('scale_shelf_life'))
    if shelf is not None and shelf < 0:
        errors.append(('scale_shelf_life must be >= 0', 'VALIDATION_FAILED'))

    msg1 = (row.get('scale_msg1') or '').strip()
    msg2 = (row.get('scale_msg2') or '').strip()
    if len(msg1) > 20:
        errors.append(('scale_msg1 max 20 chars', 'VALIDATION_FAILED'))
    if len(msg2) > 20:
        errors.append(('scale_msg2 max 20 chars', 'VALIDATION_FAILED'))

    pc = _parse_int(row.get('product_code'))
    if pc is not None:
        if pc <= 0 or pc > 99999:
            errors.append((f'product_code {pc} out of range (1-99999)', 'RANGE_EXCEEDED'))
        else:
            lo, hi = _plu_range(sold_by_weight, utype, ptype)
            if not (lo <= pc <= hi):
                errors.append((f'product_code {pc} not in valid range {lo}-{hi} for this type', 'RANGE_EXCEEDED'))

    # Consignment
    is_consignment = _parse_bool(row.get('is_consignment'), False)
    if is_consignment:
        sb = (row.get('settlement_basis') or '').strip().upper()
        if sb and sb not in ('FIXED_COST', 'PCT_OF_SALE'):
            errors.append((f"settlement_basis '{sb}' invalid — use FIXED_COST or PCT_OF_SALE", 'INVALID_TYPE'))
        if sb == 'PCT_OF_SALE':
            pct = _parse_decimal(row.get('consignment_pct'))
            if pct is None or pct <= 0 or pct > 100:
                errors.append(('consignment_pct must be 0–100 when settlement_basis=PCT_OF_SALE', 'MISSING_FIELD'))

    # Package size unit
    ps_unit = (row.get('package_size_unit') or '').strip().lower()
    if ps_unit and ps_unit not in _VALID_PACKAGE_UNITS:
        errors.append((f"package_size_unit '{ps_unit}' invalid — use g/kg/ml/L/unit", 'INVALID_TYPE'))

    # Warnings
    if not row.get('margin_pct'):
        warnings.append('margin_pct not set')
    if not row.get('description') and _parse_bool(row.get('is_for_sale'), True):
        warnings.append('description empty')
    if _parse_bool(row.get('sync_to_scale'), sold_by_weight) and not sold_by_weight:
        warnings.append('sync_to_scale=true but product is not weight/volume')
    sub_name = (row.get('sub_category') or '').strip()
    cat_name = (row.get('category') or '').strip()
    if sub_name and not cat_name:
        warnings.append('sub_category set but category is blank — sub_category will be ignored')

    fam_name = (row.get('family_name') or '').strip()
    if fam_name and not row.get('is_default_variant'):
        warnings.append('family_name set but is_default_variant is blank — defaulting to false')

    return errors, warnings


def _parse_csv(file_bytes):
    """Parse CSV bytes, return (version, rows, header_errors)."""
    text = file_bytes.decode('utf-8-sig')  # strips UTF-8 BOM
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    lines = [l for l in text.split('\n') if l.strip()]

    version = None
    if lines and lines[0].startswith('#'):
        comment = lines[0].lstrip('#').strip()
        if comment.startswith('version='):
            try:
                version = int(comment.split('=')[1])
            except Exception:
                pass
        lines = lines[1:]

    # Strip any remaining comment lines (e.g. template example labels like "# Example weight product:")
    lines = [l for l in lines if not l.strip().startswith('#')]

    if not lines:
        return None, [], ['CSV file is empty']

    reader = csv.DictReader(io.StringIO('\n'.join(lines)))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]

    header_errors = []
    if version and version != CSV_VERSION:
        header_errors.append(f'CSV version {version} not supported (expected {CSV_VERSION})')

    missing_required = REQUIRED_COLS - set(headers)
    if missing_required:
        header_errors.append(f'Missing required columns: {", ".join(sorted(missing_required))}')

    unknown = set(headers) - set(ALL_COLS) - {''}
    # Unknown columns are warnings, not errors - don't block import

    rows = []
    for row in reader:
        cleaned = {k.strip().lower(): (v or '').strip() for k, v in row.items() if k}
        rows.append(cleaned)

    return version, rows, header_errors


@bp.route('/api/products/import-template')
def api_import_template():
    """Download CSV template with headers, column guide, and example rows."""
    header = ','.join(ALL_COLS)

    guide = [
        f'# version={CSV_VERSION}',
        '# ════════════════════════════════════════════════════════════════════',
        '# PRODUCT IMPORT TEMPLATE — Farm POS',
        '# ════════════════════════════════════════════════════════════════════',
        '#',
        '# MANDATORY — every row must have these',
        '#   name           Unique product display name (required for all types)',
        '#   product_type   stock_item | recipe',
        '#',
        '# CONDITIONAL — required depending on product_type / unit_type',
        '#   unit_type      REQUIRED for stock_item: weight | volume | count',
        '#   price          REQUIRED for stock_item (count) and recipe — sale price in Rands',
        '#   price_per_unit REQUIRED for stock_item (weight or volume) — price per gram/ml',
        '#                  Leave the OTHER pricing column blank (not both, not neither)',
        '#',
        '# OPTIONAL — leave blank to use the default shown in brackets',
        '#   product_code       Auto-assigned if blank.',
        '#                      Weight/volume range: 1–19 999  |  Fixed/count: 20 000–29 999',
        '#   barcode            Auto-generated if blank (fixed/count items only)',
        '#   stock_qty          Starting stock for a NEW product only (ignored on update)',
        '#   is_for_sale        true/false  [default: true]  — show product at teller',
        '#   low_stock_threshold  Alert threshold in product base unit (g, ml, or unit)',
        '#   description        Shown on website and product detail view',
        '#   margin_pct         Target margin % — for pricing guidance only',
        '#   category           Category name. Created automatically if it does not exist.',
        '#   sub_category       Sub-category name (requires category to also be filled in).',
        '#                      Created automatically if it does not exist.',
        '#   family_name        Product family name (groups variants like colours/sizes).',
        '#                      Created automatically if it does not exist.',
        '#                      All variants in a family should share the same category.',
        '#   is_default_variant true/false  [default: false]',
        '#                      The family member shown on the website listing page.',
        '#                      Exactly one variant per family should be true.',
        '#   is_available_online  true/false  [default: false]  — list on Lady Coleen website',
        '#   is_prepared        true/false  [default: false]  — sends to kitchen queue on sale',
        '#   sync_to_scale      true/false  [default: true for weight/volume, false otherwise]',
        '#   scale_tare         Tare weight in grams printed on scale label (weight items)',
        '#   scale_shelf_life   Shelf life in days printed on scale label',
        '#   scale_open_price   true/false  [default: false]  — allow price override on scale',
        '#   scale_msg1         Extra label line 1 (max 20 chars)',
        '#   scale_msg2         Extra label line 2 (max 20 chars)',
        '#   is_consignment     true/false  [default: false]  — stock owned by supplier until sold',
        '#   settlement_basis   FIXED_COST | PCT_OF_SALE  [default: FIXED_COST]',
        '#                      Only relevant when is_consignment=true',
        '#   consignment_pct    Supplier % of sale price (1–100).',
        '#                      Only required when settlement_basis=PCT_OF_SALE',
        '#   package_size       Package quantity number (e.g. 500 for a 500 g jar)',
        '#   package_size_unit  g | kg | ml | L | unit',
        '#   package_unit       Package display name (e.g. "500g jar")',
        '#',
        '# ════════════════════════════════════════════════════════════════════',
        '# EXAMPLES — delete example rows before importing your own data',
        '# ════════════════════════════════════════════════════════════════════',
        '#',
        '# Example 1: Weight product — price_per_unit required; price must be blank',
    ]

    def row(*vals):
        return ','.join(str(v) for v in vals)

    # Col order: name,product_type,unit_type,price,price_per_unit,product_code,barcode,
    #            stock_qty,is_for_sale,low_stock_threshold,description,margin_pct,
    #            category,sub_category,family_name,is_default_variant,
    #            is_available_online,is_prepared,
    #            sync_to_scale,scale_tare,scale_shelf_life,scale_open_price,scale_msg1,scale_msg2,
    #            is_consignment,settlement_basis,consignment_pct,
    #            package_size,package_size_unit,package_unit

    ex_weight = row(
        'Springbok Biltong', 'stock_item', 'weight',
        '', '0.68',
        '', '',
        '', 'true', '50',
        'Springbok biltong from the farm', '40',
        'Biltong', 'Springbok', '', '',          # category, sub_category, family_name, is_default_variant
        'true', 'false',
        'true', '10', '14', 'false', 'Keep refrigerated', '',
        'false', '', '',
        '', '', '',
    )

    ex_fixed = row(
        'Biltong Sampler', 'stock_item', 'count',
        '89.99', '',
        '', '',
        '', 'true', '',
        'Gift pack of assorted biltong', '35',
        'Gift Packs', '', '', '',
        'true', 'false',
        'false', '', '', 'false', '', '',
        'false', '', '',
        '', '', '',
    )

    ex_volume = row(
        'Fresh Milk', 'stock_item', 'volume',
        '', '0.03',
        '', '',
        '', 'true', '1000',
        'Fresh farm milk', '40',
        'Dairy', '', '', '',
        'true', 'false',
        'true', '0', '3', 'false', '', '',
        'false', '', '',
        '1', 'L', '1L bottle',
    )

    ex_consignment = row(
        'Artisan Honey', 'stock_item', 'count',
        '125.00', '',
        '', '',
        '', 'true', '2',
        'Local artisan honey 500g jar', '20',
        'Honey & Preserves', 'Honey', '', '',
        'true', 'false',
        'false', '', '', 'false', '', '',
        'true', 'PCT_OF_SALE', '60',
        '500', 'g', '500g jar',
    )

    # Example showing two family variants (Apron Red = default, Apron Blue = non-default)
    ex_variant_default = row(
        'Lady Coleen Apron Red', 'stock_item', 'count',
        '350.00', '',
        '', '',
        '', 'true', '5',
        'Lady Coleen branded apron — red', '45',
        'Merchandise', 'Aprons', 'Lady Coleen Apron', 'true',
        'true', 'false',
        'false', '', '', 'false', '', '',
        'false', '', '',
        '', '', '',
    )
    ex_variant_other = row(
        'Lady Coleen Apron Blue', 'stock_item', 'count',
        '350.00', '',
        '', '',
        '', 'true', '5',
        'Lady Coleen branded apron — blue', '45',
        'Merchandise', 'Aprons', 'Lady Coleen Apron', 'false',
        'true', 'false',
        'false', '', '', 'false', '', '',
        'false', '', '',
        '', '', '',
    )

    lines = guide + [
        f'# {ex_weight}',
        '# Example 2: Fixed-price count product — price required; price_per_unit must be blank',
        f'# {ex_fixed}',
        '# Example 3: Volume product — price_per_unit required; price must be blank',
        f'# {ex_volume}',
        '# Example 4: Consignment product — supplier paid 60% of each sale',
        f'# {ex_consignment}',
        '# Example 5a: Family variant — default (shown on website listing)',
        f'# {ex_variant_default}',
        '# Example 5b: Family variant — non-default (reachable via options selector on detail page)',
        f'# {ex_variant_other}',
        '#',
        header,
    ]

    content = '\n'.join(lines) + '\n'
    return Response(
        content,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=products_import_template.csv'}
    )


@bp.route('/api/products/import', methods=['POST'])
def api_import_products():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    mode             = request.args.get('mode', 'preview')
    allow_name_match = request.args.get('allow_name_match', 'false').lower() == 'true'

    if mode not in ('preview', 'import', 'strict'):
        return jsonify({'error': 'mode must be preview, import, or strict'}), 400

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    file_bytes = f.read()
    file_hash  = hashlib.sha256(file_bytes).hexdigest()
    file_name  = f.filename or 'upload.csv'

    # Warn if same file was already imported
    prev = ProductImportRun.query.filter_by(file_hash=file_hash).filter(
        ProductImportRun.mode.in_(('import', 'strict'))
    ).first()
    duplicate_warning = f'This file was already imported on {prev.imported_at.strftime("%Y-%m-%d %H:%M")}' if prev else None

    version, raw_rows, header_errors = _parse_csv(file_bytes)
    if header_errors:
        return jsonify({'error': '; '.join(header_errors)}), 400

    if len(raw_rows) > 500:
        return jsonify({'error': 'Maximum 500 rows per import'}), 400

    t0 = _time.monotonic()
    results = []
    total_errors = 0

    # Detect duplicate product_codes — mark only; append in the main loop so
    # results[i] always aligns with raw_rows[i] for the zip in _do_import_*.
    seen_codes = {}
    for idx, raw in enumerate(raw_rows, start=2):
        pc = _parse_int(raw.get('product_code'))
        if pc is not None:
            if pc in seen_codes:
                raw['_skip'] = True
                raw['_dup_error'] = f'product_code {pc} appears twice in this file (first at row {seen_codes[pc]})'
            else:
                seen_codes[pc] = idx

    for idx, raw in enumerate(raw_rows, start=2):  # row 1 = header
        if raw.get('_skip'):
            results.append({'row': idx, 'name': raw.get('name', ''), 'action': 'error',
                            'error': raw.get('_dup_error', 'Duplicate product_code in batch'),
                            'error_code': 'DUPLICATE_IN_BATCH'})
            total_errors += 1
            continue
        name = _normalize_name(raw.get('name', ''))
        if not name:
            results.append({'row': idx, 'name': '', 'action': 'error',
                            'error': 'name is required', 'error_code': 'MISSING_FIELD'})
            total_errors += 1
            continue

        raw['name'] = name  # use normalized name

        errors, warnings = _validate_row(raw, idx)
        if errors:
            results.append({'row': idx, 'name': name, 'action': 'error',
                            'error': errors[0][0], 'error_code': errors[0][1],
                            'all_errors': [{'msg': e[0], 'code': e[1]} for e in errors],
                            'warnings': warnings})
            total_errors += 1
            continue

        # Find existing product
        existing = None
        pc = _parse_int(raw.get('product_code'))
        if pc:
            by_code = Product.query.filter_by(product_code=pc).first()
            if by_code:
                if _normalize_name(by_code.name).lower() != name.lower():
                    results.append({'row': idx, 'name': name, 'action': 'error',
                                    'error': f'product_code {pc} belongs to "{by_code.name}"',
                                    'error_code': 'PLU_CONFLICT', 'warnings': warnings})
                    total_errors += 1
                    continue
                existing = by_code
        if not existing and allow_name_match:
            existing = Product.query.filter(
                db.func.lower(Product.name) == name.lower()
            ).first()

        results.append(_process_row(raw, existing, idx, warnings, mode))

    duration_ms = int((_time.monotonic() - t0) * 1000)

    created   = sum(1 for r in results if r['action'] == 'create')
    updated   = sum(1 for r in results if r['action'] == 'update')
    unchanged = sum(1 for r in results if r['action'] == 'unchanged')
    skipped   = sum(1 for r in results if r['action'] == 'skip')
    errors    = sum(1 for r in results if r['action'] == 'error')

    if mode == 'preview':
        return jsonify({
            'mode': mode, 'duration_ms': duration_ms, 'file_hash': file_hash,
            'duplicate_warning': duplicate_warning,
            'rows': results,
            'summary': {'create': created, 'update': updated, 'unchanged': unchanged,
                        'error': errors, 'skip': skipped}
        })

    # Commit phase
    if mode == 'strict' and errors > 0:
        return jsonify({
            'error': f'Strict mode: {errors} validation errors - nothing imported',
            'rows': [r for r in results if r['action'] == 'error'],
            'summary': {'create': 0, 'update': 0, 'unchanged': 0, 'error': errors}
        }), 422

    # Seed lock key if it doesn't exist yet (fresh DB), then acquire atomically
    db.session.execute(db.text(
        "INSERT INTO settings (key, value) VALUES ('import_in_progress', 'false') ON CONFLICT (key) DO NOTHING"
    ))
    lock_result = db.session.execute(db.text(
        "UPDATE settings SET value='true' "
        "WHERE key='import_in_progress' AND value='false' RETURNING key"
    )).fetchone()
    if not lock_result:
        return jsonify({'error': 'Another import is already in progress'}), 409

    user = current_user()
    run = ProductImportRun(
        file_name=file_name, file_hash=file_hash, mode=mode,
        allow_name_match=allow_name_match,
        rows_total=len(raw_rows), imported_by=user.id if user else None,
    )
    db.session.add(run)
    db.session.flush()

    try:
        if mode == 'strict':
            _do_import_strict(results, raw_rows, allow_name_match)
        else:
            _do_import_partial(results, raw_rows, allow_name_match)

        run.rows_created   = sum(1 for r in results if r['action'] == 'create')
        run.rows_updated   = sum(1 for r in results if r['action'] == 'update')
        run.rows_unchanged = sum(1 for r in results if r['action'] == 'unchanged')
        run.rows_skipped   = sum(1 for r in results if r['action'] == 'skip')
        run.rows_error     = sum(1 for r in results if r['action'] == 'error')
        run.duration_ms    = int((_time.monotonic() - t0) * 1000)
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Import failed: {str(e)}'}), 500
    finally:
        # Always release import lock
        db.session.execute(db.text(
            "UPDATE settings SET value='false' WHERE key='import_in_progress'"
        ))
        db.session.commit()

    return jsonify({
        'mode': mode, 'duration_ms': run.duration_ms, 'file_hash': file_hash,
        'rows': results,
        'summary': {
            'create': run.rows_created, 'update': run.rows_updated,
            'unchanged': run.rows_unchanged, 'error': run.rows_error, 'skip': run.rows_skipped,
        }
    })


def _process_row(raw, existing, idx, warnings, mode):
    """Determine action for a row without writing to DB."""
    name  = raw['name']
    ptype = raw.get('product_type', '').strip()
    utype = raw.get('unit_type', '').strip() or None
    sold_by_weight = ptype == 'stock_item' and utype in ('weight', 'volume')

    incoming = _build_product_fields(raw, sold_by_weight, utype, ptype, existing)

    if not existing:
        return {'row': idx, 'name': name, 'action': 'create', 'warnings': warnings}

    # Check for differences
    changes = {}
    for field, new_val in incoming.items():
        if field in ('product_code', 'barcode'):
            continue  # don't show auto-assigned fields as changes
        old_val = getattr(existing, field, None)
        if old_val != new_val and not (old_val is None and new_val is None):
            # Format for display
            old_str = str(float(old_val)) if isinstance(old_val, Decimal) else str(old_val)
            new_str = str(float(new_val)) if isinstance(new_val, Decimal) else str(new_val)
            if old_str != new_str:
                changes[field] = f'{old_str} → {new_str}'

    # Family change detection (no DB lookup needed for preview)
    fam_name = (raw.get('family_name') or '').strip()
    if fam_name:
        old_fam = (existing.family.name.strip() if existing and existing.family else '') if existing else ''
        if fam_name.lower() != old_fam.lower():
            changes['family_name'] = f'{old_fam or "(none)"} → {fam_name}'

    # Category / sub_category change detection (no DB lookup needed for preview)
    cat_name = (raw.get('category') or '').strip()
    if cat_name:
        old_cat = (existing.category.name.strip() if existing and existing.category else '') if existing else ''
        if cat_name.lower() != old_cat.lower():
            changes['category'] = f'{old_cat or "(none)"} → {cat_name}'
    sub_name = (raw.get('sub_category') or '').strip()
    if sub_name and cat_name:
        old_sub = (existing.sub_category.name.strip() if existing and existing.sub_category else '') if existing else ''
        if sub_name.lower() != old_sub.lower():
            changes['sub_category'] = f'{old_sub or "(none)"} → {sub_name}'

    if not changes:
        return {'row': idx, 'name': name, 'action': 'unchanged', 'warnings': warnings}

    return {'row': idx, 'name': name, 'action': 'update', 'changes': changes, 'warnings': warnings}


def _build_product_fields(raw, sold_by_weight, utype, ptype, existing=None):
    """Build dict of field values to apply to product."""
    fields = {}
    fields['name']         = raw['name']
    fields['product_type'] = ptype
    fields['unit_type']    = utype
    fields['base_unit']    = {'weight': 'g', 'volume': 'ml', 'count': 'unit'}.get(utype) if utype else None
    fields['sold_by_weight'] = sold_by_weight
    fields['is_for_sale']  = _parse_bool(raw.get('is_for_sale'), True)
    fields['description']  = raw.get('description') or None
    fields['margin_pct']   = _parse_decimal(raw.get('margin_pct'))

    if sold_by_weight:
        fields['price_per_unit'] = _parse_decimal(raw.get('price_per_unit'))
        fields['price'] = None
    else:
        fields['price'] = _parse_decimal(raw.get('price'))
        fields['price_per_unit'] = None

    lt = _parse_decimal(raw.get('low_stock_threshold'))
    fields['low_stock_threshold'] = lt

    # Scale fields
    sync = raw.get('sync_to_scale')
    fields['sync_to_scale']    = _parse_bool(sync, sold_by_weight)
    fields['scale_tare']       = _parse_decimal(raw.get('scale_tare'))
    fields['scale_shelf_life'] = _parse_int(raw.get('scale_shelf_life'))
    fields['scale_open_price'] = _parse_bool(raw.get('scale_open_price'), False)
    msg1 = (raw.get('scale_msg1') or '').strip()[:20]
    msg2 = (raw.get('scale_msg2') or '').strip()[:20]
    fields['scale_msg1'] = msg1 or None
    fields['scale_msg2'] = msg2 or None

    # Family / variants
    fields['is_default_variant'] = _parse_bool(raw.get('is_default_variant'), False)

    # Online shop / kitchen
    fields['is_available_online'] = _parse_bool(raw.get('is_available_online'), False)
    fields['is_prepared']          = _parse_bool(raw.get('is_prepared'), False)

    # Consignment
    fields['is_consignment'] = _parse_bool(raw.get('is_consignment'), False)
    sb = (raw.get('settlement_basis') or '').strip().upper()
    fields['settlement_basis'] = sb if sb in ('FIXED_COST', 'PCT_OF_SALE') else 'FIXED_COST'
    if fields['is_consignment'] and fields['settlement_basis'] == 'PCT_OF_SALE':
        fields['consignment_pct'] = _parse_decimal(raw.get('consignment_pct'))
    else:
        fields['consignment_pct'] = None

    # Packaging
    ps = _parse_decimal(raw.get('package_size'))
    fields['package_size']      = ps
    ps_unit = (raw.get('package_size_unit') or '').strip()
    fields['package_size_unit'] = ps_unit if ps_unit.lower() in _VALID_PACKAGE_UNITS else None
    fields['package_unit']      = (raw.get('package_unit') or '').strip() or None

    return fields


def _apply_fields(product, fields, is_new, raw):
    """Write fields to product instance."""
    for k, v in fields.items():
        setattr(product, k, v)

    # product_code: use from CSV if provided, else auto-assign for new products
    if is_new:
        pc = _parse_int(raw.get('product_code'))
        if pc:
            product.product_code = pc
        else:
            product.product_code = _assign_product_code(
                fields['sold_by_weight'], fields['unit_type'], fields['product_type']
            )
        # Barcode for fixed products
        if not fields['sold_by_weight'] and fields['unit_type'] != 'volume':
            bc = raw.get('barcode', '').strip()
            if bc:
                product.barcode = bc
            else:
                product.barcode = _gen_barcode_from_code(product.product_code)
    else:
        # Only update product_code if explicitly provided and different
        pc = _parse_int(raw.get('product_code'))
        if pc and pc != product.product_code:
            product.product_code = pc
            product.scale_hash = None


def _do_import_partial(results, raw_rows, allow_name_match):
    """Write valid rows one by one, skip errors. Uses savepoints to isolate row-level failures."""
    from sqlalchemy.exc import IntegrityError
    for idx, (result, raw) in enumerate(zip(results, raw_rows), start=2):
        if result['action'] == 'error':
            continue
        sp = db.session.begin_nested()
        try:
            _write_row(result, raw, allow_name_match)
            sp.commit()
        except IntegrityError as e:
            sp.rollback()
            orig = getattr(e, 'orig', None)
            detail = str(orig) if orig else str(e)
            if 'DETAIL:' in detail:
                detail = detail.split('DETAIL:')[-1].strip()
            result['action'] = 'error'
            result['error'] = f'Duplicate value — {detail}'
            result['error_code'] = 'INTEGRITY_ERROR'
        except ValueError as e:
            sp.rollback()
            result['action'] = 'error'
            result['error'] = str(e)
            result['error_code'] = 'BARCODE_CONFLICT'


def _do_import_strict(results, raw_rows, allow_name_match):
    """Write all valid rows in a single transaction. Rolls back on any error."""
    for idx, (result, raw) in enumerate(zip(results, raw_rows), start=2):
        if result['action'] == 'error':
            raise ValueError(f"Row {idx}: {result.get('error')}")
        _write_row(result, raw, allow_name_match)


def _apply_family(p, raw):
    """Resolve family_name from raw CSV row and set product_family_id on p.
    Blank family_name = leave existing value unchanged."""
    fam_name = (raw.get('family_name') or '').strip()
    if not fam_name:
        return
    fam_id = _resolve_family_import(fam_name)
    if fam_id:
        p.product_family_id = fam_id


def _apply_category(p, raw):
    """Resolve category / sub_category from raw CSV row and set on product p.
    Only writes when the CSV cell is non-empty; blank = leave existing value unchanged."""
    cat_name = (raw.get('category') or '').strip()
    if not cat_name:
        return
    cat = get_or_create_category(cat_name)
    if not cat:
        return
    p.category_id = cat.id

    sub_name = (raw.get('sub_category') or '').strip()
    if sub_name:
        norm = sub_name.lower()
        sub = SubCategory.query.filter_by(category_id=cat.id, name_norm=norm).first()
        if not sub:
            sub = SubCategory(category_id=cat.id, name=sub_name, name_norm=norm)
            db.session.add(sub)
            db.session.flush()
        p.sub_category_id = sub.id
    else:
        # Category changed but no sub_category specified — clear old sub_category if it
        # belonged to a different category
        if p.sub_category_id:
            old_sub = db.session.get(SubCategory, p.sub_category_id)
            if old_sub and old_sub.category_id != cat.id:
                p.sub_category_id = None


def _write_row(result, raw, allow_name_match):
    """Write a single row to DB."""
    from models import Product as P
    name  = raw['name']
    ptype = raw.get('product_type', '').strip()
    utype = raw.get('unit_type', '').strip() or None
    sold_by_weight = ptype == 'stock_item' and utype in ('weight', 'volume')

    fields = _build_product_fields(raw, sold_by_weight, utype, ptype, None)

    if result['action'] == 'unchanged':
        return

    if result['action'] == 'create':
        p = P()
        _apply_fields(p, fields, is_new=True, raw=raw)
        _apply_family(p, raw)
        _apply_category(p, raw)
        db.session.add(p)
        db.session.flush()
        result['product_id'] = p.id
    elif result['action'] == 'update':
        pc = _parse_int(raw.get('product_code'))
        p = None
        if pc:
            p = P.query.filter_by(product_code=pc).first()
        if not p and allow_name_match:
            p = P.query.filter(db.func.lower(P.name) == name.lower()).first()
        if not p:
            result['action'] = 'error'
            result['error'] = 'Product not found for update'
            return
        old_fields = {k: getattr(p, k, None) for k in fields}
        _apply_fields(p, fields, is_new=False, raw=raw)
        _apply_family(p, raw)
        _apply_category(p, raw)
        # Check if scale-relevant fields changed
        scale_changed = any(fields.get(f) != old_fields.get(f) for f in SCALE_RELEVANT)
        if scale_changed:
            p.scale_hash = None
        result['product_id'] = p.id

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
)

# Unit conversion (grams/ml base)
_UNIT_CONV = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1}
from models import db, Product, ProductImportRun

bp = Blueprint('imports', __name__)

# CSV version - bump when column schema changes
CSV_VERSION = 1

REQUIRED_COLS = {'name', 'product_type'}
ALL_COLS = [
    'name', 'product_type', 'unit_type', 'price', 'price_per_unit',
    'product_code', 'stock_qty', 'barcode', 'is_for_sale',
    'low_stock_threshold', 'sync_to_scale', 'scale_tare', 'scale_shelf_life',
    'scale_msg1', 'scale_msg2', 'description', 'margin_pct',
]

SCALE_RELEVANT = {'name', 'price', 'price_per_unit', 'scale_tare', 'scale_shelf_life',
                  'scale_open_price', 'scale_msg1', 'scale_msg2', 'scale_prohibit', 'sold_by_weight'}


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


def _validate_row(row, idx):
    """Returns list of (error_msg, error_code) tuples. Empty = valid."""
    errors = []
    warnings = []
    ptype = row.get('product_type', '').strip()
    utype = row.get('unit_type', '').strip()
    name  = row.get('name', '').strip()

    if not name:
        errors.append(('name is required', 'MISSING_FIELD'))

    if ptype not in ('simple', 'stock_item', 'recipe'):
        errors.append((f"product_type '{ptype}' invalid (simple/stock_item/recipe)", 'INVALID_TYPE'))

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

    # Warnings
    if not row.get('margin_pct'):
        warnings.append('margin_pct not set')
    if not row.get('description') and _parse_bool(row.get('is_for_sale'), True):
        warnings.append('description empty')
    if _parse_bool(row.get('sync_to_scale'), sold_by_weight) and not sold_by_weight:
        warnings.append('sync_to_scale=true but product is not weight/volume')

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
    """Download CSV template with headers and example rows."""
    lines = [
        f'# version={CSV_VERSION}',
        ','.join(ALL_COLS),
        '# Example weight product (biltong):',
        'Springbok Biltong,stock_item,weight,,0.68,1,,,true,50,true,10,14,Keep refrigerated,,Springbok biltong from the farm,40',
        '# Example fixed price product:',
        'Biltong Sampler,simple,count,89.99,,20000,,,true,,false,,,,,,Gift pack of assorted biltong,35',
        '# Example volume product (milk):',
        'Fresh Milk,stock_item,volume,,0.03,30000,,,true,1000,true,0,3,,,,Fresh farm milk,40',
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

    # Detect duplicate product_codes within the same CSV batch before processing
    seen_codes = {}
    for idx, raw in enumerate(raw_rows, start=2):
        pc = _parse_int(raw.get('product_code'))
        if pc is not None:
            if pc in seen_codes:
                results.append({'row': idx, 'name': raw.get('name', ''), 'action': 'error',
                                'error': f'product_code {pc} appears twice in this file (first at row {seen_codes[pc]})',
                                'error_code': 'DUPLICATE_IN_BATCH'})
                total_errors += 1
                raw['_skip'] = True
            else:
                seen_codes[pc] = idx

    for idx, raw in enumerate(raw_rows, start=2):  # row 1 = header
        if raw.get('_skip'):
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

    # Acquire atomic import lock
    lock_result = db.session.execute(db.text(
        "UPDATE settings SET value='true', updated_at=NOW() "
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

    if ptype == 'simple':
        sq = _parse_int(raw.get('stock_qty'))
        fields['stock_qty'] = sq if sq is not None else 0

    lt = _parse_decimal(raw.get('low_stock_threshold'))
    fields['low_stock_threshold'] = lt

    # Scale fields
    sync = raw.get('sync_to_scale')
    fields['sync_to_scale'] = _parse_bool(sync, sold_by_weight)
    fields['scale_tare']       = _parse_decimal(raw.get('scale_tare'))
    fields['scale_shelf_life'] = _parse_int(raw.get('scale_shelf_life'))
    msg1 = (raw.get('scale_msg1') or '').strip()[:20]
    msg2 = (raw.get('scale_msg2') or '').strip()[:20]
    fields['scale_msg1'] = msg1 or None
    fields['scale_msg2'] = msg2 or None

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
    """Write valid rows one by one, skip errors."""
    for idx, (result, raw) in enumerate(zip(results, raw_rows), start=2):
        if result['action'] == 'error':
            continue
        _write_row(result, raw, allow_name_match)


def _do_import_strict(results, raw_rows, allow_name_match):
    """Write all valid rows in a single transaction. Rolls back on any error."""
    for idx, (result, raw) in enumerate(zip(results, raw_rows), start=2):
        if result['action'] == 'error':
            raise ValueError(f"Row {idx}: {result.get('error')}")
        _write_row(result, raw, allow_name_match)


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
        # Check if scale-relevant fields changed
        scale_changed = any(fields.get(f) != old_fields.get(f) for f in SCALE_RELEVANT)
        if scale_changed:
            p.scale_hash = None
        result['product_id'] = p.id

import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from helpers import require_role, current_user, get_or_create_category
from models import db, Product, Category, ProductBulkEditRun, StockBatch, SubCategory, ProductFamily

bp = Blueprint('bulk', __name__)
logger = logging.getLogger('pos')

# ── Editable fields and their types ─────────────────────────────────────────
_EDITABLE = {
    'name':                 {'type': 'str',      'label': 'Name'},
    'description':          {'type': 'str',      'label': 'Description'},
    'price':                {'type': 'float',    'label': 'Price (fixed)'},
    'price_per_unit':       {'type': 'float',    'label': 'Price per kg/L'},
    'margin_pct':           {'type': 'float',    'label': 'Markup %'},
    'low_stock_threshold':  {'type': 'float',    'label': 'Low stock threshold'},
    'stat_unit_size':       {'type': 'float',    'label': 'Stat unit size'},
    'is_for_sale':          {'type': 'bool',     'label': 'For Sale'},
    'is_available_online':  {'type': 'bool',     'label': 'Online'},
    'is_prepared':          {'type': 'bool',     'label': 'Prepared (kitchen)'},
    'sync_to_scale':        {'type': 'bool',     'label': 'Sync to scale'},
    'auto_price':           {'type': 'bool',     'label': 'Auto-price (markup %)'},
    'scale_shelf_life':     {'type': 'int',      'label': 'Shelf life (days)'},
    'scale_tare':           {'type': 'float',    'label': 'Scale tare (g)'},
    'scale_msg1':           {'type': 'str',      'label': 'Scale message 1'},
    'scale_msg2':           {'type': 'str',      'label': 'Scale message 2'},
    'category':              {'type': 'category',    'label': 'Category'},
    'subcategory':           {'type': 'subcategory', 'label': 'Subcategory'},
    'product_family':        {'type': 'family',      'label': 'Product family'},
    'barcode':               {'type': 'str',         'label': 'Barcode'},
    'unit_type':             {'type': 'str',         'label': 'Unit type'},
    'sold_by_weight':        {'type': 'bool',        'label': 'Sold by weight'},
    'packaging_capacity':    {'type': 'int',         'label': 'Packaging capacity (units)'},
    'category_is_packaging': {'type': 'bool',        'label': 'Mark category as packaging'},
}

# ── Filterable fields ────────────────────────────────────────────────────────
_FILTERABLE = {
    'name':                 {'type': 'str',    'label': 'Name'},
    'description':          {'type': 'str',    'label': 'Description'},
    'product_type':         {'type': 'str',    'label': 'Product type'},
    'unit_type':            {'type': 'str',    'label': 'Unit type'},
    'base_unit':            {'type': 'str',    'label': 'Base unit'},
    'barcode':              {'type': 'str',    'label': 'Barcode'},
    'category':             {'type': 'str',    'label': 'Category'},
    'sold_by_weight':       {'type': 'bool',   'label': 'Sold by weight'},
    'is_for_sale':          {'type': 'bool',   'label': 'For Sale'},
    'is_available_online':  {'type': 'bool',   'label': 'Online'},
    'is_prepared':          {'type': 'bool',   'label': 'Prepared'},
    'is_archived':          {'type': 'bool',   'label': 'Archived'},
    'sync_to_scale':        {'type': 'bool',   'label': 'Sync to scale'},
    'auto_price':           {'type': 'bool',   'label': 'Auto-price (markup %)'},
    'price':                {'type': 'float',  'label': 'Price (fixed)'},
    'price_per_unit':       {'type': 'float',  'label': 'Price per kg/L'},
    'margin_pct':           {'type': 'float',  'label': 'Markup %'},
    'stat_unit_size':       {'type': 'float',  'label': 'Stat unit size'},
    'scale_shelf_life':     {'type': 'int',    'label': 'Shelf life (days)'},
    'stock_qty':            {'type': 'int',    'label': 'Stock qty (simple)'},
    'scale_tare':           {'type': 'float',  'label': 'Scale tare (g)'},
    'subcategory':          {'type': 'str',    'label': 'Subcategory'},
    'product_family':       {'type': 'str',    'label': 'Product family'},
    'supplier':             {'type': 'str',    'label': 'Supplier (latest batch)'},
    'is_packaging':         {'type': 'bool',   'label': 'Packaging category'},
    'packaging_capacity':   {'type': 'int',    'label': 'Packaging capacity (units)'},
}

_STR_OPS  = ('contains', 'not_contains', 'starts', 'ends', 'eq', 'ne', 'empty', 'populated')
_NUM_OPS  = ('eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'empty', 'populated')
_BOOL_OPS = ('eq', 'empty', 'populated')


def _build_supplier_map():
    """Returns {product_id: comma-joined supplier names} from stock batches."""
    from models import Supplier
    rows = (db.session.query(StockBatch.product_id, Supplier.name)
            .join(Supplier, Supplier.id == StockBatch.supplier_id)
            .filter(StockBatch.supplier_id.isnot(None))
            .all())
    result = {}
    for pid, sname in rows:
        if pid in result:
            if sname and sname not in result[pid].split(', '):
                result[pid] += ', ' + sname
        else:
            result[pid] = sname or ''
    return result


def _get_field_val(p, field, supplier_map=None):
    if field == 'category':
        return p.category.name if p.category else None
    if field == 'subcategory':
        return p.sub_category.name if p.sub_category else None
    if field == 'product_family':
        return p.family.name if p.family else None
    if field == 'supplier':
        return (supplier_map or {}).get(p.id, '')
    if field in ('is_packaging', 'category_is_packaging'):
        return bool(p.category.is_packaging) if p.category else False
    if field == 'packaging_capacity':
        return p.packaging_capacity
    return getattr(p, field, None)


def _match_condition(p, cond, supplier_map=None):
    field    = cond.get('field', '')
    operator = cond.get('operator', 'contains')
    value    = cond.get('value', '')
    if field not in _FILTERABLE:
        return True  # unknown field = skip
    raw = _get_field_val(p, field, supplier_map)
    ftype = _FILTERABLE[field]['type']

    if operator == 'empty':
        return raw is None or raw == '' or raw == 0
    if operator == 'populated':
        return raw is not None and raw != '' and raw != 0

    if ftype == 'bool':
        raw_b = bool(raw) if raw is not None else False
        val_b = str(value).lower() in ('true', '1', 'yes')
        return raw_b == val_b if operator == 'eq' else raw_b != val_b

    if ftype in ('float', 'int'):
        try:
            raw_n = float(raw) if raw is not None else None
            val_n = float(value)
        except (TypeError, ValueError):
            return False
        if raw_n is None:
            return False
        if operator == 'eq':  return raw_n == val_n
        if operator == 'ne':  return raw_n != val_n
        if operator == 'gt':  return raw_n >  val_n
        if operator == 'gte': return raw_n >= val_n
        if operator == 'lt':  return raw_n <  val_n
        if operator == 'lte': return raw_n <= val_n
        return False

    # str
    raw_s = str(raw).lower() if raw else ''
    val_s = str(value).lower() if value else ''
    if operator == 'contains':     return val_s in raw_s
    if operator == 'not_contains': return val_s not in raw_s
    if operator == 'starts':       return raw_s.startswith(val_s)
    if operator == 'ends':         return raw_s.endswith(val_s)
    if operator == 'eq':           return raw_s == val_s
    if operator == 'ne':           return raw_s != val_s
    return True


def _filter_products(conditions, include_archived=False, exclude_ids=None):
    q = Product.query
    if not include_archived:
        q = q.filter(Product.is_archived == False)
    if exclude_ids:
        q = q.filter(~Product.id.in_(exclude_ids))
    products = q.order_by(Product.name.asc()).all()
    if not conditions:
        return products
    supplier_map = {}
    if any(c.get('field') == 'supplier' for c in conditions):
        supplier_map = _build_supplier_map()
    return [p for p in products if all(_match_condition(p, c, supplier_map) for c in conditions)]


def _serialize_match(p):
    return {
        'id':           p.id,
        'name':         p.name,
        'product_type': p.product_type,
        'price':        float(p.price) if p.price is not None else None,
        'price_per_unit': float(p.price_per_unit) if p.price_per_unit is not None else None,
        'barcode':      p.barcode,
        'product_code': p.product_code,
        'category':        p.category.name if p.category else None,
        'subcategory':     p.sub_category.name if p.sub_category else None,
        'product_family':  p.family.name if p.family else None,
        'is_for_sale':  p.is_for_sale,
        'is_available_online': p.is_available_online,
        'margin_pct':   float(p.margin_pct) if p.margin_pct is not None else None,
        'stat_unit_size': float(p.stat_unit_size) if p.stat_unit_size is not None else None,
        'sync_to_scale': p.sync_to_scale,
        'auto_price':    getattr(p, 'auto_price', True),
        'scale_shelf_life': p.scale_shelf_life,
    }


def _coerce_value(field, value):
    """Coerce a string value to the correct type for a field."""
    info = _EDITABLE.get(field, {})
    ftype = info.get('type', 'str')
    if ftype == 'bool':
        return str(value).lower() in ('true', '1', 'yes')
    if ftype == 'float':
        if value in (None, ''):
            return None
        return float(value)
    if ftype == 'int':
        if value in (None, ''):
            return None
        return int(value)
    if ftype in ('str', 'category'):
        return str(value) if value is not None else None
    return value


def _apply_action(p, action):
    """Apply a single bulk action to a product. Returns True if the product was changed."""
    op    = action.get('op', 'set')
    field = action.get('field', '')

    if field not in _EDITABLE:
        return False

    if op == 'set':
        value = action.get('value')
        info  = _EDITABLE[field]
        ftype = info['type']
        if ftype == 'category':
            if not value:
                p.category_id = None
            else:
                cat = get_or_create_category(str(value))
                p.category_id = cat.id if cat else None
        elif ftype == 'subcategory':
            if not value:
                p.sub_category_id = None
            else:
                # Scoped to the product's category — pre-validation in api_bulk_apply ensures
                # p.category_id is set and the subcategory exists within it.
                sub = SubCategory.query.filter(
                    db.func.lower(SubCategory.name) == str(value).strip().lower(),
                    SubCategory.category_id == p.category_id,
                ).first()
                p.sub_category_id = sub.id if sub else None
        elif ftype == 'family':
            if not value:
                p.product_family_id = None
            else:
                fam = ProductFamily.query.filter(
                    db.func.lower(ProductFamily.name) == str(value).strip().lower()
                ).first()
                p.product_family_id = fam.id if fam else None
        elif field == 'category_is_packaging':
            if not p.category_id:
                return False
            cat = db.session.get(Category, p.category_id)
            if not cat:
                return False
            new_val = str(value).lower() in ('true', '1', 'yes')
            if cat.is_packaging == new_val:
                return False
            cat.is_packaging = new_val
            return True
        else:
            coerced = _coerce_value(field, value)
            old = getattr(p, field, None)
            if old == coerced:
                return False
            setattr(p, field, coerced)
        return True

    if op == 'replace':
        if _EDITABLE[field]['type'] != 'str':
            return False
        find    = action.get('find', '')
        replace = action.get('replace', '')
        case_sensitive = bool(action.get('case_sensitive', False))
        old_val = getattr(p, field, None) or ''
        if case_sensitive:
            new_val = old_val.replace(find, replace)
        else:
            import re
            new_val = re.sub(re.escape(find), replace, old_val, flags=re.IGNORECASE)
        if new_val == old_val:
            return False
        setattr(p, field, new_val)
        return True

    return False


# ── Endpoints ────────────────────────────────────────────────────────────────

@bp.route('/api/products/bulk/fields', methods=['GET'])
def api_bulk_fields():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify({
        'filterable': [{'key': k, **v} for k, v in _FILTERABLE.items()],
        'editable':   [{'key': k, **v} for k, v in _EDITABLE.items()],
    })


@bp.route('/api/products/bulk/filter', methods=['POST'])
def api_bulk_filter():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data       = request.json or {}
    conditions = data.get('conditions', [])
    include_archived = bool(data.get('include_archived', False))
    page     = max(1, int(data.get('page', 1)))
    per_page = min(500, max(10, int(data.get('per_page', 50))))

    matched = _filter_products(conditions, include_archived)
    total   = len(matched)
    start   = (page - 1) * per_page
    page_items = matched[start:start + per_page]

    return jsonify({
        'total':   total,
        'page':    page,
        'pages':   max(1, (total + per_page - 1) // per_page),
        'products': [_serialize_match(p) for p in page_items],
    })


@bp.route('/api/products/bulk/preview', methods=['POST'])
def api_bulk_preview():
    """Dry-run: show what will change without persisting."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data       = request.json or {}
    conditions = data.get('conditions', [])
    actions    = data.get('actions', [])
    include_archived = bool(data.get('include_archived', False))
    exclude_ids = [int(i) for i in data.get('exclude_ids', []) if str(i).isdigit()]

    matched = _filter_products(conditions, include_archived, exclude_ids or None)
    changes = []
    for p in matched[:500]:  # cap preview at 500 products
        product_changes = []
        for action in actions:
            field = action.get('field', '')
            op    = action.get('op', 'set')
            info  = _EDITABLE.get(field, {})
            if field not in _EDITABLE:
                continue
            if op == 'set':
                ftype = info.get('type')
                if ftype == 'category':
                    old_val = p.category.name if p.category else None
                    new_val = action.get('value') or None
                elif ftype == 'subcategory':
                    old_val = p.sub_category.name if p.sub_category else None
                    new_val = action.get('value') or None
                elif ftype == 'family':
                    old_val = p.family.name if p.family else None
                    new_val = action.get('value') or None
                else:
                    old_val = getattr(p, field, None)
                    new_val = _coerce_value(field, action.get('value'))
                if old_val != new_val:
                    product_changes.append({
                        'field': field, 'label': info.get('label', field),
                        'op': 'set', 'old': old_val, 'new': new_val,
                    })
            elif op == 'replace' and info.get('type') == 'str':
                find   = action.get('find', '')
                replace = action.get('replace', '')
                case_sensitive = bool(action.get('case_sensitive', False))
                old_val = getattr(p, field, None) or ''
                if case_sensitive:
                    new_val = old_val.replace(find, replace)
                else:
                    import re
                    new_val = re.sub(re.escape(find), replace, old_val, flags=re.IGNORECASE)
                if new_val != old_val:
                    product_changes.append({
                        'field': field, 'label': info.get('label', field),
                        'op': 'replace', 'old': old_val, 'new': new_val,
                    })
        # Show derived price/markup changes in preview
        action_fields = {a.get('field') for a in actions if a.get('op') == 'set'}
        setting_price  = bool(action_fields & {'price', 'price_per_unit'})
        setting_markup = 'margin_pct' in action_fields
        if (setting_price and not setting_markup) or (setting_markup and not setting_price):
            batches = StockBatch.query.filter_by(product_id=p.id).filter(StockBatch.qty_remaining_base > 0).all()
            if batches:
                total_qty  = sum(float(b.qty_remaining_base) for b in batches)
                total_cost = sum(float(b.qty_remaining_base) * float(b.cost_per_base_unit) for b in batches)
                if total_qty > 0 and total_cost > 0:
                    wac = total_cost / total_qty
                    if setting_price and not setting_markup:
                        if p.sold_by_weight and p.price_per_unit is not None and wac > 0:
                            new_ppu = next((a.get('value') for a in actions if a.get('field') == 'price_per_unit'), None)
                            px = float(new_ppu) if new_ppu is not None else float(p.price_per_unit or 0)
                            new_m = round((px / wac - 1) * 100, 2) if wac > 0 else None
                        else:
                            new_p = next((a.get('value') for a in actions if a.get('field') == 'price'), None)
                            px = float(new_p) if new_p is not None else float(p.price or 0)
                            new_m = round((px / wac - 1) * 100, 2) if wac > 0 else None
                        if new_m is not None and new_m != float(p.margin_pct or 0):
                            product_changes.append({
                                'field': 'margin_pct', 'label': 'Markup % (derived)',
                                'op': 'set', 'old': float(p.margin_pct) if p.margin_pct else None, 'new': new_m,
                            })
                    elif setting_markup and not setting_price:
                        new_m = next((a.get('value') for a in actions if a.get('field') == 'margin_pct'), None)
                        if new_m is not None:
                            markup = float(new_m)
                            np = round(wac * (1 + markup / 100), 4)
                            field_key = 'price_per_unit' if (p.sold_by_weight and p.unit_type in ('weight', 'volume')) else 'price'
                            old_px = float(getattr(p, field_key) or 0)
                            if abs(np - old_px) > 0.005:
                                lbl = 'Price per kg/L (derived)' if field_key == 'price_per_unit' else 'Price (derived)'
                                product_changes.append({
                                    'field': field_key, 'label': lbl,
                                    'op': 'set', 'old': old_px, 'new': round(np, 2),
                                })

        if product_changes:
            changes.append({
                'id': p.id, 'name': p.name,
                'changes': product_changes,
            })

    return jsonify({
        'matched_total': len(matched),
        'affected': len(changes),
        'preview_capped': len(matched) > 500,
        'changes': changes,
    })


@bp.route('/api/products/bulk/apply', methods=['POST'])
def api_bulk_apply():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data       = request.json or {}
    conditions = data.get('conditions', [])
    actions    = data.get('actions', [])
    description = (data.get('description') or '').strip()[:200] or None
    include_archived = bool(data.get('include_archived', False))
    exclude_ids = [int(i) for i in data.get('exclude_ids', []) if str(i).isdigit()]

    if not actions:
        return jsonify({'error': 'No actions specified'}), 400

    matched = _filter_products(conditions, include_archived, exclude_ids or None)
    if not matched:
        return jsonify({'ok': True, 'affected': 0, 'run_id': None})

    user = current_user()

    # Pre-validate subcategory/family names before touching any product.
    # This prevents silent no-ops when a name is mistyped or doesn't exist.
    for action in actions:
        field = action.get('field', '')
        op    = action.get('op', 'set')
        value = (action.get('value') or '').strip()
        if op != 'set' or field not in _EDITABLE or not value:
            continue
        info = _EDITABLE[field]

        if info['type'] == 'subcategory':
            no_cat = [p for p in matched if not p.category_id]
            if no_cat:
                names = ', '.join(f'"{p.name}"' for p in no_cat[:3])
                more = f' and {len(no_cat) - 3} more' if len(no_cat) > 3 else ''
                return jsonify({'error': (
                    f'Cannot set subcategory: {len(no_cat)} product(s) have no category '
                    f'({names}{more}). Assign a category first.'
                )}), 400
            # Verify the subcategory name exists in every category represented in matched
            missing_in = []
            for cat_id in {p.category_id for p in matched}:
                found = SubCategory.query.filter(
                    db.func.lower(SubCategory.name) == value.lower(),
                    SubCategory.category_id == cat_id,
                ).first()
                if not found:
                    cat = db.session.get(Category, cat_id)
                    missing_in.append(cat.name if cat else str(cat_id))
            if missing_in:
                cats = ', '.join(f'"{c}"' for c in missing_in)
                return jsonify({'error': (
                    f'Subcategory "{value}" does not exist in {cats}. '
                    f'Create it first, or use the category filter to apply only within '
                    f'categories that already have this subcategory.'
                )}), 400

        elif info['type'] == 'family':
            fam = ProductFamily.query.filter(
                db.func.lower(ProductFamily.name) == value.lower()
            ).first()
            if fam is None:
                return jsonify({'error': (
                    f'Product family "{value}" not found. '
                    f'Create it first in the product editor.'
                )}), 400

    # Capture before-state for rollback
    before = {}
    for p in matched:
        before[str(p.id)] = {}
        for action in actions:
            field = action.get('field', '')
            if field not in _EDITABLE:
                continue
            info = _EDITABLE[field]
            if info.get('type') == 'category':
                before[str(p.id)]['category'] = p.category.name if p.category else None
                before[str(p.id)]['category_id'] = p.category_id
            elif info.get('type') == 'subcategory':
                before[str(p.id)]['subcategory_id'] = p.sub_category_id
            elif info.get('type') == 'family':
                before[str(p.id)]['product_family_id'] = p.product_family_id
            elif field == 'category_is_packaging':
                before[str(p.id)]['category_is_packaging'] = bool(p.category.is_packaging) if p.category else False
                before[str(p.id)]['_cat_id_for_pkg'] = p.category_id
            else:
                before[str(p.id)][field] = getattr(p, field, None)

    # Determine which price/markup fields are explicitly being set
    action_fields = {a.get('field') for a in actions if a.get('op') == 'set'}
    setting_price    = bool(action_fields & {'price', 'price_per_unit'})
    setting_markup   = 'margin_pct' in action_fields
    derive_markup    = setting_price and not setting_markup
    derive_price     = setting_markup and not setting_price

    changed_count = 0
    for p in matched:
        changed = False
        for action in actions:
            if _apply_action(p, action):
                changed = True

        # Derive the paired field from WAC when only one side is set
        if derive_markup or derive_price:
            batches = StockBatch.query.filter_by(product_id=p.id).filter(StockBatch.qty_remaining_base > 0).all()
            if batches:
                total_qty  = sum(float(b.qty_remaining_base) for b in batches)
                total_cost = sum(float(b.qty_remaining_base) * float(b.cost_per_base_unit) for b in batches)
                if total_qty > 0 and total_cost > 0:
                    wac = total_cost / total_qty
                    if derive_markup:
                        # price was set — compute the implied markup from WAC
                        if p.sold_by_weight and p.price_per_unit is not None and wac > 0:
                            p.margin_pct = round((float(p.price_per_unit) / wac - 1) * 100, 2)
                            changed = True
                        elif p.price is not None and wac > 0:
                            p.margin_pct = round((float(p.price) / wac - 1) * 100, 2)
                            changed = True
                    elif derive_price and p.margin_pct is not None:
                        # margin_pct was set — compute the price from WAC
                        markup = float(p.margin_pct)
                        new_price = wac * (1 + markup / 100)
                        if p.sold_by_weight and p.unit_type in ('weight', 'volume'):
                            p.price_per_unit = round(new_price, 4)
                        else:
                            p.price = round(new_price, 2)
                        changed = True

        # Always clear pending price when price is explicitly set
        if setting_price:
            p.pending_price = None
            p.pending_price_per_unit = None

        if changed:
            changed_count += 1

    run = ProductBulkEditRun(
        created_by=user.id if user else None,
        description=description,
        filter_json=json.dumps(conditions),
        action_json=json.dumps(actions),
        product_count=changed_count,
        before_json=json.dumps(before, default=str),
    )
    db.session.add(run)
    db.session.commit()

    return jsonify({'ok': True, 'affected': changed_count, 'run_id': run.id})


@bp.route('/api/products/bulk/history', methods=['GET'])
def api_bulk_history():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    runs = ProductBulkEditRun.query.order_by(ProductBulkEditRun.created_at.desc()).limit(50).all()
    from models import User
    uids = {r.created_by for r in runs if r.created_by}
    umap = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}
    return jsonify([{
        'id': r.id,
        'created_at': r.created_at.isoformat() if r.created_at else None,
        'created_by': umap.get(r.created_by, f'User {r.created_by}') if r.created_by else 'system',
        'description': r.description,
        'product_count': r.product_count,
        'filter_json': r.filter_json,
        'action_json': r.action_json,
        'rolled_back_at': r.rolled_back_at.isoformat() if r.rolled_back_at else None,
    } for r in runs])


@bp.route('/api/products/bulk/rollback/<int:run_id>', methods=['POST'])
def api_bulk_rollback(run_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    run = db.session.get(ProductBulkEditRun, run_id)
    if not run:
        return jsonify({'error': 'Run not found'}), 404
    if run.rolled_back_at:
        return jsonify({'error': 'Already rolled back'}), 409
    if not run.before_json:
        return jsonify({'error': 'No before-state available for rollback'}), 400

    before = json.loads(run.before_json)
    actions = json.loads(run.action_json)

    restored = 0
    for pid_str, fields in before.items():
        p = db.session.get(Product, int(pid_str))
        if not p:
            continue
        for field, old_val in fields.items():
            if field in ('category', '_cat_id_for_pkg'):
                continue  # handled via category_id / category_is_packaging
            if field == 'category_id':
                p.category_id = old_val
            elif field == 'subcategory_id':
                p.sub_category_id = old_val
            elif field == 'product_family_id':
                p.product_family_id = old_val
            elif field == 'category_is_packaging':
                cat_id = fields.get('_cat_id_for_pkg')
                if cat_id:
                    cat = db.session.get(Category, cat_id)
                    if cat:
                        cat.is_packaging = bool(old_val)
            else:
                setattr(p, field, old_val)
        restored += 1

    user = current_user()
    run.rolled_back_at = datetime.utcnow()
    run.rolled_back_by = user.id if user else None
    db.session.commit()

    return jsonify({'ok': True, 'restored': restored})

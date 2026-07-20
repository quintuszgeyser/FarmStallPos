import json as _json
import os
import uuid
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime

from flask import Blueprint, jsonify, request, current_app
from sqlalchemy import text, func

from helpers import (
    require_login, require_role, current_user,
    get_setting, get_stock_level, get_fifo_cost_per_unit,
    sync_sell_packages, _gen_barcode, _gen_barcode_from_code, _assign_product_code,
    _serialize_product, validate_product_code, current_user,
    consume_fifo, get_or_create_category,
)
from models import (
    db,
    Product, ProductImage, RecipeLine, Category,
    StockBatch, StockAdjustment, Purchase, Sale, ScalePluLog,
)

bp = Blueprint('products', __name__)
logger = logging.getLogger('pos')

_IMG_MAX_BYTES  = 10 * 1024 * 1024
_IMG_MAX_PIXELS = 20_000_000
_IMG_SIZES      = [(64, '_thumb'), (300, '_small'), (800, '')]
_UNIT_CONV      = {'g': 1, 'kg': 1000, 'ml': 1, 'L': 1000, 'unit': 1}


def _delete_product_image_files(img_dir, image_url):
    if not image_url:
        return
    base = image_url.rsplit('.', 1)[0]
    for _, suffix in _IMG_SIZES:
        path = os.path.join(img_dir, f'{base}{suffix}.jpg')
        if os.path.exists(path):
            os.remove(path)


def _process_and_save_image(f_stream, img_dir, pid):
    from PIL import Image as _PIL, ImageOps, ImageEnhance
    uid  = uuid.uuid4().hex[:8]
    base = f'{pid}_{uid}'
    try:
        img = _PIL.open(f_stream)
        img = ImageOps.exif_transpose(img).convert('RGB')
    except Exception:
        raise ValueError('Could not read image - file may be corrupt or unsupported')
    if img.width * img.height > _IMG_MAX_PIXELS:
        raise ValueError('Image resolution too large - please resize to under 4500×4500px')
    os.makedirs(img_dir, exist_ok=True)
    for dim, suffix in _IMG_SIZES:
        if suffix:
            resized = ImageOps.fit(img, (dim, dim), method=_PIL.LANCZOS)
        else:
            resized = img.copy()
            resized.thumbnail((dim, dim), _PIL.LANCZOS)
        resized = ImageEnhance.Sharpness(resized).enhance(1.2)
        tmp  = os.path.join(img_dir, f'{base}{suffix}.tmp')
        dest = os.path.join(img_dir, f'{base}{suffix}.jpg')
        resized.save(tmp, 'JPEG', quality=82, optimize=True, progressive=True)
        os.replace(tmp, dest)
    return f'{base}.jpg'


def _sync_primary_image_url(pid):
    primary = ProductImage.query.filter_by(product_id=pid, is_primary=True).first()
    p = db.session.get(Product, pid)
    if p:
        p.image_url = primary.filename if primary else None


def _resolve_category_id(data):
    """Resolve a category from request data into a category_id (or None).
    Prefers an explicit 'category_id'; otherwise treats 'category' as a name
    and auto-creates it (normalised, de-duplicated case-insensitively)."""
    if data.get('category_id'):
        try:
            cat = db.session.get(Category, int(data['category_id']))
        except (TypeError, ValueError):
            cat = None
        return cat.id if cat else None
    if data.get('category'):
        cat = get_or_create_category(data['category'])
        return cat.id if cat else None
    return None


@bp.route('/api/products', methods=['GET'])
def api_products_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    products = Product.query.order_by(Product.name.asc()).all()
    include_recipe = request.args.get('full') == '1'
    # Pre-fetch all images in one query to avoid N+1 per product
    from models import ProductImage
    from collections import defaultdict
    all_images = ProductImage.query.filter(
        ProductImage.product_id.in_([p.id for p in products])
    ).order_by(ProductImage.product_id, ProductImage.display_order).all() if products else []
    image_cache = defaultdict(list)
    for img in all_images:
        image_cache[img.product_id].append({
            'id': img.id, 'filename': img.filename,
            'is_primary': img.is_primary, 'display_order': img.display_order,
        })
    return jsonify([_serialize_product(p, include_recipe=include_recipe,
                                       include_packages=include_recipe,
                                       image_cache=image_cache) for p in products])


@bp.route('/api/products/<int:pid>', methods=['GET'])
def api_product_get_one(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(_serialize_product(p, include_recipe=True, include_packages=True))


@bp.route('/api/products', methods=['POST'])
def api_products_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data         = request.json or {}
    name         = data.get('name', '').strip()
    product_type = data.get('product_type', 'stock_item')
    barcode      = (data.get('barcode') or '').strip() or None
    if not name:
        return jsonify({'error': 'name required'}), 400
    if product_type not in ('stock_item', 'recipe'):
        return jsonify({'error': 'Invalid product_type'}), 400
    if barcode and Product.query.filter_by(barcode=barcode).first():
        return jsonify({'error': 'Barcode exists'}), 409
    if Product.query.filter_by(name=name).first():
        return jsonify({'error': 'Product name exists'}), 409

    price               = data.get('price')
    stock_qty           = int(data.get('stock_qty', 0) or 0)
    unit_type           = data.get('unit_type') or None
    base_unit           = data.get('base_unit') or None
    sold_by_weight      = bool(data.get('sold_by_weight', False))
    is_for_sale         = bool(data.get('is_for_sale', True))
    is_available_online = bool(data.get('is_available_online', False))
    description         = (data.get('description') or '').strip() or None
    price_per_unit      = data.get('price_per_unit') or None
    low_stock_threshold = data.get('low_stock_threshold') or None
    package_size        = data.get('package_size') or None
    package_size_unit   = data.get('package_size_unit') or None
    package_unit        = data.get('package_unit') or None

    try:
        price               = float(price) if price is not None else None
        price_per_unit      = float(price_per_unit) if price_per_unit is not None else None
        low_stock_threshold = float(low_stock_threshold) if low_stock_threshold is not None else None
        package_size_raw    = float(package_size) if package_size is not None else None
    except Exception:
        return jsonify({'error': 'Invalid numeric field'}), 400

    if package_size_raw is not None and package_size_unit and unit_type:
        package_size = package_size_raw * _UNIT_CONV.get(package_size_unit, 1)
    else:
        package_size = package_size_raw

    if unit_type and not base_unit:
        base_unit = {'weight': 'g', 'volume': 'ml', 'count': 'unit'}.get(unit_type)

    try:
        margin_pct = float(data.get('margin_pct')) if data.get('margin_pct') is not None else None
    except Exception:
        margin_pct = None

    # Assign product_code and derive barcode
    try:
        product_code = _assign_product_code(sold_by_weight, unit_type, product_type)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Weight/volume: no stored barcode - scale generates dynamically from product_code
    # Fixed/recipe: deterministic EAN-13 from product_code (unless manually provided)
    if not barcode:
        if not sold_by_weight and unit_type != 'volume':
            barcode = _gen_barcode_from_code(product_code)

    # Scale fields - auto-enable sync for weight/volume products
    sync_to_scale = bool(data.get('sync_to_scale', sold_by_weight))
    try:
        scale_tare       = float(data['scale_tare']) if data.get('scale_tare') not in (None, '') else None
        scale_shelf_life = int(data['scale_shelf_life']) if data.get('scale_shelf_life') not in (None, '') else None
        scale_pack_qty   = None  # removed
        scale_msg1       = str(data['scale_msg1'])[:80] if data.get('scale_msg1') not in (None, '') else None
        scale_msg2       = str(data['scale_msg2'])[:80] if data.get('scale_msg2') not in (None, '') else None
    except Exception:
        return jsonify({'error': 'Invalid scale field value'}), 400
    scale_open_price = bool(data.get('scale_open_price', False))
    scale_prohibit   = bool(data.get('scale_prohibit', False))

    try:
        stat_unit_size = float(data['stat_unit_size']) if data.get('stat_unit_size') not in (None, '') else None
    except Exception:
        stat_unit_size = None

    category_id = _resolve_category_id(data)

    is_produced  = bool(data.get('is_produced', False)) if product_type == 'recipe' else False
    try:
        batch_size = Decimal(str(data.get('batch_size', 1) or 1)) if product_type == 'recipe' else Decimal('1')
    except Exception:
        batch_size = Decimal('1')
    stock_unit = (str(data.get('stock_unit') or '').strip() or None) if product_type == 'recipe' else None

    p = Product(
        name=name, barcode=barcode, stock_qty=stock_qty,
        price=price, product_type=product_type,
        unit_type=unit_type, base_unit=base_unit,
        sold_by_weight=sold_by_weight, is_for_sale=is_for_sale,
        is_available_online=is_available_online, description=description,
        is_prepared=bool(data.get('is_prepared', False)),
        price_per_unit=price_per_unit, low_stock_threshold=low_stock_threshold,
        package_size=package_size, package_size_unit=package_size_unit,
        package_unit=package_unit, margin_pct=margin_pct,
        product_code=product_code, category_id=category_id,
        sync_to_scale=sync_to_scale,
        scale_tare=scale_tare, scale_shelf_life=scale_shelf_life,
        scale_pack_qty=scale_pack_qty, scale_open_price=scale_open_price,
        scale_msg1=scale_msg1, scale_msg2=scale_msg2, scale_prohibit=scale_prohibit,
        stat_unit_size=stat_unit_size,
        is_produced=is_produced, batch_size=batch_size, stock_unit=stock_unit,
    )
    db.session.add(p)
    db.session.flush()

    for rl in data.get('recipe_lines', []):
        ing_id   = int(rl.get('ingredient_id', 0))
        qty_base = Decimal(str(rl.get('qty_base', 0)))
        if ing_id and qty_base > 0:
            db.session.add(RecipeLine(product_id=p.id, ingredient_id=ing_id, qty_base=qty_base))

    if data.get('sell_packages'):
        sync_sell_packages(p.id, data['sell_packages'])

    db.session.commit()
    return jsonify({'ok': True, 'id': p.id})


@bp.route('/api/products/update', methods=['POST'])
def api_products_update():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    pid  = data.get('id')
    p    = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404

    if 'name' in data:
        name = data['name'].strip()
        if Product.query.filter(Product.id != p.id, Product.name == name).first():
            return jsonify({'error': 'Product name exists'}), 409
        p.name = name

    if 'barcode' in data and data['barcode']:
        bc = data['barcode'].strip()
        if Product.query.filter(Product.id != p.id, Product.barcode == bc).first():
            return jsonify({'error': 'Barcode exists'}), 409
        p.barcode = bc

    if 'price' in data and data['price'] is not None:
        try: p.price = float(data['price'])
        except Exception: return jsonify({'error': 'Invalid price'}), 400

    if 'stock_qty' in data and data['stock_qty'] is not None:
        try: p.stock_qty = int(data['stock_qty'])
        except Exception: return jsonify({'error': 'Invalid stock_qty'}), 400

    for field in ('product_type', 'unit_type', 'base_unit', 'package_size_unit', 'package_unit'):
        if field in data:
            setattr(p, field, data[field] or None)

    if p.unit_type and not p.base_unit:
        p.base_unit = {'weight': 'g', 'volume': 'ml', 'count': 'unit'}.get(p.unit_type)

    for field in ('price_per_unit', 'low_stock_threshold'):
        if field in data:
            try: setattr(p, field, float(data[field]) if data[field] is not None else None)
            except Exception: return jsonify({'error': f'Invalid {field}'}), 400

    if 'package_size' in data:
        try:
            raw = float(data['package_size']) if data['package_size'] is not None else None
            if raw is not None:
                pkg_display = data.get('package_size_unit') or p.package_size_unit
                p.package_size = raw * (_UNIT_CONV.get(pkg_display, 1) if pkg_display else 1)
            else:
                p.package_size = None
        except Exception:
            return jsonify({'error': 'Invalid package_size'}), 400

    for field in ('sold_by_weight', 'is_for_sale', 'is_prepared', 'is_archived', 'is_available_online'):
        if field in data:
            setattr(p, field, bool(data[field]))

    if 'description' in data:
        p.description = (data['description'] or '').strip() or None

    if 'margin_pct' in data:
        try: p.margin_pct = float(data['margin_pct']) if data['margin_pct'] is not None else None
        except Exception: return jsonify({'error': 'Invalid margin_pct'}), 400

    if 'stat_unit_size' in data:
        try: p.stat_unit_size = float(data['stat_unit_size']) if data['stat_unit_size'] not in (None, '') else None
        except Exception: return jsonify({'error': 'Invalid stat_unit_size'}), 400

    if 'archived_reason' in data:
        p.archived_reason = data['archived_reason'] or None

    # Category - explicit id, a name (auto-created), or cleared when both blank
    if 'category_id' in data or 'category' in data:
        if data.get('category_id') or data.get('category'):
            p.category_id = _resolve_category_id(data)
        else:
            p.category_id = None

    # PLU (product_code) change - requires lifecycle management
    if 'product_code' in data and data['product_code'] is not None:
        try:
            new_code = int(data['product_code'])
        except Exception:
            return jsonify({'error': 'Invalid product_code'}), 400
        if new_code != p.product_code:
            ok, conflict_msg = validate_product_code(new_code, p.id)
            if not ok:
                return jsonify({'error': conflict_msg}), 409
            old_code = p.product_code
            # Log the PLU change for scale lifecycle tracking
            user = current_user()
            db.session.add(ScalePluLog(
                product_id=p.id,
                old_plu=old_code,
                new_plu=new_code,
                changed_by=user.id if user else None,
            ))
            # If product was synced to scale under old PLU, mark old PLU for removal
            # by creating a ghost entry that the sync service will zero-out/prohibit
            if old_code and p.scale_last_sync_status == 'ok':
                # Set scale_last_sync_status to 'plu_changed' so sync service
                # knows to clean up old PLU before sending new one
                p.scale_last_sync_status = 'plu_changed'
                p.scale_last_sync_error = f'PLU changed from {old_code} to {new_code}'
            p.product_code = new_code
            p.scale_hash = None  # force resync with new PLU

    # Scale fields
    if 'sync_to_scale' in data:
        new_sync = bool(data['sync_to_scale'])
        if new_sync != p.sync_to_scale:
            if new_sync:
                # Re-enabling sync: clear stored barcode — scale generates it dynamically
                p.barcode = None
            else:
                # Disabling sync: assign deterministic barcode from product_code if none set
                if not p.barcode:
                    p.barcode = _gen_barcode_from_code(p.product_code)
        p.sync_to_scale = new_sync
    if 'scale_open_price' in data:
        p.scale_open_price = bool(data['scale_open_price'])
    if 'scale_prohibit' in data:
        p.scale_prohibit = bool(data['scale_prohibit'])
    for sf in ('scale_tare', 'scale_shelf_life', 'scale_msg1', 'scale_msg2'):
        if sf in data:
            try:
                v = data[sf]
                if v in (None, ''):
                    setattr(p, sf, None)
                elif sf == 'scale_tare':
                    setattr(p, sf, float(v))
                elif sf == 'scale_shelf_life':
                    setattr(p, sf, int(v))
                else:  # msg1, msg2 are text
                    setattr(p, sf, str(v)[:80])
            except Exception:
                return jsonify({'error': f'Invalid {sf}'}), 400
    # Any scale field change invalidates the hash so sync service picks it up
    if any(sf in data for sf in ('sync_to_scale','scale_tare','scale_shelf_life',
                                  'scale_pack_qty','scale_open_price','scale_msg1',
                                  'scale_msg2','scale_prohibit','name','price','price_per_unit')):
        p.scale_hash = None  # force resync

    if 'recipe_lines' in data:
        RecipeLine.query.filter_by(product_id=p.id).delete()
        for rl in data['recipe_lines']:
            ing_id   = int(rl.get('ingredient_id', 0))
            qty_base = Decimal(str(rl.get('qty_base', 0)))
            if ing_id and qty_base > 0:
                db.session.add(RecipeLine(product_id=p.id, ingredient_id=ing_id, qty_base=qty_base))

    if p.product_type == 'recipe':
        if 'is_produced' in data:
            p.is_produced = bool(data['is_produced'])
        if 'batch_size' in data:
            try:
                p.batch_size = Decimal(str(data['batch_size'] or 1))
            except Exception:
                pass
        if 'stock_unit' in data:
            p.stock_unit = (str(data['stock_unit'] or '').strip() or None)

    if 'sell_packages' in data:
        sync_sell_packages(p.id, data['sell_packages'])

    db.session.commit()
    return jsonify({'ok': True})


_VALID_COST_TYPES_P = {'shipping', 'labour', 'utilities', 'packaging', 'other'}


def _parse_addl_costs_p(raw, source='produce_run'):
    if not raw:
        return []
    result = []
    for i, entry in enumerate(raw):
        label = str(entry.get('label') or '').strip()
        ctype = str(entry.get('type') or 'other').strip()
        if ctype not in _VALID_COST_TYPES_P:
            ctype = 'other'
        try:
            amount = Decimal(str(entry.get('amount', 0))).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError):
            raise ValueError(f'additional_costs[{i}].amount is invalid')
        if not label:
            raise ValueError(f'additional_costs[{i}].label is required')
        result.append({'label': label, 'type': ctype, 'amount': float(amount),
                       'source': source, 'source_id': None})
    return result


@bp.route('/api/products/<int:pid>/produce', methods=['POST'])
def api_product_produce(pid):
    """Consume raw ingredients for N batches and create a StockBatch of finished units."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = db.session.get(Product, pid, with_for_update=True)
    if not p or p.product_type != 'recipe' or not p.is_produced:
        return jsonify({'error': 'Not a batch-produced recipe'}), 400
    data = request.json or {}
    try:
        batches = Decimal(str(data.get('batches', 1) or 1))
    except Exception:
        return jsonify({'error': 'Invalid batches value'}), 400
    if batches <= 0:
        return jsonify({'error': 'batches must be > 0'}), 400

    try:
        addl_costs = _parse_addl_costs_p(data.get('additional_costs', []), source='produce_run')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    produce_uuid = str(uuid.uuid4())
    now = datetime.utcnow()
    u = current_user()
    total_ingredient_cost = Decimal('0')
    for rl in RecipeLine.query.filter_by(product_id=pid).all():
        total_ingredient_cost += consume_fifo(rl.ingredient_id, Decimal(str(rl.qty_base)) * batches, produce_uuid, now)

    units_added    = int((Decimal(str(p.batch_size)) * batches).to_integral_value())
    overhead_total = sum(Decimal(str(c['amount'])) for c in addl_costs)
    total_cost     = total_ingredient_cost + overhead_total
    cost_per_unit  = total_cost / units_added if units_added > 0 else Decimal('0')
    before_stock   = get_stock_level(pid)

    db.session.add(StockBatch(
        product_id=pid,
        qty_purchased_base=units_added,
        qty_remaining_base=units_added,
        cost_per_base_unit=cost_per_unit,
        base_cost_total=total_ingredient_cost,
        additional_costs=_json.dumps(addl_costs) if addl_costs else None,
        purchased_at=now,
        user_id=u.id if u else None,
        produce_ref=produce_uuid,
        produce_cost=total_ingredient_cost,
    ))
    db.session.add(StockAdjustment(
        product_id=pid,
        adjustment_type='produce',
        qty_change_base=units_added,
        system_qty_before=Decimal(str(before_stock)),
        cost_written_off=total_ingredient_cost,
        base_unit=p.base_unit,
        reason=f'Batch produce: {int(batches)} batch(es)',
        adjusted_at=now,
        user_id=u.id if u else None,
    ))

    # Update last_overhead_costs for pre-population next time
    if addl_costs:
        p.last_overhead_costs = _json.dumps([
            {'label': c['label'], 'type': c['type'], 'amount': c['amount']}
            for c in addl_costs
        ])

    db.session.commit()
    new_stock = get_stock_level(pid)
    return jsonify({'ok': True, 'units_added': units_added, 'new_stock': new_stock, 'cost': float(total_ingredient_cost)})


@bp.route('/api/products/<int:pid>/image', methods=['POST'])
def api_product_image_upload(pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404
    f = request.files.get('image')
    if not f or not f.filename:
        return jsonify({'error': 'No file uploaded'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ('jpg', 'jpeg', 'png', 'webp'):
        return jsonify({'error': 'Only JPG, PNG or WebP allowed'}), 422
    f.seek(0, 2); fsize = f.tell(); f.seek(0)
    if fsize > _IMG_MAX_BYTES:
        return jsonify({'error': 'File too large - max 10MB'}), 422
    img_dir = os.path.join(current_app.static_folder, 'product_images')
    try:
        filename = _process_and_save_image(f.stream, img_dir, pid)
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    old_primary  = ProductImage.query.filter_by(product_id=pid, is_primary=True).first()
    old_filename = old_primary.filename if old_primary else None
    ProductImage.query.filter_by(product_id=pid).delete()
    db.session.add(ProductImage(product_id=pid, filename=filename, is_primary=True, display_order=0))
    _sync_primary_image_url(pid)
    db.session.commit()
    if old_filename and old_filename != filename:
        try: _delete_product_image_files(img_dir, old_filename)
        except Exception: pass
    return jsonify({'ok': True, 'image_url': filename})


@bp.route('/api/products/<int:pid>/image', methods=['DELETE'])
def api_product_image_delete(pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404
    img_dir   = os.path.join(current_app.static_folder, 'product_images')
    imgs      = ProductImage.query.filter_by(product_id=pid).all()
    filenames = [i.filename for i in imgs]
    ProductImage.query.filter_by(product_id=pid).delete()
    p.image_url = None
    db.session.commit()
    for fn in filenames:
        try: _delete_product_image_files(img_dir, fn)
        except Exception: pass
    return jsonify({'ok': True})


@bp.route('/api/products/<int:pid>/images', methods=['POST'])
def api_product_images_upload(pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Product not found'}), 404
    files = request.files.getlist('images[]')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400
    img_dir        = os.path.join(current_app.static_folder, 'product_images')
    existing_count = ProductImage.query.filter_by(product_id=pid).count()
    results, errors = [], []
    for f in files:
        if not f or not f.filename:
            continue
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        if ext not in ('jpg', 'jpeg', 'png', 'webp'):
            errors.append({'file': f.filename, 'error': 'Only JPG, PNG or WebP allowed'}); continue
        f.seek(0, 2); fsize = f.tell(); f.seek(0)
        if fsize > _IMG_MAX_BYTES:
            errors.append({'file': f.filename, 'error': 'File too large - max 10MB'}); continue
        try:
            filename = _process_and_save_image(f.stream, img_dir, pid)
        except ValueError as e:
            errors.append({'file': f.filename, 'error': str(e)}); continue
        is_first  = (existing_count == 0 and len(results) == 0)
        max_order = db.session.query(db.func.max(ProductImage.display_order)).filter_by(product_id=pid).scalar() or -1
        img = ProductImage(product_id=pid, filename=filename, is_primary=is_first, display_order=max_order + 1)
        db.session.add(img)
        db.session.flush()
        results.append({'id': img.id, 'filename': filename, 'is_primary': img.is_primary, 'display_order': img.display_order})
    _sync_primary_image_url(pid)
    db.session.commit()
    return jsonify({'images': results, 'errors': errors}), (201 if results else 422)


@bp.route('/api/products/<int:pid>/images/<int:img_id>', methods=['DELETE'])
def api_product_image_delete_one(pid, img_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    img = ProductImage.query.filter_by(id=img_id, product_id=pid).first()
    if not img:
        return jsonify({'error': 'Image not found'}), 404
    was_primary        = img.is_primary
    filename_to_delete = img.filename
    db.session.delete(img)
    db.session.flush()
    remaining = ProductImage.query.filter_by(product_id=pid).order_by(ProductImage.display_order).all()
    for i, r in enumerate(remaining):
        r.display_order = i
    if was_primary and remaining:
        for r in remaining: r.is_primary = False
        remaining[0].is_primary = True
    _sync_primary_image_url(pid)
    db.session.commit()
    try:
        _delete_product_image_files(os.path.join(current_app.static_folder, 'product_images'), filename_to_delete)
    except Exception as e:
        logger.warning('Could not delete image files for %s: %s', filename_to_delete, e)
    return jsonify({'ok': True})


@bp.route('/api/products/<int:pid>/images/<int:img_id>/primary', methods=['POST'])
def api_product_image_set_primary(pid, img_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    img = ProductImage.query.filter_by(id=img_id, product_id=pid).first()
    if not img:
        return jsonify({'error': 'Image not found'}), 404
    ProductImage.query.filter_by(product_id=pid).update({'is_primary': False})
    img.is_primary = True
    _sync_primary_image_url(pid)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/products/<int:pid>/images/reorder', methods=['POST'])
def api_product_images_reorder(pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or []
    if not isinstance(data, list):
        return jsonify({'error': 'Expected list of {id, display_order}'}), 422
    product_img_ids = {img.id for img in ProductImage.query.filter_by(product_id=pid).all()}
    req_ids    = [item.get('id') for item in data]
    req_orders = [item.get('display_order') for item in data]
    if len(set(req_ids)) != len(req_ids):
        return jsonify({'error': 'Duplicate image IDs in request'}), 422
    if not all(iid in product_img_ids for iid in req_ids):
        return jsonify({'error': 'One or more image IDs do not belong to this product'}), 422
    if sorted(req_orders) != list(range(len(data))):
        return jsonify({'error': 'display_order must be sequential starting from 0'}), 422
    for item in data:
        ProductImage.query.filter_by(id=item['id'], product_id=pid).update({'display_order': item['display_order']})
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/products/<int:pid>/archive', methods=['POST'])
def api_product_archive(pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    p    = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    replacements   = data.get('replacements', {})
    affected_recipes = []
    for rl in RecipeLine.query.filter_by(ingredient_id=pid).all():
        recipe = db.session.get(Product, rl.product_id)
        if recipe and not recipe.is_archived:
            affected_recipes.append(recipe)
    for recipe in affected_recipes:
        rep = replacements.get(str(recipe.id))
        if rep == 'remove':
            rl = RecipeLine.query.filter_by(product_id=recipe.id, ingredient_id=pid).first()
            if rl: db.session.delete(rl)
        elif rep:
            rl = RecipeLine.query.filter_by(product_id=recipe.id, ingredient_id=pid).first()
            if rl:
                if isinstance(rep, dict):
                    rl.ingredient_id = int(rep['ingredient_id'])
                    if rep.get('qty_base') is not None:
                        rl.qty_base = Decimal(str(rep['qty_base']))
                else:
                    rl.ingredient_id = int(rep)
        else:
            recipe.is_archived = True; recipe.archived_reason = 'cascade'
    p.is_archived = True; p.archived_reason = data.get('reason') or None
    if data.get('stock_action') == 'writeoff' and p.product_type == 'stock_item':
        stock_level = sum(
            Decimal(str(b.qty_remaining_base))
            for b in StockBatch.query.filter_by(product_id=pid).filter(StockBatch.qty_remaining_base > 0).all()
        )
        if stock_level > 0:
            u = current_user(); now = datetime.utcnow()
            consume_fifo(pid, stock_level, f'archive-wo-{uuid.uuid4()}', now)
            db.session.add(StockAdjustment(
                product_id=pid, adjustment_type='writeoff',
                qty_change_base=-stock_level, system_qty_before=stock_level,
                cost_written_off=Decimal('0'), base_unit=p.base_unit, reason='Product archived',
                adjusted_at=now, user_id=u.id if u else None,
            ))
    db.session.commit()
    cascaded = [r.id for r in affected_recipes if r.is_archived and r.archived_reason == 'cascade']
    return jsonify({'ok': True, 'cascaded_recipe_ids': cascaded})


@bp.route('/api/products/<int:pid>/restore', methods=['POST'])
def api_product_restore(pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    p    = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    p.is_archived = False; p.archived_reason = None
    for rid in data.get('restore_recipes', []):
        recipe = db.session.get(Product, int(rid))
        if not recipe or recipe.archived_reason != 'cascade': continue
        lines = RecipeLine.query.filter_by(product_id=recipe.id).all()
        if all((db.session.get(Product, rl.ingredient_id) or Product(is_archived=True)).is_archived == False for rl in lines):
            recipe.is_archived = False; recipe.archived_reason = None
    restorable = []
    for rl in RecipeLine.query.filter_by(ingredient_id=pid).all():
        recipe = db.session.get(Product, rl.product_id)
        if recipe and recipe.is_archived and recipe.archived_reason == 'cascade':
            lines = RecipeLine.query.filter_by(product_id=recipe.id).all()
            if all(
                (db.session.get(Product, l.ingredient_id) or Product(is_archived=True)).is_archived == False
                or l.ingredient_id == pid for l in lines
            ):
                restorable.append({'id': recipe.id, 'name': recipe.name})
    db.session.commit()
    return jsonify({'ok': True, 'restorable_recipes': restorable})


@bp.route('/api/products/<int:pid>/archive/preview', methods=['GET'])
def api_product_archive_preview(pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    affected = []
    for rl in RecipeLine.query.filter_by(ingredient_id=pid).all():
        recipe = db.session.get(Product, rl.product_id)
        if recipe and not recipe.is_archived:
            candidates = Product.query.filter(Product.product_type == 'stock_item', Product.is_archived == False, Product.id != pid).order_by(Product.name.asc()).all()
            affected.append({
                'recipe_id': recipe.id, 'recipe_name': recipe.name,
                'current_qty_base': float(rl.qty_base),
                'current_base_unit': p.base_unit or 'g',
                'current_unit_type': p.unit_type or 'weight',
                'replacements': [{'id': c.id, 'name': c.name, 'unit_type': c.unit_type, 'base_unit': c.base_unit, 'package_size': float(c.package_size) if c.package_size else None, 'package_unit': c.package_unit} for c in candidates],
            })
    stock_level = get_stock_level(pid) if p.product_type == 'stock_item' else 0
    return jsonify({'affected_recipes': affected, 'stock_level': stock_level})


@bp.route('/api/products/<name>', methods=['DELETE'])
def api_products_delete(name):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = Product.query.filter_by(name=name).first()
    if not p:
        return jsonify({'error': 'Product not found'}), 404
    if Sale.query.filter_by(product_id=p.id).count() or Purchase.query.filter_by(product_id=p.id).count() or StockBatch.query.filter_by(product_id=p.id).count():
        return jsonify({'error': 'Product has historical references - disable instead of deleting.', 'hint': 'Set is_for_sale=false to hide from teller without losing history.'}), 409
    RecipeLine.query.filter_by(product_id=p.id).delete()
    RecipeLine.query.filter_by(ingredient_id=p.id).delete()
    for child in Product.query.filter_by(parent_stock_item_id=p.id).all():
        if Sale.query.filter_by(product_id=child.id).count() == 0:
            RecipeLine.query.filter_by(product_id=child.id).delete()
            db.session.delete(child)
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/products/<int:pid>/recipe_cost')
def api_recipe_cost(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    lines = RecipeLine.query.filter_by(product_id=pid).all()
    if not lines:
        return jsonify({'product_id': pid, 'recipe_cost': 0, 'lines': [], 'suggested_prices': {}})

    def _cost_recursive(product_id, multiplier=1.0, _depth=0):
        if _depth > 10: return 0.0, []
        total, lines_out = 0.0, []
        for ln in RecipeLine.query.filter_by(product_id=product_id).all():
            ing = db.session.get(Product, ln.ingredient_id)
            scaled = float(ln.qty_base) * multiplier
            if ing and ing.product_type == 'recipe':
                per_unit = float(ing.batch_size or 1) if ing.is_produced else 1.0
                sub_cost, sub_lines = _cost_recursive(ing.id, scaled / per_unit, _depth + 1)
                total += sub_cost
                lines_out.append({'ingredient_id': ing.id, 'ingredient_name': ing.name, 'base_unit': None, 'qty_base': scaled, 'cost_per_unit': 0, 'line_cost': round(sub_cost, 4), 'is_sub_recipe': True, 'sub_lines': sub_lines})
            else:
                cost_per = get_fifo_cost_per_unit(ln.ingredient_id) if ing else 0
                line_cost = scaled * cost_per
                total += line_cost
                lines_out.append({'ingredient_id': ln.ingredient_id, 'ingredient_name': ing.name if ing else None, 'base_unit': ing.base_unit if ing else None, 'qty_base': scaled, 'cost_per_unit': cost_per, 'line_cost': round(line_cost, 4), 'is_sub_recipe': False})
        return total, lines_out

    total_cost, result_lines = _cost_recursive(pid)
    markup = float(get_setting('markup_percent', 40) or 40)
    suggested = {f'{pct}%': round(total_cost / (1 - pct / 100), 2) for pct in [30, 40, 50, 60] if pct < 100}
    return jsonify({'product_id': pid, 'recipe_cost': round(total_cost, 4), 'lines': result_lines, 'suggested_prices': suggested, 'default_markup': markup})


@bp.route('/api/products/<int:pid>/fifo_price')
def api_fifo_price(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    try:
        markup_param = request.args.get('markup')
        markup = Decimal(str(markup_param)) if markup_param else Decimal(str(get_setting('markup_percent', 40) or 40))
    except Exception:
        return jsonify({'error': 'Invalid markup'}), 400

    lines_detail = []
    avg_cost     = Decimal('0')

    def _batch_wavg(product_id):
        batches   = StockBatch.query.filter_by(product_id=product_id).filter(StockBatch.qty_remaining_base > 0).all()
        total_qty = sum(Decimal(str(b.qty_remaining_base)) for b in batches)
        if total_qty <= 0: return Decimal('0'), Decimal('0')
        return sum(Decimal(str(b.qty_remaining_base)) * Decimal(str(b.cost_per_base_unit)) for b in batches) / total_qty, total_qty

    def _recipe_cost(product_id, qty=Decimal('1'), _depth=0):
        if _depth > 10: return Decimal('0'), []
        total, detail = Decimal('0'), []
        for rl in RecipeLine.query.filter_by(product_id=product_id).all():
            ing    = db.session.get(Product, rl.ingredient_id)
            scaled = Decimal(str(rl.qty_base)) * qty
            if not ing: continue
            if ing.product_type == 'recipe':
                sub_cost, sub_detail = _recipe_cost(ing.id, scaled, _depth + 1)
                total += sub_cost
                detail.append({'label': ing.name, 'qty_per_sale': float(scaled), 'base_unit': None, 'avg_cost_per_unit': 0, 'line_cost': float(sub_cost), 'is_sub_recipe': True})
            else:
                avg_per_unit, _ = _batch_wavg(ing.id)
                line_cost = avg_per_unit * scaled
                total += line_cost
                detail.append({'label': ing.name, 'qty_per_sale': float(scaled), 'base_unit': ing.base_unit, 'avg_cost_per_unit': float(avg_per_unit), 'line_cost': float(line_cost)})
        return total, detail

    if p.product_type == 'stock_item':
        avg_per_unit, total_qty = _batch_wavg(pid)
        avg_cost = avg_per_unit
        lines_detail.append({'label': p.name, 'avg_cost_per_unit': float(avg_per_unit), 'base_unit': p.base_unit, 'total_qty': float(total_qty)})
    elif p.product_type == 'recipe':
        avg_cost, lines_detail = _recipe_cost(pid)

    if avg_cost <= 0:
        return jsonify({'product_id': pid, 'avg_cost': 0, 'suggested_price': 0, 'markup_pct': float(markup), 'lines': lines_detail, 'warning': 'No stock found - receive stock first'})

    suggested   = avg_cost * (1 + markup / 100)
    suggestions = {f'{pct}%': round(float(avg_cost * (1 + Decimal(pct) / 100)), 2) for pct in [20, 30, 40, 50, 60, 70, 100, 150, 200]}
    return jsonify({'product_id': pid, 'avg_cost': round(float(avg_cost), 4), 'suggested_price': round(float(suggested), 2), 'markup_pct': float(markup), 'lines': lines_detail, 'suggestions': suggestions})


@bp.route('/api/products/<int:pid>/suggested_price')
def api_suggested_price(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    rows      = Purchase.query.filter_by(product_id=pid).all()
    total_qty = sum(r.qty_added for r in rows)
    wac = (sum(r.qty_added * r.purchase_price for r in rows) / float(total_qty)) if total_qty > 0 else float(p.price or 0)
    try:
        markup_param = request.args.get('markup')
        markup = float(markup_param) if markup_param else float(get_setting('markup_percent', 20) or 20)
    except Exception:
        markup = 20.0
    return jsonify({'product_id': pid, 'wac': round(wac, 4), 'markup_percent': markup, 'suggested_price': round(wac * (1 + markup / 100.0), 2)})


@bp.route('/api/products/<int:pid>/substitutions', methods=['GET'])
def api_product_substitutions(pid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    p = db.session.get(Product, pid)
    if not p or p.product_type != 'recipe':
        return jsonify({'error': 'Not a recipe product'}), 404
    import json as _json
    lines = RecipeLine.query.filter_by(product_id=pid).all()
    default_ingredients = []
    for rl in lines:
        ing = db.session.get(Product, rl.ingredient_id)
        if not ing: continue
        default_ingredients.append({'ingredient_id': rl.ingredient_id, 'ingredient_name': ing.name, 'qty_base': float(rl.qty_base), 'unit_type': ing.unit_type, 'base_unit': ing.base_unit})
    alternatives = [{'id': a.id, 'name': a.name, 'unit_type': a.unit_type, 'base_unit': a.base_unit} for a in Product.query.filter_by(product_type='stock_item', is_archived=False).order_by(Product.name.asc()).all()]
    history = {}
    try:
        rows = db.session.execute(text("SELECT sub_log FROM sales WHERE product_id = :pid AND sub_log IS NOT NULL LIMIT 500"), {'pid': pid}).fetchall()
        for row in rows:
            try:
                log = _json.loads(row[0])
                for ing_id_str, rep_id in log.items():
                    ing_id = int(ing_id_str)
                    history.setdefault(ing_id, {})
                    history[ing_id][rep_id] = history[ing_id].get(rep_id, 0) + 1
            except Exception:
                pass
    except Exception:
        pass
    ranked = {ing['ingredient_id']: sorted(history.get(ing['ingredient_id'], {}).keys(), key=lambda k: history.get(ing['ingredient_id'], {}).get(k, 0), reverse=True) for ing in default_ingredients}
    return jsonify({'default_ingredients': default_ingredients, 'alternatives': alternatives, 'history_ranked': ranked})

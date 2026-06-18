"""
Scale management API — POS is single source of truth.

Scale is a downstream cache. This blueprint provides:
- GET  /api/scale/status          — sync status, reachability, pending products
- POST /api/scale/preview         — show what would be sent (dry run)
- POST /api/scale/send-pending    — send only products with changed hash
- POST /api/scale/full-resync     — force send all sync_to_scale products
- POST /api/scale/test-connection — ping the scale TCP port
- GET  /api/scale/sync-runs       — recent sync run history

The scale is NEVER allowed to overwrite POS data.
"""
import hashlib
import json
import socket
import logging
from decimal import Decimal
from datetime import datetime

from flask import Blueprint, jsonify, request

from helpers import require_role, get_setting
from models import db, Product, ScaleSyncRun, ScaleSnapshot

logger = logging.getLogger('scale_bp')
bp = Blueprint('scale', __name__)

MAX_NAME_LEN = 20


def _get_scale_config():
    return {
        'ip':   get_setting('scale_ip', '10.0.0.103'),
        'port': int(get_setting('scale_port', '7061')),
    }


def _scale_reachable(ip, port, timeout=5) -> bool:
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _price_cents(p) -> int:
    from decimal import Decimal, ROUND_HALF_UP
    if p.sold_by_weight:
        ppu = Decimal(str(p.price_per_unit))
        return int((ppu * Decimal('100000')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    else:
        price = Decimal(str(p.price))
        return int((price * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _compute_hash(p) -> str:
    parts = [
        str(p.product_code or ''),
        (p.name or '').strip().upper()[:MAX_NAME_LEN],
        str(_price_cents(p)),
        str(1 if p.sold_by_weight else 0),
        str(p.scale_tare or 0),
        str(p.scale_shelf_life or 0),
        str(p.scale_pack_qty or 0),
        str(1 if p.scale_open_price else 0),
        str(p.scale_msg1 or 0),
        str(p.scale_msg2 or 0),
        str(1 if p.scale_prohibit else 0),
    ]
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()


def _validate_product(p) -> str | None:
    """Return error string if product is not ready for scale sync, else None."""
    if not p.product_code:
        return "Missing product_code"
    if p.product_code <= 0 or p.product_code > 99999:
        return f"product_code {p.product_code} out of range (1-99999)"
    if p.sold_by_weight:
        if not p.price_per_unit or p.price_per_unit <= 0:
            return "Missing price_per_unit"
    else:
        if not p.price or p.price <= 0:
            return "Missing price"
    if not (p.name or '').strip():
        return "Empty name"
    return None


def _build_product_status(p) -> dict:
    err = _validate_product(p)
    current_hash = _compute_hash(p) if not err else None
    in_sync = (not err and p.scale_hash == current_hash and p.scale_last_sync_status == 'ok')
    return {
        'id':           p.id,
        'name':         p.name,
        'product_code': p.product_code,
        'sync_to_scale': p.sync_to_scale,
        'sold_by_weight': p.sold_by_weight,
        'price':         float(p.price) if p.price else None,
        'price_per_unit': float(p.price_per_unit) if p.price_per_unit else None,
        'scale_tare':    float(p.scale_tare) if p.scale_tare else 0,
        'scale_shelf_life': p.scale_shelf_life or 0,
        'scale_pack_qty': p.scale_pack_qty or 0,
        'scale_open_price': p.scale_open_price,
        'scale_prohibit': p.scale_prohibit,
        'scale_msg1':    p.scale_msg1 or 0,
        'scale_msg2':    p.scale_msg2 or 0,
        'last_synced_at':    p.scale_last_synced_at.isoformat() if p.scale_last_synced_at else None,
        'last_sync_status':  p.scale_last_sync_status,
        'last_sync_error':   p.scale_last_sync_error,
        'in_sync':       in_sync,
        'validation_error': err,
        'pending_change': not in_sync and not err,
    }


@bp.route('/api/scale/status')
def api_scale_status():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    cfg = _get_scale_config()
    reachable = _scale_reachable(cfg['ip'], cfg['port'])

    products = Product.query.filter_by(sync_to_scale=True, is_archived=False).order_by(Product.product_code).all()
    statuses = [_build_product_status(p) for p in products]

    pending   = [s for s in statuses if s['pending_change']]
    in_sync   = [s for s in statuses if s['in_sync']]
    errors    = [s for s in statuses if s['validation_error']]

    last_run = ScaleSyncRun.query.order_by(ScaleSyncRun.id.desc()).first()

    return jsonify({
        'scale_ip':         cfg['ip'],
        'scale_port':       cfg['port'],
        'scale_reachable':  reachable,
        'products_total':   len(statuses),
        'products_in_sync': len(in_sync),
        'products_pending': len(pending),
        'products_error':   len(errors),
        'last_run': {
            'id':          last_run.id,
            'started_at':  last_run.started_at.isoformat(),
            'status':      last_run.status,
            'products_sent': last_run.products_sent,
            'products_failed': last_run.products_failed,
            'orphans_detected': last_run.orphans_detected,
        } if last_run else None,
        'products': statuses,
    })


@bp.route('/api/scale/preview', methods=['POST'])
def api_scale_preview():
    """Show what would be sent/deleted without touching the scale."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    products = Product.query.filter_by(sync_to_scale=True, is_archived=False).order_by(Product.product_code).all()

    will_send   = []
    will_skip   = []
    will_delete = []
    will_error  = []

    for p in products:
        err = _validate_product(p)
        if err:
            will_error.append({'id': p.id, 'name': p.name, 'product_code': p.product_code, 'error': err})
            continue
        current_hash = _compute_hash(p)
        if p.scale_hash == current_hash and p.scale_last_sync_status == 'ok':
            will_skip.append({'id': p.id, 'name': p.name, 'product_code': p.product_code})
        else:
            will_send.append({
                'id': p.id, 'name': p.name, 'product_code': p.product_code,
                'reason': 'new' if not p.scale_last_sync_status else 'changed',
            })

    # Orphans: products that were synced but sync_to_scale is now false or archived
    synced_ok = Product.query.filter(
        Product.scale_last_sync_status == 'ok',
        Product.product_code.isnot(None),
    ).all()
    active_codes = {p.product_code for p in products}
    for p in synced_ok:
        if p.product_code not in active_codes:
            will_delete.append({'id': p.id, 'name': p.name, 'product_code': p.product_code})

    return jsonify({
        'will_send':   will_send,
        'will_skip':   will_skip,
        'will_delete': will_delete,
        'will_error':  will_error,
    })


@bp.route('/api/scale/test-connection', methods=['POST'])
def api_scale_test_connection():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    cfg = _get_scale_config()
    reachable = _scale_reachable(cfg['ip'], cfg['port'])
    return jsonify({'reachable': reachable, 'ip': cfg['ip'], 'port': cfg['port']})


@bp.route('/api/scale/sync-runs')
def api_scale_sync_runs():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    runs = ScaleSyncRun.query.order_by(ScaleSyncRun.id.desc()).limit(20).all()
    return jsonify([{
        'id':             r.id,
        'started_at':     r.started_at.isoformat(),
        'completed_at':   r.completed_at.isoformat() if r.completed_at else None,
        'run_type':       r.run_type,
        'status':         r.status,
        'products_sent':  r.products_sent,
        'products_failed': r.products_failed,
        'orphans_detected': r.orphans_detected,
        'orphans_removed': r.orphans_removed,
        'error_message':  r.error_message,
    } for r in runs])


@bp.route('/api/scale/force-resync', methods=['POST'])
def api_scale_force_resync():
    """Mark all sync_to_scale products as needing resync by clearing their hash."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    count = Product.query.filter_by(sync_to_scale=True).update({'scale_hash': None})
    db.session.commit()
    return jsonify({'ok': True, 'products_marked': count})

"""
Scale management API — POS is single source of truth.

Scale is a downstream cache. This blueprint provides:
- GET  /api/scale/status                    — sync status, reachability, pending products
- POST /api/scale/preview                   — show what would be sent (dry run)
- POST /api/scale/test-connection           — ping the scale TCP port
- GET  /api/scale/sync-runs                 — recent sync run history
- POST /api/scale/products/<id>/sync        — force single product resync
- POST /api/scale/force-resync              — mark all products for resync
- GET  /api/scale/contents                  — read all PLUs currently on the scale
- POST /api/scale/delete-plu                — delete a PLU from the scale
- GET  /api/scale/keyboard                  — get keyboard preset layout
- POST /api/scale/keyboard                  — save keyboard preset layout
- GET  /api/scale/adverts                   — get advertisement messages
- POST /api/scale/adverts                   — save advertisement messages
"""
import hashlib
import json
import os
import socket
import logging
import struct
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from helpers import require_role, get_setting
from models import db, Product, ScaleSyncRun, ScaleSnapshot, ScaleKeyboardPreset, ScaleAdvertMessage

SYNC_SOURCE_FILE = Path(os.environ.get('SCALE_DATA_DIR', '/scale_data')) / 'sync_source.json'


def _read_sync_source() -> str:
    try:
        data = json.loads(SYNC_SOURCE_FILE.read_text())
        src = data.get('source', 'prod')
        if src in ('prod', 'qa', 'none'):
            return src
    except Exception:
        pass
    return 'prod'


def _write_sync_source(source: str):
    SYNC_SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SYNC_SOURCE_FILE.write_text(json.dumps({'source': source}))

logger = logging.getLogger('scale_bp')
bp = Blueprint('scale', __name__)

MAX_NAME_LEN = 20
SCALE_TIMEOUT = 10


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
    if p.sold_by_weight:
        ppu = Decimal(str(p.price_per_unit))
        return int((ppu * Decimal('100000')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    else:
        price = Decimal(str(p.price))
        return int((price * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _compute_hash(p) -> str:
    """Must match compute_scale_hash() in scale_sync/plu_formatter.py exactly."""
    parts = [
        str(p.product_code or ''),
        (p.name or '').strip().upper()[:MAX_NAME_LEN],
        str(_price_cents(p)),
        str(1 if p.sold_by_weight else 0),
        str(float(p.scale_tare) if p.scale_tare is not None else 0),
        str(p.scale_shelf_life or 0),
        str(1 if p.scale_open_price else 0),
        str((p.scale_msg1 or '').strip()[:20]),
        str((p.scale_msg2 or '').strip()[:20]),
        str(1 if p.scale_prohibit else 0),
    ]
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()


def _validate_product(p) -> str | None:
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
        'id':            p.id,
        'name':          p.name,
        'product_code':  p.product_code,
        'sync_to_scale': p.sync_to_scale,
        'sold_by_weight': p.sold_by_weight,
        'price':         float(p.price) if p.price else None,
        'price_per_unit': float(p.price_per_unit) if p.price_per_unit else None,
        'scale_tare':    float(p.scale_tare) if p.scale_tare else 0,
        'scale_shelf_life': p.scale_shelf_life or 0,
        'scale_open_price': p.scale_open_price,
        'scale_prohibit': p.scale_prohibit,
        'scale_msg1':    p.scale_msg1 or '',
        'scale_msg2':    p.scale_msg2 or '',
        'last_synced_at':   p.scale_last_synced_at.isoformat() if p.scale_last_synced_at else None,
        'last_sync_status': p.scale_last_sync_status,
        'last_sync_error':  p.scale_last_sync_error,
        'in_sync':          in_sync,
        'validation_error': err,
        'pending_change':   not in_sync and not err,
    }


# ---------------------------------------------------------------------------
# Low-level scale protocol helpers (outbound connect, confirmed from Wireshark)
# ---------------------------------------------------------------------------

def _num2bcd(value: int, num_digits: int) -> bytes:
    num_bytes = (num_digits + 1) // 2
    result = bytearray(num_bytes)
    pos = num_bytes - 1
    for i in range(num_digits, 0, -1):
        digit = value % 10
        value //= 10
        if (num_digits - i) % 2 == 0:
            result[pos] = digit
        else:
            result[pos] |= (digit << 4)
            pos -= 1
    return bytes(result)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("Connection closed by scale")
        buf.extend(chunk)
    return bytes(buf)


def _scale_poll_status(ip, port) -> dict:
    """MsgNo 0026 — returns PLU count on scale."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SCALE_TIMEOUT)
    try:
        sock.connect((ip, port))
        hdr = bytearray(8)
        hdr[0:2] = _num2bcd(26, 4)
        struct.pack_into('>H', hdr, 6, 18)
        sock.sendall(bytes(hdr))
        raw = _recv_exact(sock, 20)
        # PLU count is at bytes 6-7 (confirmed from live Wireshark capture 2026-06-23)
        plu_count = struct.unpack_from('>H', raw, 6)[0]
        ack = bytearray(8)
        ack[0:2] = _num2bcd(26, 4)
        struct.pack_into('>H', ack, 6, 2)
        sock.sendall(bytes(ack))
        return {'ok': True, 'plu_count': plu_count}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    finally:
        sock.close()


def _num2bcd(value: int, num_digits: int) -> bytes:
    num_bytes = (num_digits + 1) // 2
    result = bytearray(num_bytes)
    pos = num_bytes - 1
    for i in range(num_digits, 0, -1):
        digit = value % 10
        value //= 10
        if (num_digits - i) % 2 == 0:
            result[pos] = digit
        else:
            result[pos] |= (digit << 4)
            pos -= 1
    return bytes(result)






def _scale_delete_plu(ip, port, plu_no: int) -> dict:
    """
    Delete a PLU from the scale by sending a prohibited/zeroed MsgNo 1001 record.
    The scale doesn't reliably support true deletion, so we overwrite with a
    prohibited blank record instead (same approach as scale_sync orphan removal).
    """
    # Build a minimal zeroed-out prohibited record
    f = [''] * 87
    for idx in [2,3,5,7,10,11,12,17,18,23,24,28,29,30,31,35,42,46,47,48,
                50,53,54,57,58,59,60,66,71,73,75,76,78,85,86]:
        f[idx] = '0'
    f[0]  = str(plu_no)
    f[1]  = str(plu_no)
    f[49] = f'"\x0d\x0dREMOVED\x0d\x0d"'
    f[50] = '1'
    f[51] = '0'
    f[52] = '0'
    f[56] = '1'
    f[61] = '0'
    f[67] = '0'
    f[69] = '0'
    f[70] = str(plu_no)
    record = (','.join(f) + ',').encode('utf-8')

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SCALE_TIMEOUT)
    try:
        sock.connect((ip, port))
        # Header
        hdr = bytearray(8)
        hdr[0:2] = _num2bcd(1001, 4)
        struct.pack_into('>H', hdr, 6, 1)
        sock.sendall(bytes(hdr))
        # Subheader + record
        sub = bytearray(8)
        sub[0:2] = _num2bcd(1001, 4)
        sub[2] = 1  # is_first
        struct.pack_into('>H', sub, 6, len(record))
        sock.sendall(bytes(sub))
        sock.sendall(record)
        # Read response
        raw = _recv_exact(sock, 20)
        updated = struct.unpack_from('>H', raw, 12)[0]
        return {'ok': True, 'updated': updated}
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Status & control
# ---------------------------------------------------------------------------

@bp.route('/api/scale/status')
def api_scale_status():
    if not require_role('admin', 'teller'):
        return jsonify({'error': 'Forbidden'}), 403

    cfg = _get_scale_config()
    reachable = _scale_reachable(cfg['ip'], cfg['port'])

    products = Product.query.filter_by(sync_to_scale=True, is_archived=False).order_by(Product.product_code).all()
    statuses = [_build_product_status(p) for p in products]

    pending = [s for s in statuses if s['pending_change']]
    in_sync = [s for s in statuses if s['in_sync']]
    errors  = [s for s in statuses if s['validation_error']]

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
            'id':               last_run.id,
            'started_at':       last_run.started_at.isoformat(),
            'status':           last_run.status,
            'products_sent':    last_run.products_sent,
            'products_failed':  last_run.products_failed,
            'orphans_detected': last_run.orphans_detected,
        } if last_run else None,
        'products': statuses,
    })


@bp.route('/api/scale/preview', methods=['POST'])
def api_scale_preview():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    products   = Product.query.filter_by(sync_to_scale=True, is_archived=False).order_by(Product.product_code).all()
    will_send  = []
    will_skip  = []
    will_delete = []
    will_error = []

    for p in products:
        err = _validate_product(p)
        if err:
            will_error.append({'id': p.id, 'name': p.name, 'product_code': p.product_code, 'error': err})
            continue
        current_hash = _compute_hash(p)
        if p.scale_hash == current_hash and p.scale_last_sync_status == 'ok':
            will_skip.append({'id': p.id, 'name': p.name, 'product_code': p.product_code})
        else:
            will_send.append({'id': p.id, 'name': p.name, 'product_code': p.product_code,
                               'reason': 'new' if not p.scale_last_sync_status else 'changed'})

    synced_ok = Product.query.filter(
        Product.scale_last_sync_status == 'ok',
        Product.product_code.isnot(None),
    ).all()
    active_codes = {p.product_code for p in products}
    for p in synced_ok:
        if p.product_code not in active_codes:
            will_delete.append({'id': p.id, 'name': p.name, 'product_code': p.product_code})

    return jsonify({'will_send': will_send, 'will_skip': will_skip,
                    'will_delete': will_delete, 'will_error': will_error})


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
        'id':               r.id,
        'started_at':       r.started_at.isoformat(),
        'completed_at':     r.completed_at.isoformat() if r.completed_at else None,
        'run_type':         r.run_type,
        'status':           r.status,
        'products_sent':    r.products_sent,
        'products_failed':  r.products_failed,
        'orphans_detected': r.orphans_detected,
        'orphans_removed':  r.orphans_removed,
        'error_message':    r.error_message,
    } for r in runs])


@bp.route('/api/scale/products/<int:product_id>/sync', methods=['POST'])
def api_scale_product_sync(product_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    p = Product.query.get_or_404(product_id)
    if not p.sync_to_scale:
        return jsonify({'error': 'Product not marked for scale sync'}), 400
    p.scale_hash = None
    db.session.commit()
    return jsonify({'ok': True, 'product_code': p.product_code, 'name': p.name})


@bp.route('/api/scale/force-resync', methods=['POST'])
def api_scale_force_resync():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    count = Product.query.filter_by(sync_to_scale=True).update({'scale_hash': None})
    db.session.commit()
    return jsonify({'ok': True, 'products_marked': count})


@bp.route('/api/scale/sync-source', methods=['GET'])
def api_scale_sync_source_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify({'source': _read_sync_source()})


@bp.route('/api/scale/sync-source', methods=['POST'])
def api_scale_sync_source_set():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    source = data.get('source')
    if source not in ('prod', 'qa', 'none'):
        return jsonify({'error': 'source must be prod, qa, or none'}), 400
    try:
        _write_sync_source(source)
        return jsonify({'ok': True, 'source': source})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Scale contents — read what's on the scale, delete individual PLUs
# ---------------------------------------------------------------------------

@bp.route('/api/scale/contents')
def api_scale_contents():
    """
    Returns what's on the scale by cross-referencing scale_last_sync_status='ok'
    products with POS data. Also shows the live PLU count from the scale.
    """
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    cfg = _get_scale_config()
    reachable = _scale_reachable(cfg['ip'], cfg['port'])

    plu_count = None
    if reachable:
        result = _scale_poll_status(cfg['ip'], cfg['port'])
        if result['ok']:
            plu_count = result['plu_count']

    # All products with sync_to_scale=TRUE or previously synced
    all_scale_products = Product.query.filter(
        Product.product_code.isnot(None),
        db.or_(
            Product.sync_to_scale == True,
            Product.scale_last_sync_status.in_(['ok', 'removed', 'error']),
        )
    ).order_by(Product.product_code).all()

    def fmt(p):
        status = p.scale_last_sync_status or 'pending'
        if p.is_archived or not p.sync_to_scale:
            status = 'orphan' if p.scale_last_sync_status == 'ok' else 'disabled'
        return {
            'id':            p.id,
            'product_code':  p.product_code,
            'name':          p.name,
            'sold_by_weight': p.sold_by_weight,
            'price':         float(p.price) if p.price else None,
            'price_per_unit': float(p.price_per_unit) if p.price_per_unit else None,
            'scale_tare':    float(p.scale_tare) if p.scale_tare else 0,
            'sync_status':   status,
            'last_synced_at': p.scale_last_synced_at.isoformat() if p.scale_last_synced_at else None,
            'sync_to_scale': p.sync_to_scale,
            'is_archived':   p.is_archived,
        }

    on_scale_count = len([p for p in all_scale_products if p.scale_last_sync_status == 'ok'])

    return jsonify({
        'scale_reachable':   reachable,
        'plu_count_on_scale': plu_count,
        'plu_count_tracked': on_scale_count,
        'plus': [fmt(p) for p in all_scale_products],
    })


@bp.route('/api/scale/delete-plu', methods=['POST'])
def api_scale_delete_plu():
    """Delete (prohibit/zero-out) a PLU on the scale."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json() or {}
    plu_no = data.get('plu_no')
    product_id = data.get('product_id')

    if not plu_no:
        return jsonify({'error': 'plu_no required'}), 400

    cfg = _get_scale_config()
    if not _scale_reachable(cfg['ip'], cfg['port']):
        return jsonify({'error': 'Scale not reachable'}), 503

    result = _scale_delete_plu(cfg['ip'], cfg['port'], int(plu_no))
    if not result['ok']:
        return jsonify({'error': result['error']}), 500

    # Update DB status if we know which product this is
    if product_id:
        p = Product.query.get(product_id)
        if p:
            p.scale_last_sync_status = 'removed'
            p.scale_hash = None
            db.session.commit()

    return jsonify({'ok': True, 'plu_no': plu_no, 'updated': result.get('updated', 0)})




# ---------------------------------------------------------------------------
# Keyboard presets
# ---------------------------------------------------------------------------

@bp.route('/api/scale/keyboard')
def api_scale_keyboard_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    presets = ScaleKeyboardPreset.query.order_by(ScaleKeyboardPreset.key_id).all()
    # Build lookup by key_id
    preset_map = {p.key_id: p for p in presets}

    # Build full 170-slot grid
    slots = []
    for key_id in range(1, 171):
        p = preset_map.get(key_id)
        product = None
        if p and p.plu_no:
            prod = Product.query.get(p.plu_no)
            if prod:
                product = {'id': prod.id, 'name': prod.name, 'product_code': prod.product_code}
        slots.append({
            'key_id':     key_id,
            'plu_no':     p.plu_no if p else None,
            'label':      p.label if p else None,
            'product':    product,
        })

    return jsonify({'slots': slots})


@bp.route('/api/scale/keyboard', methods=['POST'])
def api_scale_keyboard_save():
    """Save keyboard preset layout. Accepts list of {key_id, plu_no, label}."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json() or {}
    slots = data.get('slots', [])

    for slot in slots:
        key_id = slot.get('key_id')
        if not key_id or key_id < 1 or key_id > 170:
            continue
        plu_no = slot.get('plu_no') or None
        label  = (slot.get('label') or '')[:20] or None

        existing = ScaleKeyboardPreset.query.filter_by(key_id=key_id).first()
        if existing:
            existing.plu_no = plu_no
            existing.label  = label
        else:
            db.session.add(ScaleKeyboardPreset(key_id=key_id, plu_no=plu_no, label=label))

    db.session.commit()
    return jsonify({'ok': True, 'saved': len(slots)})


# ---------------------------------------------------------------------------
# Advertisement messages
# ---------------------------------------------------------------------------

@bp.route('/api/scale/adverts')
def api_scale_adverts_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    adverts = ScaleAdvertMessage.query.order_by(ScaleAdvertMessage.slot).all()
    advert_map = {a.slot: a for a in adverts}

    slots = []
    for slot in range(1, 44):
        a = advert_map.get(slot)
        slots.append({
            'slot':       slot,
            'display_no': a.display_no if a else 2,
            'text':       a.text if a else '',
            'enabled':    a.enabled if a else False,
        })

    return jsonify({'slots': slots})


@bp.route('/api/scale/adverts', methods=['POST'])
def api_scale_adverts_save():
    """Save advertisement messages. Accepts list of {slot, text, enabled, display_no}."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    data = request.get_json() or {}
    slots = data.get('slots', [])

    for slot_data in slots:
        slot = slot_data.get('slot')
        if not slot or slot < 1 or slot > 43:
            continue
        text       = (slot_data.get('text') or '')[:100]
        enabled    = bool(slot_data.get('enabled', False))
        display_no = int(slot_data.get('display_no', 2))

        existing = ScaleAdvertMessage.query.filter_by(slot=slot).first()
        if existing:
            existing.text       = text
            existing.enabled    = enabled
            existing.display_no = display_no
        else:
            db.session.add(ScaleAdvertMessage(slot=slot, text=text, enabled=enabled, display_no=display_no))

    db.session.commit()
    return jsonify({'ok': True, 'saved': len(slots)})

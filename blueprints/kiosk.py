import json

from flask import Blueprint, jsonify, request, Response

from helpers import get_setting, set_setting, require_role
from models import db

bp = Blueprint('kiosk', __name__)


def _kiosk_conn():
    api_key = get_setting('kiosk_api_key', '')
    port    = int(get_setting('kiosk_port', 2323) or 2323)
    headers = {'X-Api-Key': api_key} if api_key else {}
    return port, headers


@bp.route('/api/kiosk/tablets', methods=['GET', 'POST'])
def api_kiosk_tablets():
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    if request.method == 'GET':
        raw = get_setting('kiosk_tablets', '[]')
        try:
            tablets = json.loads(raw)
        except Exception:
            tablets = []
        return jsonify({'tablets': tablets})
    data = request.json or {}
    tablets = data.get('tablets', [])
    set_setting('kiosk_tablets', json.dumps(tablets))
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/kiosk/status/<path:tablet_ip>', methods=['GET'])
def api_kiosk_status(tablet_ip):
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    port, headers = _kiosk_conn()
    try:
        import requests as _req
        r    = _req.get(f'http://{tablet_ip}:{port}/api/status', headers=headers, timeout=4)
        body = r.json()
        return jsonify(body.get('data', body)), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@bp.route('/api/kiosk/query/<path:tablet_ip>', methods=['POST'])
def api_kiosk_query(tablet_ip):
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    data     = request.json or {}
    endpoint = data.get('endpoint', '')
    allowed_get = {
        'battery', 'brightness', 'screen', 'sensors',
        'storage', 'memory', 'wifi', 'info', 'health',
        'volume', 'autoBrightness', 'location',
    }
    if endpoint not in allowed_get:
        return jsonify({'error': f'Unknown endpoint: {endpoint}'}), 400
    port, headers = _kiosk_conn()
    try:
        import requests as _req
        r    = _req.get(f'http://{tablet_ip}:{port}/api/{endpoint}', headers=headers, timeout=5)
        body = r.json()
        return jsonify(body.get('data', body)), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@bp.route('/api/kiosk/screenshot/<path:tablet_ip>', methods=['GET'])
def api_kiosk_screenshot(tablet_ip):
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    port, headers = _kiosk_conn()
    try:
        import requests as _req
        r = _req.get(f'http://{tablet_ip}:{port}/api/screenshot', headers=headers, timeout=10, stream=True)
        return Response(r.content, content_type='image/png')
    except Exception as e:
        return jsonify({'error': str(e)}), 503


@bp.route('/api/kiosk/control/<path:tablet_ip>', methods=['POST'])
def api_kiosk_control(tablet_ip):
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    data   = request.json or {}
    action = data.get('action', '')
    allowed = {
        'screen/on', 'screen/off', 'screensaver/on', 'screensaver/off', 'wake', 'lock',
        'brightness', 'autoBrightness/enable', 'autoBrightness/disable',
        'reload', 'clearCache', 'url', 'js', 'mode',
        'volume', 'audio/play', 'audio/stop', 'audio/beep', 'tts', 'toast',
        'remote/up', 'remote/down', 'remote/left', 'remote/right',
        'remote/select', 'remote/back', 'remote/home',
        'remote/menu', 'remote/playpause', 'remote/text',
        'remote/keyboard',
        'app/launch',
        'restart-ui', 'reboot',
    }
    if action not in allowed and not any(action.startswith(a) for a in allowed):
        return jsonify({'error': f'Unknown action: {action}'}), 400
    port, headers = _kiosk_conn()
    payload      = {k: v for k, v in data.items() if k != 'action'}
    keyboard_map = payload.pop('map', None)
    try:
        import requests as _req
        url = f'http://{tablet_ip}:{port}/api/{action}'
        if keyboard_map is not None:
            r = _req.get(url, params={'map': keyboard_map}, headers=headers, timeout=5)
        else:
            r = _req.post(url, json=payload, headers=headers, timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503

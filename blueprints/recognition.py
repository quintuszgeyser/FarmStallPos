import os

from flask import Blueprint, jsonify, request

from helpers import require_role, get_setting, set_setting, audit_event, audit_policy
from models import db

bp = Blueprint('recognition', __name__)

RECOGNITION_SERVICE_URL = os.environ.get('RECOGNITION_URL', 'http://farmpos-recognition:8080')


@bp.route('/api/recognition/status', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_recognition_status():
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    try:
        import requests as _req
        r = _req.get(f'{RECOGNITION_SERVICE_URL}/status', timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@bp.route('/api/recognition/settings', methods=['GET', 'POST'])
@audit_policy('AUDITED')
def api_recognition_settings():
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    if request.method == 'GET':
        return jsonify({
            'face_threshold':        float(get_setting('face_threshold', 0.35) or 0.35),
            'link_threshold':        float(get_setting('link_threshold', 0.55) or 0.55),
            'face_quality_min':      float(get_setting('face_quality_min', 0.15) or 0.15),
            'merge_suggest_min_sim': float(get_setting('merge_suggest_min_sim', 0.75) or 0.75),
            'auto_merge_min_sim':    float(get_setting('auto_merge_min_sim', 0.95) or 0.95),
            'max_face_angles':       int(float(get_setting('max_face_angles', 24) or 24)),
            'min_angle_distance':    float(get_setting('min_angle_distance', 0.25) or 0.25),
        })
    data = request.json or {}; saved = {}
    for key, cast in [('face_threshold', float), ('link_threshold', float), ('face_quality_min', float), ('merge_suggest_min_sim', float), ('auto_merge_min_sim', float), ('max_face_angles', int), ('min_angle_distance', float)]:
        if key in data:
            try: set_setting(key, cast(data[key])); saved[key] = cast(data[key])
            except Exception: return jsonify({'error': f'Invalid {key}'}), 400
    if saved:
        # set_setting() already committed itself — audit_event() needs its own commit.
        audit_event('recognition_settings_updated', 'settings', None, after=saved)
        db.session.commit()
    return jsonify({'ok': True, 'saved': saved})


@bp.route('/api/recognition/logs', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_recognition_logs():
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    try:
        import requests as _req
        r = _req.get(f'{RECOGNITION_SERVICE_URL}/logs', params={k: request.args[k] for k in request.args}, timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@bp.route('/api/recognition/identity_events', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_recognition_identity_events():
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    try:
        import requests as _req
        r = _req.get(f'{RECOGNITION_SERVICE_URL}/identity_events', params={'n': request.args.get('n', 100)}, timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@bp.route('/api/recognition/tracks', methods=['GET'])
@audit_policy('NO_STATE_CHANGE')
def api_recognition_tracks():
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    try:
        import requests as _req
        r = _req.get(f'{RECOGNITION_SERVICE_URL}/tracks', timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@bp.route('/api/recognition/control/<action>', methods=['POST'])
@audit_policy('AUDITED')
def api_recognition_control(action):
    if not require_role('admin', 'developer'): return jsonify({'error': 'Forbidden'}), 403
    if action not in {'clear_queue', 'flush_sessions', 'clear_anon', 'sync_cache', 'requeue_clip', 'resync_customer', 'purge_customer'}:
        return jsonify({'error': 'Unknown action'}), 400
    try:
        import requests as _req
        r = _req.post(f'{RECOGNITION_SERVICE_URL}/control/{action}', json=request.json or {}, timeout=5)
        if 200 <= r.status_code < 300:
            audit_event('recognition_control_action', 'recognition_service', action,
                        after={'action': action, 'payload': request.json or {}})
            db.session.commit()
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503

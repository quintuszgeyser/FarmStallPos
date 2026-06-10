from flask import Blueprint, jsonify, request

from helpers import get_setting, set_setting, require_role

bp = Blueprint('settings', __name__)


@bp.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST' and not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if request.method == 'GET' and not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403

    if request.method == 'GET':
        return jsonify({
            'markup_percent':        float(get_setting('markup_percent', 20) or 20),
            'face_threshold':        float(get_setting('face_threshold', 0.35) or 0.35),
            'link_threshold':        float(get_setting('link_threshold', 0.55) or 0.55),
            'face_quality_min':      float(get_setting('face_quality_min', 0.15) or 0.15),
            'merge_suggest_min_sim': float(get_setting('merge_suggest_min_sim', 0.75) or 0.75),
            'auto_merge_min_sim':    float(get_setting('auto_merge_min_sim',    0.95) or 0.95),
            'max_face_angles':       int(float(get_setting('max_face_angles',   24) or 24)),
            'min_angle_distance':    float(get_setting('min_angle_distance',    0.25) or 0.25),
            'kiosk_api_key':             str(get_setting('kiosk_api_key', '') or ''),
            'kiosk_port':                int(get_setting('kiosk_port', 2323) or 2323),
            'kiosk_inactivity_minutes':  int(get_setting('kiosk_inactivity_minutes', 0) or 0),
            'kiosk_url':                 str(get_setting('kiosk_url', '') or ''),
        })

    data  = request.json or {}
    saved = {}
    for key, cast in [
        ('markup_percent', float), ('face_threshold', float),
        ('link_threshold', float), ('face_quality_min', float),
        ('merge_suggest_min_sim', float), ('auto_merge_min_sim', float),
        ('max_face_angles', int), ('min_angle_distance', float),
        ('kiosk_api_key', str), ('kiosk_port', int),
        ('kiosk_inactivity_minutes', int), ('kiosk_url', str),
    ]:
        if key in data:
            try:
                set_setting(key, cast(data[key]))
                saved[key] = cast(data[key])
            except Exception:
                return jsonify({'error': f'Invalid {key}'}), 400
    return jsonify({'ok': True, 'saved': saved})

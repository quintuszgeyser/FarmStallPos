import os
import re

from flask import Blueprint, jsonify, request

from helpers import get_setting, set_setting, require_role

bp = Blueprint('settings', __name__)

# Runtime branding keys (see White-Label Branding Plan). Validated server-side before
# store so a malicious colour/font value can never reach the <style> block (XSS).
# branding_logo_file is set only via the upload endpoint. The rest are editable here.
# ONE colour per surface (primary) - all shades are derived in CSS, so no secondary/
# border/background keys. branding_store_name overrides the display name everywhere.
_BRANDING_KEYS = (
    'branding_store_name', 'branding_logo_file', 'branding_primary', 'branding_bg',
    'branding_font',
    'branding_invoice_legal', 'branding_invoice_subtitle', 'branding_invoice_footer',
    'web_branding_primary', 'web_branding_font',
)
_COLOUR_KEYS = {'branding_primary', 'branding_bg', 'web_branding_primary'}
_FONT_KEYS   = {'branding_font', 'web_branding_font'}
_HEX_RE = re.compile(r'^#[0-9a-fA-F]{3,8}$')
_SAFE_FONTS = {
    'system-ui', 'sans-serif', 'serif', 'monospace', 'Arial', 'Helvetica',
    'Verdana', 'Tahoma', 'Georgia', 'Times New Roman', 'Courier New', 'Nunito',
}
# Max stored length per key (independent of the 2000-char DB column).
_BRANDING_MAXLEN = {
    'branding_invoice_footer': 500, 'branding_invoice_legal': 100,
    'branding_invoice_subtitle': 100, 'branding_store_name': 80,
    'branding_font': 80, 'web_branding_font': 80,
}

def _validate_branding(key, raw):
    """Return (value, None) if acceptable, else (None, error). '' always allowed = reset."""
    v = ('' if raw is None else str(raw)).strip()
    if v == '':
        return '', None
    if len(v) > _BRANDING_MAXLEN.get(key, 200):
        return None, f'{key} too long'
    if key in _COLOUR_KEYS:
        if not _HEX_RE.match(v):
            return None, f'{key} must be a hex colour like #927f57'
    elif key in _FONT_KEYS:
        if any(c in v for c in '<>{};/"\\') or v.split(',')[0].strip().strip("'\"") not in _SAFE_FONTS:
            return None, f'{key} must be a known system font'
    else:
        # free text (store name, invoice legal/subtitle/footer) - forbid HTML/CSS breakers
        if any(c in v for c in '<>'):
            return None, f'{key} may not contain < or >'
    return v, None


@bp.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST' and not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if request.method == 'GET' and not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403

    if request.method == 'GET':
        return jsonify({
            'markup_percent':        float(get_setting('markup_percent', 20) or 20),
            'markup_drift_pct':      float(get_setting('markup_drift_pct', 5) or 5),
            'vat_registered':        get_setting('vat_registered', 'false') == 'true',
            'vat_number':            str(get_setting('vat_number', '') or ''),
            'vat_rate':              float(get_setting('vat_rate', 15) or 15),
            'face_threshold':        float(get_setting('face_threshold', 0.35) or 0.35),
            'link_threshold':        float(get_setting('link_threshold', 0.55) or 0.55),
            'face_quality_min':      float(get_setting('face_quality_min', 0.15) or 0.15),
            'merge_suggest_min_sim': float(get_setting('merge_suggest_min_sim', 0.75) or 0.75),
            'auto_merge_min_sim':    float(get_setting('auto_merge_min_sim',    0.95) or 0.95),
            'max_face_angles':       int(float(get_setting('max_face_angles',   24) or 24)),
            'min_angle_distance':    float(get_setting('min_angle_distance',    0.25) or 0.25),
            'kiosk_api_key':             str(get_setting('kiosk_api_key', '') or ''),
            'kiosk_port':                int(get_setting('kiosk_port', 8080) or 8080),
            'kiosk_inactivity_minutes':  int(get_setting('kiosk_inactivity_minutes', 0) or 0),
            'kiosk_url':                 str(get_setting('kiosk_url', '') or ''),
            'visit_min_gap_seconds':     int(get_setting('visit_min_gap_seconds', 180) or 180),
            'scale_ip':                  str(get_setting('scale_ip', os.environ.get('SCALE_IP', '' if os.environ.get('STORE_ID', '').strip() else '10.0.0.103')) or ''),
            'scale_port':                int(get_setting('scale_port', os.environ.get('SCALE_PORT', 7061)) or 7061),
            **{k: str(get_setting(k, '') or '') for k in _BRANDING_KEYS},
        })

    data  = request.json or {}
    saved = {}
    for key, cast in [
        ('markup_percent', float), ('markup_drift_pct', float), ('vat_rate', float),
        ('face_threshold', float),
        ('link_threshold', float), ('face_quality_min', float),
        ('merge_suggest_min_sim', float), ('auto_merge_min_sim', float),
        ('max_face_angles', int), ('min_angle_distance', float),
        ('kiosk_api_key', str), ('kiosk_port', int),
        ('kiosk_inactivity_minutes', int), ('kiosk_url', str),
        ('visit_min_gap_seconds', int),
        ('scale_ip', str), ('scale_port', int),
        ('vat_number', str),
    ]:
        if key in data:
            try:
                set_setting(key, cast(data[key]))
                saved[key] = cast(data[key])
            except Exception:
                return jsonify({'error': f'Invalid {key}'}), 400

    if 'vat_registered' in data:
        set_setting('vat_registered', 'true' if data['vat_registered'] else 'false')
        saved['vat_registered'] = bool(data['vat_registered'])

    # Branding keys - validated (never trust a colour/font into a <style> block).
    # branding_logo_file is set only via the upload endpoint, not here.
    branding_changed = False
    for key in _BRANDING_KEYS:
        if key == 'branding_logo_file' or key not in data:
            continue
        val, err = _validate_branding(key, data[key])
        if err:
            return jsonify({'error': err}), 400
        set_setting(key, val)
        saved[key] = val
        branding_changed = True
    if branding_changed:
        try:
            from app import bust_branding_cache
            bust_branding_cache()
        except Exception:
            pass
    return jsonify({'ok': True, 'saved': saved})

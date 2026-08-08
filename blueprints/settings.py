import os
import re

from flask import Blueprint, jsonify, request

from helpers import get_setting, set_setting, require_role
from models import db, CustomisationRule

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
_CONTACT_KEYS = (
    'contact_phone', 'contact_email', 'contact_location',
    'contact_facebook', 'contact_instagram', 'contact_notes',
)
_COLOUR_KEYS = {'branding_primary', 'branding_bg', 'web_branding_primary'}
_FONT_KEYS   = {'branding_font', 'web_branding_font'}
_HEX_RE = re.compile(r'^#[0-9a-fA-F]{3,8}$')
_SAFE_URL_RE = re.compile(r'^https?://[^\s<>]{3,500}$', re.IGNORECASE)
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
            **{k: str(get_setting(k, '') or '') for k in _CONTACT_KEYS},
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

    # Contact details — typed validation per field
    _CONTACT_MAXLEN = {'contact_notes': 500, 'contact_location': 300}
    for key in _CONTACT_KEYS:
        if key not in data:
            continue
        v = ('' if data[key] is None else str(data[key])).strip()
        maxlen = _CONTACT_MAXLEN.get(key, 200)
        if len(v) > maxlen:
            return jsonify({'error': f'{key} too long (max {maxlen} chars)'}), 400
        if v:
            if key == 'contact_email':
                if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$', v):
                    return jsonify({'error': 'contact_email must be a valid email address'}), 400
            elif key in ('contact_facebook', 'contact_instagram'):
                if not _SAFE_URL_RE.match(v):
                    return jsonify({'error': f'{key} must be a valid https:// or http:// URL (no spaces; no javascript:/data:/file:)'}), 400
            else:
                if any(c in v for c in '<>'):
                    return jsonify({'error': f'{key} may not contain < or >'}), 400
        set_setting(key, v)
        saved[key] = v

    return jsonify({'ok': True, 'saved': saved})


# ── Customisation Rules ────────────────────────────────────────────────────────

@bp.route('/api/customisation-rules', methods=['GET'])
def api_customisation_rules_list():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    rules = CustomisationRule.query.order_by(
        CustomisationRule.sort_order.asc(), CustomisationRule.id.asc()
    ).all()
    return jsonify([{
        'id': r.id, 'rule_type': r.rule_type,
        'from_category': r.from_category or '',
        'to_category': r.to_category,
        'price_adj': float(r.price_adj),
        'label': r.label or '',
        'active': r.active,
        'sort_order': r.sort_order,
    } for r in rules])


@bp.route('/api/customisation-rules', methods=['POST'])
def api_customisation_rules_create():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.json or {}
    if d.get('rule_type') not in ('swap', 'extra'):
        return jsonify({'error': 'rule_type must be swap or extra'}), 400
    if not d.get('to_category', '').strip():
        return jsonify({'error': 'to_category is required'}), 400
    try:
        price_adj = float(d.get('price_adj', 0))
        if price_adj < 0:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({'error': 'price_adj must be a non-negative number'}), 400
    r = CustomisationRule(
        rule_type=d['rule_type'],
        from_category=(d.get('from_category') or '').strip() or None,
        to_category=d['to_category'].strip(),
        price_adj=price_adj,
        label=(d.get('label') or '').strip() or None,
        active=bool(d.get('active', True)),
        sort_order=int(d.get('sort_order', 0)),
    )
    db.session.add(r)
    db.session.commit()
    return jsonify({'id': r.id, 'ok': True})


@bp.route('/api/customisation-rules/<int:rid>', methods=['PUT'])
def api_customisation_rules_update(rid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    r = db.session.get(CustomisationRule, rid)
    if not r:
        return jsonify({'error': 'Not found'}), 404
    d = request.json or {}
    if 'rule_type' in d:
        if d['rule_type'] not in ('swap', 'extra'):
            return jsonify({'error': 'rule_type must be swap or extra'}), 400
        r.rule_type = d['rule_type']
    if 'from_category' in d:
        r.from_category = (d['from_category'] or '').strip() or None
    if 'to_category' in d:
        if not d['to_category'].strip():
            return jsonify({'error': 'to_category is required'}), 400
        r.to_category = d['to_category'].strip()
    if 'price_adj' in d:
        try:
            pa = float(d['price_adj'])
            if pa < 0:
                raise ValueError()
            r.price_adj = pa
        except (ValueError, TypeError):
            return jsonify({'error': 'price_adj must be a non-negative number'}), 400
    if 'label' in d:
        r.label = (d['label'] or '').strip() or None
    if 'active' in d:
        r.active = bool(d['active'])
    if 'sort_order' in d:
        r.sort_order = int(d.get('sort_order', 0))
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/customisation-rules/<int:rid>', methods=['DELETE'])
def api_customisation_rules_delete(rid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    r = db.session.get(CustomisationRule, rid)
    if not r:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(r)
    db.session.commit()
    return jsonify({'ok': True})

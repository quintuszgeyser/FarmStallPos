"""Branding logo upload - secure by construction (see White-Label Branding Plan).

Threats handled:
- SVG stored-XSS: SVG is XML that can carry <script>, on*= handlers, <foreignObject>,
  external entities. We parse with defusedxml (no entity expansion) and reject anything
  outside a strict element/attribute allowlist.
- Raster payloads / polyglots: png/jpg/webp are re-encoded through Pillow, which drops
  any embedded script/metadata.
- Extension spoofing: magic-byte check must agree with the extension.
- Lost uploads: refuse (503) if the branding dir isn't a persisted mount, rather than
  silently writing into the container layer.
"""
import os
import io
import uuid

from flask import Blueprint, jsonify, request, current_app

from helpers import require_role, set_setting

bp = Blueprint('branding', __name__)

MAX_BYTES   = 2 * 1024 * 1024               # 2 MB
ALLOWED_EXT = {'svg', 'png', 'jpg', 'jpeg', 'webp'}
BRANDING_DIR = '/app/static/branding'

# Magic-byte prefixes per type (svg is text/xml, checked separately).
_MAGIC = {
    'png':  [b'\x89PNG\r\n\x1a\n'],
    'jpg':  [b'\xff\xd8\xff'],
    'jpeg': [b'\xff\xd8\xff'],
    'webp': [b'RIFF'],                       # + 'WEBP' at offset 8, checked below
}

# Strict SVG allowlist - safe presentational elements/attributes only.
_SVG_OK_TAGS = {
    'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
    'defs', 'lineargradient', 'radialgradient', 'stop', 'text', 'tspan', 'title', 'desc',
    'clippath', 'use', 'symbol', 'mask',
}
_SVG_OK_ATTRS = {
    'd', 'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'points',
    'width', 'height', 'viewbox', 'fill', 'stroke', 'stroke-width', 'stroke-linecap',
    'stroke-linejoin', 'opacity', 'fill-opacity', 'stroke-opacity', 'transform',
    'gradientunits', 'gradienttransform', 'offset', 'stop-color', 'stop-opacity',
    'class', 'id', 'style', 'preserveaspectratio', 'font-family', 'font-size',
    'text-anchor', 'clip-path', 'xmlns',
}


def _localname(tag):
    return tag.rsplit('}', 1)[-1].lower() if '}' in tag else tag.lower()


def _svg_is_safe(raw_bytes):
    """Return True only if the SVG contains solely allowlisted elements/attributes and
    no script/handlers/external refs. Uses defusedxml (blocks entity-expansion attacks)."""
    try:
        from defusedxml.ElementTree import fromstring
    except Exception:
        return False  # no safe parser -> refuse SVG
    try:
        root = fromstring(raw_bytes)
    except Exception:
        return False
    for el in root.iter():
        if _localname(el.tag) not in _SVG_OK_TAGS:
            return False
        for attr, val in el.attrib.items():
            a = _localname(attr)
            if a.startswith('on'):                    # event handlers
                return False
            if a not in _SVG_OK_ATTRS and not a.startswith('xmlns'):
                return False
            low = (val or '').lower()
            if 'javascript:' in low or 'data:text/html' in low or '<script' in low:
                return False
            # block external references (href/xlink:href to remote / entities)
            if a in ('href', 'xlink:href') and not low.startswith('#'):
                return False
    return True


@bp.route('/api/branding/logo', methods=['POST'])
def upload_logo():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    # Refuse if the branding dir isn't a real persisted location -> avoid a logo that
    # silently vanishes on the next container rebuild.
    if not os.path.isdir(BRANDING_DIR) or not os.access(BRANDING_DIR, os.W_OK):
        return jsonify({'error': 'Branding storage not available (volume not mounted). Contact support.'}), 503

    f = request.files.get('logo')
    if not f or not f.filename:
        return jsonify({'error': 'No file'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ALLOWED_EXT:
        return jsonify({'error': f'Unsupported type .{ext}. Allowed: {", ".join(sorted(ALLOWED_EXT))}'}), 400

    raw = f.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return jsonify({'error': 'File too large (max 2 MB)'}), 400
    if not raw:
        return jsonify({'error': 'Empty file'}), 400

    out_bytes = None
    if ext == 'svg':
        if not _svg_is_safe(raw):
            return jsonify({'error': 'SVG rejected: contains script, handlers, or external references.'}), 400
        out_bytes = raw
    else:
        # magic-byte check must agree with the extension
        if not any(raw.startswith(m) for m in _MAGIC.get(ext, [])):
            return jsonify({'error': 'File content does not match its extension.'}), 400
        if ext == 'webp' and raw[8:12] != b'WEBP':
            return jsonify({'error': 'Invalid WEBP file.'}), 400
        # re-encode through Pillow to strip any embedded payload
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(raw))
            im.verify()                                  # detect truncated/malformed
            im = Image.open(io.BytesIO(raw))             # reopen (verify exhausts it)
            buf = io.BytesIO()
            fmt = 'JPEG' if ext in ('jpg', 'jpeg') else ext.upper()
            if fmt == 'JPEG' and im.mode in ('RGBA', 'P'):
                im = im.convert('RGB')
            im.save(buf, format=fmt)
            out_bytes = buf.getvalue()
        except Exception:
            return jsonify({'error': 'Could not process image file.'}), 400

    # atomic tmp-then-replace; clean up tmp on any failure
    fname = f"logo_{uuid.uuid4().hex[:8]}.{ext}"
    dest = os.path.join(BRANDING_DIR, fname)
    tmp  = dest + '.tmp'
    try:
        with open(tmp, 'wb') as w:
            w.write(out_bytes)
        os.replace(tmp, dest)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'error': 'Failed to store logo'}), 500

    set_setting('branding_logo_file', fname)
    # bust the cross-worker branding cache so all workers pick up the new logo
    try:
        from app import bust_branding_cache
        bust_branding_cache()
    except Exception:
        pass
    return jsonify({'ok': True, 'logo_url': '/static/branding/' + fname})

import os
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, jsonify, send_from_directory
from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config import Config
from models import db
from migrate import run_migrations


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    _setup_logging(app)

    if not app.config.get("SMTP_HOST"):
        logging.getLogger(__name__).warning(
            "SMTP not configured - all emails will be silently skipped. "
            "Set SMTP_HOST, SMTP_USER, SMTP_PASS, FROM_EMAIL, ADMIN_EMAIL in environment."
        )

    db.init_app(app)
    JWTManager(app)
    Limiter(
        get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://"
    )

    with app.app_context():
        run_migrations(db)

    # Blueprints
    from blueprints.auth     import auth_bp
    from blueprints.cakes    import cakes_bp
    from blueprints.admin    import admin_bp
    from blueprints.farmshop import farmshop_bp
    from blueprints.invoices import invoices_bp
    from blueprints.policies import policies_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(cakes_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(farmshop_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(policies_bp)

    # ── Runtime website branding ────────────────────────────────────────────
    # Reads web_branding_* from the SHARED settings table (same DB as the POS) and
    # injects them into every template. One colour -> all shades derived in CSS via
    # color-mix(). 30s per-worker cache. Empty = the default Lady Coleen look.
    import re as _re, time as _time
    _HEXRE = _re.compile(r'^#[0-9a-fA-F]{3,8}$')
    _SAFE_FONTS = {'system-ui','sans-serif','serif','monospace','Arial','Helvetica',
                   'Verdana','Tahoma','Georgia','Times New Roman','Courier New','Nunito'}
    _wb_cache = {'data': None, 'exp': 0.0}

    def _contrast(hx):
        v = (hx or '').strip().lstrip('#')
        if len(v) == 3: v = ''.join(c*2 for c in v)
        try: r,g,b = int(v[0:2],16),int(v[2:4],16),int(v[4:6],16)
        except Exception: return '#ffffff'
        return '#1a1a1a' if (0.299*r+0.587*g+0.114*b)/255.0 > 0.6 else '#ffffff'

    @app.context_processor
    def _inject_web_branding():
        now = _time.monotonic()
        if _wb_cache['data'] is None or now >= _wb_cache['exp']:
            data = {}
            try:
                from sqlalchemy import text as _text
                rows = db.session.execute(_text(
                    "SELECT key, value FROM settings WHERE key IN "
                    "('web_branding_primary','web_branding_font','branding_logo_file')"
                )).fetchall()
                data = {k: (v or '') for k, v in rows}
            except Exception:
                data = _wb_cache['data'] or {}
            _wb_cache.update({'data': data, 'exp': now + 30.0})
        d = _wb_cache['data'] or {}
        prim = (d.get('web_branding_primary') or '').strip()
        font = (d.get('web_branding_font') or '').strip()
        logo = (d.get('branding_logo_file') or '').strip()
        # only serve a logo filename that looks safe (set by the POS upload endpoint)
        safe_logo = bool(logo) and bool(_re.match(r'^[\w.\-]+$', logo))
        return {
            'web_primary':  prim if _HEXRE.match(prim) else '',
            'web_on_primary': _contrast(prim) if _HEXRE.match(prim) else '#ffffff',
            'web_font': font if (font in _SAFE_FONTS) else '',
            'web_logo_url': ('/brand-logo/' + logo) if safe_logo else '/static/logo.svg',
        }

    # Health check - required by Docker healthcheck
    @app.route("/health")
    def health():
        return jsonify(status="ok"), 200

    @app.route("/")
    def index():
        from flask import redirect, url_for
        # Cakes hidden for now - land on the farm shop
        return redirect("/farmshop")

    # Serve product images from shared volume mounted at /app/product_images
    @app.route("/product_images/<path:filename>")
    def serve_product_image(filename):
        import re
        if not re.match(r'^[\w\-]+\.(jpg|jpeg|png|webp)$', filename, re.IGNORECASE):
            abort(404)
        img_dir = os.path.join(os.path.dirname(__file__), "product_images")
        return send_from_directory(img_dir, filename, max_age=31_536_000)

    # Serve the runtime branding logo from the shared branding volume (written by the
    # POS upload endpoint). Mount ./data/branding -> /app/brand_logos on this container.
    @app.route("/brand-logo/<path:filename>")
    def serve_brand_logo(filename):
        import re
        if not re.match(r'^[\w.\-]+\.(svg|png|jpg|jpeg|webp)$', filename, re.IGNORECASE):
            abort(404)
        logo_dir = os.path.join(os.path.dirname(__file__), "brand_logos")
        if not os.path.isdir(logo_dir):
            abort(404)
        resp = send_from_directory(logo_dir, filename, max_age=300)
        resp.headers['X-Content-Type-Options'] = 'nosniff'   # SVG safety
        return resp

    # Serve uploaded files
    @app.route("/uploads/cake_images/<path:filename>")
    def serve_cake_image(filename):
        upload_root = app.config["UPLOAD_PATH"]
        return send_from_directory(os.path.join(upload_root, "cake_images"), filename)

    @app.route("/uploads/payment_proofs/<path:filename>")
    def serve_payment_proof(filename):
        # Admin-only
        from flask import session, abort
        if not session.get("admin_id"):
            abort(403)
        upload_root = app.config["UPLOAD_PATH"]
        return send_from_directory(os.path.join(upload_root, "payment_proofs"), filename)

    # Ensure upload dirs exist
    os.makedirs(os.path.join(app.config["UPLOAD_PATH"], "cake_images"), exist_ok=True)
    os.makedirs(os.path.join(app.config["UPLOAD_PATH"], "payment_proofs"), exist_ok=True)

    return app


def _setup_logging(app):
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)

    handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=10
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    root_log = logging.getLogger()
    root_log.setLevel(logging.INFO)
    root_log.addHandler(handler)

    if app.config["APP_ENV"] != "production":
        root_log.addHandler(logging.StreamHandler())


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=app.config["PORT"], debug=(app.config["APP_ENV"] != "production"))

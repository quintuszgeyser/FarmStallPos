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
    _wb_cache    = {'data': None, 'exp': 0.0}
    _contact_cache = {'data': None, 'exp': 0.0}

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
                    "('web_branding_primary','web_branding_font','branding_logo_file',"
                    "'branding_store_name')"
                )).fetchall()
                data = {k: (v or '') for k, v in rows}
            except Exception:
                data = _wb_cache['data'] or {}
            _wb_cache.update({'data': data, 'exp': now + 30.0})
        d = _wb_cache['data'] or {}
        prim = (d.get('web_branding_primary') or '').strip()
        font = (d.get('web_branding_font') or '').strip()
        logo = (d.get('branding_logo_file') or '').strip()
        name = (d.get('branding_store_name') or '').strip()
        # only serve a logo filename that looks safe (set by the POS upload endpoint)
        safe_logo = bool(logo) and bool(_re.match(r'^[\w.\-]+$', logo))
        # Runtime store name (white-label). Empty = the historical 'Lady Coleen' literals,
        # so an un-customised box renders byte-identical. store_name = the short brand,
        # store_name_full = the longer descriptive line used in footers/titles.
        store_name = name or 'Lady Coleen'
        # Contact settings (separate 30s cache)
        now2 = _time.monotonic()
        if _contact_cache['data'] is None or now2 >= _contact_cache['exp']:
            cdata = {}
            try:
                crow = db.session.execute(_text(
                    "SELECT key, value FROM settings WHERE key IN "
                    "('contact_phone','contact_email','contact_location',"
                    "'contact_facebook','contact_instagram','contact_notes')"
                )).fetchall()
                cdata = {k: (v or '') for k, v in crow}
            except Exception:
                cdata = _contact_cache['data'] or {}
            _contact_cache.update({'data': cdata, 'exp': now2 + 30.0})
        cd = _contact_cache['data'] or {}

        return {
            'web_primary':  prim if _HEXRE.match(prim) else '',
            'web_on_primary': _contrast(prim) if _HEXRE.match(prim) else '#ffffff',
            'web_font': font if (font in _SAFE_FONTS) else '',
            'web_logo_url': ('/brand-logo/' + logo) if safe_logo else '/static/logo.svg',
            'store_name': store_name,
            'store_name_full': (store_name + ' Boutique Farm Shop') if not name else store_name,
            'contact_phone':     cd.get('contact_phone', ''),
            'contact_email':     cd.get('contact_email', ''),
            'contact_location':  cd.get('contact_location', ''),
            'contact_facebook':  cd.get('contact_facebook', ''),
            'contact_instagram': cd.get('contact_instagram', ''),
            'contact_notes':     cd.get('contact_notes', ''),
        }

    # Health check - required by Docker healthcheck
    @app.route("/health")
    def health():
        return jsonify(status="ok"), 200

    # Google Search Console ownership verification
    @app.route("/google356d111296009565.html")
    def google_site_verification():
        from flask import Response
        return Response("google-site-verification: google356d111296009565.html",
                        mimetype="text/html")

    # XML sitemap for Google indexing
    @app.route("/sitemap.xml")
    def sitemap():
        from flask import Response, request
        from sqlalchemy import text as _text
        import datetime

        base = "https://ladycoleen.co.za"
        today = datetime.date.today().isoformat()

        static_urls = [
            (base + "/farmshop",          "weekly",  "1.0"),
            (base + "/farmshop/products", "weekly",  "0.9"),
            (base + "/cakes",             "monthly", "0.7"),
            (base + "/cakes/order",       "monthly", "0.6"),
            (base + "/policies/refund",   "yearly",  "0.3"),
            (base + "/policies/privacy",  "yearly",  "0.3"),
            (base + "/policies/terms",    "yearly",  "0.3"),
            (base + "/policies/shipping", "yearly",  "0.3"),
            (base + "/policies/returns",  "yearly",  "0.3"),
        ]

        product_urls = []
        try:
            rows = db.session.execute(_text(
                "SELECT id FROM products WHERE is_archived = false AND is_for_sale = true"
            )).fetchall()
            product_urls = [
                (f"{base}/farmshop/products/{r[0]}", "weekly", "0.8")
                for r in rows
            ]
        except Exception:
            pass

        all_urls = static_urls + product_urls

        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for loc, freq, pri in all_urls:
            lines.append(
                f"  <url><loc>{loc}</loc>"
                f"<lastmod>{today}</lastmod>"
                f"<changefreq>{freq}</changefreq>"
                f"<priority>{pri}</priority></url>"
            )
        lines.append("</urlset>")

        return Response("\n".join(lines), mimetype="application/xml")

    # Google Merchant Center product feed
    @app.route("/product-feed.xml")
    def product_feed():
        from flask import Response
        from sqlalchemy import text as _text
        from services.stock import get_available_qty
        import xml.sax.saxutils as _sax

        BASE = "https://ladycoleen.co.za"

        rows = db.session.execute(_text("""
            SELECT id, name, COALESCE(description, '') AS description,
                   COALESCE(price, 0) AS price, product_type, image_url
            FROM products
            WHERE is_for_sale = true AND is_available_online = true AND is_archived = false
            ORDER BY name ASC
        """)).fetchall()

        def _avail(product_type, qty):
            if qty <= 0:
                return "out of stock"
            return "in stock"

        def _esc(s):
            return _sax.escape(str(s or ""))

        items = []
        for r in rows:
            qty = get_available_qty(db, r.id, r.product_type)
            avail = _avail(r.product_type, qty)
            img = f"{BASE}/product_images/{r.image_url}" if r.image_url else ""
            desc = r.description.strip() if r.description else r.name
            price = f"{float(r.price):.2f} ZAR"
            items.append(
                f"    <item>\n"
                f"      <g:id>{r.id}</g:id>\n"
                f"      <g:title>{_esc(r.name)}</g:title>\n"
                f"      <g:description>{_esc(desc)}</g:description>\n"
                f"      <g:link>{BASE}/farmshop/products/{r.id}</g:link>\n"
                + (f"      <g:image_link>{_esc(img)}</g:image_link>\n" if img else "")
                + f"      <g:price>{price}</g:price>\n"
                f"      <g:availability>{avail}</g:availability>\n"
                f"      <g:condition>new</g:condition>\n"
                f"      <g:brand>Lady Coleen</g:brand>\n"
                f"    </item>"
            )

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n'
            '  <channel>\n'
            '    <title>Lady Coleen Boutique Farm Shop</title>\n'
            f'    <link>{BASE}</link>\n'
            '    <description>Handmade and farm-fresh products from Lady Coleen</description>\n'
            + "\n".join(items) + "\n"
            '  </channel>\n'
            '</rss>'
        )
        return Response(xml, mimetype="application/xml")

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


# -*- coding: utf-8 -*-

import os, uuid, logging, traceback, json
from datetime import datetime, date, timedelta
from collections import defaultdict
from io import StringIO, BytesIO
from decimal import Decimal
import time as _time

from flask import Flask, jsonify, request, session, send_file, render_template, g
from flask.json.provider import DefaultJSONProvider
from sqlalchemy import text, func, Numeric
from models import (
    db,
    User, UserSession, Setting,
    Product, ProductImage, RecipeLine,
    StockBatch, StockConsumption, StockAdjustment,
    Purchase, Sale, Special, SpecialLine, Invoice, KitchenOrder,
    Customer, CustomerPlate, CustomerFace, CustomerGait,
    CustomerVisit, PlateDetection, Supplier,
    ScaleSyncRun, ScaleSnapshot, ScalePluLog, ScaleKeyboardPreset, ScaleAdvertMessage,
    ProductImportRun, DeploySchedule, TillSession,
    LabelTemplate, LabelPrintJob, LabelPrinter,
    SESSION_TIMEOUT_MINUTES, SESSION_LOGOUT_HOURS,
)
from helpers import (
    get_setting, set_setting,
    require_login, require_role, current_user,
    seed_first_admin, get_online_user_id,
    consume_fifo, reverse_fifo,
    get_stock_level, get_fifo_cost_per_unit,
    sync_sell_packages, _gen_barcode, _gen_barcode_from_code, _assign_product_code, _ean13_check, _serialize_product,
    _parse_dt,
)

APP_VERSION = os.environ.get('APP_VERSION', '1.6.0')

# Environment configuration - explicit, never guessed
APP_ENV  = os.environ.get('APP_ENV', 'prod').lower()
IS_QA    = APP_ENV == 'qa'
DB_NAME  = os.environ.get('POSTGRES_DB', os.environ.get('DATABASE_URL', 'unknown').split('/')[-1])
LOG_PREFIX = f"[{APP_ENV.upper()}][{DB_NAME}]"

# Per-store identity - set from store.yml → .env on each appliance box (multi-store).
# STORE_ID is the switch: when it is UNSET this is the original Lady Coleen box and
# every field falls back to the exact historical strings, so that box renders and
# behaves byte-for-byte as before. A provisioned store (register-store.sh) always
# sets STORE_ID and drives all branding from its own identity.
STORE_ID = os.environ.get('STORE_ID', '').strip()
if STORE_ID:
    STORE_NAME     = os.environ.get('STORE_NAME', STORE_ID).strip()
    STORE_TAGLINE  = os.environ.get('STORE_TAGLINE', '').strip()  or STORE_NAME
    STORE_LEGAL    = os.environ.get('STORE_LEGAL', '').strip()    or STORE_NAME
    STORE_SUBTITLE = os.environ.get('STORE_SUBTITLE', '').strip()
else:
    # Original Lady Coleen box - do NOT change these literals.
    STORE_NAME     = 'Lady Coleen'
    STORE_TAGLINE  = 'Lady Coleen Boutique Farmstall'
    STORE_LEGAL    = 'Lady Coleen Boutique Farm Shop'
    STORE_SUBTITLE = 'Fresh Farm Produce & Boutique Deli'

# ---------------------------------------------------------------------------
# White-label branding (DB-backed, runtime-editable). See White-Label Branding Plan.
# Values live in the settings table as branding_* keys; '' means "use the LC/env
# fallback" so an un-customised box renders byte-identical to Lady Coleen.
# ---------------------------------------------------------------------------
import re as _re, time as _t_mod

_BRANDING_KEYS = (
    'branding_store_name',                       # runtime display-name override (all UI)
    'branding_logo_file', 'branding_primary',    # ONE colour - all shades derived from it
    'branding_bg',                               # POS page background; blank = light tint of primary
    'branding_font', 'branding_invoice_legal',
    'branding_invoice_subtitle', 'branding_invoice_footer',
    # Website appearance (read by ladycoleen_web too, from the same settings table)
    'web_branding_primary', 'web_branding_font',
)

def brand_contrast(hexval):
    """Return '#ffffff' or '#1a1a1a' for readable text on a given brand colour, chosen
    by perceived luminance. So a pale-yellow brand gets dark text, a navy brand gets white."""
    v = (hexval or '').strip().lstrip('#')
    if len(v) == 3:
        v = ''.join(c * 2 for c in v)
    try:
        r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    except (ValueError, IndexError):
        return '#ffffff'
    # perceived luminance (sRGB weights); >0.6 => light colour => dark text
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return '#1a1a1a' if lum > 0.6 else '#ffffff'
_BRANDING_SENTINEL = os.path.join(os.path.dirname(__file__), 'static', 'branding', '.cache_bust')
_branding_cache = {'data': None, 'expires': 0.0, 'sentinel_mtime': 0.0}
_HEX_RE  = _re.compile(r'^#[0-9a-fA-F]{3,8}$')
# System fonts safe offline; anything else falls back to the LC font stack.
_SAFE_FONTS = {
    'system-ui', 'sans-serif', 'serif', 'monospace', 'Arial', 'Helvetica',
    'Verdana', 'Tahoma', 'Georgia', 'Times New Roman', 'Courier New', 'Nunito',
}

def css_hex(val, fallback):
    """Jinja filter + validator: only emit a value into a <style> block if it is a
    safe hex colour, else the fallback. Defense-in-depth against CSS-injection XSS."""
    v = (val or '').strip()
    return v if _HEX_RE.match(v) else fallback

def safe_font(val, fallback):
    v = (val or '').strip().strip("'\"")
    # reject any CSS/HTML metacharacters outright
    if not v or any(c in v for c in '<>{};/"\\'):
        return fallback
    # first family name must be in the safe list (allow a trailing generic)
    first = v.split(',')[0].strip().strip("'\"")
    return v if first in _SAFE_FONTS else fallback

def _branding_sentinel_mtime():
    try:
        return os.stat(_BRANDING_SENTINEL).st_mtime
    except OSError:
        return 0.0

def bust_branding_cache():
    """Called after any branding_* write. Touches the sentinel so ALL gunicorn
    workers re-read on their next request (in-process expiry only covers this worker)."""
    try:
        os.makedirs(os.path.dirname(_BRANDING_SENTINEL), exist_ok=True)
        with open(_BRANDING_SENTINEL, 'w') as _f:
            _f.write(str(_t_mod.time()))
    except OSError:
        pass
    _branding_cache['expires'] = 0.0

def get_branding():
    """Return the 8 branding values as a dict, cached per-worker with a 30s TTL and a
    filesystem-sentinel cross-worker invalidation. Never raises - falls back to {} so
    the context_processor can't 500 the whole app during a DB blip."""
    now = _t_mod.monotonic()
    smt = _branding_sentinel_mtime()
    if (_branding_cache['data'] is not None
            and now < _branding_cache['expires']
            and smt == _branding_cache['sentinel_mtime']):
        return _branding_cache['data']
    data = {}
    try:
        from models import Setting  # local import - models imported after this module region
        rows = Setting.query.filter(Setting.key.in_(_BRANDING_KEYS)).all()  # one query, not 8
        data = {r.key: (r.value or '') for r in rows}
    except Exception as _e:
        logger.warning(f"branding cache load failed, using fallbacks: {_e}")
        data = _branding_cache['data'] or {}
    _branding_cache.update({'data': data, 'expires': now + 30.0, 'sentinel_mtime': smt})
    return data


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'pos.log')
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger('pos')


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

# -----------------------------
# Strong startup migration
# -----------------------------
def strong_migrate():
    import hashlib as _hl
    from psycopg.errors import DeadlockDetected as _Deadlock
    LOCK_ID = int(_hl.sha256(b"farmpos_migration_lock").hexdigest()[:15], 16) % (2**62)

    try:
        db.create_all()  # creates missing tables; idempotent on existing DBs
    except Exception as e:
        # On upgrade from an older DB, SQLAlchemy may try to CREATE a table that
        # already exists as a partial type (e.g. audit_log) and get a
        # UniqueViolation on the pg_type catalog. The DDL migration below handles
        # all structural changes idempotently, so this is safe to skip.
        logger.warning(f"db.create_all() non-fatal: {e.__class__.__name__}: {e}")

    engine = db.engine
    engine_name = engine.dialect.name

    with engine.begin() as conn:
        if engine_name != 'sqlite':
            # TRANSACTION-level advisory lock - held for the life of THIS transaction and
            # auto-released on commit/rollback. Must be acquired inside engine.begin() (a
            # session-level pg_try_advisory_lock on a separate connection releases when that
            # connection returns to the pool, BEFORE the migration runs - letting a second
            # gunicorn worker migrate concurrently). Blocking (not _try_): a late worker
            # waits for the migration to finish, then re-runs the idempotent DDL as a no-op.
            conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({LOCK_ID})")

        if engine_name == 'sqlite':
            # ---- sales table ----
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS sales (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sale_id TEXT NOT NULL,
              date_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              product_id INTEGER NOT NULL,
              qty REAL NOT NULL,
              unit_price REAL NOT NULL
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_sale_id ON sales (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_date_time ON sales (date_time)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_product_dt ON sales (product_id, date_time)")
            existing_sales = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(sales)").fetchall()]
            for col, defn in [('voided','INTEGER NOT NULL DEFAULT 0'),('voided_by','INTEGER'),
                               ('voided_at','TIMESTAMP'),('void_reason','TEXT'),('user_id','INTEGER'),
                               ('flagged','INTEGER NOT NULL DEFAULT 0'),('flag_note','TEXT'),
                               ('flag_resolved','INTEGER NOT NULL DEFAULT 0')]:
                if col not in existing_sales:
                    conn.exec_driver_sql(f"ALTER TABLE sales ADD COLUMN {col} {defn}")

            # ---- products new columns ----
            existing_prod = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(products)").fetchall()]
            for col, defn in [
                ('product_type',         "TEXT NOT NULL DEFAULT 'stock_item'"),
                ('unit_type',            'TEXT'),
                ('base_unit',            'TEXT'),
                ('sold_by_weight',       'INTEGER NOT NULL DEFAULT 0'),
                ('is_for_sale',          'INTEGER NOT NULL DEFAULT 1'),
                ('price_per_unit',       'REAL'),
                ('low_stock_threshold',  'REAL'),
                ('package_size',         'REAL'),
                ('package_size_unit',    'TEXT'),
                ('package_unit',         'TEXT'),
                ('parent_stock_item_id', 'INTEGER'),
                ('margin_pct',  'REAL'),
                ('is_prepared', 'INTEGER NOT NULL DEFAULT 0'),
                ('is_available_online', 'INTEGER NOT NULL DEFAULT 0'),
                ('image_url',   'TEXT'),
                ('description', 'TEXT'),
                ('category_id', 'INTEGER'),
            ]:
                if col not in existing_prod:
                    conn.exec_driver_sql(f"ALTER TABLE products ADD COLUMN {col} {defn}")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_products_category_id ON products (category_id)")

            # updated_at for scale sync change detection
            if 'updated_at' not in existing_prod:
                conn.exec_driver_sql(
                    "ALTER TABLE products ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                )
                conn.exec_driver_sql("""
                    CREATE TRIGGER IF NOT EXISTS trg_products_updated_at
                    BEFORE UPDATE ON products FOR EACH ROW
                    WHEN NEW.updated_at = OLD.updated_at
                    BEGIN
                        UPDATE products SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
                    END
                """)
                conn.exec_driver_sql(
                    "UPDATE products SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
                )

            # product_images table (SQLite)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS product_images (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
              filename      TEXT NOT NULL,
              is_primary    INTEGER NOT NULL DEFAULT 0,
              display_order INTEGER NOT NULL DEFAULT 0,
              created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_product_images_product ON product_images (product_id, display_order)")

            # Migrate existing single images into product_images (idempotent)
            conn.exec_driver_sql("""
              INSERT INTO product_images (product_id, filename, is_primary, display_order)
              SELECT id, image_url, 1, 0 FROM products
              WHERE image_url IS NOT NULL
                AND id NOT IN (SELECT DISTINCT product_id FROM product_images)
            """)

            # kitchen_orders table (SQLite)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS kitchen_orders (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              sale_id      TEXT NOT NULL,
              product_id   INTEGER,
              product_name TEXT NOT NULL,
              qty          REAL NOT NULL,
              ingredients  TEXT,
              status       TEXT NOT NULL DEFAULT 'pending',
              sort_order   INTEGER NOT NULL DEFAULT 0,
              queued_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              completed_at TIMESTAMP,
              teller_id    INTEGER,
              notes        TEXT
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_sale_id   ON kitchen_orders (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_status    ON kitchen_orders (status)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_queued_at ON kitchen_orders (queued_at)")

            # ---- new tables ----
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS suppliers (
              id      INTEGER PRIMARY KEY AUTOINCREMENT,
              name    TEXT NOT NULL UNIQUE,
              contact TEXT,
              notes   TEXT
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS supplier_documents (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              supplier_id   INTEGER NOT NULL REFERENCES suppliers(id),
              filename      TEXT NOT NULL,
              original_name TEXT NOT NULL,
              uploaded_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              uploaded_by   INTEGER
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_supplier_docs_supplier ON supplier_documents (supplier_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS recipe_lines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id INTEGER NOT NULL,
              ingredient_id INTEGER NOT NULL,
              qty_base REAL NOT NULL
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_recipe_lines_product ON recipe_lines (product_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id INTEGER NOT NULL,
              qty_purchased_base REAL NOT NULL,
              qty_remaining_base REAL NOT NULL,
              cost_per_base_unit REAL NOT NULL,
              purchased_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              supplier_id INTEGER,
              user_id INTEGER
            )""")
            # Add columns if missing on existing table (SQLite)
            existing_sb = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(stock_batches)").fetchall()]
            if 'supplier_id' not in existing_sb:
                conn.exec_driver_sql("ALTER TABLE stock_batches ADD COLUMN supplier_id INTEGER")
            if 'sort_order' not in existing_sb:
                conn.exec_driver_sql("ALTER TABLE stock_batches ADD COLUMN sort_order INTEGER")
            if 'import_run_id' not in existing_sb:
                conn.exec_driver_sql("ALTER TABLE stock_batches ADD COLUMN import_run_id VARCHAR(36)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_batches_product ON stock_batches (product_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_consumption (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sale_id TEXT NOT NULL,
              ingredient_id INTEGER NOT NULL,
              batch_id INTEGER NOT NULL,
              qty_consumed_base REAL NOT NULL,
              cost_per_base_unit REAL NOT NULL,
              consumed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_consumption_sale ON stock_consumption (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_consumption_ingredient ON stock_consumption (ingredient_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_adjustments (
              id                INTEGER PRIMARY KEY AUTOINCREMENT,
              product_id        INTEGER NOT NULL,
              adjustment_type   TEXT NOT NULL,
              qty_change_base   REAL NOT NULL,
              system_qty_before REAL NOT NULL,
              cost_written_off  REAL,
              reason            TEXT NOT NULL,
              adjusted_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              user_id           INTEGER
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_adj_product ON stock_adjustments (product_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_adj_date ON stock_adjustments (adjusted_at)")

            # ---- customer identification tables (SQLite) ----
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customers (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              name        TEXT NOT NULL,
              phone       TEXT,
              email       TEXT,
              notes       TEXT,
              enrolled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              enrolled_by INTEGER,
              last_visit  TIMESTAMP,
              visit_count INTEGER NOT NULL DEFAULT 0,
              active      INTEGER NOT NULL DEFAULT 1
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_plates (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              customer_id  INTEGER NOT NULL,
              plate_number TEXT NOT NULL UNIQUE,
              enrolled_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              active       INTEGER NOT NULL DEFAULT 1
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_plates_cid ON customer_plates (customer_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_faces (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              customer_id INTEGER NOT NULL,
              embedding   BLOB NOT NULL,
              enrolled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              active      INTEGER NOT NULL DEFAULT 1
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_faces_cid ON customer_faces (customer_id)")

            # ---- customer_faces new columns ----
            conn.exec_driver_sql(
                "ALTER TABLE customer_faces ADD COLUMN IF NOT EXISTS photo BYTEA"
            )

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_gaits (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              customer_id   INTEGER NOT NULL,
              gait_features BLOB NOT NULL,
              enrolled_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              active        INTEGER NOT NULL DEFAULT 1
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_gaits_cid ON customer_gaits (customer_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_visits (
              id                INTEGER PRIMARY KEY AUTOINCREMENT,
              customer_id       INTEGER NOT NULL,
              detected_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              matched_signals   TEXT NOT NULL,
              confidence_scores TEXT,
              camera_source     TEXT,
              acknowledged      INTEGER NOT NULL DEFAULT 0
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_visits_cid ON customer_visits (customer_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_visits_dt  ON customer_visits (detected_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_visits_ack ON customer_visits (acknowledged)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS plate_detections (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              plate_number  TEXT NOT NULL,
              confidence    REAL,
              detected_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              customer_id   INTEGER,
              matched       INTEGER NOT NULL DEFAULT 0,
              snapshot_path TEXT,
              camera_source TEXT
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_plate_det_dt  ON plate_detections (detected_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_plate_det_cid ON plate_detections (customer_id)")

            # ---- Phase 1: Auto-Enrollment System (SQLite - 2026-05-12) ----
            existing_customers = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(customers)").fetchall()]
            for col, defn in [
                ('auto_enrolled',   'INTEGER DEFAULT 0'),
                ('customer_number', 'TEXT'),  # SQLite: can't add UNIQUE on ALTER, add later as index
                ('first_seen',      'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
                ('is_employee',     'INTEGER DEFAULT 0'),
            ]:
                if col not in existing_customers:
                    conn.exec_driver_sql(f"ALTER TABLE customers ADD COLUMN {col} {defn}")

            # Add unique index for customer_number (SQLite workaround)
            conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_number ON customers(customer_number) WHERE customer_number IS NOT NULL")

            existing_sales = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(sales)").fetchall()]
            if 'customer_id' not in existing_sales:
                conn.exec_driver_sql("ALTER TABLE sales ADD COLUMN customer_id INTEGER")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_physical_attributes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                height_cm INTEGER,
                hair_color TEXT,
                skin_tone TEXT,
                build TEXT,
                eye_color TEXT,
                age_range TEXT,
                gender TEXT,
                wearing_glasses INTEGER,
                facial_hair TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                camera_source TEXT,
                confidence REAL
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS visit_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                session_start TIMESTAMP NOT NULL,
                session_end TIMESTAMP,
                entry_camera TEXT,
                checkout_camera TEXT,
                dwell_seconds INTEGER,
                purchase_made INTEGER DEFAULT 0,
                sale_ids TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS detection_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                camera_source TEXT NOT NULL,
                camera_zone TEXT,
                object_type TEXT,
                detected_at TIMESTAMP NOT NULL,
                processed INTEGER DEFAULT 0,
                plate_number TEXT,
                face_embedding BLOB,
                gait_features BLOB,
                physical_attributes TEXT,
                tracked_person_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS person_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_uuid TEXT UNIQUE,
                customer_id INTEGER,
                first_seen TIMESTAMP NOT NULL,
                entry_camera TEXT,
                associated_plate TEXT,
                last_seen TIMESTAMP,
                last_camera TEXT,
                height_cm_avg INTEGER,
                hair_color_consensus TEXT,
                gender_consensus TEXT,
                best_face_embedding BLOB,
                best_gait_features BLOB,
                enrolled_as_customer INTEGER DEFAULT 0,
                session_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS till_detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                camera_source TEXT
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id_a INTEGER NOT NULL,
                customer_id_b INTEGER NOT NULL,
                score_a REAL,
                score_b REAL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved INTEGER DEFAULT 0,
                merged_into INTEGER
            )""")

        else:
            # ---- PostgreSQL ----
            _pg_try_counter = [0]
            def pg_try(sql):
                # Use a unique savepoint name per call so concurrent gunicorn workers
                # running migrations in parallel don't clobber each other's savepoints.
                _pg_try_counter[0] += 1
                sp = f"sp_{_pg_try_counter[0]}"
                try:
                    conn.exec_driver_sql(f"SAVEPOINT {sp}")
                    conn.exec_driver_sql(sql)
                    conn.exec_driver_sql(f"RELEASE SAVEPOINT {sp}")
                except Exception:
                    conn.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {sp}")

            # sales table
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS sales (
              id SERIAL PRIMARY KEY,
              sale_id TEXT NOT NULL,
              date_time TIMESTAMP NOT NULL DEFAULT NOW(),
              product_id INTEGER NOT NULL REFERENCES products(id),
              qty NUMERIC(10,4) NOT NULL,
              unit_price NUMERIC(10,2) NOT NULL,
              user_id INTEGER REFERENCES users(id),
              voided BOOLEAN NOT NULL DEFAULT FALSE,
              voided_by INTEGER REFERENCES users(id),
              voided_at TIMESTAMP,
              void_reason TEXT
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_sale_id ON sales (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_date_time ON sales (date_time)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_product_dt ON sales (product_id, date_time)")

            for col, defn in [
                ('voided',        'BOOLEAN NOT NULL DEFAULT FALSE'),
                ('voided_by',     'INTEGER'),
                ('voided_at',     'TIMESTAMP'),
                ('void_reason',   'TEXT'),
                ('user_id',       'INTEGER'),
                ('flagged',       'BOOLEAN NOT NULL DEFAULT FALSE'),
                ('flag_note',     'TEXT'),
                ('flag_resolved', 'BOOLEAN NOT NULL DEFAULT FALSE'),
            ]:
                pg_try(f"ALTER TABLE sales ADD COLUMN {col} {defn}")

            # Fix money + qty columns
            for tbl, col, typ in [
                ('products',  'price',          'NUMERIC(10,2)'),
                ('purchases', 'purchase_price', 'NUMERIC(10,2)'),
                ('sales',     'unit_price',     'NUMERIC(10,2)'),
                ('sales',     'qty',            'NUMERIC(10,4)'),
            ]:
                pg_try(f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE {typ} USING {col}::{typ}")

            # products new columns
            for col, defn in [
                ('product_type',         "VARCHAR(20) NOT NULL DEFAULT 'stock_item'"),
                ('unit_type',            'VARCHAR(10)'),
                ('base_unit',            'VARCHAR(10)'),
                ('sold_by_weight',       'BOOLEAN NOT NULL DEFAULT FALSE'),
                ('is_for_sale',          'BOOLEAN NOT NULL DEFAULT TRUE'),
                ('price_per_unit',       'NUMERIC(10,4)'),
                ('low_stock_threshold',  'NUMERIC(10,4)'),
                ('package_size',         'NUMERIC(10,4)'),
                ('package_size_unit',    'VARCHAR(10)'),
                ('package_unit',         'VARCHAR(30)'),
                ('parent_stock_item_id', 'INTEGER'),
                ('margin_pct',  'NUMERIC(5,2)'),
                ('is_prepared', 'BOOLEAN NOT NULL DEFAULT FALSE'),
                ('is_available_online', 'BOOLEAN NOT NULL DEFAULT FALSE'),
                ('image_url',   'VARCHAR(200)'),
                ('description', 'TEXT'),
            ]:
                pg_try(f"ALTER TABLE products ADD COLUMN {col} {defn}")

            # product_images table (PostgreSQL)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS product_images (
              id            SERIAL PRIMARY KEY,
              product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
              filename      VARCHAR(200) NOT NULL,
              is_primary    BOOLEAN NOT NULL DEFAULT FALSE,
              display_order INTEGER NOT NULL DEFAULT 0,
              created_at    TIMESTAMPTZ DEFAULT now()
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_product_images_product ON product_images (product_id, display_order)")

            # Migrate existing single images into product_images (idempotent)
            conn.exec_driver_sql("""
              INSERT INTO product_images (product_id, filename, is_primary, display_order)
              SELECT id, image_url, true, 0 FROM products
              WHERE image_url IS NOT NULL
                AND id NOT IN (SELECT DISTINCT product_id FROM product_images)
            """)

            # kitchen_orders table (PostgreSQL)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS kitchen_orders (
              id           SERIAL PRIMARY KEY,
              sale_id      TEXT NOT NULL,
              product_id   INTEGER REFERENCES products(id),
              product_name VARCHAR(120) NOT NULL,
              qty          NUMERIC(10,4) NOT NULL,
              ingredients  TEXT,
              status       VARCHAR(20) NOT NULL DEFAULT 'pending',
              sort_order   INTEGER NOT NULL DEFAULT 0,
              queued_at    TIMESTAMP NOT NULL DEFAULT NOW(),
              completed_at TIMESTAMP,
              teller_id    INTEGER REFERENCES users(id),
              notes        TEXT
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_sale_id   ON kitchen_orders (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_status    ON kitchen_orders (status)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_ko_queued_at ON kitchen_orders (queued_at)")

            # Allow price and barcode to be null (stock_items may not have a selling price)
            pg_try("ALTER TABLE products ALTER COLUMN price DROP NOT NULL")
            pg_try("ALTER TABLE products ALTER COLUMN barcode DROP NOT NULL")

            # FK constraints
            for constraint, tbl, col, ref_tbl, ref_col in [
                ('fk_sales_user',        'sales',    'user_id',             'users',    'id'),
                ('fk_sales_voided_by',   'sales',    'voided_by',           'users',    'id'),
                ('fk_purchases_user',    'purchases','user_id',             'users',    'id'),
                ('fk_products_parent',   'products', 'parent_stock_item_id','products', 'id'),
            ]:
                pg_try(f"ALTER TABLE {tbl} ADD CONSTRAINT {constraint} FOREIGN KEY ({col}) REFERENCES {ref_tbl}({ref_col})")

            # purchases user_id
            pg_try("ALTER TABLE purchases ADD COLUMN user_id INTEGER")
            pg_try("ALTER TABLE purchases ADD CONSTRAINT fk_purchases_user2 FOREIGN KEY (user_id) REFERENCES users(id)")

            # ---- new tables ----
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS suppliers (
              id      SERIAL PRIMARY KEY,
              name    VARCHAR(120) NOT NULL UNIQUE,
              contact VARCHAR(200),
              notes   VARCHAR(500)
            )""")

            # Add supplier_id to stock_batches
            pg_try("ALTER TABLE stock_batches ADD COLUMN supplier_id INTEGER")
            pg_try("ALTER TABLE stock_batches ADD CONSTRAINT fk_batches_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers(id)")
            pg_try("ALTER TABLE stock_batches ADD COLUMN sort_order INTEGER")
            pg_try("ALTER TABLE stock_batches ADD COLUMN import_run_id VARCHAR(36)")
            # Split contact into phone/email/website
            pg_try("ALTER TABLE suppliers ADD COLUMN phone   VARCHAR(50)")
            pg_try("ALTER TABLE suppliers ADD COLUMN email   VARCHAR(120)")
            pg_try("ALTER TABLE suppliers ADD COLUMN website VARCHAR(200)")
            pg_try("UPDATE suppliers SET phone = contact WHERE contact IS NOT NULL AND email IS NULL AND website IS NULL")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS supplier_documents (
              id            SERIAL PRIMARY KEY,
              supplier_id   INTEGER NOT NULL REFERENCES suppliers(id),
              filename      VARCHAR(200) NOT NULL,
              original_name VARCHAR(200) NOT NULL,
              uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              uploaded_by   INTEGER REFERENCES users(id)
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_supplier_docs_supplier ON supplier_documents (supplier_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS recipe_lines (
              id            SERIAL PRIMARY KEY,
              product_id    INTEGER NOT NULL REFERENCES products(id),
              ingredient_id INTEGER NOT NULL REFERENCES products(id),
              qty_base      NUMERIC(10,4) NOT NULL
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_recipe_lines_product ON recipe_lines (product_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_batches (
              id                  SERIAL PRIMARY KEY,
              product_id          INTEGER NOT NULL REFERENCES products(id),
              qty_purchased_base  NUMERIC(10,4) NOT NULL,
              qty_remaining_base  NUMERIC(10,4) NOT NULL,
              cost_per_base_unit  NUMERIC(10,6) NOT NULL,
              purchased_at        TIMESTAMP NOT NULL DEFAULT NOW(),
              user_id             INTEGER REFERENCES users(id)
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_batches_product ON stock_batches (product_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_batches_remaining ON stock_batches (product_id, qty_remaining_base)")

            # ---- Migrate 'simple' → 'stock_item' (idempotent) ----
            conn.exec_driver_sql("""
                UPDATE products
                SET product_type = 'stock_item',
                    unit_type    = COALESCE(unit_type,  'count'),
                    base_unit    = COALESCE(base_unit,  'unit')
                WHERE product_type = 'simple'
            """)
            # Create an opening stock batch for converted products that had stock_qty > 0
            conn.exec_driver_sql("""
                INSERT INTO stock_batches (product_id, qty_purchased_base, qty_remaining_base, cost_per_base_unit, purchased_at)
                SELECT id, stock_qty, stock_qty, 0, NOW()
                FROM products
                WHERE stock_qty > 0
                  AND product_type = 'stock_item'
                  AND id NOT IN (SELECT DISTINCT product_id FROM stock_batches)
            """)

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_consumption (
              id                  SERIAL PRIMARY KEY,
              sale_id             TEXT NOT NULL,
              ingredient_id       INTEGER NOT NULL REFERENCES products(id),
              batch_id            INTEGER NOT NULL REFERENCES stock_batches(id),
              qty_consumed_base   NUMERIC(10,4) NOT NULL,
              cost_per_base_unit  NUMERIC(10,6) NOT NULL,
              consumed_at         TIMESTAMP NOT NULL DEFAULT NOW()
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_consumption_sale ON stock_consumption (sale_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_consumption_ingredient ON stock_consumption (ingredient_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS stock_adjustments (
              id                SERIAL PRIMARY KEY,
              product_id        INTEGER NOT NULL REFERENCES products(id),
              adjustment_type   VARCHAR(20) NOT NULL,
              qty_change_base   NUMERIC(10,4) NOT NULL,
              system_qty_before NUMERIC(10,4) NOT NULL,
              cost_written_off  NUMERIC(10,4),
              reason            TEXT NOT NULL,
              adjusted_at       TIMESTAMP NOT NULL DEFAULT NOW(),
              user_id           INTEGER REFERENCES users(id)
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_adj_product ON stock_adjustments (product_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_stock_adj_date ON stock_adjustments (adjusted_at)")

            pg_try("ALTER TABLE products ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT FALSE")
            pg_try("ALTER TABLE products ADD COLUMN archived_reason VARCHAR(200)")

            # updated_at for scale sync change detection
            pg_try("ALTER TABLE products ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW()")
            pg_try("""
                CREATE OR REPLACE FUNCTION trg_products_set_updated_at()
                RETURNS TRIGGER AS $$
                BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
                $$ LANGUAGE plpgsql
            """)
            pg_try("DROP TRIGGER IF EXISTS trg_products_updated_at ON products")
            pg_try("""
                CREATE TRIGGER trg_products_updated_at
                BEFORE UPDATE ON products
                FOR EACH ROW EXECUTE FUNCTION trg_products_set_updated_at()
            """)
            pg_try("UPDATE products SET updated_at = NOW() WHERE updated_at IS NULL")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS user_sessions (
              id          SERIAL PRIMARY KEY,
              user_id     INTEGER NOT NULL REFERENCES users(id),
              logged_in   TIMESTAMP NOT NULL DEFAULT NOW(),
              logged_out  TIMESTAMP
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_user_sessions_user ON user_sessions (user_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_user_sessions_in ON user_sessions (logged_in)")
            pg_try("ALTER TABLE user_sessions ADD COLUMN last_active TIMESTAMP")

            # sub_log: JSON map {ingredient_id: replacement_id} for recipe substitutions
            pg_try("ALTER TABLE sales ADD COLUMN sub_log TEXT")
            # discount tracking
            pg_try("ALTER TABLE sales ADD COLUMN discount_json TEXT")
            pg_try("ALTER TABLE sales ADD COLUMN discount_by INTEGER REFERENCES users(id)")
            # tender info (ISSUE-29) - nullable/additive, safe for existing rows
            pg_try("ALTER TABLE sales ADD COLUMN payment_method VARCHAR(16)")
            pg_try("ALTER TABLE sales ADD COLUMN cash_tendered NUMERIC(10,2)")
            pg_try("ALTER TABLE sales ADD COLUMN card_amount NUMERIC(10,2)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_sales_payment_method ON sales (payment_method)")

            # till sessions for end-of-day cash-up (ISSUE-33)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS till_sessions (
              id              SERIAL PRIMARY KEY,
              opened_at       TIMESTAMP NOT NULL,
              closed_at       TIMESTAMP NOT NULL DEFAULT NOW(),
              opened_by       INTEGER REFERENCES users(id),
              closed_by       INTEGER REFERENCES users(id),
              opening_float   NUMERIC(10,2) NOT NULL DEFAULT 0,
              counted_cash    NUMERIC(10,2) NOT NULL,
              pos_cash_sales  NUMERIC(10,2) NOT NULL,
              pos_card_sales  NUMERIC(10,2) NOT NULL DEFAULT 0,
              pos_total_sales NUMERIC(10,2) NOT NULL,
              expected_cash   NUMERIC(10,2) NOT NULL,
              over_under      NUMERIC(10,2) NOT NULL,
              void_total      NUMERIC(10,2) NOT NULL DEFAULT 0,
              notes           TEXT
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_till_sessions_closed ON till_sessions (closed_at)")

            # append-only audit trail for voids/edits (ISSUE-31)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS audit_log (
              id            SERIAL PRIMARY KEY,
              created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              event_type    VARCHAR(40) NOT NULL,
              actor_user_id INTEGER REFERENCES users(id),
              target_table  VARCHAR(40),
              target_id     VARCHAR(64),
              before_json   TEXT,
              note          VARCHAR(500)
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_audit_log_created ON audit_log (created_at)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS specials (
              id            SERIAL PRIMARY KEY,
              name          VARCHAR(120) NOT NULL,
              special_price NUMERIC(10,2) NOT NULL,
              active        BOOLEAN NOT NULL DEFAULT TRUE
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS special_lines (
              id         SERIAL PRIMARY KEY,
              special_id INTEGER NOT NULL REFERENCES specials(id) ON DELETE CASCADE,
              product_id INTEGER NOT NULL REFERENCES products(id),
              qty        INTEGER NOT NULL DEFAULT 1
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_special_lines_special ON special_lines (special_id)")
            pg_try("ALTER TABLE specials ADD COLUMN schedule TEXT")

            # ---- invoices ----
            pg_try("ALTER TABLE invoices ADD COLUMN bank_details TEXT")
            pg_try("ALTER TABLE invoices ADD COLUMN sale_id VARCHAR(64)")
            pg_try("""
            CREATE TABLE invoices (
              id               SERIAL PRIMARY KEY,
              invoice_number   VARCHAR(20) UNIQUE NOT NULL,
              created_at       TIMESTAMP NOT NULL DEFAULT NOW(),
              due_date         VARCHAR(20),
              customer_name    VARCHAR(120),
              customer_phone   VARCHAR(50),
              customer_email   VARCHAR(120),
              customer_address TEXT,
              notes            TEXT,
              lines_json       TEXT NOT NULL DEFAULT '[]',
              subtotal         NUMERIC(10,2) NOT NULL DEFAULT 0,
              discount_pct     NUMERIC(5,2),
              total            NUMERIC(10,2) NOT NULL DEFAULT 0,
              status           VARCHAR(20) NOT NULL DEFAULT 'draft',
              created_by       INTEGER REFERENCES users(id)
            )""")

            # ---- customer identification tables (PostgreSQL) ----
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customers (
              id          SERIAL PRIMARY KEY,
              name        VARCHAR(120) NOT NULL,
              phone       VARCHAR(50),
              email       VARCHAR(120),
              notes       TEXT,
              enrolled_at TIMESTAMP NOT NULL DEFAULT NOW(),
              enrolled_by INTEGER REFERENCES users(id),
              last_visit  TIMESTAMP,
              visit_count INTEGER NOT NULL DEFAULT 0,
              active      BOOLEAN NOT NULL DEFAULT TRUE
            )""")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_plates (
              id           SERIAL PRIMARY KEY,
              customer_id  INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
              plate_number VARCHAR(20) NOT NULL,
              enrolled_at  TIMESTAMP NOT NULL DEFAULT NOW(),
              active       BOOLEAN NOT NULL DEFAULT TRUE
            )""")
            pg_try("ALTER TABLE customer_plates ADD CONSTRAINT uq_customer_plates_plate UNIQUE (plate_number)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_plates_cid ON customer_plates (customer_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_faces (
              id          SERIAL PRIMARY KEY,
              customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
              embedding   BYTEA NOT NULL,
              enrolled_at TIMESTAMP NOT NULL DEFAULT NOW(),
              active      BOOLEAN NOT NULL DEFAULT TRUE
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_faces_cid ON customer_faces (customer_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_gaits (
              id            SERIAL PRIMARY KEY,
              customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
              gait_features BYTEA NOT NULL,
              enrolled_at   TIMESTAMP NOT NULL DEFAULT NOW(),
              active        BOOLEAN NOT NULL DEFAULT TRUE
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_gaits_cid ON customer_gaits (customer_id)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_visits (
              id                SERIAL PRIMARY KEY,
              customer_id       INTEGER NOT NULL REFERENCES customers(id),
              detected_at       TIMESTAMP NOT NULL DEFAULT NOW(),
              matched_signals   VARCHAR(50) NOT NULL,
              confidence_scores TEXT,
              camera_source     VARCHAR(20),
              acknowledged      BOOLEAN NOT NULL DEFAULT FALSE
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_visits_cid ON customer_visits (customer_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_visits_dt  ON customer_visits (detected_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_customer_visits_ack ON customer_visits (acknowledged)")

            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS plate_detections (
              id            SERIAL PRIMARY KEY,
              plate_number  VARCHAR(20) NOT NULL,
              confidence    NUMERIC(3,2),
              detected_at   TIMESTAMP NOT NULL DEFAULT NOW(),
              customer_id   INTEGER REFERENCES customers(id),
              matched       BOOLEAN NOT NULL DEFAULT FALSE,
              snapshot_path TEXT,
              camera_source VARCHAR(20)
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_plate_det_dt  ON plate_detections (detected_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_plate_det_cid ON plate_detections (customer_id)")

            # ---- Phase 1: Auto-Enrollment System (2026-05-12) ----

            # Make customers.name nullable for anonymous customers
            pg_try("ALTER TABLE customers ALTER COLUMN name DROP NOT NULL")

            # Add auto-enrollment fields to customers
            pg_try("ALTER TABLE customers ADD COLUMN auto_enrolled BOOLEAN DEFAULT FALSE")
            pg_try("ALTER TABLE customers ADD COLUMN customer_number VARCHAR(20) UNIQUE")
            pg_try("ALTER TABLE customers ADD COLUMN first_seen TIMESTAMP DEFAULT NOW()")
            pg_try("ALTER TABLE customers ADD COLUMN is_employee BOOLEAN DEFAULT FALSE")
            pg_try("ALTER TABLE customers ADD COLUMN merged_into INTEGER REFERENCES customers(id)")
            pg_try("ALTER TABLE customers ADD COLUMN is_online_customer BOOLEAN NOT NULL DEFAULT FALSE")
            pg_try("ALTER TABLE customers ADD COLUMN is_pos_customer BOOLEAN NOT NULL DEFAULT FALSE")
            pg_try("ALTER TABLE invoices ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id)")
            pg_try("ALTER TABLE customer_faces ADD COLUMN body_photo BYTEA")

            # Link sales to customers
            pg_try("ALTER TABLE sales ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_sales_datetime_customer ON sales(date_time, customer_id)")

            # Physical attributes tracking
            pg_try("""
            CREATE TABLE IF NOT EXISTS customer_physical_attributes (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                height_cm INTEGER,
                hair_color VARCHAR(20),
                skin_tone VARCHAR(20),
                build VARCHAR(20),
                eye_color VARCHAR(20),
                age_range VARCHAR(20),
                gender VARCHAR(10),
                wearing_glasses BOOLEAN,
                facial_hair VARCHAR(20),
                detected_at TIMESTAMP DEFAULT NOW(),
                camera_source VARCHAR(50),
                confidence NUMERIC(3,2)
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_physical_attrs_customer ON customer_physical_attributes(customer_id)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_physical_attrs_height ON customer_physical_attributes(height_cm)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_physical_attrs_hair ON customer_physical_attributes(hair_color)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_physical_attrs_build ON customer_physical_attributes(build)")
            pg_try("ALTER TABLE customer_physical_attributes ADD COLUMN IF NOT EXISTS height_category VARCHAR(10)")

            # Visit sessions (dwell time tracking)
            # NOTE: these recognition tables go through pg_try (savepoint-isolated) so a
            # single failing statement can't abort the migration transaction and silently
            # skip every table after it. See 2026-06-26 prod schema-drift fix.
            pg_try("""
            CREATE TABLE IF NOT EXISTS visit_sessions (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                session_start TIMESTAMP NOT NULL,
                session_end TIMESTAMP,
                entry_camera VARCHAR(50),
                checkout_camera VARCHAR(50),
                dwell_seconds INTEGER,
                purchase_made BOOLEAN DEFAULT FALSE,
                sale_ids TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_visit_sessions_customer ON visit_sessions(customer_id)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_visit_sessions_start ON visit_sessions(session_start)")

            # Signal confidence history
            pg_try("""
            CREATE TABLE IF NOT EXISTS customer_signal_history (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                signal_type VARCHAR(20) NOT NULL,
                confidence NUMERIC(5,3),
                camera_source VARCHAR(50),
                detected_at TIMESTAMP DEFAULT NOW()
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_signal_history_customer ON customer_signal_history(customer_id, signal_type)")

            # Detection events stream
            pg_try("""
            CREATE TABLE IF NOT EXISTS detection_events (
                id SERIAL PRIMARY KEY,
                event_id VARCHAR(50) UNIQUE,
                camera_source VARCHAR(50) NOT NULL,
                camera_zone VARCHAR(20),
                object_type VARCHAR(20),
                detected_at TIMESTAMP NOT NULL,
                snapshot_path TEXT,
                processed BOOLEAN DEFAULT FALSE,
                plate_number VARCHAR(20),
                plate_confidence NUMERIC(3,2),
                person_bbox JSON,
                face_embedding BYTEA,
                gait_features BYTEA,
                physical_attributes JSON,
                tracked_person_id INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_detection_events_time ON detection_events(detected_at)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_detection_events_camera_zone ON detection_events(camera_zone, detected_at)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_detection_events_person ON detection_events(tracked_person_id)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_detection_events_unprocessed ON detection_events(processed) WHERE processed = FALSE")

            # Person tracking across cameras
            pg_try("""
            CREATE TABLE IF NOT EXISTS person_tracks (
                id SERIAL PRIMARY KEY,
                track_uuid VARCHAR(36) UNIQUE,
                customer_id INTEGER REFERENCES customers(id),
                first_seen TIMESTAMP NOT NULL,
                entry_camera VARCHAR(50),
                entry_zone VARCHAR(20),
                associated_plate VARCHAR(20),
                last_seen TIMESTAMP,
                last_camera VARCHAR(50),
                last_zone VARCHAR(20),
                height_cm_avg INTEGER,
                hair_color_consensus VARCHAR(20),
                skin_tone_consensus VARCHAR(20),
                build_consensus VARCHAR(20),
                gender_consensus VARCHAR(10),
                best_face_embedding BYTEA,
                best_gait_features BYTEA,
                enrolled_as_customer BOOLEAN DEFAULT FALSE,
                session_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_person_tracks_active ON person_tracks(session_active, last_seen)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_person_tracks_plate ON person_tracks(associated_plate)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_person_tracks_customer ON person_tracks(customer_id)")

            # Till detections for purchase linking
            pg_try("""
            CREATE TABLE IF NOT EXISTS till_detections (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                detected_at TIMESTAMP DEFAULT NOW(),
                camera_source VARCHAR(50)
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_till_detections_time ON till_detections(detected_at DESC)")

            # Customer conflicts for reconciliation
            pg_try("""
            CREATE TABLE IF NOT EXISTS customer_conflicts (
                id SERIAL PRIMARY KEY,
                customer_id_a INTEGER NOT NULL REFERENCES customers(id),
                customer_id_b INTEGER NOT NULL REFERENCES customers(id),
                score_a NUMERIC(5,2),
                score_b NUMERIC(5,2),
                detected_at TIMESTAMP DEFAULT NOW(),
                resolved BOOLEAN DEFAULT FALSE,
                merged_into INTEGER REFERENCES customers(id)
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_conflicts_unresolved ON customer_conflicts(resolved) WHERE resolved = FALSE")
            pg_try("""CREATE UNIQUE INDEX idx_conflicts_pair ON customer_conflicts(
                LEAST(customer_id_a, customer_id_b),
                GREATEST(customer_id_a, customer_id_b)
            ) WHERE resolved = FALSE""")

            # Customer exclusions (for identical twins, etc.)
            pg_try("""
            CREATE TABLE IF NOT EXISTS customer_exclusions (
                id SERIAL PRIMARY KEY,
                customer_id_a INTEGER NOT NULL REFERENCES customers(id),
                customer_id_b INTEGER NOT NULL REFERENCES customers(id),
                reason VARCHAR(200),
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_excl_a ON customer_exclusions(customer_id_a)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_excl_b ON customer_exclusions(customer_id_b)")

            # Merge audit log - one row per merge operation, survives unmerge
            pg_try("""
            CREATE TABLE IF NOT EXISTS customer_merge_log (
                id SERIAL PRIMARY KEY,
                primary_id INTEGER NOT NULL REFERENCES customers(id),
                source_id  INTEGER NOT NULL REFERENCES customers(id),
                merged_at  TIMESTAMP NOT NULL DEFAULT NOW(),
                auto_merged BOOLEAN NOT NULL DEFAULT FALSE,
                similarity  NUMERIC(5,3),
                source_name            VARCHAR(200),
                source_customer_number VARCHAR(20),
                source_visit_count     INTEGER,
                source_face_photo      BYTEA,
                unmerged_at            TIMESTAMP
            )""")
            pg_try("CREATE INDEX IF NOT EXISTS idx_merge_log_primary ON customer_merge_log(primary_id)")
            pg_try("CREATE INDEX IF NOT EXISTS idx_merge_log_source  ON customer_merge_log(source_id)")

            # Track which customer originally owned each face/gait row - needed for unmerge
            pg_try("ALTER TABLE customer_faces ADD COLUMN original_customer_id INTEGER REFERENCES customers(id)")
            pg_try("ALTER TABLE customer_gaits ADD COLUMN original_customer_id INTEGER REFERENCES customers(id)")
            pg_try("ALTER TABLE customer_physical_attributes ADD COLUMN original_customer_id INTEGER REFERENCES customers(id)")
            # Embedding quality + camera source for per-angle quality upgrade and camera boost
            pg_try("ALTER TABLE customer_faces ADD COLUMN quality NUMERIC(4,3)")
            pg_try("ALTER TABLE customer_faces ADD COLUMN camera_source VARCHAR(20)")

        # scale sync fields on products - POS is single source of truth
        pg_try("ALTER TABLE products ADD COLUMN sync_to_scale BOOLEAN NOT NULL DEFAULT FALSE")
        pg_try("ALTER TABLE products ADD COLUMN scale_tare NUMERIC(8,3)")
        pg_try("ALTER TABLE products ADD COLUMN scale_shelf_life INTEGER")
        pg_try("ALTER TABLE products ADD COLUMN scale_pack_qty INTEGER")
        pg_try("ALTER TABLE products ADD COLUMN scale_open_price BOOLEAN NOT NULL DEFAULT FALSE")
        pg_try("ALTER TABLE products ADD COLUMN scale_msg1 VARCHAR(80)")
        pg_try("ALTER TABLE products ADD COLUMN scale_msg2 VARCHAR(80)")
        # Migrate existing integer msg fields to varchar if needed
        pg_try("ALTER TABLE products ALTER COLUMN scale_msg1 TYPE VARCHAR(80) USING scale_msg1::VARCHAR")
        pg_try("ALTER TABLE products ALTER COLUMN scale_msg2 TYPE VARCHAR(80) USING scale_msg2::VARCHAR")
        pg_try("ALTER TABLE products ADD COLUMN scale_prohibit BOOLEAN NOT NULL DEFAULT FALSE")
        pg_try("ALTER TABLE products ADD COLUMN scale_last_synced_at TIMESTAMPTZ")
        pg_try("ALTER TABLE products ADD COLUMN scale_last_sync_status VARCHAR(20)")
        pg_try("ALTER TABLE products ADD COLUMN scale_last_sync_error TEXT")
        pg_try("ALTER TABLE products ADD COLUMN scale_hash VARCHAR(64)")

        # Backfill sync_to_scale=true for existing weight/volume products that are for sale
        pg_try("""
            UPDATE products SET sync_to_scale = TRUE
            WHERE sold_by_weight = TRUE
              AND is_for_sale = TRUE
              AND is_archived = FALSE
              AND sync_to_scale = FALSE
        """)

        # Scale audit tables
        pg_try("""
            CREATE TABLE IF NOT EXISTS scale_sync_runs (
              id               SERIAL PRIMARY KEY,
              started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              completed_at     TIMESTAMPTZ,
              run_type         VARCHAR(20) NOT NULL,
              status           VARCHAR(20) NOT NULL DEFAULT 'running',
              products_total   INTEGER NOT NULL DEFAULT 0,
              products_sent    INTEGER NOT NULL DEFAULT 0,
              products_failed  INTEGER NOT NULL DEFAULT 0,
              orphans_detected INTEGER NOT NULL DEFAULT 0,
              orphans_removed  INTEGER NOT NULL DEFAULT 0,
              error_message    TEXT,
              triggered_by     INTEGER REFERENCES users(id)
            )
        """)
        pg_try("""
            CREATE TABLE IF NOT EXISTS scale_snapshots (
              id            SERIAL PRIMARY KEY,
              captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              run_id        INTEGER REFERENCES scale_sync_runs(id),
              plu_count     INTEGER NOT NULL DEFAULT 0,
              snapshot_json TEXT
            )
        """)

        # PLU audit log - tracks product_code changes for scale lifecycle management
        pg_try("""
            CREATE TABLE IF NOT EXISTS scale_plu_log (
              id          SERIAL PRIMARY KEY,
              product_id  INTEGER NOT NULL REFERENCES products(id),
              old_plu     INTEGER,
              new_plu     INTEGER,
              changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              changed_by  INTEGER REFERENCES users(id),
              sync_cleared BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        pg_try("CREATE INDEX IF NOT EXISTS ix_scale_plu_log_product ON scale_plu_log (product_id)")
        pg_try("CREATE INDEX IF NOT EXISTS ix_scale_plu_log_old_plu ON scale_plu_log (old_plu) WHERE old_plu IS NOT NULL")

        # DB constraint: product_code must be positive if set
        pg_try("ALTER TABLE products ADD CONSTRAINT chk_product_code_positive CHECK (product_code IS NULL OR product_code > 0)")

        # product_code: sequential code by type for scale/barcode integration
        pg_try("ALTER TABLE products ADD COLUMN product_code INTEGER UNIQUE")
        pg_try("CREATE UNIQUE INDEX IF NOT EXISTS ix_products_product_code ON products (product_code)")

        # Backfill product_code for existing products that don't have one yet
        # Ranges: weight=00001-19999, fixed=20000-29999, volume=30000-39999, other=40000-49999
        needs_code = conn.execute(text(
            "SELECT id, sold_by_weight, unit_type, product_type FROM products WHERE product_code IS NULL ORDER BY id"
        )).fetchall()
        if needs_code:
            # Find max codes already assigned per range
            def max_in_range(lo, hi):
                r = conn.execute(text(
                    "SELECT MAX(product_code) FROM products WHERE product_code >= :lo AND product_code <= :hi"
                ), {'lo': lo, 'hi': hi}).scalar()
                return r or (lo - 1)

            next_weight = max_in_range(1, 19999) + 1
            next_fixed  = max_in_range(20000, 29999) + 1
            next_volume = max_in_range(30000, 39999) + 1
            next_other  = max_in_range(40000, 49999) + 1

            for row in needs_code:
                pid, sbw, unit_type, ptype = row
                if sbw and unit_type == 'volume':
                    code = next_volume; next_volume += 1
                elif sbw:
                    code = next_weight; next_weight += 1
                elif ptype == 'stock_item':
                    code = next_fixed; next_fixed += 1
                else:
                    code = next_other; next_other += 1
                conn.exec_driver_sql(
                    "UPDATE products SET product_code = %s WHERE id = %s", (code, pid)
                )

            # Also update barcode for fixed/recipe products to be deterministic from product_code
            # Weight/volume products have no stored barcode (scale generates dynamically)
            fixed_no_code = conn.execute(text("""
                SELECT id, product_code FROM products
                WHERE sold_by_weight = FALSE
                  AND product_code IS NOT NULL
                  AND (barcode IS NULL OR barcode NOT LIKE '1%')
            """)).fetchall()
            # We'll let helpers._gen_barcode_from_code handle this at runtime

        # Scheduled deployments table
        pg_try("""
            CREATE TABLE IF NOT EXISTS deploy_schedules (
              id           SERIAL PRIMARY KEY,
              scheduled_at TIMESTAMPTZ NOT NULL,
              description  VARCHAR(200),
              status       VARCHAR(20) NOT NULL DEFAULT 'pending',
              created_by   INTEGER REFERENCES users(id),
              created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              executed_at  TIMESTAMPTZ,
              result_log   TEXT
            )
        """)
        pg_try("CREATE INDEX IF NOT EXISTS ix_deploy_schedules_status ON deploy_schedules(status)")
        pg_try("ALTER TABLE deploy_schedules ADD COLUMN IF NOT EXISTS action VARCHAR(20) NOT NULL DEFAULT 'deploy'")  # deploy/rollback

        # CSV import audit table
        pg_try("""
            CREATE TABLE IF NOT EXISTS product_import_runs (
              id             SERIAL PRIMARY KEY,
              imported_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              file_name      VARCHAR(200),
              file_hash      VARCHAR(64),
              mode           VARCHAR(20) NOT NULL,
              allow_name_match BOOLEAN NOT NULL DEFAULT FALSE,
              duration_ms    INTEGER,
              rows_total     INTEGER NOT NULL DEFAULT 0,
              rows_created   INTEGER NOT NULL DEFAULT 0,
              rows_updated   INTEGER NOT NULL DEFAULT 0,
              rows_unchanged INTEGER NOT NULL DEFAULT 0,
              rows_skipped   INTEGER NOT NULL DEFAULT 0,
              rows_error     INTEGER NOT NULL DEFAULT 0,
              imported_by    INTEGER REFERENCES users(id),
              error_log      TEXT
            )
        """)

        # Performance indexes for import
        pg_try("CREATE INDEX IF NOT EXISTS idx_products_product_code ON products(product_code)")
        pg_try("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name)")

        # Stats normalisation: typical serving/portion size for weighted products
        pg_try("ALTER TABLE products ADD COLUMN stat_unit_size NUMERIC(10,4)")

        # Till sessions: track cash paid out for returns
        pg_try("ALTER TABLE till_sessions ADD COLUMN cash_refunds NUMERIC(10,2) DEFAULT 0")

        # Return tracking: dedicated column instead of void_reason string pattern
        pg_try("ALTER TABLE sales ADD COLUMN original_sale_id VARCHAR(36)")
        pg_try("CREATE INDEX IF NOT EXISTS ix_sales_original_sale_id ON sales(original_sale_id)")

        # Invoice number sequence — safe under concurrent creates
        pg_try("""
            DO $$
            DECLARE max_num INTEGER;
            BEGIN
              CREATE SEQUENCE IF NOT EXISTS invoice_number_seq START 1;
              SELECT COALESCE(MAX(
                CASE WHEN invoice_number ~ '^INV-[0-9]+$'
                     THEN CAST(SPLIT_PART(invoice_number, '-', 2) AS INTEGER)
                     ELSE 0 END), 0) INTO max_num FROM invoices;
              IF max_num > 0 THEN
                PERFORM setval('invoice_number_seq', max_num);
              END IF;
            END$$
        """)

        # Bulk product edit audit table
        pg_try("""
            CREATE TABLE IF NOT EXISTS product_bulk_edit_runs (
              id            SERIAL PRIMARY KEY,
              created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              created_by    INTEGER REFERENCES users(id),
              description   VARCHAR(200),
              filter_json   TEXT NOT NULL,
              action_json   TEXT NOT NULL,
              product_count INTEGER NOT NULL DEFAULT 0,
              before_json   TEXT,
              rolled_back_at TIMESTAMPTZ,
              rolled_back_by INTEGER REFERENCES users(id)
            )
        """)

        # Settings: add updated_at for TTL-based import lock
        pg_try("ALTER TABLE settings ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW()")

        # Widen settings.value for branding fields (invoice footer etc). Idempotent:
        # a no-op when already varchar(2000). Model is String(2000) in the same image,
        # so SQLAlchemy won't truncate to 200 before the DB sees the value.
        pg_try("ALTER TABLE settings ALTER COLUMN value TYPE VARCHAR(2000)")
        # Assert the widen actually took (a SAVEPOINT rollback could have swallowed it).
        try:
            _w = conn.execute(text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name='settings' AND column_name='value'"
            )).scalar()
            if _w is not None and _w < 2000:
                logger.warning(f"settings.value width is {_w}, expected >=2000 - branding footer may truncate")
        except Exception as _e:
            logger.warning(f"settings.value width check skipped: {_e}")

        # Import in-progress flag (atomic lock for CSV imports)
        pg_try("INSERT INTO settings (key, value) VALUES ('import_in_progress', 'false') ON CONFLICT DO NOTHING")

        # Receipt / printer settings
        pg_try("INSERT INTO settings (key, value) VALUES ('receipt_width_mm', '72') ON CONFLICT DO NOTHING")
        pg_try("INSERT INTO settings (key, value) VALUES ('receipt_printer_id', '') ON CONFLICT DO NOTHING")
        pg_try("INSERT INTO settings (key, value) VALUES ('auto_print_receipt', 'false') ON CONFLICT DO NOTHING")

        # White-label branding keys (seed as '' = use Lady Coleen / env fallback, so the
        # LC box and any un-customised store render byte-identical). See White-Label
        # Branding Plan. Runtime-editable via /api/settings + the Branding UI card.
        for _bk in _BRANDING_KEYS:
            pg_try(f"INSERT INTO settings (key, value) VALUES ('{_bk}', '') ON CONFLICT DO NOTHING")

        # DB constraints
        pg_try("ALTER TABLE products ADD CONSTRAINT chk_product_code_positive CHECK (product_code IS NULL OR product_code > 0)")

        # Product categories (categories table is created by db.create_all above)
        pg_try("ALTER TABLE products ADD COLUMN category_id INTEGER")
        pg_try("ALTER TABLE products ADD CONSTRAINT fk_products_category FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL")
        pg_try("CREATE INDEX IF NOT EXISTS ix_products_category_id ON products (category_id)")

        # ── Label printing subsystem ───────────────────────────────────────────
        pg_try("""
            CREATE TABLE IF NOT EXISTS label_templates (
              id               SERIAL PRIMARY KEY,
              name             VARCHAR(100) NOT NULL,
              description      VARCHAR(300),
              width_mm         NUMERIC(6,2) NOT NULL,
              height_mm        NUMERIC(6,2) NOT NULL,
              category         VARCHAR(30) NOT NULL DEFAULT 'custom',
              elements_json    TEXT NOT NULL DEFAULT '[]',
              background_color VARCHAR(10) NOT NULL DEFAULT '#ffffff',
              border           BOOLEAN NOT NULL DEFAULT FALSE,
              is_archived      BOOLEAN NOT NULL DEFAULT FALSE,
              created_by       INTEGER REFERENCES users(id),
              created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at       TIMESTAMPTZ
            )
        """)
        pg_try("""
            CREATE TABLE IF NOT EXISTS label_print_jobs (
              id          SERIAL PRIMARY KEY,
              template_id INTEGER REFERENCES label_templates(id),
              product_id  INTEGER REFERENCES products(id),
              qty         INTEGER NOT NULL DEFAULT 1,
              printer_id  INTEGER,
              status      VARCHAR(20) NOT NULL DEFAULT 'sent',
              user_id     INTEGER REFERENCES users(id),
              printed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              notes       TEXT
            )
        """)
        pg_try("CREATE INDEX IF NOT EXISTS ix_label_jobs_printed ON label_print_jobs(printed_at DESC)")
        pg_try("""
            CREATE TABLE IF NOT EXISTS label_printers (
              id         SERIAL PRIMARY KEY,
              name       VARCHAR(80)  NOT NULL,
              model      VARCHAR(60)  NOT NULL DEFAULT 'xprinter_xp365b',
              connection VARCHAR(20)  NOT NULL DEFAULT 'usb',
              address    VARCHAR(120),
              is_active  BOOLEAN NOT NULL DEFAULT TRUE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Seed four built-in templates (idempotent — skip if they already exist)
        pg_try("""
            INSERT INTO label_templates (name, description, width_mm, height_mm, category, elements_json, border)
            SELECT 'Small Barcode Label', '40×20mm — barcode + name + price', 40, 20, 'small_barcode',
            '[{"type":"product_name","x":1,"y":1,"w":38,"h":5,"font_size":6,"bold":true},
              {"type":"barcode","x":1,"y":7,"w":24,"h":11,"barcode_format":"auto"},
              {"type":"price","x":26,"y":7,"w":12,"h":11,"font_size":10,"bold":true,"align":"center"},
              {"type":"sku","x":1,"y":17,"w":38,"h":3,"font_size":5}]',
            true
            WHERE NOT EXISTS (SELECT 1 FROM label_templates WHERE name = 'Small Barcode Label')
        """)
        pg_try("""
            INSERT INTO label_templates (name, description, width_mm, height_mm, category, elements_json, border)
            SELECT 'Shelf Label', '60×30mm — product name, price, weight, category', 60, 30, 'shelf',
            '[{"type":"store_name","x":1,"y":1,"w":58,"h":5,"font_size":6,"align":"center"},
              {"type":"product_name","x":1,"y":7,"w":58,"h":7,"font_size":9,"bold":true,"align":"center"},
              {"type":"category","x":1,"y":14,"w":58,"h":4,"font_size":6,"align":"center"},
              {"type":"price","x":10,"y":19,"w":40,"h":9,"font_size":14,"bold":true,"align":"center"},
              {"type":"sku","x":1,"y":27,"w":58,"h":3,"font_size":5}]',
            true
            WHERE NOT EXISTS (SELECT 1 FROM label_templates WHERE name = 'Shelf Label')
        """)
        pg_try("""
            INSERT INTO label_templates (name, description, width_mm, height_mm, category, elements_json, border)
            SELECT 'Product Sticker', '50×50mm — logo, name, price, barcode', 50, 50, 'sticker',
            '[{"type":"store_logo","x":1,"y":1,"w":48,"h":12},
              {"type":"product_name","x":1,"y":14,"w":48,"h":8,"font_size":9,"bold":true,"align":"center"},
              {"type":"price","x":1,"y":23,"w":48,"h":10,"font_size":14,"bold":true,"align":"center"},
              {"type":"barcode","x":5,"y":34,"w":40,"h":13,"barcode_format":"auto"},
              {"type":"sku","x":1,"y":47,"w":48,"h":3,"font_size":5,"align":"center"}]',
            false
            WHERE NOT EXISTS (SELECT 1 FROM label_templates WHERE name = 'Product Sticker')
        """)
        pg_try("""
            INSERT INTO label_templates (name, description, width_mm, height_mm, category, elements_json, border)
            SELECT 'Price Tag', '30×15mm — name + price only', 30, 15, 'price_tag',
            '[{"type":"product_name","x":1,"y":1,"w":28,"h":5,"font_size":6,"bold":false},
              {"type":"price","x":1,"y":7,"w":28,"h":7,"font_size":11,"bold":true,"align":"center"}]',
            true
            WHERE NOT EXISTS (SELECT 1 FROM label_templates WHERE name = 'Price Tag')
        """)

        # Batch-produce workflow columns
        pg_try("ALTER TABLE products ADD COLUMN is_produced BOOLEAN NOT NULL DEFAULT FALSE")
        pg_try("ALTER TABLE products ADD COLUMN yields_units NUMERIC(10,2) NOT NULL DEFAULT 1")

        # Legacy backfill
        sales_count = conn.execute(text("SELECT COUNT(*) FROM sales")).scalar_one()
        if sales_count == 0:
            legacy_ok = False
            try:
                conn.execute(text("SELECT 1 FROM transactions LIMIT 1"))
                conn.execute(text("SELECT 1 FROM transaction_lines LIMIT 1"))
                legacy_ok = True
            except Exception:
                pass
            if legacy_ok:
                conn.exec_driver_sql("""
                INSERT INTO sales (sale_id, date_time, product_id, qty, unit_price)
                SELECT CAST(t.id AS TEXT), t.date_time, tl.product_id, tl.qty, tl.unit_price
                FROM transaction_lines tl
                JOIN transactions t ON tl.transaction_id = t.id
                """)

    # No explicit unlock needed: the transaction-level advisory lock acquired inside
    # the engine.begin() block above auto-releases when that transaction committed.


def create_app():
    app = Flask(__name__)

    # Decimal → float for JSON
    class _JSONProvider(DefaultJSONProvider):
        def default(self, o):
            if isinstance(o, Decimal):
                return float(o)
            return super().default(o)
    app.json_provider_class = _JSONProvider
    app.json = _JSONProvider(app)
    # SECRET_KEY must be unique on a provisioned appliance box. Fail loud there rather
    # than ship a fleet that shares the well-known dev fallback (session-forgery risk).
    # Gated on STORE_ID so the original Lady Coleen box (which never set SECRET_KEY)
    # keeps its historical behaviour untouched.
    _secret = os.getenv('SECRET_KEY', 'dev-secret-key')
    if STORE_ID and _secret == 'dev-secret-key':
        raise RuntimeError(
            f"SECRET_KEY is unset on provisioned store '{STORE_ID}'. "
            "register-store.sh must generate a unique SECRET_KEY per store."
        )
    app.secret_key = _secret

    # Trust Cloudflare / nginx reverse proxy headers so Flask sees HTTPS correctly.
    # Required for secure session cookies and PWA install prompt.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # DB URL
    db_url = os.getenv('DATABASE_URL', 'sqlite:///pos.db')
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql+psycopg://', 1)
    elif db_url.startswith('postgresql://') and '+psycopg://' not in db_url:
        db_url = 'postgresql+psycopg://' + db_url.split('://', 1)[1]

    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,    # test connection before use — prevents mid-sale 500s on stale connections
        'pool_recycle':  1800,    # recycle connections every 30 min (before Postgres idle timeout)
        'pool_size':     5,       # per-worker pool; 4 workers × 5 = 20 max connections
        'max_overflow':  5,       # allow brief spikes to 10 per worker
    }

    db.init_app(app)

    # Inject environment into Jinja2 globals - used by QA banner in index.html
    app.jinja_env.globals['app_env']       = APP_ENV
    app.jinja_env.globals['is_qa']         = IS_QA
    # True on appliance boxes (STORE_ID set) - hides recognition/monitor tabs
    app.jinja_env.globals['is_appliance']  = bool(STORE_ID)
    # Per-store branding - templates render these instead of hardcoded strings.
    # On the original Lady Coleen box (STORE_ID unset) they resolve to the exact
    # historical literals, so nothing renders differently there.
    app.jinja_env.globals['store_name']     = STORE_NAME
    app.jinja_env.globals['store_tagline']  = STORE_TAGLINE
    app.jinja_env.globals['store_legal']    = STORE_LEGAL
    app.jinja_env.globals['store_subtitle'] = STORE_SUBTITLE

    # CSS-safe filters for the runtime branding <style> overrides (XSS defense-in-depth)
    app.jinja_env.filters['css_hex']   = css_hex
    app.jinja_env.filters['safe_font'] = safe_font

    # Runtime branding: inject DB-backed values into every template. Empty values keep
    # the LC/env fallback so an un-customised box is byte-identical. Never raises.
    @app.context_processor
    def _inject_branding():
        b = get_branding()
        logo_file = (b.get('branding_logo_file') or '').strip()
        _branded_name = (b.get('branding_store_name') or '').strip()
        return {
            'branding': b,
            'branding_logo_url': ('/static/branding/' + logo_file) if logo_file else '/static/logo.svg',
            # Runtime display name - overrides the startup Jinja global EVERYWHERE {{ store_name }}
            # is used (context processor wins over jinja_env.globals). Empty = env/LC default.
            'store_name':  _branded_name or STORE_NAME,
            # The visible header renders {{ store_tagline }}, not {{ store_name }} - override it
            # too so a branded store name actually shows in the header (not just the <title>).
            # Empty branded name = keep the env/LC tagline exactly.
            'store_tagline': _branded_name or STORE_TAGLINE,
            # Auto-contrast text colour for the chosen primary (readable on light OR dark).
            'brand_on_primary': brand_contrast(b.get('branding_primary') or '#927f57'),
            # invoice text: DB value if set, else the env-driven Jinja global fallback
            'brand_invoice_legal':    (b.get('branding_invoice_legal') or '').strip()    or STORE_LEGAL,
            'brand_invoice_subtitle': (b.get('branding_invoice_subtitle') or '').strip() or STORE_SUBTITLE,
            'brand_invoice_footer':   (b.get('branding_invoice_footer') or '').strip(),
        }

    logger.info(f"{LOG_PREFIX} POS starting - ENV={APP_ENV} DB={DB_NAME} IS_QA={IS_QA}")

    # /api/env - environment info for frontend
    @app.route('/api/env')
    def _api_env():
        return jsonify({'env': APP_ENV, 'is_qa': IS_QA})

    # /api/health - non-blocking health check with cached scale check
    _health_cache = {'scale': False, 'checked_at': 0}
    @app.route('/api/health')
    def _api_health():
        import time as _t, socket as _s
        now = _t.time()
        # LC box (STORE_ID unset) keeps its historical 10.0.0.103 fallback; a
        # provisioned store uses its own SCALE_IP, and empty means "no scale" (skip).
        scale_ip = os.environ.get('SCALE_IP', '' if STORE_ID else '10.0.0.103').strip()
        if not IS_QA and scale_ip and now - _health_cache['checked_at'] > 30:
            try:
                c = _s.create_connection((scale_ip, 7061), timeout=2); c.close()
                _health_cache.update({'scale': True, 'checked_at': now})
            except Exception:
                _health_cache.update({'scale': False, 'checked_at': now})
        with db.engine.connect() as conn:
            imp = conn.execute(text(
                "SELECT COALESCE((SELECT value FROM settings WHERE key='import_in_progress'),'false')"
            )).scalar()
        # Backup health (ISSUE gap): backup.sh writes /app/config/backup_status.json.
        # Absent on the Lady Coleen box (no appliance backup cron) -> no warning shown.
        backup_warn = None
        try:
            import json as _json, datetime as _dt
            with open('/app/config/backup_status.json') as _bf:
                _bs = _json.load(_bf)
            _last = _dt.datetime.fromisoformat(_bs.get('last_backup'))
            _age_h = (_dt.datetime.now(_last.tzinfo) - _last).total_seconds() / 3600
            if _bs.get('disk_warn'):        backup_warn = f"Disk {_bs.get('disk_pct','?')}% full"
            elif _bs.get('last_push_ok') is False: backup_warn = "Off-site backup push failing"
            elif _age_h > 48:               backup_warn = f"No backup in {int(_age_h)}h"
        except Exception:
            backup_warn = None  # no status file / unreadable -> stay silent
        return jsonify({
            'env': APP_ENV, 'db': DB_NAME, 'is_qa': IS_QA,
            'scale_reachable': _health_cache['scale'] if not IS_QA else False,
            'import_in_progress': (imp or 'false').lower() == 'true',
            'backup_warning': backup_warn,
        })

    # Request / response logging
    @app.before_request
    def _log_request():
        g._req_start = _time.monotonic()
        if request.path.startswith('/static'):
            return
        logger.info('REQ  %s %s  user=%s', request.method, request.path,
                    session.get('user_id', '-'))
        uid = session.get('user_id')
        if not uid or request.path == '/api/me':
            return
        now = datetime.utcnow()
        sid = session.get('session_id')
        if sid:
            sess = db.session.get(UserSession, sid)
            if sess and sess.logged_out is None:
                cutoff = now - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
                last   = sess.last_active or sess.logged_in
                if last < cutoff:
                    sess.logged_out = last + timedelta(minutes=SESSION_TIMEOUT_MINUTES)
                    db.session.commit()
                    session.pop('session_id', None)
                    sid = None
                else:
                    sess.last_active = now
                    db.session.commit()
            elif sess is None:
                session.pop('session_id', None)
                sid = None
        if not sid:
            existing = UserSession.query.filter_by(
                user_id=uid, logged_out=None
            ).order_by(UserSession.logged_in.desc()).first()
            if existing:
                session['session_id'] = existing.id
                existing.last_active  = now
                db.session.commit()
            else:
                new_sess = UserSession(user_id=uid, logged_in=now, last_active=now)
                db.session.add(new_sess)
                db.session.commit()
                session['session_id'] = new_sess.id

    @app.after_request
    def _log_response(response):
        if request.path.startswith('/static'):
            return response
        elapsed_ms = round((_time.monotonic() - getattr(g, '_req_start', 0)) * 1000)
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(level, 'RESP %s %s  status=%s  %dms',
                   request.method, request.path, response.status_code, elapsed_ms)
        return response

    @app.errorhandler(Exception)
    def _handle_exception(e):
        logger.error('UNHANDLED EXCEPTION  %s %s\n%s',
                     request.method, request.path, traceback.format_exc())
        return jsonify({'error': 'Internal server error', 'detail': str(e)}), 500

    # Startup: migrate + seed
    with app.app_context():
        strong_migrate()
        seed_first_admin()
        try:
            _stale_cutoff = datetime.utcnow() - timedelta(hours=SESSION_LOGOUT_HOURS)
            with db.engine.begin() as _conn:
                _conn.execute(text("""
                    UPDATE user_sessions
                    SET logged_out = COALESCE(last_active, logged_in)
                    WHERE logged_out IS NULL
                      AND (last_active IS NULL OR last_active < :cutoff)
                """), {'cutoff': _stale_cutoff})
        except Exception as _e:
            logger.warning('Stale session cleanup skipped: %s', _e)

    _register_routes(app)
    return app


def _register_routes(_app):
    from blueprints.auth         import bp as auth_bp
    from blueprints.kiosk        import bp as kiosk_bp
    from blueprints.kitchen      import bp as kitchen_bp
    from blueprints.settings     import bp as settings_bp
    from blueprints.specials     import bp as specials_bp
    from blueprints.suppliers    import bp as suppliers_bp
    from blueprints.products     import bp as products_bp
    from blueprints.categories   import bp as categories_bp
    from blueprints.stock        import bp as stock_bp
    from blueprints.transactions import bp as transactions_bp
    from blueprints.customers    import bp as customers_bp
    from blueprints.stats        import bp as stats_bp
    from blueprints.invoices     import bp as invoices_bp
    from blueprints.recognition  import bp as recognition_bp
    from blueprints.core         import bp as core_bp
    from blueprints.scale        import bp as scale_bp
    from blueprints.imports         import bp as imports_bp
    from blueprints.deploy_schedule import bp as deploy_schedule_bp
    from blueprints.branding        import bp as branding_bp
    from blueprints.till_sessions   import bp as till_sessions_bp
    from blueprints.labels          import bp as labels_bp
    from blueprints.bulk            import bp as bulk_bp
    _app.register_blueprint(auth_bp)
    _app.register_blueprint(kiosk_bp)
    _app.register_blueprint(kitchen_bp)
    _app.register_blueprint(settings_bp)
    _app.register_blueprint(specials_bp)
    _app.register_blueprint(suppliers_bp)
    _app.register_blueprint(products_bp)
    _app.register_blueprint(categories_bp)
    _app.register_blueprint(stock_bp)
    _app.register_blueprint(transactions_bp)
    _app.register_blueprint(customers_bp)
    _app.register_blueprint(stats_bp)
    _app.register_blueprint(invoices_bp)
    _app.register_blueprint(recognition_bp)
    _app.register_blueprint(core_bp)
    _app.register_blueprint(scale_bp)
    _app.register_blueprint(imports_bp)
    _app.register_blueprint(deploy_schedule_bp)
    _app.register_blueprint(branding_bp)
    _app.register_blueprint(till_sessions_bp)
    _app.register_blueprint(labels_bp)
    _app.register_blueprint(bulk_bp)

    # Start background deploy scheduler (only in QA - QA schedules deploys to PROD)
    if IS_QA:
        from blueprints.deploy_schedule import _start_scheduler
        _start_scheduler(_app)


# Module-level app instance - used by gunicorn (`app:app`) and @app.route decorators.
# Must be defined AFTER strong_migrate (below) and BEFORE the route definitions.
# create_app() is called here, which runs strong_migrate + seed on startup.




# Create the module-level app instance. strong_migrate() is defined above so
# this is safe. All @app.route decorators below bind against this instance.
app = create_app()




if __name__ == '__main__':
    # Look for certificates in config directory (production) or same directory (dev)
    _config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
    _cert = os.path.join(_config_dir, 'cert.pem')
    _key  = os.path.join(_config_dir, 'key.pem')

    # Fallback to dev location
    if not (os.path.exists(_cert) and os.path.exists(_key)):
        _cert = os.path.join(os.path.dirname(__file__), 'cert.pem')
        _key  = os.path.join(os.path.dirname(__file__), 'key.pem')

    _ssl  = (_cert, _key) if os.path.exists(_cert) and os.path.exists(_key) else None

    if _ssl:
        logger.info(f'Starting with HTTPS on port {os.getenv("PORT", "5443")}')
    else:
        logger.warning('SSL certificates not found - running on HTTP')

    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5443' if _ssl else '5000')), ssl_context=_ssl)

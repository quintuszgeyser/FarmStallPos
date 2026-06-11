
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
    SESSION_TIMEOUT_MINUTES, SESSION_LOGOUT_HOURS,
)
from helpers import (
    get_setting, set_setting,
    require_login, require_role, current_user,
    seed_first_admin, get_online_user_id,
    consume_fifo, reverse_fifo,
    get_stock_level, get_fifo_cost_per_unit,
    sync_sell_packages, _gen_barcode, _ean13_check, _serialize_product,
    _parse_dt,
)

APP_VERSION = '1.6.0'

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
    db.create_all()
    engine = db.engine
    engine_name = engine.dialect.name

    with engine.begin() as conn:

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
                ('product_type',         "TEXT NOT NULL DEFAULT 'simple'"),
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
            ]:
                if col not in existing_prod:
                    conn.exec_driver_sql(f"ALTER TABLE products ADD COLUMN {col} {defn}")

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
            # Add supplier_id if missing on existing table (SQLite)
            existing_sb = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(stock_batches)").fetchall()]
            if 'supplier_id' not in existing_sb:
                conn.exec_driver_sql("ALTER TABLE stock_batches ADD COLUMN supplier_id INTEGER")
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
            def pg_try(sql):
                try:
                    conn.exec_driver_sql("SAVEPOINT sp")
                    conn.exec_driver_sql(sql)
                    conn.exec_driver_sql("RELEASE SAVEPOINT sp")
                except Exception:
                    conn.exec_driver_sql("ROLLBACK TO SAVEPOINT sp")

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
                ('product_type',         "VARCHAR(20) NOT NULL DEFAULT 'simple'"),
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
            # Split contact into phone/email/website
            pg_try("ALTER TABLE suppliers ADD COLUMN phone   VARCHAR(50)")
            pg_try("ALTER TABLE suppliers ADD COLUMN email   VARCHAR(120)")
            pg_try("ALTER TABLE suppliers ADD COLUMN website VARCHAR(200)")
            pg_try("UPDATE suppliers SET phone = contact WHERE contact IS NOT NULL AND email IS NULL AND website IS NULL")

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
            conn.exec_driver_sql("""
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
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_physical_attrs_customer ON customer_physical_attributes(customer_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_physical_attrs_height ON customer_physical_attributes(height_cm)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_physical_attrs_hair ON customer_physical_attributes(hair_color)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_physical_attrs_build ON customer_physical_attributes(build)")
            conn.exec_driver_sql("ALTER TABLE customer_physical_attributes ADD COLUMN IF NOT EXISTS height_category VARCHAR(10)")

            # Visit sessions (dwell time tracking)
            conn.exec_driver_sql("""
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
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_visit_sessions_customer ON visit_sessions(customer_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_visit_sessions_start ON visit_sessions(session_start)")

            # Signal confidence history
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_signal_history (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                signal_type VARCHAR(20) NOT NULL,
                confidence NUMERIC(5,3),
                camera_source VARCHAR(50),
                detected_at TIMESTAMP DEFAULT NOW()
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_signal_history_customer ON customer_signal_history(customer_id, signal_type)")

            # Detection events stream
            conn.exec_driver_sql("""
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
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_detection_events_time ON detection_events(detected_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_detection_events_camera_zone ON detection_events(camera_zone, detected_at)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_detection_events_person ON detection_events(tracked_person_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_detection_events_unprocessed ON detection_events(processed) WHERE processed = FALSE")

            # Person tracking across cameras
            conn.exec_driver_sql("""
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
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_person_tracks_active ON person_tracks(session_active, last_seen)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_person_tracks_plate ON person_tracks(associated_plate)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_person_tracks_customer ON person_tracks(customer_id)")

            # Till detections for purchase linking
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS till_detections (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                detected_at TIMESTAMP DEFAULT NOW(),
                camera_source VARCHAR(50)
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_till_detections_time ON till_detections(detected_at DESC)")

            # Customer conflicts for reconciliation
            conn.exec_driver_sql("""
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
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_conflicts_unresolved ON customer_conflicts(resolved) WHERE resolved = FALSE")
            pg_try("""CREATE UNIQUE INDEX idx_conflicts_pair ON customer_conflicts(
                LEAST(customer_id_a, customer_id_b),
                GREATEST(customer_id_a, customer_id_b)
            ) WHERE resolved = FALSE""")

            # Customer exclusions (for identical twins, etc.)
            conn.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS customer_exclusions (
                id SERIAL PRIMARY KEY,
                customer_id_a INTEGER NOT NULL REFERENCES customers(id),
                customer_id_b INTEGER NOT NULL REFERENCES customers(id),
                reason VARCHAR(200),
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_excl_a ON customer_exclusions(customer_id_a)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_excl_b ON customer_exclusions(customer_id_b)")

            # Merge audit log — one row per merge operation, survives unmerge
            conn.exec_driver_sql("""
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
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_merge_log_primary ON customer_merge_log(primary_id)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_merge_log_source  ON customer_merge_log(source_id)")

            # Track which customer originally owned each face/gait row — needed for unmerge
            pg_try("ALTER TABLE customer_faces ADD COLUMN original_customer_id INTEGER REFERENCES customers(id)")
            pg_try("ALTER TABLE customer_gaits ADD COLUMN original_customer_id INTEGER REFERENCES customers(id)")
            pg_try("ALTER TABLE customer_physical_attributes ADD COLUMN original_customer_id INTEGER REFERENCES customers(id)")
            # Embedding quality + camera source for per-angle quality upgrade and camera boost
            pg_try("ALTER TABLE customer_faces ADD COLUMN quality NUMERIC(4,3)")
            pg_try("ALTER TABLE customer_faces ADD COLUMN camera_source VARCHAR(20)")

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
    app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key')

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

    db.init_app(app)

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
    from blueprints.stock        import bp as stock_bp
    from blueprints.transactions import bp as transactions_bp
    from blueprints.customers    import bp as customers_bp
    from blueprints.stats        import bp as stats_bp
    from blueprints.invoices     import bp as invoices_bp
    from blueprints.recognition  import bp as recognition_bp
    from blueprints.core         import bp as core_bp
    _app.register_blueprint(auth_bp)
    _app.register_blueprint(kiosk_bp)
    _app.register_blueprint(kitchen_bp)
    _app.register_blueprint(settings_bp)
    _app.register_blueprint(specials_bp)
    _app.register_blueprint(suppliers_bp)
    _app.register_blueprint(products_bp)
    _app.register_blueprint(stock_bp)
    _app.register_blueprint(transactions_bp)
    _app.register_blueprint(customers_bp)
    _app.register_blueprint(stats_bp)
    _app.register_blueprint(invoices_bp)
    _app.register_blueprint(recognition_bp)
    _app.register_blueprint(core_bp)


# Module-level app instance — used by gunicorn (`app:app`) and @app.route decorators.
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

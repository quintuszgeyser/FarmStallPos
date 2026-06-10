
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
    from blueprints.system       import bp as system_bp
    from blueprints.kitchen      import bp as kitchen_bp
    from blueprints.settings     import bp as settings_bp
    from blueprints.specials     import bp as specials_bp
    from blueprints.suppliers    import bp as suppliers_bp
    from blueprints.products     import bp as products_bp
    from blueprints.stock        import bp as stock_bp
    from blueprints.transactions import bp as transactions_bp
    _app.register_blueprint(auth_bp)
    _app.register_blueprint(kiosk_bp)
    _app.register_blueprint(system_bp)
    _app.register_blueprint(kitchen_bp)
    _app.register_blueprint(settings_bp)
    _app.register_blueprint(specials_bp)
    _app.register_blueprint(suppliers_bp)
    _app.register_blueprint(products_bp)
    _app.register_blueprint(stock_bp)
    _app.register_blueprint(transactions_bp)


# Module-level app instance — used by gunicorn (`app:app`) and @app.route decorators.
# Must be defined AFTER strong_migrate (below) and BEFORE the route definitions.
# create_app() is called here, which runs strong_migrate + seed on startup.




# Create the module-level app instance. strong_migrate() is defined above so
# this is safe. All @app.route decorators below bind against this instance.
app = create_app()




# -----------------------------
# Suppliers (admin)
# -----------------------------

# -----------------------------
# Customers (identification)
# -----------------------------
import json as _json

def _customer_dict(c, _extra=None):
    """Build customer dict. _extra is an optional pre-fetched row from _customer_extras_bulk."""
    if _extra:
        has_face, has_gait, has_photo, has_body, hr_avg, hr_std, plates_str = _extra
        plates = plates_str.split(',') if plates_str else []
    else:
        from sqlalchemy import text as _txt
        row = db.session.execute(_txt('''
            SELECT
              (SELECT COUNT(*) FROM customer_faces WHERE customer_id=:cid AND active=TRUE) > 0,
              (SELECT COUNT(*) FROM customer_gaits WHERE customer_id=:cid AND active=TRUE) > 0,
              (SELECT COUNT(*) FROM customer_faces WHERE customer_id=:cid AND photo IS NOT NULL) > 0,
              (SELECT COUNT(*) FROM customer_faces WHERE customer_id=:cid AND body_photo IS NOT NULL) > 0,
              (SELECT AVG(EXTRACT(HOUR FROM detected_at)) FROM customer_visits WHERE customer_id=:cid),
              (SELECT STDDEV(EXTRACT(HOUR FROM detected_at)) FROM customer_visits WHERE customer_id=:cid),
              (SELECT STRING_AGG(plate_number,',') FROM customer_plates WHERE customer_id=:cid AND active=TRUE)
        '''), {'cid': c.id}).fetchone()
        has_face, has_gait, has_photo, has_body = row[0], row[1], row[2], row[3]
        hr_avg, hr_std = row[4], row[5]
        plates = row[6].split(',') if row[6] else []
    return {
        'id': c.id, 'name': c.name, 'phone': c.phone, 'email': c.email,
        'notes': c.notes, 'visit_count': c.visit_count, 'active': c.active,
        'enrolled_at': c.enrolled_at.isoformat() if c.enrolled_at else None,
        'last_visit': c.last_visit.isoformat() if c.last_visit else None,
        'customer_number': c.customer_number,
        'auto_enrolled': c.auto_enrolled,
        'first_seen': c.first_seen.isoformat() if c.first_seen else None,
        'is_employee': c.is_employee,
        'merged_into': c.merged_into,
        'is_online_customer': c.is_online_customer,
        'is_pos_customer': c.is_pos_customer,
        'plates': plates,
        'has_face': bool(has_face),
        'has_gait': bool(has_gait),
        'has_photo': bool(has_photo),
        'has_body_photo': bool(has_body),
        'visit_hour_avg': float(hr_avg) if hr_avg is not None else None,
        'visit_hour_std': float(hr_std) if hr_std is not None else None,
    }


def _build_customer_list(customers):
    """Build list of customer dicts using a single bulk query instead of N×6 queries."""
    if not customers:
        return []
    from sqlalchemy import text as _txt
    cids = [c.id for c in customers]
    # Single query for all extras — O(1) instead of O(N×6)
    rows = db.session.execute(_txt('''
        SELECT
          c.id,
          (SELECT COUNT(*) > 0 FROM customer_faces WHERE customer_id=c.id AND active=TRUE),
          (SELECT COUNT(*) > 0 FROM customer_gaits WHERE customer_id=c.id AND active=TRUE),
          (SELECT COUNT(*) > 0 FROM customer_faces WHERE customer_id=c.id AND photo IS NOT NULL),
          (SELECT COUNT(*) > 0 FROM customer_faces WHERE customer_id=c.id AND body_photo IS NOT NULL),
          (SELECT AVG(EXTRACT(HOUR FROM detected_at)) FROM customer_visits WHERE customer_id=c.id),
          (SELECT STDDEV(EXTRACT(HOUR FROM detected_at)) FROM customer_visits WHERE customer_id=c.id),
          (SELECT STRING_AGG(plate_number, ',') FROM customer_plates WHERE customer_id=c.id AND active=TRUE)
        FROM customers c WHERE c.id = ANY(:cids)
    '''), {'cids': cids}).fetchall()
    extras = {row[0]: row[1:] for row in rows}

    # Voted physical attributes for all customers in one query
    attr_rows = db.session.execute(_txt('''
        SELECT customer_id, height_cm, hair_color, skin_tone, build, eye_color,
               age_range, gender, wearing_glasses, facial_hair, detected_at,
               camera_source, confidence, height_category
        FROM customer_physical_attributes
        WHERE customer_id = ANY(:cids)
        ORDER BY customer_id, detected_at DESC
    '''), {'cids': cids}).fetchall()

    from collections import defaultdict
    attr_by_cid = defaultdict(list)
    for r in attr_rows:
        attr_by_cid[r[0]].append(r[1:])  # strip cid from front

    def _quick_vote(rows):
        if not rows:
            return None
        from collections import Counter
        def mode_of(vals):
            counts = Counter(v for v in vals if v is not None and v != '')
            return counts.most_common(1)[0][0] if counts else None
        return {
            'gender':     mode_of([r[6]  for r in rows]),
            'build':      mode_of([r[3]  for r in rows]),
            'hair_color': mode_of([r[1]  for r in rows]),
            'age_range':  mode_of([r[5]  for r in rows]),
        }

    voted_attrs = {cid: _quick_vote(attr_by_cid[cid]) for cid in cids}

    result = []
    for c in customers:
        d = _customer_dict(c, extras.get(c.id))
        d['physical_attributes'] = voted_attrs.get(c.id)
        result.append(d)
    return result

@app.route('/api/customers', methods=['GET'])
def api_customers_get():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    customers = Customer.query.filter_by(active=True).order_by(Customer.name.asc()).all()
    return jsonify(_build_customer_list(customers))

@app.route('/api/customers/<int:cid>', methods=['GET'])
def api_customer_get_single(cid):
    """Return a single customer by id — used by recognition service for merge-chain resolution."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(_customer_dict(c))

def _resolve_online_customer(email, name, phone):
    """
    Find-or-create a Customer for an online order.
    - If email matches an existing active customer → return that customer (merge path).
    - Otherwise → create a new customer marked as online-only.
    Returns (customer, created: bool).
    """
    from sqlalchemy import text as _txt
    email_clean = (email or '').strip().lower()
    if email_clean:
        row = db.session.execute(_txt(
            "SELECT id FROM customers WHERE LOWER(TRIM(email)) = :e AND active = true LIMIT 1"
        ), {'e': email_clean}).fetchone()
        if row:
            c = db.session.get(Customer, row[0])
            if not c.is_online_customer:
                c.is_online_customer = True
                db.session.commit()
            return c, False

    # No match — create new online customer
    u = current_user()
    c = Customer(
        name=(name or '').strip() or None,
        phone=(phone or '').strip() or None,
        email=email_clean or None,
        enrolled_by=u.id if u else None,
        is_online_customer=True,
        is_pos_customer=False,
    )
    db.session.add(c)
    db.session.commit()
    return c, True


@app.route('/api/customers', methods=['POST'])
def api_customers_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}

    # Support auto-enrollment with nullable name
    name = (data.get('name') or '').strip() or None
    auto_enrolled = data.get('auto_enrolled', False)
    customer_number = data.get('customer_number')
    first_seen_str = data.get('first_seen')
    is_online = bool(data.get('is_online_customer', False))
    # Manually enrolled customers default to is_pos_customer=True unless they are online-only
    is_pos    = bool(data.get('is_pos_customer', not is_online))

    # Name is optional — a customer can be saved with just a plate, note, or phone

    u = current_user()
    c = Customer(
        name=name,
        phone=(data.get('phone') or '').strip() or None,
        email=(data.get('email') or '').strip() or None,
        notes=(data.get('notes') or '').strip() or None,
        enrolled_by=u.id if u else None,
        auto_enrolled=auto_enrolled,
        customer_number=customer_number,
        first_seen=datetime.fromisoformat(first_seen_str) if first_seen_str else None,
        is_online_customer=is_online,
        is_pos_customer=is_pos,
    )
    db.session.add(c)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': 'customer_number_conflict'}), 409
        raise
    return jsonify({'ok': True, 'id': c.id})

@app.route('/api/customers/<int:cid>', methods=['POST'])
def api_customers_update(cid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    if 'name'               in data: c.name               = (data['name'] or '').strip() or None
    if 'phone'              in data: c.phone              = (data['phone'] or '').strip() or None
    if 'email'              in data: c.email              = (data['email'] or '').strip() or None
    if 'notes'              in data: c.notes              = (data['notes'] or '').strip() or None
    if 'active'             in data: c.active             = bool(data['active'])
    if 'is_employee'        in data: c.is_employee        = bool(data['is_employee'])
    if 'is_online_customer' in data: c.is_online_customer = bool(data['is_online_customer'])
    if 'is_pos_customer'    in data: c.is_pos_customer    = bool(data['is_pos_customer'])
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/<int:cid>', methods=['DELETE'])
def api_customers_delete(cid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    c.active = False
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/cleanup_empty', methods=['POST'])
def api_customers_cleanup_empty():
    """Delete auto-enrolled customers with no face embeddings, no name, and no visits in 30 days."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    from sqlalchemy import text as _txt
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=30)
    rows = db.session.execute(_txt('''
        SELECT c.id FROM customers c
        WHERE c.auto_enrolled = TRUE
          AND c.name IS NULL
          AND c.active = TRUE
          AND NOT EXISTS (SELECT 1 FROM customer_faces WHERE customer_id=c.id AND active=TRUE)
          AND (c.last_visit IS NULL OR c.last_visit < :cutoff)
    '''), {'cutoff': cutoff}).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return jsonify({'ok': True, 'deleted': 0})
    id_list = ','.join(str(i) for i in ids)
    # Clear FK-constrained tables before deleting customers
    db.session.execute(_txt(f'DELETE FROM customer_exclusions WHERE customer_id_a IN ({id_list}) OR customer_id_b IN ({id_list})'))
    db.session.execute(_txt(f'DELETE FROM customer_merge_log WHERE source_id IN ({id_list}) OR primary_id IN ({id_list})'))
    for cid in ids:
        c = db.session.get(Customer, cid)
        if c:
            db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True, 'deleted': len(ids)})


@app.route('/api/customers/<int:cid>/delete_permanent', methods=['POST'])
def api_customers_delete_permanent(cid):
    """Permanently delete a customer and all associated data."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    from sqlalchemy import text as _text
    try:
        # Delete all associated data
        for tbl in ['customer_physical_attributes', 'customer_faces', 'customer_gaits',
                    'customer_visits', 'customer_plates', 'visit_sessions',
                    'till_detections', 'customer_signal_history']:
            db.session.execute(_text(f'DELETE FROM {tbl} WHERE customer_id = :cid'), {'cid': cid})
        # Remove merge log entries referencing this customer (as source or primary)
        db.session.execute(_text('DELETE FROM customer_merge_log WHERE source_id = :cid OR primary_id = :cid'), {'cid': cid})
        # Remove exclusion pairs referencing this customer
        db.session.execute(_text('DELETE FROM customer_exclusions WHERE customer_id_a = :cid OR customer_id_b = :cid'), {'cid': cid})
        # Unlink sales (keep the sale, just remove the customer link)
        db.session.execute(_text('UPDATE sales SET customer_id = NULL WHERE customer_id = :cid'), {'cid': cid})
        # Unlink any customers that were merged into this one
        db.session.execute(_text('UPDATE customers SET merged_into = NULL WHERE merged_into = :cid'), {'cid': cid})
        db.session.execute(_text('DELETE FROM customers WHERE id = :cid'), {'cid': cid})
        db.session.commit()

        # Tell the recognition service to immediately drop this customer from its cache
        # and purge any anonymous identities built from their embeddings.
        # This ensures the next time they walk in they start completely fresh.
        try:
            import requests as _req
            _req.post(f'{RECOGNITION_SERVICE_URL}/control/purge_customer',
                      json={'customer_id': cid}, timeout=3)
        except Exception:
            pass  # Non-fatal — cache will self-correct within 60s anyway

        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/customers/<int:cid>/profile', methods=['GET'])
def api_customer_profile(cid):
    """Comprehensive customer analytics profile."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    customer = db.session.get(Customer, cid)
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404

    # Purchase history
    sales = Sale.query.filter(
        Sale.customer_id == cid,
        Sale.voided == False
    ).order_by(Sale.date_time.desc()).all()

    # Group by sale_id (receipts)
    receipts = {}
    product_counts = {}
    total_spent = Decimal('0')

    for sale in sales:
        # Track receipts
        if sale.sale_id not in receipts:
            receipts[sale.sale_id] = {
                'sale_id': sale.sale_id,
                'date_time': sale.date_time.isoformat(),
                'total': Decimal('0'),
                'items': []
            }

        item_total = sale.qty * sale.unit_price
        receipts[sale.sale_id]['total'] += item_total
        total_spent += item_total

        receipts[sale.sale_id]['items'].append({
            'product_id': sale.product_id,
            'product_name': sale.product.name if sale.product else 'Unknown',
            'qty': float(sale.qty),
            'unit_price': float(sale.unit_price)
        })

        # Track product preferences
        pid = sale.product_id
        if pid not in product_counts:
            product_counts[pid] = {
                'product_id': pid,
                'name': sale.product.name if sale.product else 'Unknown',
                'count': 0,
                'total_spent': Decimal('0')
            }
        product_counts[pid]['count'] += 1
        product_counts[pid]['total_spent'] += item_total

    # Top products by purchase count
    top_products = sorted(
        product_counts.values(),
        key=lambda x: x['count'],
        reverse=True
    )[:10]

    # Convert Decimal to float for JSON
    for p in top_products:
        p['total_spent'] = float(p['total_spent'])

    # Visit sessions (dwell time)
    from sqlalchemy import text
    sessions_result = db.session.execute(
        text("""SELECT session_start, session_end, dwell_seconds,
                       purchase_made, sale_ids
                FROM visit_sessions
                WHERE customer_id = :cid
                ORDER BY session_start DESC
                LIMIT 20"""),
        {'cid': cid}
    ).fetchall()

    sessions = []
    total_dwell = 0
    for row in sessions_result:
        sessions.append({
            'session_start': row[0].isoformat() if row[0] else None,
            'session_end': row[1].isoformat() if row[1] else None,
            'dwell_seconds': row[2],
            'purchase_made': row[3],
            'sale_ids': row[4]
        })
        if row[2]:
            total_dwell += row[2]

    avg_dwell = total_dwell / len(sessions) if sessions else 0

    # CLV calculation
    avg_basket = float(total_spent) / len(receipts) if receipts else 0

    # Signals
    plates = [p.plate_number for p in customer.plates if p.active]
    face_enrolled = any(f.active for f in customer.faces)
    gait_enrolled = any(g.active for g in customer.gaits)

    return jsonify({
        'customer_id': cid,
        'customer_number': customer.customer_number,
        'name': customer.name,
        'phone': customer.phone,
        'email': customer.email,
        'auto_enrolled': customer.auto_enrolled,
        'first_seen': customer.first_seen.isoformat() if customer.first_seen else None,
        'last_visit': customer.last_visit.isoformat() if customer.last_visit else None,
        'visit_count': customer.visit_count,
        'total_spent': float(total_spent),
        'avg_basket': avg_basket,
        'avg_dwell_seconds': avg_dwell,
        'receipts': [
            {
                **r,
                'total': float(r['total'])
            }
            for r in receipts.values()
        ],
        'top_products': top_products,
        'recent_sessions': sessions,
        'signals': {
            'plates': plates,
            'face_enrolled': face_enrolled,
            'gait_enrolled': gait_enrolled
        }
    })

@app.route('/api/customers/<int:cid>/radar', methods=['GET'])
def api_customer_radar(cid):
    """360° customer intelligence radar — 8 non-overlapping dimensions."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    from sqlalchemy import text as _text
    import json as _json, statistics as _stats

    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404

    # ── 1. IDENTITY — face angle coverage (breadth of biometric data) ─────────
    face_angles = CustomerFace.query.filter_by(customer_id=cid, active=True).count()
    identity_score = min(1.0, face_angles / 10.0)

    # ── 2. RECOGNITION STABILITY — consistent ID across visits, not just peak ──
    sim_rows = db.session.execute(
        _text("SELECT confidence_scores FROM customer_visits WHERE customer_id=:cid AND confidence_scores IS NOT NULL ORDER BY detected_at DESC LIMIT 20"),
        {'cid': cid}
    ).fetchall()
    face_sims = []
    for (s,) in sim_rows:
        try:
            sc = _json.loads(s)
            sim = float(sc.get('face_similarity', 0) or 0)
            if sim > 0:
                face_sims.append(sim)
        except Exception:
            pass
    if face_sims:
        avg_sim = sum(face_sims) / len(face_sims)
        variance = _stats.variance(face_sims) if len(face_sims) > 1 else 0.0
        # High avg + low variance = stable. Penalise high variance.
        stability_score = min(1.0, avg_sim * (1.0 - min(1.0, variance * 5)))
    else:
        stability_score = 0.0
    best_sim = max(face_sims) if face_sims else 0.0

    # ── 3. FRESHNESS — how current and recently refreshed is this profile? ─────
    last_visit_days = None
    recency = 0.0
    if c.last_visit:
        last_visit_days = (datetime.utcnow() - c.last_visit).days
        recency = max(0.0, 1.0 - last_visit_days / 30.0)
    # Also factor in how recently the embedding was updated
    latest_face = CustomerFace.query.filter_by(customer_id=cid, active=True).order_by(
        CustomerFace.enrolled_at.desc()).first()
    emb_age_days = (datetime.utcnow() - latest_face.enrolled_at).days if latest_face else 999
    emb_freshness = max(0.0, 1.0 - emb_age_days / 14.0)  # 14 days = stale
    freshness_score = (recency * 0.6 + emb_freshness * 0.4)

    # ── 4. CONVERSION — buyer vs browser, stepped scoring ─────────────────────
    purchase_receipts = db.session.execute(
        _text("SELECT COUNT(DISTINCT sale_id) FROM sales WHERE customer_id=:cid AND voided=FALSE"),
        {'cid': cid}
    ).scalar() or 0
    if purchase_receipts == 0:
        conversion_score = 0.0
    elif purchase_receipts == 1:
        conversion_score = 0.33
    elif purchase_receipts <= 3:
        conversion_score = 0.66
    else:
        conversion_score = 1.0

    # ── 5. BASKET FAMILIARITY — how well do we know what they buy? ────────────
    product_variety = db.session.execute(
        _text("SELECT COUNT(DISTINCT product_id) FROM sales WHERE customer_id=:cid AND voided=FALSE"),
        {'cid': cid}
    ).scalar() or 0
    if purchase_receipts == 0:
        basket_score = 0.0
    elif purchase_receipts == 1:
        basket_score = 0.30
    elif purchase_receipts <= 4:
        basket_score = 0.60
    else:
        # Full basket familiarity = multiple visits with product variety
        basket_score = min(1.0, 0.60 + (product_variety / 10.0) * 0.4)

    # ── 6. PLATE CONFIDENCE — repeated confirmed plate linkage ────────────────
    plate_count = CustomerPlate.query.filter_by(customer_id=cid, active=True).count()
    if plate_count == 0:
        plate_score = 0.0
    else:
        # How many visits had a plate signal?
        plate_visits = db.session.execute(
            _text("SELECT COUNT(*) FROM customer_visits WHERE customer_id=:cid AND matched_signals LIKE '%plate%'"),
            {'cid': cid}
        ).scalar() or 0
        if plate_visits == 0:
            plate_score = 0.4   # plate enrolled but not yet matched in visits
        elif plate_visits <= 2:
            plate_score = 0.7
        else:
            plate_score = 1.0

    # ── 7. REGULARITY — predictable, repeatable visit pattern ─────────────────
    distinct_days = db.session.execute(
        _text("SELECT COUNT(DISTINCT DATE(detected_at)) FROM customer_visits WHERE customer_id=:cid"),
        {'cid': cid}
    ).scalar() or 0
    visit_count = c.visit_count or 0
    # Regularity = distinct days spread (not 15 detections in 1 hour)
    if distinct_days == 0:
        regularity_score = 0.0
    else:
        day_spread_score = min(1.0, distinct_days / 10.0)
        # Bonus for consistent pattern (visits per distinct day)
        visits_per_day = visit_count / distinct_days if distinct_days else 0
        consistency_bonus = min(0.2, visits_per_day / 10.0)
        regularity_score = min(1.0, day_spread_score + consistency_bonus)

    # ── 8. PROFILE DEPTH — meta: how rich is this profile overall? ────────────
    voted_attrs = _voted_attributes(_fetch_attr_rows(cid))
    attr_fields  = [voted_attrs[k] for k in ('hair_color','build','height_category','age_range','gender','skin_tone','eye_color','facial_hair','wearing_glasses')] if voted_attrs else []
    attrs_filled = sum(1 for v in attr_fields if v is not None and v != '' and v is not False)
    attrs_total  = len(attr_fields) or 9
    has_gait     = CustomerGait.query.filter_by(customer_id=cid, active=True).count() > 0
    has_photo    = CustomerFace.query.filter_by(customer_id=cid).filter(CustomerFace.photo != None).count() > 0
    is_named     = bool(c.name and c.name.strip())
    # Weighted composite of all data layers
    depth_score = (
        (face_angles / 10.0)           * 0.25 +  # biometric breadth
        (attrs_filled / attrs_total)   * 0.20 +  # description
        (1.0 if has_gait else 0.0)     * 0.15 +  # gait
        (1.0 if has_photo else 0.0)    * 0.15 +  # visual
        (1.0 if is_named else 0.0)     * 0.15 +  # identity
        (1.0 if plate_count > 0 else 0) * 0.10   # plate
    )
    depth_score = min(1.0, depth_score)

    return jsonify({
        'customer_id': cid,
        'name': c.name or c.customer_number,
        # Two radar charts: Biometric profile + Behavioural intelligence
        'biometric': {
            'Identity':    identity_score,
            'Stability':   stability_score,
            'Freshness':   freshness_score,
            'Attributes':  attrs_filled / attrs_total,
            'Gait':        1.0 if has_gait else 0.0,
            'Photo':       1.0 if has_photo else 0.0,
            'Named':       1.0 if is_named else 0.0,
            'Plate conf':  plate_score,
        },
        'behavioural': {
            'Conversion':  conversion_score,
            'Basket':      basket_score,
            'Regularity':  regularity_score,
            'Depth':       depth_score,
            'Plate':       plate_score,
            'Purchases':   min(1.0, purchase_receipts / 10.0),
            'Days active': min(1.0, distinct_days / 14.0),
            'Recency':     recency,
        },
        # Keep 'scores' for backward compat with existing merge modal
        'scores': {
            'Identity':    identity_score,
            'Stability':   stability_score,
            'Freshness':   freshness_score,
            'Conversion':  conversion_score,
            'Basket':      basket_score,
            'Plate':       plate_score,
            'Regularity':  regularity_score,
            'Depth':       depth_score,
        },
        'details': {
            'face_angles':       face_angles,
            'best_face_sim':     round(best_sim * 100, 1),
            'avg_face_sim':      round((sum(face_sims)/len(face_sims)*100) if face_sims else 0, 1),
            'attrs_filled':      attrs_filled,
            'attrs_total':       attrs_total,
            'plate_count':       plate_count,
            'is_named':          is_named,
            'purchase_count':    purchase_receipts,
            'product_variety':   product_variety,
            'visit_count':       visit_count,
            'distinct_days':     distinct_days,
            'last_visit_days':   last_visit_days,
            'emb_age_days':      emb_age_days if emb_age_days < 999 else None,
        }
    })

@app.route('/api/customers/<int:cid>/visits', methods=['GET'])
def api_customer_visits(cid):
    """Recent visits with signal breakdown for the detail view."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    visits = (CustomerVisit.query
              .filter_by(customer_id=cid)
              .order_by(CustomerVisit.detected_at.desc())
              .limit(20).all())
    result = []
    for v in visits:
        scores = {}
        try:
            scores = _json.loads(v.confidence_scores) if v.confidence_scores else {}
        except Exception:
            pass
        result.append({
            'id': v.id,
            'detected_at': v.detected_at.isoformat(),
            'matched_signals': v.matched_signals,
            'confidence_scores': scores,
            'camera_source': v.camera_source,
        })
    return jsonify(result)

@app.route('/api/customers/merge_suggestions', methods=['GET'])
def api_customers_merge_suggestions():
    """Compare all active face embeddings pairwise and return pairs that are
    likely the same person (cosine similarity above merge_suggest_min_sim)."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    import numpy as np, base64 as _b64
    min_sim = float(get_setting('merge_suggest_min_sim', 0.75) or 0.75)

    # Load ALL embeddings per customer and take the best pairwise sim.
    # Single-embedding comparison produces noisy matches for multi-angle customers.
    customers = Customer.query.filter_by(active=True).all()
    embeddings = {}
    for c in customers:
        if c.is_employee:
            continue  # employees move through store all day — never suggest merging them
        rows = CustomerFace.query.filter_by(customer_id=c.id, active=True).all()
        embs = []
        for row in rows:
            if row.embedding and len(row.embedding) == 2048:
                emb = np.frombuffer(row.embedding, dtype=np.float32)
                norm = np.linalg.norm(emb)
                if norm > 0:
                    embs.append(emb / norm)
        if embs:
            embeddings[c.id] = (embs, c)

    # Load excluded pairs so we never suggest them again
    excl_rows = db.session.execute(
        db.text("SELECT customer_id_a, customer_id_b FROM customer_exclusions")
    ).fetchall()
    excluded = {(min(r[0], r[1]), max(r[0], r[1])) for r in excl_rows}

    cids = list(embeddings.keys())
    suggestions = []
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            a_id, b_id = cids[i], cids[j]
            if (min(a_id, b_id), max(a_id, b_id)) in excluded:
                continue
            a_embs, a_c = embeddings[a_id]
            b_embs, b_c = embeddings[b_id]
            # Best-of-N comparison: use top 5 embeddings per customer to limit O(n²)
            a_top = sorted(a_embs, key=lambda e: np.linalg.norm(e), reverse=True)[:5]
            b_top = sorted(b_embs, key=lambda e: np.linalg.norm(e), reverse=True)[:5]
            sim = max(float(np.dot(a, b)) for a in a_top for b in b_top)
            if sim >= min_sim:
                suggestions.append({
                    'similarity': round(sim, 3),
                    'customer_a': {'id': a_c.id, 'customer_number': a_c.customer_number,
                                   'name': a_c.name, 'visit_count': a_c.visit_count},
                    'customer_b': {'id': b_c.id, 'customer_number': b_c.customer_number,
                                   'name': b_c.name, 'visit_count': b_c.visit_count},
                })
    suggestions.sort(key=lambda x: x['similarity'], reverse=True)
    return jsonify(suggestions)

@app.route('/api/customers/exclusions', methods=['POST'])
def api_customers_add_exclusion():
    """Mark two customers as definitely NOT the same person.
    Future merge suggestions for this pair will be suppressed."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json() or {}
    id_a = data.get('customer_a_id')
    id_b = data.get('customer_b_id')
    reason = (data.get('reason') or 'Declined by user')[:200]
    if not id_a or not id_b or id_a == id_b:
        return jsonify({'error': 'Two distinct customer IDs required'}), 400
    # Normalise order so (a,b) and (b,a) are the same pair
    lo, hi = min(id_a, id_b), max(id_a, id_b)
    # Upsert — ignore if already excluded
    existing = db.session.execute(
        db.text("SELECT id FROM customer_exclusions WHERE customer_id_a=:a AND customer_id_b=:b"),
        {'a': lo, 'b': hi}
    ).fetchone()
    if not existing:
        db.session.execute(
            db.text("INSERT INTO customer_exclusions (customer_id_a, customer_id_b, reason) VALUES (:a, :b, :r)"),
            {'a': lo, 'b': hi, 'r': reason}
        )
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/<int:cid>/name', methods=['POST'])
def api_customer_name(cid):
    """Quick-name a customer from the till."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    c.name = name
    db.session.commit()
    return jsonify({'ok': True})

def _merge_primary_score(row):
    """Score a customer row to determine who should be primary on merge.
    Higher score = more established customer = keep as primary.
    Columns: id, name, visit_count, has_face, has_gait, first_seen
    """
    score = 0
    score += (row[2] or 0) * 10       # visit_count × 10
    if row[1]:      score += 100       # has name
    if row[3]:      score += 50        # has face
    if row[4]:      score += 30        # has gait
    if row[5]:                         # older first_seen = more established
        from datetime import timezone
        try:
            fs = row[5]
            if hasattr(fs, 'replace'):
                age_days = (datetime.utcnow() - fs.replace(tzinfo=None)).days
                score += min(age_days, 365)
        except Exception:
            pass
    return score

@app.route('/api/customers/merge_suggest_primary', methods=['POST'])
def api_merge_suggest_primary():
    """Given a list of customer ids, return which one should be primary and the score breakdown."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    ids = data.get('ids', [])
    if len(ids) < 2:
        return jsonify({'error': 'Need at least 2 ids'}), 400

    from sqlalchemy import text as _text
    rows = []
    for cid in ids:
        row = db.session.execute(_text('''
            SELECT c.id, c.name, c.visit_count,
                   EXISTS(SELECT 1 FROM customer_faces WHERE customer_id=c.id AND active=TRUE) has_face,
                   EXISTS(SELECT 1 FROM customer_gaits WHERE customer_id=c.id AND active=TRUE) has_gait,
                   c.first_seen
            FROM customers c WHERE c.id = :id
        '''), {'id': cid}).fetchone()
        if row:
            rows.append(row)

    if not rows:
        return jsonify({'error': 'No valid customers'}), 404

    scored = [(r, _merge_primary_score(r)) for r in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    primary_row, primary_score = scored[0]

    reasons = []
    if primary_row[1]: reasons.append('named')
    if primary_row[2]: reasons.append(f'{primary_row[2]} visits')
    if primary_row[3]: reasons.append('face enrolled')
    if primary_row[4]: reasons.append('gait enrolled')

    return jsonify({
        'primary_id': primary_row[0],
        'reason': ', '.join(reasons) if reasons else 'best candidate',
        'scores': [{'id': r[0], 'score': s} for r, s in scored],
    })

@app.route('/api/customers/merge', methods=['POST'])
def api_customers_merge():
    """Merge multiple customers into one primary customer.
    primary_id is optional — if omitted, auto-selected by score.
    Moves all faces, gaits, plates, visits, physical_attributes, visit_sessions, and sales to the primary.
    Tags each face/gait row with original_customer_id for future unmerge.
    Writes a customer_merge_log entry per source customer.
    """
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    primary_id = data.get('primary_id')
    merge_ids = data.get('merge_ids', [])
    auto_merged = data.get('auto_merged', False)
    similarity = data.get('similarity')

    all_ids = ([primary_id] if primary_id else []) + list(merge_ids)
    if len(all_ids) < 2:
        return jsonify({'error': 'Need at least 2 customer ids'}), 400

    from sqlalchemy import text as _text

    # Auto-select primary if not provided
    if not primary_id:
        rows = []
        for cid in all_ids:
            row = db.session.execute(_text('''
                SELECT c.id, c.name, c.visit_count,
                       EXISTS(SELECT 1 FROM customer_faces WHERE customer_id=c.id AND active=TRUE),
                       EXISTS(SELECT 1 FROM customer_gaits WHERE customer_id=c.id AND active=TRUE),
                       c.first_seen
                FROM customers c WHERE c.id = :id
            '''), {'id': cid}).fetchone()
            if row:
                rows.append(row)
        rows.sort(key=_merge_primary_score, reverse=True)
        primary_id = rows[0][0]
        merge_ids = [r[0] for r in rows[1:]]
    else:
        if not merge_ids:
            return jsonify({'error': 'merge_ids required when primary_id provided'}), 400

    try:
        # Verify primary exists
        row = db.session.execute(_text('SELECT id FROM customers WHERE id = :id'), {'id': primary_id}).fetchone()
        if not row:
            return jsonify({'error': 'Primary customer not found'}), 404

        merged_count = 0
        for mid in merge_ids:
            if mid == primary_id:
                continue
            src_row = db.session.execute(_text('''
                SELECT id, visit_count, last_visit, first_seen, name, phone, email, notes, is_employee
                FROM customers WHERE id = :id
            '''), {'id': mid}).fetchone()
            if not src_row:
                continue

            # Capture source face photo for merge log (best = largest JPEG)
            src_photo_row = db.session.execute(_text('''
                SELECT photo FROM customer_faces
                WHERE customer_id = :sid AND active = TRUE AND photo IS NOT NULL
                ORDER BY LENGTH(photo) DESC LIMIT 1
            '''), {'sid': mid}).fetchone()
            src_face_photo = src_photo_row[0] if src_photo_row else None

            # TAG face/gait/attrs rows with their origin before moving — enables unmerge
            # Only set original_customer_id if not already set (preserves deeper merge chains)
            db.session.execute(_text('''
                UPDATE customer_faces SET original_customer_id = :sid
                WHERE customer_id = :sid AND original_customer_id IS NULL
            '''), {'sid': mid})
            db.session.execute(_text('''
                UPDATE customer_gaits SET original_customer_id = :sid
                WHERE customer_id = :sid AND original_customer_id IS NULL
            '''), {'sid': mid})
            db.session.execute(_text('''
                UPDATE customer_physical_attributes SET original_customer_id = :sid
                WHERE customer_id = :sid AND original_customer_id IS NULL
            '''), {'sid': mid})

            # Move biometrics and visits
            db.session.execute(_text('UPDATE customer_faces              SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(_text('UPDATE customer_gaits              SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(_text('UPDATE customer_visits             SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(_text('UPDATE customer_physical_attributes SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(_text('UPDATE visit_sessions              SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})
            db.session.execute(_text('UPDATE sales                       SET customer_id = :pid WHERE customer_id = :sid'), {'pid': primary_id, 'sid': mid})

            # Plates: skip any that would duplicate an existing plate on the primary
            db.session.execute(_text('''
                UPDATE customer_plates SET customer_id = :pid
                WHERE customer_id = :sid
                AND plate_number NOT IN (
                    SELECT plate_number FROM customer_plates WHERE customer_id = :pid
                )
            '''), {'pid': primary_id, 'sid': mid})
            # Delete any remaining plates on the source (duplicates)
            db.session.execute(_text('DELETE FROM customer_plates WHERE customer_id = :sid'), {'sid': mid})

            # Roll up visit stats on primary
            db.session.execute(_text('''
                UPDATE customers SET
                    visit_count = visit_count + :src_vc,
                    last_visit  = GREATEST(last_visit,  :src_lv),
                    first_seen  = LEAST(first_seen, :src_fs)
                WHERE id = :pid
            '''), {
                'pid':    primary_id,
                'src_vc': src_row[1] or 0,
                'src_lv': src_row[2],
                'src_fs': src_row[3],
            })
            # Fill in any missing fields on the primary from the source
            pri = db.session.execute(
                _text('SELECT name, phone, email, notes, is_employee FROM customers WHERE id = :pid'),
                {'pid': primary_id}
            ).fetchone()
            updates = {}
            if src_row[4] and not pri[0]:   updates['name']  = src_row[4]
            if src_row[5] and not pri[1]:   updates['phone'] = src_row[5]
            if src_row[6] and not pri[2]:   updates['email'] = src_row[6]
            if src_row[7] and not pri[3]:   updates['notes'] = src_row[7]
            if src_row[8] and not pri[4]:   updates['is_employee'] = True
            if updates:
                set_clause = ', '.join(f'{k} = :{k}' for k in updates)
                updates['pid'] = primary_id
                db.session.execute(_text(f'UPDATE customers SET {set_clause} WHERE id = :pid'), updates)

            # Deactivate source and set merged_into
            db.session.execute(_text('UPDATE customers SET active = FALSE, merged_into = :pid WHERE id = :sid'), {'pid': primary_id, 'sid': mid})

            # Write merge audit log
            db.session.execute(_text('''
                INSERT INTO customer_merge_log
                    (primary_id, source_id, merged_at, auto_merged, similarity,
                     source_name, source_customer_number, source_visit_count, source_face_photo)
                VALUES (:pid, :sid, NOW(), :auto, :sim, :name, :cnum, :vc, :photo)
            '''), {
                'pid':   primary_id,
                'sid':   mid,
                'auto':  auto_merged,
                'sim':   float(similarity) if similarity is not None else None,
                'name':  src_row[4],
                'cnum':  db.session.execute(_text('SELECT customer_number FROM customers WHERE id=:id'), {'id': mid}).scalar(),
                'vc':    src_row[1],
                'photo': src_face_photo,
            })

            merged_count += 1

        # After all merges: compute a centroid embedding from all active face rows,
        # paired with the best available photo (largest JPEG = sharpest crop).
        # The centroid is the L2-normalised mean of all embeddings — it represents
        # the "average appearance" across captures and matches better than any single one.
        import numpy as np, base64 as _b64
        face_rows = db.session.execute(_text('''
            SELECT id, embedding, photo FROM customer_faces
            WHERE customer_id = :pid AND active = TRUE
        '''), {'pid': primary_id}).fetchall()

        if face_rows:
            # Decode all embeddings (each is 2048 bytes = 512 float32 values)
            embeddings = []
            for row in face_rows:
                emb = np.frombuffer(row[1], dtype=np.float32)
                if emb.shape == (512,):
                    embeddings.append(emb)

            # Best photo = largest JPEG among rows that have one
            best_photo = None
            for row in sorted(face_rows, key=lambda r: len(r[2]) if r[2] else 0, reverse=True):
                if row[2]:
                    best_photo = row[2]
                    break

            if embeddings:
                # Select up to MAX_EMBEDDINGS distinct-angle embeddings from all
                # merged faces. Like building an iPhone fingerprint: keep each
                # angle that covers new ground (cosine distance > MIN_DISTANCE).
                MAX_EMBEDDINGS  = int(float(get_setting('max_face_angles', 24) or 24))
                MIN_DISTANCE    = float(get_setting('min_angle_distance', 0.25) or 0.25)

                # Normalise all embeddings — keep original bytes for DB insert
                normed = []
                for raw_emb, row_id, row_photo in zip(
                    embeddings,
                    [r[0] for r in face_rows],
                    [r[2] for r in face_rows]
                ):
                    n = np.linalg.norm(raw_emb)
                    if n > 0:
                        raw_bytes = bytes(raw_emb.tobytes())  # ensure plain bytes for psycopg
                        normed.append((raw_emb / n, raw_bytes, row_photo))

                # Greedy selection of distinct angles (best quality = longest path through space)
                selected = []   # [(normed_emb, raw_bytes, photo)]
                for normed_emb, raw_bytes, photo in normed:
                    is_new = True
                    for sel_normed, _, _ in selected:
                        if float(np.dot(normed_emb, sel_normed)) > (1.0 - MIN_DISTANCE):
                            is_new = False
                            break
                    if is_new:
                        selected.append((normed_emb, raw_bytes, photo))
                    if len(selected) >= MAX_EMBEDDINGS:
                        break

                # Deactivate all existing face rows for this customer
                db.session.execute(_text(
                    'UPDATE customer_faces SET active = FALSE WHERE customer_id = :pid'
                ), {'pid': primary_id})

                # Re-insert the selected distinct-angle embeddings
                for _, raw_bytes, photo in selected:
                    db.session.execute(_text('''
                        INSERT INTO customer_faces (customer_id, embedding, photo, enrolled_at, active)
                        VALUES (:pid, :emb, :photo, NOW(), TRUE)
                    '''), {'pid': primary_id, 'emb': raw_bytes, 'photo': photo})

                app.logger.info(
                    f'Merge [{primary_id}]: {len(selected)} distinct angles '
                    f'selected from {len(embeddings)} total embeddings'
                )

        db.session.commit()
        return jsonify({'ok': True, 'merged': merged_count, 'primary_id': primary_id})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Merge error: {e}')
        return jsonify({'error': str(e)}), 500


def _recompute_customer_embeddings(customer_id, _text_fn):
    """Reselect distinct-angle embeddings from all face rows for a customer.
    Deactivates all existing rows, re-inserts only the selected distinct angles.
    """
    import numpy as _np
    MAX_EMBEDDINGS = int(float(get_setting('max_face_angles', 24) or 24))
    MIN_DISTANCE   = float(get_setting('min_angle_distance', 0.25) or 0.25)

    face_rows = db.session.execute(_text_fn('''
        SELECT id, embedding, photo, original_customer_id FROM customer_faces
        WHERE customer_id = :cid AND active = TRUE
    '''), {'cid': customer_id}).fetchall()

    if not face_rows:
        return 0

    normed = []
    for row in face_rows:
        emb = _np.frombuffer(row[1], dtype=_np.float32)
        if emb.shape == (512,):
            n = _np.linalg.norm(emb)
            if n > 0:
                normed.append((emb / n, bytes(emb.tobytes()), row[2], row[3]))

    selected = []
    for normed_emb, raw_bytes, photo, orig_cid in normed:
        is_new = all(float(_np.dot(normed_emb, s[0])) < (1.0 - MIN_DISTANCE) for s in selected)
        if is_new:
            selected.append((normed_emb, raw_bytes, photo, orig_cid))
        if len(selected) >= MAX_EMBEDDINGS:
            break

    db.session.execute(_text_fn('UPDATE customer_faces SET active = FALSE WHERE customer_id = :cid'), {'cid': customer_id})
    for _, raw_bytes, photo, orig_cid in selected:
        db.session.execute(_text_fn('''
            INSERT INTO customer_faces (customer_id, embedding, photo, enrolled_at, active, original_customer_id)
            VALUES (:cid, :emb, :photo, NOW(), TRUE, :orig)
        '''), {'cid': customer_id, 'emb': raw_bytes, 'photo': photo, 'orig': orig_cid})

    return len(selected)


@app.route('/api/customers/merge_log/<int:log_id>/unmerge', methods=['POST'])
def api_customers_unmerge(log_id):
    """Reverse a merge. Reactivates the source customer and moves their face/gait/attrs back.
    For merges done after the original_customer_id tracking was added, biometrics are fully restored.
    For older merges (no tags), the source is reactivated with no biometrics — they re-enroll naturally.
    Visit and sales history stay on the primary (cannot be reliably split).
    """
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    from sqlalchemy import text as _text
    try:
        log = db.session.execute(_text('''
            SELECT id, primary_id, source_id, unmerged_at FROM customer_merge_log WHERE id = :id
        '''), {'id': log_id}).fetchone()

        if not log:
            return jsonify({'error': 'Merge log entry not found'}), 404
        if log[3] is not None:
            return jsonify({'error': 'Already unmerged'}), 400

        primary_id = log[1]
        source_id  = log[2]

        # Check source still points to this primary (guard against re-merges)
        src = db.session.execute(_text(
            'SELECT id, merged_into FROM customers WHERE id = :id'
        ), {'id': source_id}).fetchone()
        if not src:
            return jsonify({'error': 'Source customer not found'}), 404

        # Move face rows back to source
        moved_faces = db.session.execute(_text('''
            UPDATE customer_faces SET customer_id = :sid
            WHERE customer_id = :pid AND original_customer_id = :sid
        '''), {'pid': primary_id, 'sid': source_id}).rowcount

        # Move gait rows back
        db.session.execute(_text('''
            UPDATE customer_gaits SET customer_id = :sid
            WHERE customer_id = :pid AND original_customer_id = :sid
        '''), {'pid': primary_id, 'sid': source_id})

        # Move physical attributes back
        db.session.execute(_text('''
            UPDATE customer_physical_attributes SET customer_id = :sid
            WHERE customer_id = :pid AND original_customer_id = :sid
        '''), {'pid': primary_id, 'sid': source_id})

        # Reactivate source customer
        db.session.execute(_text('''
            UPDATE customers SET active = TRUE, merged_into = NULL WHERE id = :sid
        '''), {'sid': source_id})

        # Recompute embeddings for both customers from their own rows
        _recompute_customer_embeddings(source_id,  _text)
        _recompute_customer_embeddings(primary_id, _text)

        # Stamp unmerge time on log
        db.session.execute(_text('''
            UPDATE customer_merge_log SET unmerged_at = NOW() WHERE id = :id
        '''), {'id': log_id})

        soft = moved_faces == 0
        db.session.commit()
        return jsonify({'ok': True, 'soft_unmerge': soft,
                        'message': 'Customer reactivated. Biometric data will rebuild automatically.' if soft
                                   else 'Customer reactivated with their original biometric data.'})

    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Unmerge error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/customers/<int:cid>/merge_history', methods=['GET'])
def api_customer_merge_history(cid):
    """Returns full merge history for a customer — both as primary and as source."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    import base64 as _b64
    from sqlalchemy import text as _text

    # Merges where this customer is the PRIMARY (absorbed others)
    absorbed = db.session.execute(_text('''
        SELECT ml.id, ml.source_id, ml.merged_at, ml.auto_merged, ml.similarity,
               ml.source_name, ml.source_customer_number, ml.source_visit_count,
               ml.source_face_photo, ml.unmerged_at,
               c.active as source_active
        FROM customer_merge_log ml
        LEFT JOIN customers c ON c.id = ml.source_id
        WHERE ml.primary_id = :cid
        ORDER BY ml.merged_at DESC
    '''), {'cid': cid}).fetchall()

    # Merge where this customer IS the source (was merged into another)
    is_source = db.session.execute(_text('''
        SELECT ml.id, ml.primary_id, ml.merged_at, ml.auto_merged, ml.similarity,
               ml.unmerged_at,
               c.name as primary_name, c.customer_number as primary_number
        FROM customer_merge_log ml
        JOIN customers c ON c.id = ml.primary_id
        WHERE ml.source_id = :cid
        ORDER BY ml.merged_at DESC
        LIMIT 1
    '''), {'cid': cid}).fetchone()

    def fmt_photo(photo_bytes):
        if not photo_bytes:
            return None
        try:
            return 'data:image/jpeg;base64,' + _b64.b64encode(bytes(photo_bytes)).decode()
        except Exception:
            return None

    return jsonify({
        'absorbed': [{
            'log_id':                row[0],
            'source_id':             row[1],
            'merged_at':             row[2].isoformat() if row[2] else None,
            'auto_merged':           row[3],
            'similarity':            float(row[4]) if row[4] is not None else None,
            'source_name':           row[5],
            'source_customer_number': row[6],
            'source_visit_count':    row[7],
            'source_face_photo':     fmt_photo(row[8]),
            'unmerged_at':           row[9].isoformat() if row[9] else None,
            'source_active':         row[10],
        } for row in absorbed],
        'merged_into': {
            'log_id':           is_source[0],
            'primary_id':       is_source[1],
            'merged_at':        is_source[2].isoformat() if is_source[2] else None,
            'unmerged_at':      is_source[5].isoformat() if is_source[5] else None,
            'primary_name':     is_source[6],
            'primary_number':   is_source[7],
        } if is_source and not is_source[5] else None,
    })


@app.route('/api/customers/<int:cid>/enroll/plate', methods=['POST'])
def api_customers_enroll_plate(cid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    plate = data.get('plate_number', '').strip().upper()
    if not plate:
        return jsonify({'error': 'plate_number required'}), 400
    existing = CustomerPlate.query.filter_by(plate_number=plate).first()
    if existing:
        return jsonify({'error': 'Plate already enrolled'}), 409
    db.session.add(CustomerPlate(customer_id=cid, plate_number=plate))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/<int:cid>/enroll/plate/<int:pid>', methods=['DELETE'])
def api_customers_delete_plate(cid, pid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    cp = db.session.get(CustomerPlate, pid)
    if not cp or cp.customer_id != cid:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(cp)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/<int:cid>/enroll/face', methods=['POST'])
def api_customers_enroll_face(cid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    embedding_b64 = data.get('embedding_b64')
    if not embedding_b64:
        return jsonify({'error': 'embedding_b64 required'}), 400
    import base64
    embedding_bytes = base64.b64decode(embedding_b64)
    photo_b64 = data.get('photo_b64')
    photo_bytes = base64.b64decode(photo_b64) if photo_b64 else None
    snapshot_only = data.get('snapshot_only', False)
    camera_source_val = data.get('camera_source') or None

    body_photo_b64 = data.get('body_photo_b64')
    body_photo_bytes = base64.b64decode(body_photo_b64) if body_photo_b64 else None

    if snapshot_only:
        # Store/update body snapshot for display without affecting matching embeddings.
        # Since we now crop to the person bounding box, every snapshot has the person
        # in it — always update to the latest so the photo stays current.
        new_area = float(data.get('snapshot_area', 0.0))
        existing_body_row = (CustomerFace.query
                             .filter_by(customer_id=cid)
                             .filter(CustomerFace.body_photo != None)
                             .order_by(CustomerFace.enrolled_at.desc())
                             .first())
        if body_photo_bytes:
            if not existing_body_row:
                db.session.add(CustomerFace(
                    customer_id=cid, embedding=embedding_bytes,
                    photo=photo_bytes, body_photo=body_photo_bytes, active=False
                ))
            else:
                # Always update — person is always in frame now (bounding box crop)
                existing_body_row.body_photo = body_photo_bytes
                if photo_bytes:
                    existing_body_row.photo = photo_bytes
            db.session.commit()
    else:
        # Multi-angle enrollment: add new embedding only if it's distinct from
        # existing ones (fills a gap). Like iPhone fingerprint — keep lifting and
        # re-presenting from new angles until full angular coverage is built up.
        import numpy as np
        MAX_EMBEDDINGS   = int(float(get_setting('max_face_angles', 24) or 24))
        MIN_DISTANCE     = float(get_setting('min_angle_distance', 0.25) or 0.25)
        replace_if_better = data.get('replace_if_better', False)
        new_quality       = float(data.get('quality', 0.0))

        new_emb = np.frombuffer(embedding_bytes, dtype=np.float32).copy()
        norm = np.linalg.norm(new_emb)
        if norm > 0:
            new_emb /= norm

        existing = CustomerFace.query.filter_by(customer_id=cid, active=True).all()
        is_new_angle = True
        replaced = False
        for row in existing:
            stored = np.frombuffer(row.embedding, dtype=np.float32).copy()
            s_norm = np.linalg.norm(stored)
            if s_norm > 0:
                stored /= s_norm
            sim = float(np.dot(new_emb, stored))
            if sim > (1.0 - MIN_DISTANCE):  # same angle region
                stored_quality = float(row.quality) if row.quality else 0.0
                if (replace_if_better and
                        new_quality > 0 and
                        new_quality > stored_quality + 0.10):
                    # Replace with higher-quality embedding (hysteresis: +0.10 margin)
                    row.active = False
                    db.session.flush()
                    db.session.add(CustomerFace(
                        customer_id=cid, embedding=embedding_bytes,
                        photo=photo_bytes or row.photo,
                        body_photo=body_photo_bytes,
                        quality=new_quality,
                        camera_source=camera_source_val,
                    ))
                    replaced = True
                    is_new_angle = False
                else:
                    # Same angle, not better enough — just update photo if sharper
                    if photo_bytes and (not row.photo or len(photo_bytes) > len(row.photo)):
                        row.photo = photo_bytes
                    is_new_angle = False
                break

        if is_new_angle and not replaced:
            if len(existing) >= MAX_EMBEDDINGS:
                # Drop the oldest embedding to make room
                oldest = min(existing, key=lambda r: r.enrolled_at)
                oldest.active = False

            db.session.add(CustomerFace(
                customer_id=cid, embedding=embedding_bytes,
                photo=photo_bytes, body_photo=body_photo_bytes,
                quality=new_quality if new_quality > 0 else None,
                camera_source=camera_source_val,
            ))

        db.session.commit()
    return jsonify({'ok': True, 'new_angle': is_new_angle if not snapshot_only else None})

@app.route('/api/customers/<int:cid>/photo', methods=['GET'])
def api_customer_photo(cid):
    """Returns face photo (or body snapshot fallback) as JPEG.
    Selects by quality DESC so the sharpest, most reliable face wins.
    Skips tiny photos (< 4KB) that are likely non-face crops (hands, objects)."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    from flask import Response
    from sqlalchemy import text as _t2

    # Best quality active face photo — must be a real face (≥4KB, quality set)
    rows = (CustomerFace.query
            .filter_by(customer_id=cid, active=True)
            .filter(CustomerFace.photo != None)
            .filter(CustomerFace.quality != None)
            .order_by(CustomerFace.quality.desc())
            .all())
    for row in rows:
        if row.photo and len(row.photo) >= 4000:
            return Response(row.photo, mimetype='image/jpeg')

    # Fall back: any active face photo regardless of size
    for row in rows:
        if row.photo:
            return Response(row.photo, mimetype='image/jpeg')

    # Fall back: any active face photo without quality metadata (older enrollments)
    row = (CustomerFace.query
           .filter_by(customer_id=cid, active=True)
           .filter(CustomerFace.photo != None)
           .filter(CustomerFace.quality == None)
           .order_by(CustomerFace.enrolled_at.desc())
           .first())
    if row and row.photo:
        return Response(row.photo, mimetype='image/jpeg')

    # Last resort: body snapshot as profile picture
    snap_row = (CustomerFace.query.filter_by(customer_id=cid)
                .filter(CustomerFace.body_photo != None)
                .order_by(CustomerFace.enrolled_at.desc()).first())
    if snap_row and snap_row.body_photo:
        return Response(snap_row.body_photo, mimetype='image/jpeg')

    return '', 404

@app.route('/api/customers/<int:cid>/body_photo', methods=['GET'])
def api_customer_body_photo(cid):
    """Returns full-body snapshot thumbnail as JPEG."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    row = (CustomerFace.query.filter_by(customer_id=cid)
           .filter(CustomerFace.body_photo != None)
           .order_by(CustomerFace.enrolled_at.desc()).first())
    if not row or not row.body_photo:
        return '', 404
    from flask import Response
    return Response(row.body_photo, mimetype='image/jpeg')

@app.route('/api/customers/<int:cid>/enroll/gait', methods=['POST'])
def api_customers_enroll_gait(cid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    features_b64 = data.get('features_b64')
    if not features_b64:
        return jsonify({'error': 'features_b64 required'}), 400
    import base64
    features_bytes = base64.b64decode(features_b64)
    CustomerGait.query.filter_by(customer_id=cid).update({'active': False})
    db.session.add(CustomerGait(customer_id=cid, gait_features=features_bytes))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/identify', methods=['POST'])
def api_customers_identify():
    """Called by recognition_service to log a visit when a customer is identified."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    cid             = data.get('customer_id')
    matched_signals = data.get('matched_signals', '')
    confidence_scores = data.get('confidence_scores')
    camera_source   = data.get('camera_source')
    if not cid:
        return jsonify({'error': 'customer_id required'}), 400
    c = db.session.get(Customer, cid)
    if not c:
        return jsonify({'error': 'Not found'}), 404
    # If this customer was merged, log the visit against the primary instead
    if not c.active and c.merged_into:
        primary = db.session.get(Customer, c.merged_into)
        if primary and primary.active:
            c = primary
            cid = c.id

    # Dedup: skip if a visit from the same camera was logged very recently.
    # Guards against the recognition service firing multiple times per person.
    from sqlalchemy import text as _t
    visit_min_gap = int(float(get_setting('visit_min_gap_seconds', 180) or 180))
    if camera_source:
        recent = db.session.execute(_t('''
            SELECT detected_at FROM customer_visits
            WHERE customer_id = :cid AND camera_source = :cam
            ORDER BY detected_at DESC LIMIT 1
        '''), {'cid': cid, 'cam': camera_source}).fetchone()
        if recent and recent[0]:
            gap = (datetime.utcnow() - recent[0]).total_seconds()
            if gap < visit_min_gap:
                return jsonify({'ok': True, 'skipped': True, 'reason': 'too_soon', 'gap_seconds': int(gap)})

    visit = CustomerVisit(
        customer_id=cid,
        matched_signals=matched_signals,
        confidence_scores=_json.dumps(confidence_scores) if confidence_scores else None,
        camera_source=camera_source,
    )
    db.session.add(visit)
    c.visit_count = (c.visit_count or 0) + 1
    c.last_visit = datetime.utcnow()
    if not c.is_pos_customer:
        c.is_pos_customer = True

    # Write a visit_sessions row when dwell time is provided by the recognition service
    dwell_seconds = data.get('dwell_seconds')
    if dwell_seconds and int(dwell_seconds) > 0:
        from sqlalchemy import text as _t2
        now_utc = datetime.utcnow()
        db.session.execute(_t2("""
            INSERT INTO visit_sessions
                (customer_id, session_start, session_end, entry_camera, dwell_seconds)
            VALUES (:cid, :start, :end, :cam, :dwell)
        """), {
            'cid': cid,
            'start': now_utc - timedelta(seconds=int(dwell_seconds)),
            'end': now_utc,
            'cam': camera_source,
            'dwell': int(dwell_seconds),
        })

    db.session.commit()
    return jsonify({'ok': True, 'visit_id': visit.id})

@app.route('/api/customers/log_plate', methods=['POST'])
def api_customers_log_plate():
    """Called by recognition_service to log every plate detection (matched or not)."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    pd = PlateDetection(
        plate_number=data.get('plate_number', '').upper(),
        confidence=data.get('confidence'),
        customer_id=data.get('customer_id'),
        matched=bool(data.get('matched', False)),
        snapshot_path=data.get('snapshot_path'),
        camera_source=data.get('camera_source'),
    )
    db.session.add(pd)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/pending_visits', methods=['GET'])
def api_customers_pending_visits():
    """Returns unacknowledged visits from the last 5 minutes for teller greeting."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    visits = (CustomerVisit.query
              .filter_by(acknowledged=False)
              .filter(CustomerVisit.detected_at >= cutoff)
              .order_by(CustomerVisit.detected_at.desc())
              .all())
    result = []
    seen_customers = set()  # deduplicate — one greeting per customer per poll
    for v in visits:
        c = db.session.get(Customer, v.customer_id)
        if not c:
            continue
        # If this customer was merged, follow to the primary
        if not c.active and c.merged_into:
            primary = db.session.get(Customer, c.merged_into)
            if primary and primary.active:
                c = primary
        if not c.active:
            continue
        if not (c.name or c.customer_number):
            continue
        if c.id in seen_customers:
            continue
        seen_customers.add(c.id)
        result.append({
            'id': v.id,
            'customer_name': c.name or c.customer_number,
            'visit_count': c.visit_count,
            'matched_signals': v.matched_signals,
            'detected_at': v.detected_at.isoformat(),
        })
    return jsonify(result)

@app.route('/api/customers/visits/<int:vid>/acknowledge', methods=['POST'])
def api_customers_acknowledge_visit(vid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    v = db.session.get(CustomerVisit, vid)
    if v:
        v.acknowledged = True
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/faces_raw', methods=['GET'])
def api_customers_faces_raw():
    """Internal: returns all active face embeddings as base64 for the recognition service."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    import base64
    rows = CustomerFace.query.filter_by(active=True).all()
    return jsonify([{'customer_id': r.customer_id, 'embedding_b64': base64.b64encode(r.embedding).decode()} for r in rows])

@app.route('/api/customers/gaits_raw', methods=['GET'])
def api_customers_gaits_raw():
    """Internal: returns all active gait features as base64 for the recognition service."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    import base64
    rows = CustomerGait.query.filter_by(active=True).all()
    return jsonify([{'customer_id': r.customer_id, 'features_b64': base64.b64encode(r.gait_features).decode()} for r in rows])

@app.route('/api/customers/<int:cid>/faces_raw', methods=['GET'])
def api_customer_faces_raw(cid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    import base64 as _b64
    # Return up to 5 active embeddings (distinct angles) for multi-embedding matching
    rows = (CustomerFace.query
            .filter_by(customer_id=cid, active=True)
            .order_by(CustomerFace.enrolled_at.desc())
            .limit(10).all())
    return jsonify([{
        'embedding_b64': _b64.b64encode(r.embedding).decode(),
        'camera': r.camera_source,
        'quality': float(r.quality) if r.quality is not None else None,
    } for r in rows])

@app.route('/api/customers/<int:cid>/gaits_raw', methods=['GET'])
def api_customer_gaits_raw(cid):
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    import base64 as _b64
    rows = CustomerGait.query.filter_by(customer_id=cid, active=True).all()
    return jsonify([{'features_b64': _b64.b64encode(r.gait_features).decode()} for r in rows])

@app.route('/api/customers/plate_log', methods=['GET'])
def api_customers_plate_log():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    limit = int(request.args.get('limit', 50))
    rows = PlateDetection.query.order_by(PlateDetection.detected_at.desc()).limit(limit).all()
    return jsonify([{
        'id': r.id, 'plate_number': r.plate_number, 'confidence': float(r.confidence) if r.confidence else None,
        'detected_at': r.detected_at.isoformat(), 'customer_id': r.customer_id,
        'matched': r.matched, 'camera_source': r.camera_source,
    } for r in rows])

@app.route('/api/customers/max_number', methods=['GET'])
def api_customers_max_number():
    """Returns the highest customer number for auto-enrollment."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    # Get highest customer_number (format: CUST-0001)
    max_customer = db.session.query(Customer).filter(
        Customer.customer_number.isnot(None)
    ).order_by(Customer.customer_number.desc()).first()

    if max_customer and max_customer.customer_number:
        try:
            cn = max_customer.customer_number
            num = int(cn.split('-')[1]) if '-' in cn else int(cn)
            return jsonify({'max_number': num})
        except (IndexError, ValueError):
            pass

    return jsonify({'max_number': 0})

_ATTR_WINDOW = 10  # how many recent detections to vote over

def _fetch_attr_rows(cid, limit=None):
    from sqlalchemy import text as _t
    lim = limit if limit is not None else _ATTR_WINDOW
    return db.session.execute(
        _t("""SELECT height_cm, hair_color, skin_tone, build, eye_color,
                     age_range, gender, wearing_glasses, facial_hair,
                     detected_at, camera_source, confidence, height_category
              FROM customer_physical_attributes
              WHERE customer_id = :cid
              ORDER BY detected_at DESC LIMIT :lim"""),
        {'cid': cid, 'lim': lim}
    ).fetchall()


def _voted_attributes(rows):
    """Majority-vote over recent observations so one bad detection doesn't flip the profile.
    Categorical fields: most frequent non-null value wins.
    Boolean fields: True only if >50% of non-null observations are True.
    Numeric fields (height_cm): median of non-null values.
    """
    from collections import Counter
    if not rows:
        return None

    def mode_of(vals):
        counts = Counter(v for v in vals if v is not None and v != '')
        return counts.most_common(1)[0][0] if counts else None

    def bool_vote(vals):
        non_null = [v for v in vals if v is not None]
        if not non_null:
            return None
        return sum(1 for v in non_null if v) > len(non_null) / 2

    def median_int(vals):
        nums = sorted(v for v in vals if v is not None)
        return nums[len(nums) // 2] if nums else None

    return {
        'height_cm':       median_int([r[0] for r in rows]),
        'hair_color':      mode_of([r[1] for r in rows]),
        'skin_tone':       mode_of([r[2] for r in rows]),
        'build':           mode_of([r[3] for r in rows]),
        'eye_color':       mode_of([r[4] for r in rows]),
        'age_range':       mode_of([r[5] for r in rows]),
        'gender':          mode_of([r[6] for r in rows]),
        'wearing_glasses': bool_vote([r[7] for r in rows]),
        'facial_hair':     mode_of([r[8] for r in rows]),
        'detected_at':     rows[0][9].isoformat() if rows[0][9] else None,
        'camera_source':   rows[0][10],
        'confidence':      float(rows[0][11]) if rows[0][11] else None,
        'height_category': mode_of([r[12] for r in rows]),
    }


@app.route('/api/customers/<int:cid>/attributes', methods=['GET', 'POST'])
def api_customer_attributes(cid):
    """Get or store physical attributes for a customer."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    customer = db.session.get(Customer, cid)
    if not customer:
        return jsonify({'error': 'Customer not found'}), 404

    if request.method == 'GET':
        rows = _fetch_attr_rows(cid)
        voted = _voted_attributes(rows)
        return jsonify(voted)

    else:  # POST
        data = request.get_json()
        from sqlalchemy import text
        db.session.execute(
            text("""INSERT INTO customer_physical_attributes
                    (customer_id, height_cm, hair_color, skin_tone, build, eye_color,
                     age_range, gender, wearing_glasses, facial_hair, camera_source, confidence,
                     height_category)
                    VALUES (:cid, :height, :hair, :skin, :build, :eye, :age, :gender,
                            :glasses, :facial, :camera, :conf, :height_cat)"""),
            {
                'cid': cid,
                'height': data.get('height_cm'),
                'hair': data.get('hair_color'),
                'skin': data.get('skin_tone'),
                'build': data.get('build'),
                'eye': data.get('eye_color'),
                'age': data.get('age_range'),
                'gender': data.get('gender'),
                'glasses': data.get('wearing_glasses'),
                'facial': data.get('facial_hair'),
                'camera': data.get('camera_source'),
                'conf': data.get('confidence'),
                'height_cat': data.get('height_category'),
            }
        )
        db.session.commit()
        return jsonify({'ok': True})

@app.route('/api/customers/attributes_bulk', methods=['GET'])
def api_customers_attributes_bulk():
    """Get voted attributes for all customers in bulk (for caching)."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    from sqlalchemy import text
    # Fetch last _ATTR_WINDOW rows per customer via a ranked subquery
    result = db.session.execute(
        text(f"""SELECT customer_id, height_cm, hair_color, skin_tone, build,
                        eye_color, age_range, gender, wearing_glasses, facial_hair,
                        detected_at, camera_source, confidence, height_category
               FROM (
                   SELECT *, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY detected_at DESC) AS rn
                   FROM customer_physical_attributes
               ) ranked
               WHERE rn <= {_ATTR_WINDOW}
               ORDER BY customer_id, detected_at DESC""")
    ).fetchall()

    # Group rows by customer_id then vote
    from collections import defaultdict
    rows_by_cid = defaultdict(list)
    for row in result:
        rows_by_cid[row[0]].append(row[1:])  # strip customer_id prefix

    # Wrap in namedtuple-compatible tuples matching _voted_attributes indexing
    attributes_by_customer = {}
    for cid, cid_rows in rows_by_cid.items():
        # cid_rows already have same column order as _fetch_attr_rows (minus customer_id)
        voted = _voted_attributes(cid_rows)
        if voted:
            attributes_by_customer[str(cid)] = voted

    return jsonify(attributes_by_customer)

@app.route('/api/till/active_customer', methods=['GET'])
def api_till_active_customer():
    """Returns customer detected at till in last 30 seconds (only if they have a name)."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    from sqlalchemy import text
    cutoff = datetime.utcnow() - timedelta(seconds=30)

    # Only return customers with names (exclude anonymous auto-enrolled)
    result = db.session.execute(
        text("""SELECT td.customer_id, td.detected_at, c.name, c.customer_number
                FROM till_detections td
                JOIN customers c ON c.id = td.customer_id
                WHERE td.detected_at >= :cutoff
                  AND c.name IS NOT NULL
                ORDER BY td.detected_at DESC LIMIT 1"""),
        {'cutoff': cutoff}
    ).fetchone()

    if not result:
        return jsonify({'customer_id': None})

    return jsonify({
        'customer_id': result[0],
        'name': result[2],
        'customer_number': result[3],
        'detected_at': result[1].isoformat() if result[1] else None
    })

@app.route('/api/till/detect', methods=['POST'])
def api_till_detect():
    """Log customer detection at till."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    from sqlalchemy import text
    db.session.execute(
        text("""INSERT INTO till_detections (customer_id, camera_source)
                VALUES (:cid, :camera)"""),
        {'cid': data['customer_id'], 'camera': data.get('camera_source')}
    )
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/customers/visits/recent', methods=['GET'])
def api_customers_visits_recent():
    """Get recent customer visit detections for session aggregation."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    hours = int(request.args.get('hours', 2))
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    from sqlalchemy import text
    result = db.session.execute(
        text("""SELECT cv.customer_id, cv.detected_at, cv.camera_source
                FROM customer_visits cv
                WHERE cv.detected_at >= :cutoff
                ORDER BY cv.customer_id, cv.detected_at"""),
        {'cutoff': cutoff}
    ).fetchall()

    visits = []
    for row in result:
        visits.append({
            'customer_id': row[0],
            'detected_at': row[1].isoformat() if row[1] else None,
            'camera_source': row[2]
        })

    return jsonify(visits)

@app.route('/api/customers/<int:cid>/sales', methods=['GET'])
def api_customer_sales(cid):
    """Get customer's sales within a date range."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    start = request.args.get('start')
    end = request.args.get('end')

    query = Sale.query.filter(
        Sale.customer_id == cid,
        Sale.voided == False
    )

    if start:
        query = query.filter(Sale.date_time >= datetime.fromisoformat(start))
    if end:
        query = query.filter(Sale.date_time <= datetime.fromisoformat(end))

    sales = query.all()

    return jsonify([{
        'sale_id': s.sale_id,
        'date_time': s.date_time.isoformat(),
        'product_id': s.product_id,
        'qty': float(s.qty),
        'unit_price': float(s.unit_price)
    } for s in sales])

@app.route('/api/customers/sessions', methods=['POST'])
def api_customers_sessions():
    """Create a visit session record."""
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    from sqlalchemy import text

    # Check if session already exists (avoid duplicates)
    existing = db.session.execute(
        text("""SELECT id FROM visit_sessions
                WHERE customer_id = :cid
                AND session_start = :start
                LIMIT 1"""),
        {
            'cid': data['customer_id'],
            'start': datetime.fromisoformat(data['session_start'])
        }
    ).fetchone()

    if existing:
        return jsonify({'ok': True, 'id': existing[0], 'already_exists': True})

    # Create new session
    db.session.execute(
        text("""INSERT INTO visit_sessions
                (customer_id, session_start, session_end, entry_camera,
                 checkout_camera, dwell_seconds, purchase_made, sale_ids)
                VALUES (:cid, :start, :end, :entry, :checkout, :dwell, :purchase, :sales)"""),
        {
            'cid': data['customer_id'],
            'start': datetime.fromisoformat(data['session_start']),
            'end': datetime.fromisoformat(data['session_end']),
            'entry': data.get('entry_camera'),
            'checkout': data.get('checkout_camera'),
            'dwell': data.get('dwell_seconds', 0),
            'purchase': data.get('purchase_made', False),
            'sales': data.get('sale_ids')
        }
    )
    db.session.commit()

    return jsonify({'ok': True})


# -----------------------------
# Stock — FIFO batch management





# -----------------------------
# CSV Exports (admin)
# -----------------------------
@app.route('/admin/export/products')
def export_products_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    default_markup = float(get_setting('markup_percent', 40) or 40)

    # FIFO cost per base unit for all stock items (oldest non-empty batch)
    fifo_costs = {}
    for batch in (StockBatch.query
                  .filter(StockBatch.qty_remaining_base > 0)
                  .order_by(StockBatch.product_id, StockBatch.purchased_at.asc(), StockBatch.id.asc())
                  .all()):
        if batch.product_id not in fifo_costs:
            fifo_costs[batch.product_id] = float(batch.cost_per_base_unit)

    def recipe_cost(product_id, _depth=0):
        """Recursively sum FIFO cost for one unit of a recipe product."""
        if _depth > 10:
            return 0.0
        total = 0.0
        for rl in RecipeLine.query.filter_by(product_id=product_id).all():
            ing = db.session.get(Product, rl.ingredient_id)
            if not ing:
                continue
            if ing.product_type == 'recipe':
                total += recipe_cost(ing.id, _depth + 1) * float(rl.qty_base)
            else:
                total += fifo_costs.get(ing.id, 0.0) * float(rl.qty_base)
        return total

    products = (Product.query
                .filter_by(is_archived=False, is_for_sale=True)
                .order_by(Product.name.asc())
                .all())

    sio = StringIO()
    sio.write('Product,Barcode,Category,Sold By,Unit,Wholesale Cost,Retail Price,Recommended Retail Price,Stock Available\n')

    for p in products:
        # Determine category label
        category = {'simple': 'General', 'stock_item': 'Stock Item', 'recipe': 'Prepared / Bundle'}.get(p.product_type, '')

        # Unit description
        if p.sold_by_weight and p.unit_type:
            big = 'kg' if p.unit_type == 'weight' else 'L'
            sold_by = f'Per {big}'
            unit    = big
        elif p.package_unit:
            sold_by = f'Per {p.package_unit}'
            unit    = p.package_unit
        else:
            sold_by = 'Per unit'
            unit    = 'unit'

        # Wholesale cost (FIFO)
        if p.product_type == 'stock_item':
            cost_base = fifo_costs.get(p.id, 0.0)
            if p.sold_by_weight:
                # Cost per kg or L
                conv = 1000.0  # g→kg or ml→L
                wholesale = round(cost_base * conv, 4)
            else:
                pkg = float(p.package_size or 0)
                wholesale = round(cost_base * pkg, 4) if pkg else ''
        elif p.product_type == 'recipe':
            c = recipe_cost(p.id)
            wholesale = round(c, 4) if c > 0 else ''
        else:
            wholesale = ''

        # Retail price (current selling price)
        if p.sold_by_weight and p.price_per_unit is not None:
            conv = 1000.0
            retail = round(float(p.price_per_unit) * conv, 2)
        elif p.price is not None:
            retail = round(float(p.price), 2)
        else:
            retail = ''

        # Recommended retail = wholesale × (1 + markup)
        if wholesale != '':
            rrp = round(float(wholesale) * (1 + default_markup / 100), 2)
        else:
            rrp = ''

        # Stock available
        if p.product_type == 'stock_item':
            stock_level = float(p.stock_level) if hasattr(p, 'stock_level') and p.stock_level else ''
            # Recompute from batches for accuracy
            total_remaining = db.session.query(
                func.sum(StockBatch.qty_remaining_base)
            ).filter_by(product_id=p.id).scalar() or 0
            if p.sold_by_weight:
                stock_disp = f"{round(float(total_remaining)/1000, 3)}{unit}"
            else:
                pkg = float(p.package_size or 1)
                stock_disp = f"{int(float(total_remaining) / pkg)} {unit}s" if pkg else ''
        elif p.product_type == 'simple':
            stock_disp = str(p.stock_qty or 0)
        else:
            stock_disp = ''

        name    = (p.name    or '').replace(',', ';')
        barcode = (p.barcode or '').replace(',', ';')

        sio.write(f"{name},{barcode},{category},{sold_by},{unit},{wholesale},{retail},{rrp},{stock_disp}\n")

    buf = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    from datetime import date as _date
    fname = f"product_catalogue_{_date.today().isoformat()}.csv"
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=fname)

@app.route('/admin/export/transactions')
def export_transactions_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    start_param    = request.args.get('start')
    end_param      = request.args.get('end')
    product_id_str = request.args.get('product_id')
    today          = date.today()
    start_dt       = _parse_dt(start_param) or datetime(today.year, today.month, today.day)
    end_dt         = _parse_dt(end_param, is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    try:
        product_id_filter = int(product_id_str) if product_id_str else None
    except (ValueError, TypeError):
        product_id_filter = None

    # Load rows — always line-level for detailed CSV
    q = (db.session.query(Sale)
         .filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False))
    if product_id_filter:
        q = q.filter(Sale.product_id == product_id_filter)
    rows = q.order_by(Sale.date_time.asc(), Sale.sale_id, Sale.id).all()

    # Pre-load product and user names
    pids  = {r.product_id for r in rows}
    uids  = {r.user_id for r in rows if r.user_id}
    pname = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    uname = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    sio = StringIO()
    sio.write('sale_id,date_time,product,qty,unit_price,subtotal,teller,discount\n')
    for r in rows:
        subtotal = round(float(r.qty * r.unit_price), 2)
        disc     = ''
        if r.discount_json:
            try:
                import json as _json
                d = _json.loads(r.discount_json)
                parts = []
                if d.get('special'): parts.append(f"Special:{d['special']}")
                if d.get('item'):    parts.append(f"Item:{d['item'].get('value')}{d['item'].get('type','')}")
                if d.get('cart'):    parts.append(f"Cart:{d['cart'].get('value')}{d['cart'].get('type','')}")
                disc = ' | '.join(parts)
            except Exception:
                pass
        product_name = pname.get(r.product_id, str(r.product_id)).replace(',', ';')
        teller       = uname.get(r.user_id, '').replace(',', ';')
        sio.write(f"{r.sale_id},{r.date_time.isoformat()},{product_name},{float(r.qty):.4f},{float(r.unit_price):.2f},{subtotal},{teller},{disc}\n")

    product_name_slug = ''
    if product_id_filter:
        fp = db.session.get(Product, product_id_filter)
        if fp:
            product_name_slug = '_' + fp.name.replace(' ', '_')[:20]

    buf   = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    fname = f"sales{product_name_slug}_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv"
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=fname)


@app.route('/admin/export/profit')
def export_profit_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    today     = date.today()
    start_dt  = _parse_dt(request.args.get('start')) or datetime(today.year, today.month, today.day)
    end_dt    = _parse_dt(request.args.get('end'), is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)
    pid_str   = request.args.get('product_id')
    try:    pid_filter = int(pid_str) if pid_str else None
    except: pid_filter = None

    q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
    if pid_filter:
        q = q.filter(Sale.product_id == pid_filter)
    rows = q.all()

    sale_ids = list({r.sale_id for r in rows})
    consumptions = StockConsumption.query.filter(StockConsumption.sale_id.in_(sale_ids)).all() if sale_ids else []

    rev_map  = defaultdict(float)
    qty_map  = defaultdict(float)
    for r in rows:
        rev_map[r.product_id]  += float(Decimal(str(r.qty)) * r.unit_price)
        qty_map[r.product_id]  += float(r.qty)

    sale_product_map = {r.sale_id: r.product_id for r in rows}
    cogs_map = defaultdict(float)
    for c in consumptions:
        pid = sale_product_map.get(c.sale_id)
        if pid:
            cogs_map[pid] += float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)))

    pids  = set(rev_map.keys())
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}

    sio = StringIO()
    sio.write('product,qty_sold,revenue,cogs,gross_profit,margin_pct\n')
    for pid in sorted(pids, key=lambda x: rev_map[x], reverse=True):
        rev    = rev_map[pid]
        cogs   = cogs_map.get(pid, 0)
        profit = rev - cogs
        margin = round(profit / rev * 100, 1) if rev > 0 else ''
        name   = names.get(pid, str(pid)).replace(',', ';')
        sio.write(f"{name},{round(qty_map[pid],2)},{round(rev,2)},{round(cogs,2)},{round(profit,2)},{margin}\n")

    buf   = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    fname = f"profit_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv"
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=fname)


@app.route('/admin/export/writeoffs')
def export_writeoffs_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    today    = date.today()
    start_dt = _parse_dt(request.args.get('start')) or datetime(today.year, today.month, today.day)
    end_dt   = _parse_dt(request.args.get('end'), is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)

    writeoffs = StockAdjustment.query.filter(
        StockAdjustment.adjustment_type == 'writeoff',
        StockAdjustment.adjusted_at >= start_dt,
        StockAdjustment.adjusted_at <= end_dt
    ).order_by(StockAdjustment.adjusted_at.asc()).all()

    pids  = {w.product_id for w in writeoffs}
    uids  = {w.user_id for w in writeoffs if w.user_id}
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    users = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    sio = StringIO()
    sio.write('date,product,qty_written_off,base_unit,cost_lost,reason,by\n')
    for w in writeoffs:
        name   = names.get(w.product_id, str(w.product_id)).replace(',', ';')
        reason = (w.reason or '').replace(',', ';')
        by     = users.get(w.user_id, '').replace(',', ';')
        sio.write(f"{w.adjusted_at.isoformat()},{name},{abs(float(w.qty_change_base or 0)):.4f},{w.base_unit or ''},{round(float(w.cost_written_off or 0),2)},{reason},{by}\n")

    buf   = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    fname = f"writeoffs_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv"
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=fname)


@app.route('/admin/export/suppliers')
def export_suppliers_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    today    = date.today()
    start_dt = _parse_dt(request.args.get('start')) or datetime(today.year, today.month, today.day)
    end_dt   = _parse_dt(request.args.get('end'), is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)

    batches = StockBatch.query.filter(
        StockBatch.purchased_at >= start_dt, StockBatch.purchased_at <= end_dt
    ).order_by(StockBatch.purchased_at.asc()).all()

    pids  = {b.product_id for b in batches}
    sids  = {b.supplier_id for b in batches if b.supplier_id}
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    sups  = {s.id: s.name for s in Supplier.query.filter(Supplier.id.in_(sids)).all()} if sids else {}

    sio = StringIO()
    sio.write('date,supplier,product,qty_purchased,base_unit,cost_per_unit,total_cost\n')
    for b in batches:
        sup_name  = sups.get(b.supplier_id, 'Unknown').replace(',', ';')
        prod_name = names.get(b.product_id, str(b.product_id)).replace(',', ';')
        total     = round(float(b.qty_purchased_base) * float(b.cost_per_base_unit), 2)
        sio.write(f"{b.purchased_at.isoformat()},{sup_name},{prod_name},{float(b.qty_purchased_base):.4f},{b.base_unit or ''},{float(b.cost_per_base_unit):.4f},{total}\n")

    buf   = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    fname = f"supplier_spend_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv"
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=fname)


@app.route('/admin/export/staff')
def export_staff_csv():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    today    = date.today()
    start_dt = _parse_dt(request.args.get('start')) or datetime(today.year, today.month, today.day)
    end_dt   = _parse_dt(request.args.get('end'), is_end=True) or datetime(today.year, today.month, today.day, 23, 59, 59)

    uid_str = request.args.get('user_id')
    try:    uid_filter = int(uid_str) if uid_str else None
    except: uid_filter = None

    sale_q = db.session.query(Sale).filter(
        Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False
    )
    if uid_filter:
        sale_ids_by_user = {
            r.sale_id for r in db.session.query(Sale.sale_id)
            .filter(Sale.user_id == uid_filter,
                    Sale.date_time >= start_dt, Sale.date_time <= end_dt,
                    Sale.voided == False).all()
        }
        sale_q = sale_q.filter(Sale.sale_id.in_(sale_ids_by_user))
    rows = sale_q.all()

    sess_q = UserSession.query.filter(
        UserSession.logged_in >= start_dt, UserSession.logged_in <= end_dt
    )
    if uid_filter:
        sess_q = sess_q.filter(UserSession.user_id == uid_filter)
    sessions = sess_q.all()

    all_uids = {r.user_id for r in rows if r.user_id} | {s.user_id for s in sessions}
    user_map = {u.id: u for u in User.query.filter(User.id.in_(all_uids)).all()} if all_uids else {}

    # Aggregate per user
    emp_revenue  = defaultdict(float)
    emp_tx       = defaultdict(set)
    emp_items    = defaultdict(float)
    emp_first    = {}
    emp_last     = {}
    for r in rows:
        uid = r.user_id or 0
        if not uid: continue
        val = float(Decimal(str(r.qty)) * r.unit_price)
        emp_revenue[uid] += val
        emp_tx[uid].add(r.sale_id)
        emp_items[uid]   += float(r.qty)
        dt = r.date_time
        if uid not in emp_first or dt < emp_first[uid]: emp_first[uid] = dt
        if uid not in emp_last  or dt > emp_last[uid]:  emp_last[uid]  = dt

    now_utc = datetime.utcnow()
    emp_session_minutes = defaultdict(float)
    emp_session_count   = defaultdict(int)
    emp_first_login     = {}
    emp_last_activity   = {}
    for s in sessions:
        natural_end  = s.logged_out or now_utc
        clamped_end  = min(natural_end, end_dt, now_utc)
        dur          = (clamped_end - s.logged_in).total_seconds() / 60.0
        if dur <= 0: continue
        emp_session_minutes[s.user_id] += dur
        emp_session_count[s.user_id]   += 1
        uid = s.user_id
        if uid not in emp_first_login or s.logged_in < emp_first_login[uid]:
            emp_first_login[uid] = s.logged_in
        act = s.last_active or clamped_end
        if uid not in emp_last_activity or act > emp_last_activity[uid]:
            emp_last_activity[uid] = act

    sio = StringIO()
    sio.write('employee,role,transactions,revenue,avg_sale,items_sold,sessions,time_logged_in_min,revenue_per_hour,sales_per_hour,first_sale,last_sale\n')

    all_emp_uids = set(emp_revenue.keys()) | set(emp_session_minutes.keys())
    sorted_uids  = sorted(all_emp_uids, key=lambda u: emp_revenue.get(u, 0), reverse=True)

    for uid in sorted_uids:
        u         = user_map.get(uid)
        name      = (u.username if u else f'User {uid}').replace(',', ';')
        role      = (u.role if u else '').replace(',', ';')
        tx_count  = len(emp_tx.get(uid, set()))
        rev       = emp_revenue.get(uid, 0)
        items     = emp_items.get(uid, 0)
        sess_mins = emp_session_minutes.get(uid, 0)
        sess_cnt  = emp_session_count.get(uid, 0)
        avg_sale  = round(rev / tx_count, 2) if tx_count > 0 else 0
        first_login   = emp_first_login.get(uid)
        last_activity = emp_last_activity.get(uid)
        if first_login and last_activity and last_activity > first_login:
            span_mins = (last_activity - first_login).total_seconds() / 60.0
        else:
            span_mins = sess_mins
        rev_per_hour  = round(rev   / (span_mins / 60), 2) if span_mins > 0 else ''
        tx_per_hour   = round(tx_count / (span_mins / 60), 2) if span_mins > 0 else ''
        first_sale    = emp_first.get(uid, '')
        last_sale     = emp_last.get(uid, '')
        sio.write(
            f"{name},{role},{tx_count},{round(rev,2)},{avg_sale},{round(items,2)},"
            f"{sess_cnt},{round(sess_mins,1)},{rev_per_hour},{tx_per_hour},"
            f"{first_sale.isoformat() if first_sale else ''},"
            f"{last_sale.isoformat() if last_sale else ''}\n"
        )

    buf   = BytesIO(sio.getvalue().encode('utf-8')); buf.seek(0)
    emp_slug = ''
    if uid_filter:
        fu = db.session.get(User, uid_filter)
        if fu: emp_slug = f"_{fu.username.replace(' ','_')}"
    fname = f"staff_stats{emp_slug}_{start_dt.date().isoformat()}_to_{end_dt.date().isoformat()}.csv"
    return send_file(buf, mimetype='text/csv', as_attachment=True, download_name=fname)




# -----------------------------
# Recognition Monitor (developer + admin)
# -----------------------------
RECOGNITION_SERVICE_URL = os.environ.get('RECOGNITION_URL', 'http://farmpos-recognition:8080')

@app.route('/api/recognition/status', methods=['GET'])
def api_recognition_status():
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        import requests as _req
        r = _req.get(f'{RECOGNITION_SERVICE_URL}/status', timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503

@app.route('/api/recognition/settings', methods=['GET', 'POST'])
def api_recognition_settings():
    """Developer-accessible recognition settings — subset of /api/settings."""
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
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
    data = request.json or {}
    saved = {}
    for key, cast in [
        ('face_threshold', float), ('link_threshold', float),
        ('face_quality_min', float), ('merge_suggest_min_sim', float),
        ('auto_merge_min_sim', float), ('max_face_angles', int),
        ('min_angle_distance', float),
    ]:
        if key in data:
            try:
                set_setting(key, cast(data[key]))
                saved[key] = cast(data[key])
            except Exception:
                return jsonify({'error': f'Invalid {key}'}), 400
    return jsonify({'ok': True, 'saved': saved})


@app.route('/api/recognition/logs', methods=['GET'])
def api_recognition_logs():
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        import requests as _req
        params = {k: request.args[k] for k in request.args}
        r = _req.get(f'{RECOGNITION_SERVICE_URL}/logs', params=params, timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@app.route('/api/recognition/identity_events', methods=['GET'])
def api_recognition_identity_events():
    """Return the last N structured identity events from the recognition service.
    Used by the Monitor tab to show the identity chain event log."""
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        import requests as _req
        n = request.args.get('n', 100)
        r = _req.get(f'{RECOGNITION_SERVICE_URL}/identity_events', params={'n': n}, timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@app.route('/api/recognition/tracks', methods=['GET'])
def api_recognition_tracks():
    """Return all active stable tracks with their full identity chain + stability breakdown.
    Used by the Monitor tab to show the per-person pipeline state."""
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        import requests as _req
        r = _req.get(f'{RECOGNITION_SERVICE_URL}/tracks', timeout=3)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503


@app.route('/api/recognition/control/<action>', methods=['POST'])
def api_recognition_control(action):
    if not require_role('admin', 'developer'):
        return jsonify({'error': 'Forbidden'}), 403
    allowed = {'clear_queue', 'flush_sessions', 'clear_anon', 'sync_cache', 'requeue_clip', 'resync_customer', 'purge_customer'}
    if action not in allowed:
        return jsonify({'error': 'Unknown action'}), 400
    try:
        import requests as _req
        r = _req.post(f'{RECOGNITION_SERVICE_URL}/control/{action}',
                      json=request.json or {}, timeout=5)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e), 'available': False}), 503




# -----------------------------
# Stats (admin)
# -----------------------------
@app.route('/api/stats/today')
def api_stats_today():
    # Legacy alias — redirects to /api/stats with today's date
    today = date.today().isoformat()
    request.args = request.args.copy()
    from werkzeug.datastructures import ImmutableMultiDict
    request.args = ImmutableMultiDict([('start', today), ('end', today)])
    return api_stats()

@app.route('/api/stats')
def api_stats():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    today = date.today()
    try:
        start_dt = datetime.fromisoformat(request.args.get('start', today.isoformat()))
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(request.args.get('end', today.isoformat()))
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    product_id_filter = request.args.get('product_id')
    try:
        product_id_filter = int(product_id_filter) if product_id_filter else None
    except (ValueError, TypeError):
        product_id_filter = None

    user_id_filter = request.args.get('user_id')
    try:
        user_id_filter = int(user_id_filter) if user_id_filter else None
    except (ValueError, TypeError):
        user_id_filter = None

    sale_q = db.session.query(Sale).filter(
        Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False
    )
    if product_id_filter:
        sale_ids_with_product = {
            r.sale_id for r in db.session.query(Sale.sale_id)
            .filter(Sale.product_id == product_id_filter,
                    Sale.date_time >= start_dt, Sale.date_time <= end_dt,
                    Sale.voided == False).all()
        }
        sale_q = sale_q.filter(Sale.product_id == product_id_filter,
                               Sale.sale_id.in_(sale_ids_with_product))
    if user_id_filter:
        # Filter to sale_ids made by this user, show all lines in those sales
        sale_ids_by_user = {
            r.sale_id for r in db.session.query(Sale.sale_id)
            .filter(Sale.user_id == user_id_filter,
                    Sale.date_time >= start_dt, Sale.date_time <= end_dt,
                    Sale.voided == False).all()
        }
        sale_q = sale_q.filter(Sale.sale_id.in_(sale_ids_by_user))
    rows = sale_q.all()

    # ── Core totals ──
    transactions_count = len({r.sale_id for r in rows})
    total_sales_value  = float(sum(Decimal(str(r.qty)) * r.unit_price for r in rows))
    total_items_sold   = float(sum(r.qty for r in rows))

    basket_value_map = defaultdict(float)
    basket_qty_map   = defaultdict(float)
    for r in rows:
        val = float(Decimal(str(r.qty)) * r.unit_price)
        basket_value_map[r.sale_id] += val
        basket_qty_map[r.sale_id]   += float(r.qty)
    avg_basket_value = (sum(basket_value_map.values()) / len(basket_value_map)) if basket_value_map else 0.0
    avg_basket_qty   = (sum(basket_qty_map.values())   / len(basket_qty_map))   if basket_qty_map   else 0.0

    # ── COGS & profit ──
    sale_ids = list({r.sale_id for r in rows})
    total_cogs = 0.0
    if sale_ids:
        consumptions = StockConsumption.query.filter(StockConsumption.sale_id.in_(sale_ids)).all()
        total_cogs   = float(sum(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)) for c in consumptions))
    gross_profit = total_sales_value - total_cogs
    gross_margin = round(gross_profit / total_sales_value * 100, 1) if total_sales_value > 0 else None

    # ── Write-offs ──
    writeoffs = StockAdjustment.query.filter(
        StockAdjustment.adjustment_type == 'writeoff',
        StockAdjustment.adjusted_at >= start_dt,
        StockAdjustment.adjusted_at <= end_dt
    ).all()
    total_writeoff_cost  = float(sum(float(w.cost_written_off or 0) for w in writeoffs))
    total_writeoff_count = len(writeoffs)

    # ── Kitchen ──
    kitchen_in_range = KitchenOrder.query.filter(
        KitchenOrder.queued_at >= start_dt, KitchenOrder.queued_at <= end_dt
    ).all()
    kitchen_completed_list = [k for k in kitchen_in_range if k.status == 'completed']
    kitchen_count = len(kitchen_completed_list)
    now_dt = datetime.utcnow()
    pending_orders = KitchenOrder.query.filter_by(status='pending').order_by(KitchenOrder.queued_at.asc()).all()
    max_wait_seconds = None
    if pending_orders:
        max_wait_seconds = round((now_dt - pending_orders[0].queued_at).total_seconds(), 0)
    # Average completed wait time in the period
    completed_waits = [
        (k.completed_at - k.queued_at).total_seconds()
        for k in kitchen_completed_list if k.completed_at and k.queued_at
    ]
    avg_completed_wait = round(sum(completed_waits) / len(completed_waits)) if completed_waits else None

    # ── Top products by qty AND revenue ──
    top_qty_map     = defaultdict(float)
    top_revenue_map = defaultdict(float)
    for r in rows:
        top_qty_map[r.product_id]     += float(r.qty)
        top_revenue_map[r.product_id] += float(Decimal(str(r.qty)) * r.unit_price)

    all_pids = set(top_qty_map.keys()) | set(top_revenue_map.keys())
    name_map = {p.id: p.name for p in Product.query.filter(Product.id.in_(all_pids)).all()} if all_pids else {}

    top_by_qty = [
        {'product_id': pid, 'name': name_map.get(pid, str(pid)), 'qty_sold': qty,
         'revenue': round(top_revenue_map.get(pid, 0), 2)}
        for pid, qty in sorted(top_qty_map.items(), key=lambda x: x[1], reverse=True)[:10]
    ]
    top_by_revenue = [
        {'product_id': pid, 'name': name_map.get(pid, str(pid)),
         'revenue': round(rev, 2), 'qty_sold': round(top_qty_map.get(pid, 0), 2)}
        for pid, rev in sorted(top_revenue_map.items(), key=lambda x: x[1], reverse=True)[:10]
    ]

    # ── Revenue by hour (today view) ──
    revenue_per_hour = defaultdict(float)
    for r in rows:
        revenue_per_hour[r.date_time.hour] += float(Decimal(str(r.qty)) * r.unit_price)
    hourly = [{'hour': h, 'revenue': round(v, 2)} for h, v in sorted(revenue_per_hour.items())]

    # ── Revenue by day (multi-day view) ──
    revenue_per_day  = defaultdict(float)
    tx_per_day       = defaultdict(set)
    profit_per_day   = defaultdict(float)
    for r in rows:
        d = r.date_time.date().isoformat()
        revenue_per_day[d] += float(Decimal(str(r.qty)) * r.unit_price)
        tx_per_day[d].add(r.sale_id)
    # Attach COGS per day via consumptions
    if sale_ids:
        sale_date_map = {r.sale_id: r.date_time.date().isoformat() for r in rows}
        for c in consumptions:
            d = sale_date_map.get(c.sale_id)
            if d:
                profit_per_day[d] += float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)))
    daily = [
        {
            'date':        d,
            'revenue':     round(revenue_per_day[d], 2),
            'profit':      round(revenue_per_day[d] - profit_per_day.get(d, 0), 2),
            'tx_count':    len(tx_per_day[d]),
        }
        for d in sorted(revenue_per_day.keys())
    ]

    # ── Best / worst day ──
    best_day  = max(daily, key=lambda x: x['revenue'], default=None)
    worst_day = min(daily, key=lambda x: x['revenue'], default=None) if len(daily) > 1 else None

    # ── Revenue by minute (only useful for single-day ranges) ──
    revenue_per_minute = defaultdict(float)
    for r in rows:
        minute_key = r.date_time.strftime('%H:%M')
        revenue_per_minute[minute_key] += float(Decimal(str(r.qty)) * r.unit_price)
    minutely = [{'minute': m, 'revenue': round(v, 2)} for m, v in sorted(revenue_per_minute.items())]

    # ── Employee stats ──
    emp_revenue   = defaultdict(float)
    emp_tx        = defaultdict(set)
    emp_items     = defaultdict(float)
    emp_first     = {}
    emp_last      = {}
    for r in rows:
        uid = r.user_id or 0
        val = float(Decimal(str(r.qty)) * r.unit_price)
        emp_revenue[uid] += val
        emp_tx[uid].add(r.sale_id)
        emp_items[uid]   += float(r.qty)
        dt = r.date_time
        if uid not in emp_first or dt < emp_first[uid]: emp_first[uid] = dt
        if uid not in emp_last  or dt > emp_last[uid]:  emp_last[uid]  = dt

    # Session durations — only sessions that started within the range, capped at range end.
    # Excludes zombie sessions (started before range, never logged out) which would
    # otherwise inflate time by 24h per unclosed session.
    sessions_in_range = UserSession.query.filter(
        UserSession.logged_in >= start_dt,
        UserSession.logged_in <= end_dt
    ).all()
    emp_session_minutes = defaultdict(float)
    emp_session_count   = defaultdict(int)
    emp_sessions        = defaultdict(list)
    emp_first_login     = {}   # earliest login in range
    emp_last_activity   = {}   # latest last_active / logged_out in range
    now_utc = datetime.utcnow()
    for s in sessions_in_range:
        natural_end  = s.logged_out or now_utc
        clamped_end  = min(natural_end, end_dt, now_utc)
        duration_min = (clamped_end - s.logged_in).total_seconds() / 60.0
        if duration_min <= 0:
            continue
        emp_session_minutes[s.user_id] += duration_min
        emp_session_count[s.user_id]   += 1
        emp_sessions[s.user_id].append({
            'login':       s.logged_in.isoformat(),
            'logout':      s.logged_out.isoformat() if s.logged_out else None,
            'last_active': s.last_active.isoformat() if s.last_active else None,
            'duration_min': round(duration_min, 1),
            'open':        s.logged_out is None,
        })
        # Track span for rate calculation
        uid = s.user_id
        if uid not in emp_first_login or s.logged_in < emp_first_login[uid]:
            emp_first_login[uid] = s.logged_in
        activity_end = s.last_active or clamped_end
        if uid not in emp_last_activity or activity_end > emp_last_activity[uid]:
            emp_last_activity[uid] = activity_end

    # Build name map from ALL user IDs that appear in sales or sessions
    all_user_ids = list(
        {r.user_id for r in rows if r.user_id} | set(emp_session_minutes.keys())
    )
    user_name_map = {u.id: u.username for u in User.query.filter(User.id.in_(all_user_ids)).all()} if all_user_ids else {}

    employee_stats = []
    for uid in set(list(emp_revenue.keys()) + list(emp_session_minutes.keys())):
        if uid == 0:
            continue
        name         = user_name_map.get(uid, f'User {uid}')
        tx_count     = len(emp_tx.get(uid, set()))
        rev          = emp_revenue.get(uid, 0)
        items        = emp_items.get(uid, 0)
        sess_mins    = emp_session_minutes.get(uid, 0)
        sess_count   = emp_session_count.get(uid, 0)
        # Work span = first login to last activity — more realistic denominator for rates
        first_login    = emp_first_login.get(uid)
        last_activity  = emp_last_activity.get(uid)
        if first_login and last_activity and last_activity > first_login:
            work_span_mins = (last_activity - first_login).total_seconds() / 60.0
        else:
            work_span_mins = sess_mins
        rev_per_hour = (rev / (work_span_mins / 60)) if work_span_mins > 0 else None
        tx_per_hour  = (tx_count / (work_span_mins / 60)) if work_span_mins > 0 else None
        avg_tx_val   = (rev / tx_count) if tx_count > 0 else 0
        first_sale   = emp_first.get(uid)
        last_sale    = emp_last.get(uid)
        employee_stats.append({
            'user_id':         uid,
            'name':            name,
            'transactions':    tx_count,
            'revenue':         round(rev, 2),
            'items_sold':      round(items, 2),
            'avg_tx_value':    round(avg_tx_val, 2),
            'session_count':   sess_count,
            'session_minutes': round(sess_mins, 1),
            'revenue_per_hour': round(rev_per_hour, 2) if rev_per_hour is not None else None,
            'tx_per_hour':     round(tx_per_hour, 2) if tx_per_hour is not None else None,
            'first_sale':      first_sale.isoformat() if first_sale else None,
            'last_sale':       last_sale.isoformat() if last_sale else None,
            'sessions':        sorted(emp_sessions.get(uid, []), key=lambda x: x['login']),
        })
    employee_stats.sort(key=lambda x: x['revenue'], reverse=True)

    # ── Supplier cost breakdown ──
    supplier_costs = defaultdict(float)
    batches_in_range = StockBatch.query.filter(
        StockBatch.purchased_at >= start_dt, StockBatch.purchased_at <= end_dt
    ).all()
    for b in batches_in_range:
        total_cost = float(b.qty_purchased_base) * float(b.cost_per_base_unit)
        sup_name = db.session.get(Supplier, b.supplier_id).name if b.supplier_id else 'Unknown'
        supplier_costs[sup_name] += total_cost
    supplier_breakdown = [
        {'supplier': k, 'total_cost': round(v, 2)}
        for k, v in sorted(supplier_costs.items(), key=lambda x: x[1], reverse=True)
    ]

    filtered_product_name = None
    if product_id_filter:
        fp = db.session.get(Product, product_id_filter)
        filtered_product_name = fp.name if fp else None

    filtered_user_name = None
    if user_id_filter:
        fu = db.session.get(User, user_id_filter)
        filtered_user_name = fu.username if fu else None

    return jsonify({
        'filtered_product_id':   product_id_filter,
        'filtered_product_name': filtered_product_name,
        'filtered_user_id':      user_id_filter,
        'filtered_user_name':    filtered_user_name,
        'transactions_count':    transactions_count,
        'total_sales_value':     round(total_sales_value, 2),
        'total_items_sold':      round(total_items_sold, 2),
        'avg_basket_value':      round(avg_basket_value, 2),
        'avg_basket_qty':        round(avg_basket_qty, 2),
        'total_cogs':            round(total_cogs, 2),
        'gross_profit':          round(gross_profit, 2),
        'gross_margin':          gross_margin,
        'total_writeoff_cost':   round(total_writeoff_cost, 2),
        'writeoff_count':        total_writeoff_count,
        'kitchen_orders_today':   kitchen_count,
        'avg_wait_seconds':       max_wait_seconds,
        'avg_completed_wait':     avg_completed_wait,
        'top_products':          top_by_qty,
        'top_by_revenue':        top_by_revenue,
        'revenue_per_hour':      hourly,
        'revenue_per_day':       daily,
        'best_day':              best_day,
        'worst_day':             worst_day,
        'supplier_breakdown':    supplier_breakdown,
        'revenue_per_minute':    minutely,
        'employee_stats':        employee_stats,
    })


@app.route('/api/stats/drilldown')
def api_stats_drilldown():
    """Return all transactions for a specific slice (day, hour, minute, product, user)."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    slice_type        = request.args.get('type')   # day | hour | minute | product | user | range
    slice_val         = request.args.get('value')  # ISO date, hour int, HH:MM, product_id, user_id
    start_arg         = request.args.get('start')
    end_arg           = request.args.get('end')
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)

    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    q = db.session.query(Sale).filter(Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False)
    if user_id_filter:    q = q.filter(Sale.user_id    == user_id_filter)
    if product_id_filter: q = q.filter(Sale.product_id == product_id_filter)

    if slice_type == 'day' and slice_val:
        try:
            d = date.fromisoformat(slice_val)
            q = q.filter(Sale.date_time >= datetime(d.year, d.month, d.day),
                         Sale.date_time <= datetime(d.year, d.month, d.day, 23, 59, 59))
        except Exception:
            pass
    elif slice_type == 'hour' and slice_val is not None:
        q = q.filter(db.func.extract('hour', Sale.date_time) == int(slice_val))
    elif slice_type == 'minute' and slice_val:
        try:
            hh, mm = slice_val.split(':')
            q = q.filter(db.func.extract('hour',   Sale.date_time) == int(hh),
                         db.func.extract('minute', Sale.date_time) == int(mm))
        except Exception:
            pass
    elif slice_type == 'product' and slice_val:
        q = q.filter(Sale.product_id == int(slice_val))
    elif slice_type == 'user' and slice_val:
        q = q.filter(Sale.user_id == int(slice_val))

    rows = q.order_by(Sale.date_time.desc()).all()

    # Group by sale_id
    sale_map = defaultdict(list)
    for r in rows:
        sale_map[r.sale_id].append(r)

    pids = {r.product_id for r in rows}
    uids = {r.user_id for r in rows if r.user_id}
    prod_names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    user_names = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    transactions = []
    for sid, sale_rows in sale_map.items():
        sale_rows_sorted = sorted(sale_rows, key=lambda r: r.date_time)
        total = float(sum(Decimal(str(r.qty)) * r.unit_price for r in sale_rows))
        transactions.append({
            'sale_id':   sid[:8],
            'date_time': sale_rows_sorted[0].date_time.isoformat(),
            'teller':    user_names.get(sale_rows_sorted[0].user_id, '—'),
            'total':     round(total, 2),
            'item_count': sum(float(r.qty) for r in sale_rows),
            'lines': [
                {
                    'product':    prod_names.get(r.product_id, str(r.product_id)),
                    'qty':        float(r.qty),
                    'unit_price': float(r.unit_price),
                    'line_total': round(float(Decimal(str(r.qty)) * r.unit_price), 2),
                }
                for r in sorted(sale_rows, key=lambda x: x.product_id)
            ],
        })
    transactions.sort(key=lambda x: x['date_time'], reverse=True)

    # ── Summary block ──
    total_revenue  = sum(t['total'] for t in transactions)
    total_tx       = len(transactions)
    avg_tx_value   = total_revenue / total_tx if total_tx else 0
    largest_sale   = max(transactions, key=lambda x: x['total'], default=None)
    smallest_sale  = min(transactions, key=lambda x: x['total'], default=None) if total_tx > 1 else None

    # Top products in this slice
    prod_rev   = defaultdict(float)
    prod_qty   = defaultdict(float)
    for t in transactions:
        for l in t['lines']:
            prod_rev[l['product']]  += l['line_total']
            prod_qty[l['product']]  += l['qty']
    top_products = sorted(
        [{'product': p, 'revenue': round(v, 2), 'qty': round(prod_qty[p], 2)} for p, v in prod_rev.items()],
        key=lambda x: x['revenue'], reverse=True
    )[:5]

    # Peak hour in this slice
    hour_rev = defaultdict(float)
    for t in transactions:
        h = int(t['date_time'][11:13])
        hour_rev[h] += t['total']
    peak_hour = max(hour_rev, key=hour_rev.get) if hour_rev else None

    # Teller breakdown
    teller_rev = defaultdict(float)
    teller_tx  = defaultdict(int)
    for t in transactions:
        teller_rev[t['teller']] += t['total']
        teller_tx[t['teller']]  += 1
    teller_breakdown = sorted(
        [{'teller': k, 'revenue': round(v, 2), 'tx_count': teller_tx[k]} for k, v in teller_rev.items()],
        key=lambda x: x['revenue'], reverse=True
    )

    summary = {
        'total_revenue':  round(total_revenue, 2),
        'tx_count':       total_tx,
        'avg_tx_value':   round(avg_tx_value, 2),
        'largest_sale':   largest_sale,
        'smallest_sale':  smallest_sale,
        'top_products':   top_products,
        'peak_hour':      peak_hour,
        'teller_breakdown': teller_breakdown,
    }

    return jsonify({'summary': summary, 'transactions': transactions})


@app.route('/api/stats/drilldown/supplier')
def api_stats_drilldown_supplier():
    """Return stock batches purchased from a supplier in the date range."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    supplier_name = request.args.get('supplier', '')
    start_arg     = request.args.get('start')
    end_arg       = request.args.get('end')
    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    if supplier_name and supplier_name != 'Unknown':
        sup = Supplier.query.filter_by(name=supplier_name).first()
        if sup:
            batches = StockBatch.query.filter(
                StockBatch.supplier_id == sup.id,
                StockBatch.purchased_at >= start_dt,
                StockBatch.purchased_at <= end_dt
            ).order_by(StockBatch.purchased_at.desc()).all()
        else:
            batches = []
    else:
        batches = StockBatch.query.filter(
            StockBatch.supplier_id == None,
            StockBatch.purchased_at >= start_dt,
            StockBatch.purchased_at <= end_dt
        ).order_by(StockBatch.purchased_at.desc()).all()

    pids = {b.product_id for b in batches}
    prod_names = {p.id: p.name for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}

    result = []
    for b in batches:
        total = float(b.qty_purchased_base) * float(b.cost_per_base_unit)
        result.append({
            'date':        b.purchased_at.isoformat(),
            'product':     prod_names.get(b.product_id, str(b.product_id)),
            'qty_base':    float(b.qty_purchased_base),
            'cost_per_unit': float(b.cost_per_base_unit),
            'total_cost':  round(total, 2),
            'remaining':   float(b.qty_remaining_base),
        })
    return jsonify(result)


@app.route('/api/stats/drilldown/kitchen')
def api_stats_drilldown_kitchen():
    """Return kitchen orders in the date range."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    start_arg         = request.args.get('start')
    end_arg           = request.args.get('end')
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)
    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    kq = KitchenOrder.query.filter(
        KitchenOrder.queued_at >= start_dt,
        KitchenOrder.queued_at <= end_dt
    )
    if user_id_filter:    kq = kq.filter(KitchenOrder.teller_id   == user_id_filter)
    if product_id_filter: kq = kq.filter(KitchenOrder.product_id  == product_id_filter)
    orders = kq.order_by(KitchenOrder.queued_at.desc()).all()

    uids = {o.teller_id for o in orders if o.teller_id}
    user_names = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    result = []
    for o in orders:
        wait = None
        if o.completed_at and o.queued_at:
            wait = round((o.completed_at - o.queued_at).total_seconds())
        result.append({
            'id':           o.id,
            'sale_id':      o.sale_id[:8],
            'product':      o.product_name,
            'qty':          float(o.qty),
            'status':       o.status,
            'teller':       user_names.get(o.teller_id, '—'),
            'queued_at':    o.queued_at.isoformat() if o.queued_at else None,
            'completed_at': o.completed_at.isoformat() if o.completed_at else None,
            'wait_seconds': wait,
            'notes':        o.notes or '',
        })
    return jsonify(result)


@app.route('/api/stats/drilldown/writeoffs')
def api_stats_drilldown_writeoffs():
    """Return write-off adjustments in the date range."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    start_arg         = request.args.get('start')
    end_arg           = request.args.get('end')
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)
    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    wq = StockAdjustment.query.filter(
        StockAdjustment.adjustment_type == 'writeoff',
        StockAdjustment.adjusted_at >= start_dt,
        StockAdjustment.adjusted_at <= end_dt
    )
    if user_id_filter:    wq = wq.filter(StockAdjustment.user_id    == user_id_filter)
    if product_id_filter: wq = wq.filter(StockAdjustment.product_id == product_id_filter)
    writeoffs = wq.order_by(StockAdjustment.adjusted_at.desc()).all()

    pids  = {w.product_id for w in writeoffs}
    uids  = {w.user_id for w in writeoffs if w.user_id}
    prods = {p.id: p for p in Product.query.filter(Product.id.in_(pids)).all()} if pids else {}
    users = {u.id: u.username for u in User.query.filter(User.id.in_(uids)).all()} if uids else {}

    result = []
    for w in writeoffs:
        p = prods.get(w.product_id)
        result.append({
            'date':       w.adjusted_at.isoformat() if w.adjusted_at else None,
            'product':    p.name if p else str(w.product_id),
            'qty_change': float(w.qty_change_base),
            'base_unit':  p.base_unit if p else '',
            'cost':       float(w.cost_written_off) if w.cost_written_off else 0,
            'by':         users.get(w.user_id, '—'),
        })
    return jsonify(result)


@app.route('/api/stats/drilldown/profit')
def api_stats_drilldown_profit():
    """Return per-product profit breakdown for the date range."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    start_arg         = request.args.get('start')
    end_arg           = request.args.get('end')
    user_id_filter    = request.args.get('user_id',    type=int)
    product_id_filter = request.args.get('product_id', type=int)
    today = date.today()
    try:
        start_dt = datetime.fromisoformat(start_arg) if start_arg else datetime(today.year, today.month, today.day)
    except Exception:
        start_dt = datetime(today.year, today.month, today.day)
    try:
        end_dt = datetime.fromisoformat(end_arg) if end_arg else datetime(today.year, today.month, today.day, 23, 59, 59)
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except Exception:
        end_dt = datetime(today.year, today.month, today.day, 23, 59, 59)

    q = db.session.query(Sale).filter(
        Sale.date_time >= start_dt, Sale.date_time <= end_dt, Sale.voided == False
    )
    if user_id_filter:    q = q.filter(Sale.user_id    == user_id_filter)
    if product_id_filter: q = q.filter(Sale.product_id == product_id_filter)
    rows = q.all()

    sale_ids = list({r.sale_id for r in rows})
    consumptions = StockConsumption.query.filter(
        StockConsumption.sale_id.in_(sale_ids)
    ).all() if sale_ids else []

    # Revenue per product
    rev_map = defaultdict(float)
    qty_map = defaultdict(float)
    for r in rows:
        rev_map[r.product_id] += float(Decimal(str(r.qty)) * r.unit_price)
        qty_map[r.product_id] += float(r.qty)

    # COGS: map consumption back to sale → product
    sale_product_map = {}
    for r in rows:
        if r.sale_id not in sale_product_map:
            sale_product_map[r.sale_id] = r.product_id

    cogs_map = defaultdict(float)
    for c in consumptions:
        pid = sale_product_map.get(c.sale_id)
        if pid:
            cogs_map[pid] += float(Decimal(str(c.qty_consumed_base)) * Decimal(str(c.cost_per_base_unit)))

    all_pids = set(rev_map.keys())
    names = {p.id: p.name for p in Product.query.filter(Product.id.in_(all_pids)).all()} if all_pids else {}

    result = []
    for pid in sorted(all_pids, key=lambda x: rev_map[x], reverse=True):
        rev  = rev_map[pid]
        cogs = cogs_map.get(pid, 0)
        profit = rev - cogs
        margin = round(profit / rev * 100, 1) if rev > 0 else None
        result.append({
            'product':  names.get(pid, str(pid)),
            'qty_sold': round(qty_map[pid], 2),
            'revenue':  round(rev, 2),
            'cogs':     round(cogs, 2),
            'profit':   round(profit, 2),
            'margin':   margin,
        })
    return jsonify(result)


# -----------------------------
# UI / Diagnostics
# -----------------------------
@app.route('/')
def index():
    return render_template('index.html', app_env=os.getenv('APP_ENV', 'qa'))

@app.route('/health')
def health_check():
    """Lightweight health check for updater service (localhost only)."""
    return jsonify({
        'status': 'healthy',
        'version': APP_VERSION,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/guide')
def user_guide():
    return render_template('user_guide.html')

@app.route('/api/logs')
def api_logs():
    """Admin-only: return last N lines of the log file."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    n = int(request.args.get('n', 200))
    try:
        with open(LOG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return jsonify({'lines': lines[-n:], 'total': len(lines), 'path': LOG_PATH})
    except FileNotFoundError:
        return jsonify({'lines': [], 'total': 0, 'path': LOG_PATH})


@app.route('/api/db-health')
def api_db_health():
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/__version')
def version():
    return jsonify({'version': APP_VERSION})


# -----------------------------
# Invoices
# -----------------------------
def _next_invoice_number():
    last = db.session.query(Invoice).order_by(Invoice.id.desc()).first()
    if last:
        try:
            num = int(last.invoice_number.split('-')[-1]) + 1
        except Exception:
            num = last.id + 1
    else:
        num = 1
    return f'INV-{num:04d}'


@app.route('/api/invoices', methods=['GET'])
def api_invoices_list():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    invs = db.session.query(Invoice).order_by(Invoice.created_at.desc()).all()
    return jsonify([{
        'id': i.id, 'invoice_number': i.invoice_number,
        'created_at': i.created_at.isoformat() if i.created_at else None,
        'due_date': i.due_date, 'customer_name': i.customer_name,
        'customer_phone': i.customer_phone, 'customer_email': i.customer_email,
        'total': float(i.total), 'status': i.status,
        'customer_id': i.customer_id,
    } for i in invs])


@app.route('/api/invoices', methods=['POST'])
def api_invoices_create():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    lines = data.get('lines', [])
    subtotal = sum(float(l.get('subtotal', 0)) for l in lines)
    disc = float(data.get('discount_pct') or 0)
    total = subtotal * (1 - disc / 100) if disc else subtotal

    # Resolve customer via email match (online orders) or explicit customer_id
    cust_id = data.get('customer_id') or None
    is_online_inv = bool(data.get('notes') and '[ONLINE' in (data.get('notes') or ''))
    if not cust_id and is_online_inv:
        email = (data.get('customer_email') or '').strip()
        name  = (data.get('customer_name')  or '').strip()
        phone = (data.get('customer_phone') or '').strip()
        cust, _ = _resolve_online_customer(email, name, phone)
        cust_id = cust.id

    inv = Invoice(
        invoice_number=_next_invoice_number(),
        due_date=data.get('due_date') or None,
        customer_name=data.get('customer_name') or None,
        customer_phone=data.get('customer_phone') or None,
        customer_email=data.get('customer_email') or None,
        customer_address=data.get('customer_address') or None,
        notes=data.get('notes') or None,
        bank_details=data.get('bank_details') or None,
        lines_json=_json.dumps(lines),
        subtotal=round(subtotal, 2),
        discount_pct=disc or None,
        total=round(total, 2),
        status='draft',
        created_by=current_user().id if current_user() else None,
        customer_id=cust_id,
    )
    db.session.add(inv)
    db.session.commit()
    return jsonify({'id': inv.id, 'invoice_number': inv.invoice_number, 'customer_id': inv.customer_id})


@app.route('/api/invoices/<int:inv_id>', methods=['GET'])
def api_invoices_get(inv_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'id': inv.id, 'invoice_number': inv.invoice_number,
        'created_at': inv.created_at.isoformat() if inv.created_at else None,
        'due_date': inv.due_date, 'customer_name': inv.customer_name,
        'customer_phone': inv.customer_phone, 'customer_email': inv.customer_email,
        'customer_address': inv.customer_address, 'notes': inv.notes, 'bank_details': inv.bank_details,
        'lines': _json.loads(inv.lines_json or '[]'),
        'subtotal': float(inv.subtotal), 'discount_pct': float(inv.discount_pct or 0),
        'total': float(inv.total), 'status': inv.status, 'sale_id': inv.sale_id,
        'customer_id': inv.customer_id,
    })


@app.route('/api/invoices/<int:inv_id>', methods=['POST'])
def api_invoices_update(inv_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    # Invoices with a sale_id have stock already deducted — lock line items but allow
    # status/contact/notes changes so admin can track paid → sent → finalised progression
    if inv.sale_id:
        allowed = ('due_date', 'customer_name', 'customer_phone', 'customer_email',
                   'customer_address', 'notes', 'bank_details', 'status')
        for field in allowed:
            if field in data:
                setattr(inv, field, data[field] or None)
        db.session.commit()
        return jsonify({'ok': True})
    for field in ('due_date', 'customer_name', 'customer_phone', 'customer_email',
                  'customer_address', 'notes', 'bank_details', 'status'):
        if field in data:
            setattr(inv, field, data[field] or None)
    if 'lines' in data:
        lines = data['lines']
        subtotal = sum(float(l.get('subtotal', 0)) for l in lines)
        disc = float(data.get('discount_pct') or inv.discount_pct or 0)
        total = subtotal * (1 - disc / 100) if disc else subtotal
        inv.lines_json = _json.dumps(lines)
        inv.subtotal = round(subtotal, 2)
        inv.discount_pct = disc or None
        inv.total = round(total, 2)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/invoices/<int:inv_id>/delete', methods=['POST'])
def api_invoices_delete(inv_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(inv)
    db.session.commit()
    return jsonify({'ok': True})


_UNIT_TO_BASE = {
    'weight': {'g': 1, 'kg': 1000},
    'volume': {'ml': 1, 'L': 1000},
    'count':  {'unit': 1},
}

@app.route('/api/invoices/<int:inv_id>/finalise', methods=['POST'])
def api_invoices_finalise(inv_id):
    """Create a real sale from the invoice lines, deducting stock via FIFO."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv:
        return jsonify({'error': 'Not found'}), 404
    if inv.sale_id and inv.status == 'finalised':
        # Already finalised — idempotent no-op
        return jsonify({'ok': True, 'sale_id': inv.sale_id})

    is_online = bool(inv.notes and '[ONLINE' in inv.notes)

    # Resolve customer — match by email or use already-linked customer_id
    if not inv.customer_id:
        email = (inv.customer_email or '').strip()
        name  = (inv.customer_name  or '').strip()
        phone = (inv.customer_phone or '').strip()
        if is_online and (email or name or phone):
            cust, _ = _resolve_online_customer(email, name, phone)
            inv.customer_id = cust.id

    if inv.customer_id:
        cust = db.session.get(Customer, inv.customer_id)
        if cust:
            if is_online and not cust.is_online_customer:
                cust.is_online_customer = True
            if not is_online and not cust.is_pos_customer:
                cust.is_pos_customer = True

    if inv.sale_id:
        # Stock already deducted (online order) — just mark as finalised, no stock action
        inv.status = 'finalised'
        db.session.commit()
        return jsonify({'ok': True, 'sale_id': inv.sale_id})

    lines = _json.loads(inv.lines_json or '[]')
    if not lines:
        return jsonify({'error': 'Invoice has no items'}), 400

    sale_uuid    = str(uuid.uuid4())
    now          = datetime.utcnow()
    u            = current_user()
    # Online invoices are tagged with [ONLINE in their notes — attribute sale to Online Shop user
    sale_user_id = get_online_user_id() if is_online else (u.id if u else None)

    for line in lines:
        name       = (line.get('name') or '').strip()
        qty_disp   = Decimal(str(line.get('qty', 1)))
        unit_price = Decimal(str(line.get('unit_price', 0)))
        unit       = line.get('unit', 'unit')

        # Try to match product by exact name, then by prefix (strip unit suffix)
        base_name = name.split('(')[0].strip() if '(' in name else name
        p = Product.query.filter(
            Product.name.ilike(base_name),
            Product.is_archived == False
        ).first()

        if p:
            # Convert display qty to base units for FIFO
            if p.product_type == 'stock_item':
                # weight/volume: convert display unit (kg/L) to base unit (g/ml)
                # count: conv=1 (1 unit = 1 base unit)
                conv     = _UNIT_TO_BASE.get(p.unit_type, {}).get(unit, 1) if (unit and p.unit_type in ('weight', 'volume')) else 1
                qty_base = qty_disp * Decimal(str(conv))
                consume_fifo(p.id, qty_base, sale_uuid, now)
            elif p.product_type == 'simple':
                p.stock_qty = max(0, (p.stock_qty or 0) - int(qty_disp))
            elif p.product_type == 'recipe':
                rl_rows = RecipeLine.query.filter_by(product_id=p.id).all()
                for rl in rl_rows:
                    consume_fifo(rl.ingredient_id, Decimal(str(rl.qty_base)) * qty_disp, sale_uuid, now)

        # Only create a Sale row if we matched a real product — custom line items
        # (no product match) are recorded on the invoice but don't deduct stock.
        if p:
            db.session.add(Sale(
                sale_id=sale_uuid, date_time=now,
                product_id=p.id,
                qty=qty_disp, unit_price=unit_price,
                user_id=sale_user_id,
            ))

    inv.sale_id = sale_uuid
    inv.status  = 'finalised'
    db.session.commit()
    return jsonify({'ok': True, 'sale_id': sale_uuid})


@app.route('/api/invoices/<int:inv_id>/undo', methods=['POST'])
def api_invoices_undo(inv_id):
    """Reverse a finalised invoice: void the sale and restore stock."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    inv = db.session.get(Invoice, inv_id)
    if not inv:
        return jsonify({'error': 'Not found'}), 404
    if not inv.sale_id:
        # Already undone — idempotent no-op
        return jsonify({'ok': True})

    sale_uuid = inv.sale_id
    u   = current_user()
    now = datetime.utcnow()
    stamp = (f"[UNDO] Sale {sale_uuid[:8]} reversed by "
             f"{u.username if u else '?'} @ {now.strftime('%Y-%m-%d %H:%M')} UTC")

    sale_rows = Sale.query.filter_by(sale_id=sale_uuid, voided=False).all()

    for s in sale_rows:
        s.voided    = True
        s.voided_by = u.id if u else None
        s.voided_at = now
        s.void_reason = f'Invoice {inv.invoice_number} undone'

        # Restore stock per product type
        p = db.session.get(Product, s.product_id) if s.product_id else None
        if not p:
            continue
        if p.product_type == 'simple':
            p.stock_qty = (p.stock_qty or 0) + int(s.qty)

    # Restore all FIFO batch consumption for this sale
    consumed = StockConsumption.query.filter_by(sale_id=sale_uuid).all()
    for c in consumed:
        batch = db.session.get(StockBatch, c.batch_id)
        if batch:
            batch.qty_remaining_base = (batch.qty_remaining_base or 0) + c.qty_consumed_base
    StockConsumption.query.filter_by(sale_id=sale_uuid).delete()

    inv.notes   = ((inv.notes or '') + ' ' + stamp).strip()
    inv.sale_id = None
    inv.status  = 'draft'   # reset to draft so it can be re-edited and re-finalised
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/invoices/<int:inv_id>/print')
def invoice_print(inv_id):
    if not require_login():
        return 'Unauthorized', 401
    inv = db.session.get(Invoice, inv_id)
    if not inv:
        return 'Not found', 404
    lines = _json.loads(inv.lines_json or '[]')
    return render_template('invoice.html', inv=inv, lines=lines)

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

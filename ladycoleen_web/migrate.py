"""
Idempotent migration runner - same pattern as POS strong_migrate().
Runs on every app startup. Safe to re-run.
Uses SAVEPOINTs so each step can fail independently without aborting the transaction.
"""
import logging
from sqlalchemy import text

log = logging.getLogger(__name__)

_sp = 0


def run_migrations(db):
    with db.engine.connect() as conn:
        _create_tables(conn)
        _create_phase2_tables(conn)
        _create_password_reset_tokens(conn)
        _create_payment_sessions(conn)
        _add_missing_columns(conn)
        _add_constraints(conn)
        _add_indexes(conn)
        conn.commit()
    log.info("Migrations complete")


def _exec(conn, sql, label=""):
    global _sp
    _sp += 1
    sp = f"mig_{_sp}"
    try:
        conn.execute(text(f"SAVEPOINT {sp}"))
        conn.execute(text(sql))
        conn.execute(text(f"RELEASE SAVEPOINT {sp}"))
    except Exception as e:
        conn.execute(text(f"ROLLBACK TO SAVEPOINT {sp}"))
        conn.execute(text(f"RELEASE SAVEPOINT {sp}"))
        log.debug("Migration step '%s' skipped: %s", label or sql[:80], e)


def _create_tables(conn):
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS web_customers (
            id              SERIAL PRIMARY KEY,
            name            VARCHAR(200) NOT NULL,
            email           VARCHAR(200) UNIQUE NOT NULL,
            phone           VARCHAR(50),
            password_hash   TEXT NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT now(),
            deleted_at      TIMESTAMPTZ,
            pos_customer_id INTEGER
        )
    """, "create web_customers")

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS cake_orders (
            id                  SERIAL PRIMARY KEY,
            reference           VARCHAR(20) UNIQUE NOT NULL,
            web_customer_id     INTEGER REFERENCES web_customers(id),
            guest_name          VARCHAR(200),
            guest_email         VARCHAR(200),
            guest_phone         VARCHAR(50),
            status              VARCHAR(30) NOT NULL DEFAULT 'pending',
            date_required       DATE NOT NULL,
            size                VARCHAR(100) NOT NULL,
            flavor              VARCHAR(200) NOT NULL,
            serves              INTEGER,
            design_description  TEXT,
            image_path          TEXT,
            admin_notes         TEXT,
            quoted_price        NUMERIC(10,2),
            invoice_id          INTEGER,
            created_at          TIMESTAMPTZ DEFAULT now(),
            updated_at          TIMESTAMPTZ DEFAULT now()
        )
    """, "create cake_orders")

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS payments (
            id               SERIAL PRIMARY KEY,
            reference        VARCHAR(100) UNIQUE,
            order_type       VARCHAR(20) NOT NULL,
            order_id         INTEGER NOT NULL,
            amount           NUMERIC(10,2) NOT NULL,
            method           VARCHAR(20) NOT NULL DEFAULT 'eft',
            status           VARCHAR(20) NOT NULL DEFAULT 'pending',
            proof_path       TEXT,
            external_payload JSONB,
            paid_at          TIMESTAMPTZ,
            notes            TEXT,
            created_at       TIMESTAMPTZ DEFAULT now()
        )
    """, "create payments")


def _create_phase2_tables(conn):
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS online_orders (
            id               SERIAL PRIMARY KEY,
            reference        VARCHAR(20) UNIQUE,
            web_customer_id  INTEGER REFERENCES web_customers(id),
            guest_name       VARCHAR(200),
            guest_email      VARCHAR(200),
            guest_phone      VARCHAR(50),
            status           VARCHAR(30) NOT NULL DEFAULT 'pending',
            delivery_method  VARCHAR(20) NOT NULL DEFAULT 'collection',
            delivery_address TEXT,
            notes            TEXT,
            subtotal         NUMERIC(10,2),
            shipping_fee     NUMERIC(10,2) DEFAULT 0,
            total            NUMERIC(10,2),
            pos_sale_id      VARCHAR(36) UNIQUE,
            invoice_id       INTEGER,
            pudo_point_name  VARCHAR(200),
            pudo_suburb      VARCHAR(100),
            pudo_city        VARCHAR(100),
            pudo_point_id    VARCHAR(50),
            created_at       TIMESTAMPTZ DEFAULT now(),
            updated_at       TIMESTAMPTZ DEFAULT now()
        )
    """, "create online_orders")

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS online_order_lines (
            id                   SERIAL PRIMARY KEY,
            online_order_id      INTEGER REFERENCES online_orders(id) NOT NULL,
            product_id           INTEGER NOT NULL,
            product_name_snapshot VARCHAR(200),
            qty                  NUMERIC(10,4) NOT NULL,
            unit_price           NUMERIC(10,2) NOT NULL,
            line_total           NUMERIC(10,2) NOT NULL
        )
    """, "create online_order_lines")

    # Drop and recreate to add 'draft' (for undone orders)
    _exec(conn, "ALTER TABLE online_orders DROP CONSTRAINT IF EXISTS chk_online_status",
          "drop chk_online_status")
    _exec(conn, """
        ALTER TABLE online_orders ADD CONSTRAINT chk_online_status
        CHECK (status IN ('draft','pending','confirmed','ready','dispatched','completed','cancelled'))
    """, "chk_online_status")

    # Drop and recreate to add 'pudo' (SAVEPOINT handles failure if already correct)
    _exec(conn, "ALTER TABLE online_orders DROP CONSTRAINT IF EXISTS chk_online_delivery",
          "drop chk_online_delivery")
    _exec(conn, """
        ALTER TABLE online_orders ADD CONSTRAINT chk_online_delivery
        CHECK (delivery_method IN ('collection','delivery','pudo'))
    """, "chk_online_delivery")

    _exec(conn, """
        ALTER TABLE online_orders ADD CONSTRAINT chk_confirmed_has_sale
        CHECK (status = 'pending' OR pos_sale_id IS NOT NULL
               OR status IN ('cancelled'))
    """, "chk_confirmed_has_sale")

    # Pudo columns for existing tables
    _exec(conn, "ALTER TABLE online_orders ADD COLUMN IF NOT EXISTS invoice_id INTEGER",
          "online_orders.invoice_id")
    _exec(conn, "ALTER TABLE online_orders ADD COLUMN IF NOT EXISTS pudo_point_name VARCHAR(200)",
          "online_orders.pudo_point_name")
    _exec(conn, "ALTER TABLE online_orders ADD COLUMN IF NOT EXISTS pudo_suburb VARCHAR(100)",
          "online_orders.pudo_suburb")
    _exec(conn, "ALTER TABLE online_orders ADD COLUMN IF NOT EXISTS pudo_city VARCHAR(100)",
          "online_orders.pudo_city")
    _exec(conn, "ALTER TABLE online_orders ADD COLUMN IF NOT EXISTS pudo_point_id VARCHAR(50)",
          "online_orders.pudo_point_id")
    _exec(conn, "ALTER TABLE online_orders ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(100)",
          "online_orders.payment_reference")
    _exec(conn, "ALTER TABLE online_orders ADD COLUMN IF NOT EXISTS shipping_fee NUMERIC(10,2) DEFAULT 0",
          "online_orders.shipping_fee")

    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_online_orders_status ON online_orders(status)",
          "idx_online_orders_status")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_online_order_lines_order ON online_order_lines(online_order_id)",
          "idx_online_order_lines_order")


def _create_password_reset_tokens(conn):
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         SERIAL PRIMARY KEY,
            token      VARCHAR(64) UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL REFERENCES web_customers(id) ON DELETE CASCADE,
            used       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT now(),
            expires_at TIMESTAMPTZ DEFAULT now() + INTERVAL '1 hour'
        )
    """, "create password_reset_tokens")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_prt_token ON password_reset_tokens(token)",
          "idx_prt_token")


def _create_payment_sessions(conn):
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS payment_sessions (
            id           SERIAL PRIMARY KEY,
            session_id   VARCHAR(64) UNIQUE NOT NULL,
            cart_json    TEXT NOT NULL,
            customer_json TEXT NOT NULL,
            delivery_json TEXT NOT NULL,
            amount       NUMERIC(10,2) NOT NULL,
            status       VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at   TIMESTAMPTZ DEFAULT now(),
            expires_at   TIMESTAMPTZ DEFAULT now() + INTERVAL '2 hours'
        )
    """, "create payment_sessions")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_payment_sessions_id ON payment_sessions(session_id)",
          "idx_payment_sessions_id")


def _add_missing_columns(conn):
    _exec(conn, "ALTER TABLE products ADD COLUMN IF NOT EXISTS description TEXT",
          "products.description")
    _exec(conn, "ALTER TABLE web_customers ADD COLUMN IF NOT EXISTS pos_customer_id INTEGER",
          "web_customers.pos_customer_id")
    _exec(conn, "ALTER TABLE web_customers ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
          "web_customers.deleted_at")
    _exec(conn, "ALTER TABLE cake_orders ADD COLUMN IF NOT EXISTS serves INTEGER",
          "cake_orders.serves")
    _exec(conn, "ALTER TABLE payments ADD COLUMN IF NOT EXISTS external_payload JSONB",
          "payments.external_payload")
    _exec(conn, "ALTER TABLE payment_sessions ADD COLUMN IF NOT EXISTS pf_payment_id VARCHAR(64)",
          "payment_sessions.pf_payment_id")


def _add_constraints(conn):
    # Drop and recreate to add 'customer_confirmed'
    _exec(conn, "ALTER TABLE cake_orders DROP CONSTRAINT IF EXISTS chk_cake_status",
          "drop chk_cake_status")
    _exec(conn, """
        ALTER TABLE cake_orders ADD CONSTRAINT chk_cake_status
        CHECK (status IN ('pending','quoted','customer_confirmed','confirmed','in_production','completed','cancelled'))
    """, "chk_cake_status")

    _exec(conn, """
        ALTER TABLE payments ADD CONSTRAINT chk_payment_status
        CHECK (status IN ('pending','paid','failed'))
    """, "chk_payment_status")

    # Drop and recreate to add paygate and card
    _exec(conn, "ALTER TABLE payments DROP CONSTRAINT IF EXISTS chk_payment_method",
          "drop chk_payment_method")
    _exec(conn, """
        ALTER TABLE payments ADD CONSTRAINT chk_payment_method
        CHECK (method IN ('eft','payfast','paygate','card'))
    """, "chk_payment_method")

    _exec(conn, """
        ALTER TABLE payments ADD CONSTRAINT chk_payment_order_type
        CHECK (order_type IN ('cake','farmshop'))
    """, "chk_payment_order_type")


def _add_indexes(conn):
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_cake_orders_status ON cake_orders(status)",
          "idx_cake_orders_status")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_cake_orders_email ON cake_orders(guest_email)",
          "idx_cake_orders_email")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)",
          "idx_payments_status")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_type, order_id)",
          "idx_payments_order")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_web_customers_email ON web_customers(email)",
          "idx_web_customers_email")

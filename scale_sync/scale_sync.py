"""
BC-4000 Scale Sync Service

POS is the single source of truth. Scale is a downstream cache.

Architecture (confirmed from Wireshark 2026-06-23):
  - PC connects OUT to scale:7061 (scale is the server)
  - Poll loop runs every SYNC_INTERVAL_SEC seconds
  - On each cycle: connect, push changed PLUs, disconnect
  - Auto-reconnects after any scale or PC restart — no manual intervention needed

Protocol: MsgNo 1001 (PLU push), MsgNo 0026 (status/delete)
"""
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import psycopg
import psycopg.rows

from bc4000_client import send_chunk, poll_status, ProtocolError, MSG_NO_PLU_SEND
from plu_formatter import (
    should_sync, validate_for_scale, format_full_plu, format_delete_plu,
    format_prohibit_plu, compute_scale_hash, ScaleSyncValidationError,
    MSG_NO_FULL_PLU, MSG_NO_KEYBOARD, MSG_NO_ADVERT,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

APP_ENV   = os.environ.get('APP_ENV', 'prod').lower()
SCALE_IP  = os.environ['SCALE_IP']
SCALE_PORT    = int(os.environ.get('SCALE_PORT', '7061'))
SCALE_TIMEOUT = int(os.environ.get('SCALE_TIMEOUT_SEC', '10'))

POSTGRES_HOST     = os.environ.get('POSTGRES_HOST', 'localhost')
POSTGRES_PORT     = int(os.environ.get('POSTGRES_PORT', '5432'))
POSTGRES_DB       = os.environ.get('POSTGRES_DB', 'farm_pos_prod')
POSTGRES_USER     = os.environ.get('POSTGRES_USER', 'farmstall')
POSTGRES_PASSWORD = os.environ['POSTGRES_PASSWORD']

POSTGRES_DB_QA    = os.environ.get('POSTGRES_DB_QA', 'farm_pos_qa')

SYNC_INTERVAL   = int(os.environ.get('SYNC_INTERVAL_SEC', '60'))
CHUNK_SIZE      = int(os.environ.get('CHUNK_SIZE', '50'))
DRY_RUN         = os.environ.get('DRY_RUN', 'false').lower() == 'true'
FORCE_FULL_SYNC = os.environ.get('FORCE_FULL_SYNC', 'false').lower() == 'true'

LOG_PREFIX = f"[{APP_ENV.upper()}][{POSTGRES_DB}][{SCALE_IP}]"

DATA_DIR          = Path('/data') / APP_ENV
SUMMARY_FILE      = DATA_DIR / 'sync_summary.json'
HEARTBEAT_FILE    = DATA_DIR / 'last_success.timestamp'
SYNC_SOURCE_FILE  = Path('/data/sync_source.json')

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger('scale_sync')

if DRY_RUN:
    logger.warning('DRY_RUN=true -- no data will be sent to scale')
if FORCE_FULL_SYNC:
    logger.warning('FORCE_FULL_SYNC=true -- ignoring scale_hash (full resync)')

# ---------------------------------------------------------------------------
# Atomic file writes
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: str):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(data, encoding='utf-8')
    tmp.replace(path)

def write_heartbeat():
    _atomic_write(HEARTBEAT_FILE, datetime.now(timezone.utc).isoformat())

def write_summary(sent, failed, deleted, orphans):
    _atomic_write(SUMMARY_FILE, json.dumps({
        'last_run': datetime.now(timezone.utc).isoformat(),
        'sent': sent, 'failed': failed,
        'deleted': deleted, 'orphans_detected': orphans,
    }, indent=2))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def read_sync_source() -> str:
    try:
        data = json.loads(SYNC_SOURCE_FILE.read_text())
        src = data.get('source', 'prod')
        if src in ('prod', 'qa', 'none'):
            return src
    except Exception:
        pass
    return 'prod'


def _db_connect(db_name: str = None):
    return psycopg.connect(
        host=POSTGRES_HOST, port=POSTGRES_PORT,
        dbname=db_name or POSTGRES_DB, user=POSTGRES_USER, password=POSTGRES_PASSWORD,
        connect_timeout=10, row_factory=psycopg.rows.dict_row,
        autocommit=True,
    )

def db_connect_with_retry(retries=10, delay=3, db_name: str = None):
    for attempt in range(retries):
        try:
            conn = _db_connect(db_name)
            logger.info(f"Database connection established ({db_name or POSTGRES_DB}).")
            return conn
        except Exception as e:
            if attempt == retries - 1: raise
            logger.warning(f"DB not ready (attempt {attempt+1}/{retries}): {e}")
            time.sleep(delay)


def wait_for_schema(conn, retries=20, delay=5):
    required = ['products', 'scale_sync_runs', 'scale_plu_log']
    for attempt in range(retries):
        try:
            rows = conn.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = ANY(%(tables)s)
            """, {'tables': required}).fetchall()
            found = {r['table_name'] for r in rows}
            missing = [t for t in required if t not in found]
            if not missing:
                logger.info("Schema ready.")
                return
            logger.warning(f"Waiting for POS migrations — missing: {missing} (attempt {attempt+1}/{retries})")
        except Exception as e:
            logger.warning(f"Schema check failed: {e}")
        time.sleep(delay)
    raise RuntimeError(f"Required tables still missing after {retries} attempts.")

def fetch_keyboard_presets(conn) -> List[dict]:
    """Fetch all 170 keyboard slots. Empty slots come back as plu_no=NULL."""
    rows = conn.execute("""
        SELECT key_id, plu_no, label
        FROM scale_keyboard_presets
        ORDER BY key_id
    """).fetchall()
    conn.commit()
    preset_map = {r['key_id']: r for r in rows}

    # Build lookup: key_id → product_code
    plu_ids = [r['plu_no'] for r in rows if r['plu_no']]
    product_codes = {}
    if plu_ids:
        prod_rows = conn.execute("""
            SELECT id, product_code FROM products WHERE id = ANY(%(ids)s)
        """, {'ids': plu_ids}).fetchall()
        conn.commit()
        product_codes = {r['id']: r['product_code'] for r in prod_rows}

    slots = []
    for key_id in range(1, 171):
        p = preset_map.get(key_id)
        plu_no = 0
        if p and p['plu_no']:
            plu_no = product_codes.get(p['plu_no'], 0)
        slots.append({'key_id': key_id, 'plu_no': plu_no})
    return slots


def fetch_advert_messages(conn) -> List[dict]:
    """Fetch all 43 advert slots."""
    rows = conn.execute("""
        SELECT slot, display_no, text, enabled
        FROM scale_advert_messages
        ORDER BY slot
    """).fetchall()
    conn.commit()
    advert_map = {r['slot']: r for r in rows}

    slots = []
    for slot in range(1, 44):
        a = advert_map.get(slot)
        slots.append({
            'slot':       slot,
            'display_no': a['display_no'] if a else 2,
            'text':       a['text'] if a else '',
            'enabled':    a['enabled'] if a else False,
        })
    return slots


def format_keyboard_record(slot: dict) -> bytes:
    """Format one keyboard preset record for MsgNo 1024."""
    plu_no = slot['plu_no'] or 0
    assigned = 1 if plu_no else 0
    csv = f"{slot['key_id']},0,{assigned},{plu_no},"
    return csv.encode('utf-8')


def format_advert_record(slot: dict) -> bytes:
    """Format one advertisement record for MsgNo 1029."""
    text    = (slot['text'] or '').replace('"', '""')
    enabled = 1 if slot['enabled'] and slot['text'] else 0
    csv = f"{slot['slot']},{slot['display_no']},0,0,\"{text}\",{enabled},"
    return csv.encode('utf-8')


def fetch_scale_products(conn) -> List[dict]:
    rows = conn.execute("""
        SELECT id, name, price, price_per_unit, sold_by_weight,
               is_archived, is_for_sale, product_type,
               barcode, product_code, sync_to_scale,
               scale_tare, scale_shelf_life, scale_pack_qty,
               scale_open_price, scale_msg1, scale_msg2,
               scale_hash, scale_last_sync_status
        FROM products
        WHERE sync_to_scale = TRUE
        ORDER BY product_code
    """).fetchall()
    conn.commit()
    return rows

def fetch_synced_codes(conn) -> set:
    rows = conn.execute("""
        SELECT product_code FROM products
        WHERE scale_last_sync_status IN ('ok', 'plu_changed')
          AND product_code IS NOT NULL
    """).fetchall()
    conn.commit()
    return {r['product_code'] for r in rows}

def fetch_plu_changed(conn) -> list:
    rows = conn.execute("""
        SELECT p.id, p.product_code as new_plu, l.old_plu
        FROM products p
        JOIN scale_plu_log l ON l.product_id = p.id
        WHERE p.scale_last_sync_status = 'plu_changed'
          AND l.sync_cleared = FALSE
          AND l.old_plu IS NOT NULL
        ORDER BY l.changed_at
    """).fetchall()
    conn.commit()
    return rows

def mark_product_synced(conn, product_id: int, scale_hash: str):
    conn.execute("""
        UPDATE products SET
            scale_last_synced_at = NOW(),
            scale_last_sync_status = 'ok',
            scale_last_sync_error = NULL,
            scale_hash = %(hash)s
        WHERE id = %(id)s
    """, {'hash': scale_hash, 'id': product_id})

def mark_product_failed(conn, product_id: int, error: str):
    conn.execute("""
        UPDATE products SET
            scale_last_sync_status = 'error',
            scale_last_sync_error = %(error)s
        WHERE id = %(id)s
    """, {'error': error[:500], 'id': product_id})

def create_sync_run(conn, run_type: str) -> int:
    row = conn.execute("""
        INSERT INTO scale_sync_runs (started_at, run_type, status,
            products_total, products_sent, products_failed, orphans_detected, orphans_removed)
        VALUES (NOW(), %(run_type)s, 'running', 0, 0, 0, 0, 0)
        RETURNING id
    """, {'run_type': run_type}).fetchone()
    return row['id']

def complete_sync_run(conn, run_id: int, status: str, sent=0, failed=0, orphans=0, removed=0, error=None):
    conn.execute("""
        UPDATE scale_sync_runs SET
            completed_at = NOW(),
            status = %(status)s,
            products_sent = %(sent)s,
            products_failed = %(failed)s,
            orphans_detected = %(orphans)s,
            orphans_removed = %(removed)s,
            error_message = %(error)s
        WHERE id = %(id)s
    """, {'status': status, 'sent': sent, 'failed': failed,
          'orphans': orphans, 'removed': removed, 'error': error, 'id': run_id})

# ---------------------------------------------------------------------------
# Scale connectivity (outbound — we connect to scale)
# ---------------------------------------------------------------------------

def scale_reachable() -> bool:
    import socket as _socket
    try:
        s = _socket.create_connection((SCALE_IP, SCALE_PORT), timeout=SCALE_TIMEOUT)
        s.close()
        return True
    except Exception as e:
        logger.warning(f"{LOG_PREFIX} Scale unreachable at {SCALE_IP}:{SCALE_PORT} — {e}")
        return False

def _send_with_retry(msg_no, records, label=''):
    for attempt in range(3):
        try:
            return send_chunk(
                host=SCALE_IP, port=SCALE_PORT, timeout=SCALE_TIMEOUT,
                msg_no=msg_no, records=records,
            ), None
        except (ProtocolError, Exception) as e:
            last_exc = e
            logger.warning(f"{LOG_PREFIX} {label} attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
    return None, Exception(f"{LOG_PREFIX} {label} failed after 3 retries")

def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]

# ---------------------------------------------------------------------------
# Main sync cycle
# ---------------------------------------------------------------------------

def run_sync_cycle(conn):
    products = fetch_scale_products(conn)

    to_send           = []
    to_skip           = []
    validation_errors = []

    for p in products:
        if p['is_archived'] or not p['is_for_sale']:
            continue

        try:
            validate_for_scale(p)
        except ScaleSyncValidationError as e:
            validation_errors.append((p['id'], str(e)))
            mark_product_failed(conn, p['id'], str(e))
            logger.warning(f"Validation error: {e}")
            continue

        new_hash = compute_scale_hash(p)
        if not FORCE_FULL_SYNC and p.get('scale_hash') == new_hash and p.get('scale_last_sync_status') == 'ok':
            to_skip.append(p['id'])
            continue

        try:
            payload = format_full_plu(p)
        except Exception as e:
            validation_errors.append((p['id'], str(e)))
            mark_product_failed(conn, p['id'], str(e))
            logger.warning(f"Format error for product {p['id']}: {e}")
            continue

        to_send.append((p, payload, new_hash))

    # PLU changes: delete old PLU from scale before sending new one
    plu_changes = fetch_plu_changed(conn)
    if plu_changes:
        logger.info(f"PLU changes pending: {[(r['old_plu'], r['new_plu']) for r in plu_changes]}")
        old_plu_records = [format_prohibit_plu(r['old_plu']) for r in plu_changes]
        if DRY_RUN:
            for r in plu_changes:
                logger.info(f"DRY_RUN: prohibit old PLU {r['old_plu']} (→ {r['new_plu']})")
        else:
            result, exc = _send_with_retry(MSG_NO_FULL_PLU, old_plu_records, label='prohibit old PLUs')
            if result is not None:
                conn.execute("""
                    UPDATE scale_plu_log SET sync_cleared = TRUE
                    WHERE sync_cleared = FALSE AND old_plu = ANY(%(old_plus)s)
                """, {'old_plus': [r['old_plu'] for r in plu_changes]})
                conn.execute("""
                    UPDATE products SET scale_last_sync_status = NULL, scale_hash = NULL
                    WHERE id = ANY(%(ids)s)
                """, {'ids': [r['id'] for r in plu_changes]})
                logger.info(f"Prohibited {len(plu_changes)} old PLUs, products queued for resync")
            else:
                logger.error(f"Failed to prohibit old PLUs: {exc}")

    # Orphan detection: PLUs previously synced that are no longer active
    synced_codes = fetch_synced_codes(conn)
    active_codes = {p['product_code'] for p in products if p.get('product_code')}
    orphan_codes = synced_codes - active_codes

    if to_skip:
        logger.info(f"In sync (skipped): {len(to_skip)} products")
    if not to_send and not orphan_codes and not plu_changes:
        logger.info("No changes to sync.")
        write_heartbeat()
        write_summary(0, 0, 0, 0)
        return

    logger.info(f"Pending: {len(to_send)} products to send, {len(orphan_codes)} orphans to delete")

    if not DRY_RUN and not scale_reachable():
        logger.error(f"{LOG_PREFIX} Scale unreachable — skipping sync cycle, will retry in {SYNC_INTERVAL}s")
        return

    run_id = create_sync_run(conn, 'full' if FORCE_FULL_SYNC else 'incremental')
    conn.commit()

    total_sent = total_failed = 0

    # Send updated PLUs
    for chunk in chunked(to_send, CHUNK_SIZE):
        products_in_chunk = [p for p, _, _ in chunk]
        payloads          = [pl for _, pl, _ in chunk]
        hashes            = {p['id']: h for p, _, h in chunk}

        if DRY_RUN:
            for p, pl, h in chunk:
                logger.info(f"DRY_RUN: send PLU {p['product_code']} ({p['name']})")
                mark_product_synced(conn, p['id'], h)
            total_sent += len(chunk)
            conn.commit()
            continue

        result, exc = _send_with_retry(MSG_NO_FULL_PLU, payloads, label='send chunk')
        if result is None:
            for p in products_in_chunk:
                mark_product_failed(conn, p['id'], str(exc))
                total_failed += 1
            conn.commit()
            logger.error(f"Chunk send failed: {exc}")
        else:
            for p in products_in_chunk:
                mark_product_synced(conn, p['id'], hashes[p['id']])
                total_sent += 1
            conn.commit()
            logger.info(f"Sent {len(chunk)} PLUs: updated={result['updated']} {result['duration_ms']:.0f}ms")

    # Remove orphan PLUs by overwriting with a prohibited/zeroed record
    orphans_removed = 0
    if orphan_codes:
        logger.info(f"Prohibiting {len(orphan_codes)} orphan PLUs: {sorted(orphan_codes)}")
        zero_records = [format_prohibit_plu(code) for code in sorted(orphan_codes)]

        if DRY_RUN:
            for code in sorted(orphan_codes):
                logger.info(f"DRY_RUN: zero-out PLU {code}")
            orphans_removed = len(orphan_codes)
        else:
            result, exc = _send_with_retry(MSG_NO_FULL_PLU, zero_records, label='zero orphans')
            if result is not None:
                orphans_removed = len(orphan_codes)
                conn.execute("""
                    UPDATE products SET scale_last_sync_status = 'removed'
                    WHERE product_code = ANY(%(codes)s)
                """, {'codes': list(orphan_codes)})
                conn.commit()
                logger.info(f"Zeroed {orphans_removed} orphan PLUs")
            else:
                logger.error(f"Orphan zero-out failed: {exc}")

    complete_sync_run(conn, run_id, 'ok' if total_failed == 0 else 'partial',
                      sent=total_sent, failed=total_failed,
                      orphans=len(orphan_codes), removed=orphans_removed)
    conn.commit()
    write_heartbeat()
    write_summary(total_sent, total_failed, orphans_removed, len(orphan_codes))
    logger.info(f"Sync complete: sent={total_sent} failed={total_failed} orphans_removed={orphans_removed}")

    # Push keyboard presets
    if not DRY_RUN:
        try:
            kb_slots = fetch_keyboard_presets(conn)
            kb_records = [format_keyboard_record(s) for s in kb_slots]
            result, exc = _send_with_retry(MSG_NO_KEYBOARD, kb_records, label='keyboard presets')
            if result is not None:
                logger.info(f"Keyboard presets sent: updated={result['updated']} {result['duration_ms']:.0f}ms")
            else:
                logger.warning(f"Keyboard preset sync failed: {exc}")
        except Exception as e:
            logger.warning(f"Keyboard preset sync error: {e}")

    # Push advert messages
    if not DRY_RUN:
        try:
            ad_slots = fetch_advert_messages(conn)
            ad_records = [format_advert_record(s) for s in ad_slots]
            result, exc = _send_with_retry(MSG_NO_ADVERT, ad_records, label='advert messages')
            if result is not None:
                logger.info(f"Advert messages sent: updated={result['updated']} {result['duration_ms']:.0f}ms")
            else:
                logger.warning(f"Advert message sync failed: {exc}")
        except Exception as e:
            logger.warning(f"Advert message sync error: {e}")

# ---------------------------------------------------------------------------
# Entry point — outbound poll loop (auto-reconnects on scale/PC restart)
# ---------------------------------------------------------------------------

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"{LOG_PREFIX} Scale sync service starting")
    logger.info(f"{LOG_PREFIX}   Scale:    {SCALE_IP}:{SCALE_PORT}")
    logger.info(f"{LOG_PREFIX}   Interval: {SYNC_INTERVAL}s  DRY_RUN={DRY_RUN}  FORCE_FULL_SYNC={FORCE_FULL_SYNC}")
    logger.info(f"{LOG_PREFIX}   Sync source file: {SYNC_SOURCE_FILE}")

    current_source = read_sync_source()
    active_db = POSTGRES_DB_QA if current_source == 'qa' else POSTGRES_DB
    logger.info(f"{LOG_PREFIX}   Initial sync source: {current_source} → {active_db}")

    conn = db_connect_with_retry(db_name=active_db)
    wait_for_schema(conn)

    while True:
        try:
            new_source = read_sync_source()

            if new_source == 'none':
                logger.info(f"{LOG_PREFIX} Sync source=none — paused, skipping cycle")
                time.sleep(SYNC_INTERVAL)
                continue

            new_db = POSTGRES_DB_QA if new_source == 'qa' else POSTGRES_DB
            if new_source != current_source:
                logger.info(f"{LOG_PREFIX} Sync source changed: {current_source} → {new_source} (db: {active_db} → {new_db})")
                try: conn.close()
                except Exception: pass
                current_source = new_source
                active_db = new_db
                conn = db_connect_with_retry(db_name=active_db)
                wait_for_schema(conn)

            # Reconnect DB if connection dropped
            try:
                conn.execute("SELECT 1")
            except Exception:
                logger.warning(f"{LOG_PREFIX} DB connection lost — reconnecting to {active_db}")
                try: conn.close()
                except Exception: pass
                conn = db_connect_with_retry(retries=5, delay=5, db_name=active_db)

            logger.info(f"{LOG_PREFIX} Syncing from [{current_source.upper()}] db={active_db}")
            run_sync_cycle(conn)

        except Exception as e:
            logger.error(f"{LOG_PREFIX} Sync cycle error: {e}", exc_info=True)

        logger.debug(f"Sleeping {SYNC_INTERVAL}s")
        time.sleep(SYNC_INTERVAL)


if __name__ == '__main__':
    main()

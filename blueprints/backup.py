import glob
import hashlib
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import urlparse, unquote

import requests as _req
from flask import Blueprint, current_app, jsonify, request

from helpers import get_setting, require_role, set_setting
from models import BackupLog, db

bp = Blueprint('backup', __name__)
logger = logging.getLogger('pos')

# ── Google API endpoints ────────────────────────────────────────────────────
GOOGLE_DEVICE_CODE_URL = 'https://oauth2.googleapis.com/device/code'
GOOGLE_TOKEN_URL       = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL    = 'https://www.googleapis.com/oauth2/v3/userinfo'
GOOGLE_DRIVE_SCOPE     = 'https://www.googleapis.com/auth/drive.file'
DRIVE_FILES_URL        = 'https://www.googleapis.com/drive/v3/files'
DRIVE_UPLOAD_URL       = 'https://www.googleapis.com/upload/drive/v3/files'

# ── Module-level state ───────────────────────────────────────────────────────
_token_cache = {'access_token': None, 'expires_at': 0.0}
_backup_lock = threading.Lock()  # prevents concurrent backups

# _pending_auth is intentionally NOT a module-level dict — gunicorn workers have
# separate memory, so start/poll could land on different workers.  We persist the
# pending state in two settings keys instead so all workers share the same view.
def _set_pending_auth(nonce: str, state: dict):
    set_setting('backup_gdrive_pending_nonce', nonce)
    set_setting('backup_gdrive_pending_state', json.dumps(state))

def _get_pending_auth(nonce: str):
    stored = get_setting('backup_gdrive_pending_nonce', '')
    if stored != nonce:
        return None
    raw = get_setting('backup_gdrive_pending_state', '')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def _clear_pending_auth():
    set_setting('backup_gdrive_pending_nonce', '')
    set_setting('backup_gdrive_pending_state', '')


# ── Database switching ────────────────────────────────────────────────────────
_DB_OVERRIDE_FILE = '/tmp/farmpos_db_override'

def _get_active_db() -> str:
    """Return the currently active database name."""
    try:
        with open(_DB_OVERRIDE_FILE) as f:
            url = f.read().strip()
            if url:
                return _parse_db_url(url)['dbname']
    except FileNotFoundError:
        pass
    base_url = os.environ.get('DATABASE_URL', '')
    return _parse_db_url(base_url)['dbname'] if base_url else ''

def _list_databases() -> list:
    """Return all non-template databases on the server."""
    import psycopg
    base_url = os.environ.get('DATABASE_URL', '')
    if not base_url:
        return []
    p = _parse_db_url(base_url)
    with psycopg.connect(host=p['host'], port=int(p['port']), user=p['user'],
                         password=p['password'], dbname='postgres') as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"
        ).fetchall()
    return [r[0] for r in rows]


# ── Config helpers ───────────────────────────────────────────────────────────
def _client_id():
    return current_app.config.get('GOOGLE_CLIENT_ID', '')

def _client_secret():
    return current_app.config.get('GOOGLE_CLIENT_SECRET', '')


# ── OAuth / access token ─────────────────────────────────────────────────────
def _get_access_token() -> str:
    """Return a valid access token, refreshing via the stored refresh token if needed."""
    if _token_cache['access_token'] and time.time() < _token_cache['expires_at'] - 30:
        return _token_cache['access_token']
    refresh_token = get_setting('backup_gdrive_refresh_token', '')
    if not refresh_token:
        raise RuntimeError('Google Drive not connected — no refresh token stored')
    r = _req.post(GOOGLE_TOKEN_URL, data={
        'client_id':     _client_id(),
        'client_secret': _client_secret(),
        'refresh_token': refresh_token,
        'grant_type':    'refresh_token',
    }, timeout=15)
    r.raise_for_status()
    j = r.json()
    _token_cache['access_token'] = j['access_token']
    _token_cache['expires_at']   = time.time() + j.get('expires_in', 3600)
    return _token_cache['access_token']

def _gdrive_headers() -> dict:
    return {'Authorization': f'Bearer {_get_access_token()}'}


# ── Google Drive helpers ─────────────────────────────────────────────────────
def _gdrive_ensure_folder(name: str) -> str:
    """Return Drive folder ID for `name`, creating it if needed. Stores the ID in settings."""
    folder_id = get_setting('backup_gdrive_folder_id', '')
    if folder_id:
        return folder_id
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r = _req.get(DRIVE_FILES_URL, headers=_gdrive_headers(),
                 params={'q': q, 'fields': 'files(id)', 'pageSize': 1}, timeout=15)
    r.raise_for_status()
    files = r.json().get('files', [])
    if files:
        folder_id = files[0]['id']
    else:
        cr = _req.post(DRIVE_FILES_URL, headers=_gdrive_headers(),
                       json={'name': name, 'mimeType': 'application/vnd.google-apps.folder'},
                       timeout=15)
        cr.raise_for_status()
        folder_id = cr.json()['id']
    set_setting('backup_gdrive_folder_id', folder_id)
    return folder_id


def _gdrive_list(folder_id: str) -> list:
    """List backup files oldest-first."""
    q = f"'{folder_id}' in parents and trashed=false"
    r = _req.get(DRIVE_FILES_URL, headers=_gdrive_headers(), params={
        'q': q,
        'fields': 'files(id,name,size,createdTime,description)',
        'pageSize': 1000,
        'orderBy': 'createdTime asc',
    }, timeout=15)
    r.raise_for_status()
    return r.json().get('files', [])


def _gdrive_upload(folder_id: str, filename: str, data: bytes, description_json: str) -> str:
    """Upload bytes as a Drive file; return the file ID."""
    boundary = 'fposboundary42'
    meta_json = json.dumps({'name': filename, 'parents': [folder_id], 'description': description_json})
    body = (
        f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
        + meta_json.encode()
        + f'\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        + data
        + f'\r\n--{boundary}--'.encode()
    )
    r = _req.post(DRIVE_UPLOAD_URL, headers={
        **_gdrive_headers(),
        'Content-Type': f'multipart/related; boundary={boundary}',
    }, params={'uploadType': 'multipart', 'fields': 'id'}, data=body, timeout=120)
    r.raise_for_status()
    return r.json()['id']


def _gdrive_download(file_id: str) -> bytes:
    r = _req.get(f'{DRIVE_FILES_URL}/{file_id}', headers=_gdrive_headers(),
                 params={'alt': 'media'}, timeout=120)
    r.raise_for_status()
    return r.content


def _gdrive_get_meta(file_id: str) -> dict:
    r = _req.get(f'{DRIVE_FILES_URL}/{file_id}', headers=_gdrive_headers(),
                 params={'fields': 'description'}, timeout=15)
    r.raise_for_status()
    try:
        return json.loads(r.json().get('description') or '{}')
    except Exception:
        return {}


def _gdrive_delete(file_id: str):
    r = _req.delete(f'{DRIVE_FILES_URL}/{file_id}', headers=_gdrive_headers(), timeout=15)
    if r.status_code not in (200, 204):
        r.raise_for_status()


def _gdrive_prune(folder_id: str, keep_count: int):
    """Delete the oldest regular backups beyond keep_count. Safety backups are excluded."""
    try:
        files = _gdrive_list(folder_id)
        regular = [f for f in files
                   if '_prerestore' not in f.get('name', '')
                   and '_preupgrade' not in f.get('name', '')]
        to_delete = regular[:-keep_count] if keep_count < len(regular) else []
        for f in to_delete:
            try:
                _gdrive_delete(f['id'])
                logger.info(f'[backup] pruned {f.get("name")}')
            except Exception as e:
                logger.warning(f'[backup] prune {f["id"]}: {e}')
    except Exception as e:
        logger.warning(f'[backup] prune failed: {e}')


def _prune_local(folder_path: str, keep_count: int):
    """Delete oldest regular local backups beyond keep_count."""
    try:
        files = sorted(glob.glob(os.path.join(folder_path, 'farmpos_*.backup*')))
        regular = [f for f in files if '_prerestore' not in f and '_preupgrade' not in f]
        for f in regular[:-keep_count] if keep_count < len(regular) else []:
            try:
                os.unlink(f)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f'[backup] local prune: {e}')


# ── Encryption ───────────────────────────────────────────────────────────────
MAGIC = b'FPOSBACK'


def _encrypt(data: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    salt = os.urandom(16)
    kdf  = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
    key  = kdf.derive(passphrase.encode('utf-8'))
    nonce = os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, data, None)


def _decrypt(data: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    if not data.startswith(MAGIC):
        raise ValueError('Not an encrypted FarmPOS backup (bad magic bytes)')
    d     = data[len(MAGIC):]
    salt  = d[:16]
    nonce = d[16:28]
    ct    = d[28:]
    kdf   = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
    key   = kdf.derive(passphrase.encode('utf-8'))
    return AESGCM(key).decrypt(nonce, ct, None)


# ── DB URL parsing ────────────────────────────────────────────────────────────
def _parse_db_url(url: str) -> dict:
    url = url.replace('postgresql+psycopg://', 'postgresql://')
    url = url.replace('postgres://', 'postgresql://')
    p = urlparse(url)
    return {
        'host':     p.hostname or 'localhost',
        'port':     str(p.port or 5432),
        'user':     unquote(p.username or 'postgres'),
        'password': unquote(p.password or ''),
        'dbname':   p.path.lstrip('/'),
    }


# ── Schema version proxy ──────────────────────────────────────────────────────
def _schema_version() -> int:
    try:
        from sqlalchemy import text
        return int(db.session.execute(
            text("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public'")
        ).scalar() or 0)
    except Exception:
        return 0


# ── Available restore targets ─────────────────────────────────────────────────
def _available_restore_targets() -> list:
    targets = []
    base_url = os.environ.get('DATABASE_URL', '')
    if not base_url:
        return targets
    parsed = _parse_db_url(base_url)
    today  = datetime.utcnow().strftime('%Y%m%d')

    # New DB (always first — recommended)
    new_db  = f'farm_pos_restore_{today}'
    new_url = base_url.rsplit('/', 1)[0] + f'/{new_db}'
    targets.append({'label': f'{new_db} (new — recommended)', 'value': new_url,
                    'is_new': True, 'dbname': new_db})

    # Current DB
    targets.append({'label': f'{parsed["dbname"]} (current)', 'value': base_url,
                    'is_new': False, 'dbname': parsed['dbname']})

    # Prod DB (QA env only)
    prod_url = os.environ.get('PROD_DATABASE_URL', '')
    if prod_url:
        pp = _parse_db_url(prod_url)
        targets.append({'label': f'{pp["dbname"]} (production)', 'value': prod_url,
                        'is_new': False, 'dbname': pp['dbname']})
    return targets


# ── Backup pipeline ───────────────────────────────────────────────────────────
def _enqueue_backup(app=None, triggered_by='manual'):
    """Create a BackupLog row, spawn a daemon thread, return log_id. Non-blocking."""
    if app is None:
        app = current_app._get_current_object()
    log = BackupLog(started_at=datetime.utcnow(), status='running', triggered_by=triggered_by)
    db.session.add(log)
    db.session.commit()
    log_id = log.id
    threading.Thread(
        target=_run_backup_in_context, args=(app, log_id, triggered_by),
        daemon=True, name=f'backup-{triggered_by}-{log_id}'
    ).start()
    return log_id


def _run_backup_in_context(app, log_id, triggered_by):
    with app.app_context():
        _do_backup(log_id, triggered_by)


def _do_backup(log_id: int, triggered_by: str):
    """Full backup pipeline — runs in a background thread."""
    tmp_path = None
    acquired = _backup_lock.acquire(blocking=False)
    if not acquired:
        logger.info('[backup] skipped — another backup is already running')
        log = db.session.get(BackupLog, log_id)
        if log:
            log.status = 'failed'
            log.completed_at = datetime.utcnow()
            log.error = 'Skipped — another backup already running'
        db.session.commit()
        return
    try:
        db_url = os.environ.get('DATABASE_URL', '')
        parsed = _parse_db_url(db_url)
        env    = {**os.environ, 'PGPASSWORD': parsed['password']}

        # pg_dump to temp file
        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.backup', prefix='farmpos_dump_')
        os.close(tmp_fd)
        result = subprocess.run(
            ['pg_dump', '-h', parsed['host'], '-p', parsed['port'],
             '-U', parsed['user'], '-Fc', parsed['dbname'], '-f', tmp_path],
            env=env, capture_output=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(f'pg_dump failed: {result.stderr.decode()[:500]}')

        with open(tmp_path, 'rb') as f:
            raw_bytes = f.read()

        sha256    = hashlib.sha256(raw_bytes).hexdigest()
        file_size = len(raw_bytes)

        encrypt_enabled = get_setting('backup_encryption_enabled', 'false') == 'true'
        passphrase      = get_setting('backup_encryption_passphrase', '')
        if encrypt_enabled and passphrase:
            upload_bytes = _encrypt(raw_bytes, passphrase)
            ext = '.backup.enc'
        else:
            upload_bytes = raw_bytes
            ext = '.backup'

        provider    = get_setting('backup_provider', 'google_drive')
        folder_name = get_setting('backup_gdrive_folder_name', 'FarmPOS Backups')
        ts          = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        db_name     = parsed['dbname']
        suffix      = f'_{triggered_by}' if triggered_by not in ('manual', 'schedule') else ''
        filename    = f'farmpos_{db_name}_{ts}{suffix}{ext}'
        app_version = os.environ.get('APP_VERSION', 'unknown')
        schema_ver  = _schema_version()

        meta = {
            'appVersion':    app_version,
            'schemaVersion': schema_ver,
            'dbName':        db_name,
            'createdAt':     datetime.utcnow().isoformat(),
            'sha256':        sha256,
            'fileSize':      file_size,
            'encrypted':     encrypt_enabled and bool(passphrase),
        }
        meta_json = json.dumps(meta)

        drive_file_id = None
        if provider == 'google_drive':
            folder_id     = _gdrive_ensure_folder(folder_name)
            drive_file_id = _gdrive_upload(folder_id, filename, upload_bytes, meta_json)
            keep_count    = int(get_setting('backup_keep_count', '30') or 30)
            _gdrive_prune(folder_id, keep_count)
        elif provider == 'local_folder':
            local_path = get_setting('backup_local_path', '')
            if not local_path:
                raise RuntimeError('Local folder path not configured')
            os.makedirs(local_path, exist_ok=True)
            with open(os.path.join(local_path, filename), 'wb') as f:
                f.write(upload_bytes)
            keep_count = int(get_setting('backup_keep_count', '30') or 30)
            _prune_local(local_path, keep_count)

        log = db.session.get(BackupLog, log_id)
        if log:
            log.status        = 'ok'
            log.completed_at  = datetime.utcnow()
            log.db_name       = db_name
            log.file_name     = filename
            log.file_size     = file_size
            log.sha256        = sha256
            log.provider      = provider
            log.drive_file_id = drive_file_id
            log.app_version   = app_version
            log.schema_version = schema_ver
        set_setting('backup_last_run_at',     datetime.utcnow().isoformat())
        set_setting('backup_last_run_status', 'ok')
        set_setting('backup_fail_count',      '0')
        set_setting('backup_last_error',      '')
        db.session.commit()
        logger.info(f'[backup] {filename} — {file_size:,} bytes — ok')

    except Exception as e:
        err = str(e)[:500]
        logger.error(f'[backup] {triggered_by} failed: {err}')
        try:
            log = db.session.get(BackupLog, log_id)
            if log:
                log.status       = 'failed'
                log.completed_at = datetime.utcnow()
                log.error        = err
            fail_count = int(get_setting('backup_fail_count', '0') or '0') + 1
            set_setting('backup_last_run_status', 'failed')
            set_setting('backup_last_error',      err)
            set_setting('backup_fail_count',      str(fail_count))
            db.session.commit()
        except Exception:
            db.session.rollback()
    finally:
        _backup_lock.release()
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ── Restore pipeline ──────────────────────────────────────────────────────────
def _run_restore_in_context(app, file_id, restore_target, log_id):
    with app.app_context():
        _do_restore(file_id, restore_target, log_id)


def _do_restore(file_id: str, restore_target: str, log_id: int):
    """Full restore pipeline — runs in a background thread."""
    tmp_path = None
    try:
        provider = get_setting('backup_provider', 'google_drive')

        # Download
        if provider == 'google_drive':
            raw = _gdrive_download(file_id)
            meta = _gdrive_get_meta(file_id)
        else:
            with open(file_id, 'rb') as f:
                raw = f.read()
            meta = {}

        expected_sha = meta.get('sha256')
        encrypted    = meta.get('encrypted', raw.startswith(MAGIC))

        # Decrypt
        if encrypted:
            passphrase = get_setting('backup_encryption_passphrase', '')
            if not passphrase:
                raise RuntimeError('Backup is encrypted but no passphrase is configured')
            restore_bytes = _decrypt(raw, passphrase)
        else:
            restore_bytes = raw

        # Verify SHA256 (against unencrypted content)
        if expected_sha:
            actual_sha = hashlib.sha256(restore_bytes).hexdigest()
            if actual_sha != expected_sha:
                raise RuntimeError('SHA256 mismatch — backup file may be corrupted')

        # Write to temp file
        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.backup', prefix='farmpos_restore_')
        os.close(tmp_fd)
        with open(tmp_path, 'wb') as f:
            f.write(restore_bytes)

        parsed = _parse_db_url(restore_target)
        env    = {**os.environ, 'PGPASSWORD': parsed['password']}
        target_dbname = parsed['dbname']

        # Terminate active connections to target DB (best effort)
        try:
            import psycopg
            conn_str = (f"host={parsed['host']} port={parsed['port']} "
                        f"user={parsed['user']} password={parsed['password']} dbname=postgres")
            with psycopg.connect(conn_str, autocommit=True) as ac:
                ac.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=%s AND pid<>pg_backend_pid()",
                    (target_dbname,)
                )
        except Exception as te:
            logger.warning(f'[backup] terminate connections: {te}')

        # pg_restore
        result = subprocess.run(
            ['pg_restore', '--clean', '--if-exists', '--no-owner', '--no-privileges',
             '-h', parsed['host'], '-p', parsed['port'],
             '-U', parsed['user'], '-d', target_dbname, tmp_path],
            env=env, capture_output=True, timeout=600
        )
        if result.returncode != 0:
            stderr = result.stderr.decode()
            real_errors = [l for l in stderr.splitlines()
                           if 'error:' in l.lower() and 'does not exist' not in l.lower()
                           and 'relation' not in l.lower()]
            if real_errors:
                raise RuntimeError(f'pg_restore: {"; ".join(real_errors[:3])}')

        log = db.session.get(BackupLog, log_id)
        if log:
            log.restore_status       = 'ok'
            log.restore_completed_at = datetime.utcnow()
            log.restore_target_db    = target_dbname
        db.session.commit()
        logger.info(f'[backup] restore to {target_dbname} — ok')

    except Exception as e:
        err = str(e)[:500]
        logger.error(f'[backup] restore failed: {err}')
        try:
            log = db.session.get(BackupLog, log_id)
            if log:
                log.restore_status       = 'failed'
                log.restore_completed_at = datetime.utcnow()
                log.error                = err
            db.session.commit()
        except Exception:
            db.session.rollback()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ── Verify pipeline ───────────────────────────────────────────────────────────
def _do_verify(file_id: str, log_id: int) -> dict:
    """Download, decrypt, run pg_restore --list to confirm readability."""
    tmp_path = None
    try:
        provider = get_setting('backup_provider', 'google_drive')
        if provider == 'google_drive':
            raw = _gdrive_download(file_id)
        else:
            with open(file_id, 'rb') as f:
                raw = f.read()

        if raw.startswith(MAGIC):
            passphrase = get_setting('backup_encryption_passphrase', '')
            if not passphrase:
                raise RuntimeError('Backup is encrypted but no passphrase configured')
            data = _decrypt(raw, passphrase)
        else:
            data = raw

        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.backup', prefix='farmpos_verify_')
        os.close(tmp_fd)
        with open(tmp_path, 'wb') as f:
            f.write(data)

        result = subprocess.run(['pg_restore', '--list', tmp_path],
                                capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f'pg_restore --list: {result.stderr.decode()[:200]}')

        object_count = len([l for l in result.stdout.decode().splitlines()
                            if l.strip() and not l.startswith(';')])
        log = db.session.get(BackupLog, log_id)
        if log:
            log.status       = 'ok'
            log.completed_at = datetime.utcnow()
            log.error        = f'Verified: {object_count} objects'
        db.session.commit()
        return {'ok': True, 'object_count': object_count}

    except Exception as e:
        err = str(e)[:300]
        try:
            log = db.session.get(BackupLog, log_id)
            if log:
                log.status       = 'failed'
                log.completed_at = datetime.utcnow()
                log.error        = err
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {'ok': False, 'error': err}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ── Routes ────────────────────────────────────────────────────────────────────
@bp.route('/api/backup/status', methods=['GET'])
def api_backup_status():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    latest = (BackupLog.query
              .filter(BackupLog.triggered_by != 'verify')
              .order_by(BackupLog.id.desc()).first())
    prev = (BackupLog.query
            .filter(BackupLog.status == 'ok', BackupLog.triggered_by != 'verify',
                    BackupLog.id != (latest.id if latest else -1))
            .order_by(BackupLog.id.desc()).first())

    latest_data = None
    if latest:
        latest_data = {
            'id':             latest.id,
            'status':         latest.status,
            'triggered_by':   latest.triggered_by,
            'started_at':     latest.started_at.isoformat() if latest.started_at else None,
            'completed_at':   latest.completed_at.isoformat() if latest.completed_at else None,
            'file_name':      latest.file_name,
            'file_size':      latest.file_size,
            'app_version':    latest.app_version,
            'schema_version': latest.schema_version,
        }

    return jsonify({
        'enabled':                   get_setting('backup_enabled', 'false') == 'true',
        'provider':                  get_setting('backup_provider', 'google_drive'),
        'gdrive_connected':          bool(get_setting('backup_gdrive_refresh_token', '')),
        'gdrive_email':              get_setting('backup_gdrive_user_email', ''),
        'gdrive_folder_name':        get_setting('backup_gdrive_folder_name', 'FarmPOS Backups'),
        'local_path':                get_setting('backup_local_path', ''),
        'schedule_frequency':        get_setting('backup_schedule_frequency', 'daily'),
        'schedule_time':             get_setting('backup_schedule_time', '12:00'),
        'schedule_day':              get_setting('backup_schedule_day', 'monday'),
        'keep_count':                int(get_setting('backup_keep_count', '30') or 30),
        'encryption_enabled':        get_setting('backup_encryption_enabled', 'false') == 'true',
        'encryption_passphrase_set': bool(get_setting('backup_encryption_passphrase', '')),
        'last_run_at':               get_setting('backup_last_run_at', ''),
        'last_run_status':           get_setting('backup_last_run_status', ''),
        'last_error':                get_setting('backup_last_error', ''),
        'fail_count':                int(get_setting('backup_fail_count', '0') or 0),
        'latest_log':                latest_data,
        'prev_file_size':            prev.file_size if prev else None,
        'available_restore_targets': _available_restore_targets(),
        'google_oauth_configured':   bool(current_app.config.get('GOOGLE_CLIENT_ID')),
        'active_db':                 _get_active_db(),
        'default_db':                _parse_db_url(os.environ.get('DATABASE_URL', ''))['dbname'] if os.environ.get('DATABASE_URL') else '',
    })


@bp.route('/api/backup/connect/start', methods=['POST'])
def api_backup_connect_start():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if not _client_id():
        return jsonify({'error': 'GOOGLE_CLIENT_ID not configured on this server'}), 400
    try:
        r = _req.post(GOOGLE_DEVICE_CODE_URL, data={
            'client_id': _client_id(),
            'scope':     GOOGLE_DRIVE_SCOPE,
        }, timeout=15)
        r.raise_for_status()
        j = r.json()
        nonce = secrets.token_urlsafe(16)
        _set_pending_auth(nonce, {
            'device_code':      j['device_code'],
            'user_code':        j['user_code'],
            'verification_url': j['verification_url'],
            'interval':         j.get('interval', 5),
            'expires_at':       time.time() + j.get('expires_in', 300),
        })
        return jsonify({
            'nonce':            nonce,
            'user_code':        j['user_code'],
            'verification_url': j['verification_url'],
            'expires_in':       j.get('expires_in', 300),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/backup/connect/poll', methods=['GET'])
def api_backup_connect_poll():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    nonce = request.args.get('nonce', '')
    state = _get_pending_auth(nonce)
    if not state or time.time() > state['expires_at']:
        _clear_pending_auth()
        return jsonify({'status': 'expired'})
    try:
        r = _req.post(GOOGLE_TOKEN_URL, data={
            'client_id':     _client_id(),
            'client_secret': _client_secret(),
            'device_code':   state['device_code'],
            'grant_type':    'urn:ietf:params:oauth:grant-type:device_code',
        }, timeout=15)
        j = r.json()
        if 'refresh_token' in j:
            set_setting('backup_gdrive_refresh_token', j['refresh_token'])
            _token_cache['access_token'] = j['access_token']
            _token_cache['expires_at']   = time.time() + j.get('expires_in', 3600)
            try:
                ui = _req.get(GOOGLE_USERINFO_URL,
                              headers={'Authorization': f'Bearer {j["access_token"]}'}, timeout=10)
                if ui.ok:
                    set_setting('backup_gdrive_user_email', ui.json().get('email', ''))
            except Exception:
                pass
            _clear_pending_auth()
            return jsonify({'status': 'authorized', 'email': get_setting('backup_gdrive_user_email', '')})
        err = j.get('error', '')
        if err == 'access_denied':
            _clear_pending_auth()
            return jsonify({'status': 'denied'})
        return jsonify({'status': 'pending'})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)})


@bp.route('/api/backup/disconnect', methods=['POST'])
def api_backup_disconnect():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    set_setting('backup_gdrive_refresh_token', '')
    set_setting('backup_gdrive_user_email',    '')
    set_setting('backup_gdrive_folder_id',     '')
    _token_cache['access_token'] = None
    _token_cache['expires_at']   = 0.0
    return jsonify({'ok': True})


@bp.route('/api/backup/now', methods=['POST'])
def api_backup_now():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    provider = get_setting('backup_provider', 'google_drive')
    if provider == 'google_drive' and not get_setting('backup_gdrive_refresh_token', ''):
        return jsonify({'error': 'Google Drive not connected'}), 400
    log_id = _enqueue_backup(triggered_by='manual')
    return jsonify({'ok': True, 'log_id': log_id})


@bp.route('/api/backup/job/<int:log_id>', methods=['GET'])
def api_backup_job(log_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    log = db.session.get(BackupLog, log_id)
    if not log:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'id':             log.id,
        'status':         log.status,
        'triggered_by':   log.triggered_by,
        'started_at':     log.started_at.isoformat() if log.started_at else None,
        'completed_at':   log.completed_at.isoformat() if log.completed_at else None,
        'file_name':      log.file_name,
        'file_size':      log.file_size,
        'error':          log.error,
        'restore_status': log.restore_status,
    })


@bp.route('/api/backup/list', methods=['GET'])
def api_backup_list():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    provider = get_setting('backup_provider', 'google_drive')
    try:
        if provider == 'google_drive':
            if not get_setting('backup_gdrive_refresh_token', ''):
                return jsonify([])
            folder_id = get_setting('backup_gdrive_folder_id', '')
            if not folder_id:
                folder_id = _gdrive_ensure_folder(
                    get_setting('backup_gdrive_folder_name', 'FarmPOS Backups'))
            files = list(reversed(_gdrive_list(folder_id)))  # newest first
            result = []
            for f in files:
                try:
                    meta = json.loads(f.get('description') or '{}')
                except Exception:
                    meta = {}
                result.append({
                    'id':             f['id'],
                    'name':           f.get('name', ''),
                    'size':           int(f.get('size', 0) or 0),
                    'created_time':   f.get('createdTime', ''),
                    'app_version':    meta.get('appVersion', ''),
                    'schema_version': meta.get('schemaVersion', ''),
                    'encrypted':      meta.get('encrypted', False),
                    'sha256':         meta.get('sha256', ''),
                })
            return jsonify(result)
        elif provider == 'local_folder':
            local_path = get_setting('backup_local_path', '')
            if not local_path or not os.path.isdir(local_path):
                return jsonify([])
            files = sorted(glob.glob(os.path.join(local_path, 'farmpos_*.backup*')), reverse=True)
            return jsonify([{
                'id':           fp,
                'name':         os.path.basename(fp),
                'size':         os.stat(fp).st_size,
                'created_time': datetime.fromtimestamp(os.stat(fp).st_mtime).isoformat(),
                'app_version':  '',
                'schema_version': '',
                'encrypted':    fp.endswith('.enc'),
            } for fp in files])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/backup/verify', methods=['POST'])
def api_backup_verify():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    file_id = (request.get_json(silent=True) or {}).get('file_id', '')
    if not file_id:
        return jsonify({'error': 'file_id required'}), 400
    log = BackupLog(started_at=datetime.utcnow(), status='running',
                    triggered_by='verify', drive_file_id=file_id)
    db.session.add(log)
    db.session.commit()
    return jsonify(_do_verify(file_id, log.id))


@bp.route('/api/backup/restore', methods=['POST'])
def api_backup_restore():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data           = request.get_json(silent=True) or {}
    file_id        = data.get('file_id', '')
    restore_target = data.get('restore_target', '')
    confirmed      = data.get('confirmed', False)
    if not file_id or not restore_target:
        return jsonify({'error': 'file_id and restore_target required'}), 400
    if not confirmed:
        return jsonify({'error': 'confirmed must be true'}), 400

    targets      = _available_restore_targets()
    target_info  = next((t for t in targets if t['value'] == restore_target), None)
    needs_prerestore = target_info and not target_info.get('is_new', False)

    if needs_prerestore:
        pre_log_id = _enqueue_backup(triggered_by='pre-restore')
        deadline   = time.time() + 300
        while time.time() < deadline:
            pre_log = db.session.get(BackupLog, pre_log_id)
            db.session.refresh(pre_log)
            if pre_log and pre_log.status in ('ok', 'failed'):
                break
            time.sleep(2)

    log = BackupLog(started_at=datetime.utcnow(), status='running', triggered_by='restore')
    db.session.add(log)
    db.session.commit()
    log_id = log.id

    app = current_app._get_current_object()
    threading.Thread(
        target=_run_restore_in_context, args=(app, file_id, restore_target, log_id),
        daemon=True, name=f'restore-{log_id}'
    ).start()
    return jsonify({'ok': True, 'log_id': log_id})


@bp.route('/api/backup/<file_id>', methods=['DELETE'])
def api_backup_delete(file_id):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        _gdrive_delete(file_id)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/backup/settings', methods=['POST'])
def api_backup_settings():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.get_json(silent=True) or {}
    set_setting('backup_enabled',            'true' if d.get('enabled') else 'false')
    set_setting('backup_provider',           d.get('provider', 'google_drive'))
    set_setting('backup_gdrive_folder_name', d.get('folder_name', 'FarmPOS Backups'))
    set_setting('backup_local_path',         d.get('local_path', ''))
    set_setting('backup_schedule_frequency', d.get('frequency', 'daily'))
    set_setting('backup_schedule_time',      d.get('schedule_time', '12:00'))
    set_setting('backup_schedule_day',       d.get('schedule_day', 'monday'))
    set_setting('backup_keep_count',         str(int(d.get('keep_count', 30) or 30)))
    set_setting('backup_encryption_enabled', 'true' if d.get('encryption_enabled') else 'false')
    if d.get('encryption_passphrase'):
        set_setting('backup_encryption_passphrase', d['encryption_passphrase'])
    return jsonify({'ok': True})


@bp.route('/api/backup/databases', methods=['GET'])
def api_backup_list_databases():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        dbs       = _list_databases()
        active    = _get_active_db()
        base_url  = os.environ.get('DATABASE_URL', '')
        prod_url  = os.environ.get('PROD_DATABASE_URL', '')
        default_db = _parse_db_url(base_url)['dbname'] if base_url else ''
        prod_db    = _parse_db_url(prod_url)['dbname'] if prod_url else None
        return jsonify([{
            'name':       db,
            'is_active':  db == active,
            'is_default': db == default_db,
            'is_prod':    db == prod_db,
            'is_restore': db.startswith('farm_pos_restore'),
            'deletable':  (db != default_db and db != prod_db
                           and db.startswith('farm_pos_') and db != active),
        } for db in dbs])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/backup/switch-database', methods=['POST'])
def api_switch_database():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data   = request.get_json(silent=True) or {}
    dbname = data.get('dbname', '').strip()
    if not dbname:
        return jsonify({'error': 'dbname required'}), 400
    base_url = os.environ.get('DATABASE_URL', '')
    if not base_url:
        return jsonify({'error': 'DATABASE_URL not configured'}), 500
    default_db = _parse_db_url(base_url)['dbname']
    if dbname == default_db:
        # Switching back to default — remove override
        try:
            os.remove(_DB_OVERRIDE_FILE)
        except FileNotFoundError:
            pass
    else:
        new_url = base_url.rsplit('/', 1)[0] + f'/{dbname}'
        with open(_DB_OVERRIDE_FILE, 'w') as f:
            f.write(new_url)
    # Graceful worker reload — deferred so response reaches client first
    def _reload():
        import signal as _sig
        time.sleep(0.4)
        try:
            os.kill(os.getppid(), _sig.SIGHUP)
        except Exception:
            pass
    threading.Thread(target=_reload, daemon=True).start()
    return jsonify({'ok': True, 'dbname': dbname})


@bp.route('/api/backup/database/<dbname>', methods=['DELETE'])
def api_delete_database(dbname):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    import psycopg
    from psycopg import sql as pgsql
    base_url  = os.environ.get('DATABASE_URL', '')
    prod_url  = os.environ.get('PROD_DATABASE_URL', '')
    active    = _get_active_db()
    default_db = _parse_db_url(base_url)['dbname'] if base_url else ''
    prod_db    = _parse_db_url(prod_url)['dbname'] if prod_url else None
    if dbname == default_db:
        return jsonify({'error': 'Cannot delete the default database'}), 400
    if prod_db and dbname == prod_db:
        return jsonify({'error': 'Cannot delete the production database'}), 400
    if dbname == active:
        return jsonify({'error': 'Cannot delete the active database — switch away first'}), 400
    if not dbname.startswith('farm_pos_'):
        return jsonify({'error': 'Can only delete farm_pos_* databases'}), 400
    try:
        p = _parse_db_url(base_url)
        with psycopg.connect(host=p['host'], port=int(p['port']), user=p['user'],
                             password=p['password'], dbname='postgres', autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (dbname,)
            )
            conn.execute(pgsql.SQL("DROP DATABASE IF EXISTS {}").format(pgsql.Identifier(dbname)))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

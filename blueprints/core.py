import os
import subprocess

from flask import Blueprint, jsonify, request, render_template
from sqlalchemy import text

from helpers import require_login, require_role
from models import db

bp = Blueprint('core', __name__)

LOG_PATH    = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'pos.log')
APP_VERSION = None  # set via init_app or lazy import
_CACHED_GIT_COMMIT = None


def _app_version():
    import app as _app_module
    return _app_module.APP_VERSION


def _git_commit():
    global _CACHED_GIT_COMMIT
    if _CACHED_GIT_COMMIT is not None:
        return _CACHED_GIT_COMMIT
    # 1. Explicit env var (set via Docker build arg in appliance builds)
    val = os.environ.get('GIT_COMMIT', '').strip()
    if val and val != 'unknown':
        _CACHED_GIT_COMMIT = val
        return _CACHED_GIT_COMMIT
    # 2. /app/COMMIT file written by deploy.sh after QA docker-commit bake
    try:
        _commit_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'COMMIT')
        with open(_commit_file) as _f:
            _fval = _f.read().strip()
        if _fval and _fval != 'unknown':
            _CACHED_GIT_COMMIT = _fval
            return _CACHED_GIT_COMMIT
    except Exception:
        pass
    # 3. Ask git (works in server containers where source is git-cloned)
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            _CACHED_GIT_COMMIT = result.stdout.strip()
            return _CACHED_GIT_COMMIT
    except Exception:
        pass
    _CACHED_GIT_COMMIT = 'unknown'
    return _CACHED_GIT_COMMIT


@bp.route('/')
def index():
    app_env = os.getenv('APP_ENV', 'qa')
    return render_template('index.html',
                           app_env=app_env,
                           is_qa=(app_env == 'qa'),
                           is_appliance=(os.getenv('IS_APPLIANCE', '').lower() in ('1', 'true')))


@bp.route('/health')
def health_check():
    # Public endpoint — returns minimal info only (no DB name, no env details).
    # Used by Docker HEALTHCHECK and appliance bootstrap. No auth required by design.
    return jsonify({'status': 'healthy', 'version': _app_version()})


@bp.route('/api/health')
def api_health():
    """Extended health for authenticated admin clients (dashboard banner, JS _checkBackupHealth)."""
    from helpers import get_setting
    from datetime import datetime, timedelta

    backup_warning = None
    try:
        enabled = get_setting('backup_enabled', 'false') == 'true'
        if enabled:
            last_run    = get_setting('backup_last_run_at', '')
            last_status = get_setting('backup_last_run_status', '')
            fail_count  = int(get_setting('backup_fail_count', '0') or '0')
            last_error  = get_setting('backup_last_error', '')
            if fail_count >= 3:
                backup_warning = f'Backup failed {fail_count} time(s): {last_error[:100]}'
            elif not last_run:
                backup_warning = 'Backups are enabled but have never run'
            else:
                try:
                    age_days = (datetime.utcnow() - datetime.fromisoformat(last_run)).days
                    if age_days >= 3:
                        backup_warning = f'Last backup was {age_days} day(s) ago'
                except Exception:
                    pass
    except Exception:
        pass

    return jsonify({
        'status':         'healthy',
        'version':        _app_version(),
        'backup_warning': backup_warning,
    })


@bp.route('/guide')
def user_guide():
    return render_template('user_guide.html')


@bp.route('/__version')
def version():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'version': _app_version(), 'commit': _git_commit(), 'env': os.environ.get('APP_ENV', 'qa')})


@bp.route('/api/ping')
def api_ping():
    """Keep-alive endpoint — updates last_active via before_request hook."""
    if not require_login():
        return jsonify({'ok': False}), 401
    return jsonify({'ok': True})


@bp.route('/api/logs')
def api_logs():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    n = min(int(request.args.get('n', 200)), 2000)
    try:
        with open(LOG_PATH, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        return jsonify({'lines': lines[-n:], 'total': len(lines), 'path': LOG_PATH})
    except FileNotFoundError:
        return jsonify({'lines': [], 'total': 0, 'path': LOG_PATH})


@bp.route('/api/db-health')
def api_db_health():
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@bp.route('/api/db-migrate', methods=['POST'])
def api_db_migrate():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    import app as _app_module
    _app_module.strong_migrate()
    return jsonify({'ok': True})

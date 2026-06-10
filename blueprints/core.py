import os

from flask import Blueprint, jsonify, request, render_template
from sqlalchemy import text

from helpers import require_login, require_role
from models import db

bp = Blueprint('core', __name__)

LOG_PATH    = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'pos.log')
APP_VERSION = None  # set via init_app or lazy import


def _app_version():
    import app as _app_module
    return _app_module.APP_VERSION


@bp.route('/')
def index():
    return render_template('index.html', app_env=os.getenv('APP_ENV', 'qa'))


@bp.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'version': _app_version(), 'timestamp': __import__('datetime').datetime.utcnow().isoformat()})


@bp.route('/guide')
def user_guide():
    return render_template('user_guide.html')


@bp.route('/__version')
def version():
    return jsonify({'version': _app_version()})


@bp.route('/api/logs')
def api_logs():
    if not require_role('admin'): return jsonify({'error': 'Forbidden'}), 403
    n = int(request.args.get('n', 200))
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

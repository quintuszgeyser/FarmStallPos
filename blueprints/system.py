import os
import json
import logging

from flask import Blueprint, jsonify, request, session

from helpers import get_setting, set_setting, require_role
from models import db

bp = Blueprint('system', __name__)
logger = logging.getLogger('pos')

APP_VERSION = None  # injected by create_app via bp.app_version


def _app_version():
    # Import lazily to avoid circular at module load time
    import app as _app_module
    return _app_module.APP_VERSION


@bp.route('/api/system/update-status', methods=['GET'])
def api_system_update_status():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    state_file = os.path.join('C:', 'ProgramData', 'FarmPOS', 'Updater', 'state.json')
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r') as f:
                state_data = json.load(f)
        except Exception as e:
            logger.error('Failed to read update state: %s', e)
            state_data = {}
    else:
        state_data = {}

    return jsonify({
        'current_version': _app_version(),
        'state': state_data.get('state', 'idle'),
        'available_version': state_data.get('available_version'),
        'available_type': state_data.get('available_type'),
        'safe_auto_update': state_data.get('safe_auto_update', False),
        'progress_pct': state_data.get('progress_pct', 0),
        'current_action': state_data.get('current_action', ''),
        'last_check': state_data.get('last_check'),
        'last_error': state_data.get('last_error'),
        'auto_update_enabled': bool(get_setting('auto_update_enabled', False)),
        'auto_update_minor': bool(get_setting('auto_update_minor', False)),
    })


@bp.route('/api/system/update-check', methods=['POST'])
def api_system_update_check():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    set_setting('update_check_requested', True)
    db.session.commit()
    logger.info('Manual update check requested by user %s', session.get('user_id'))
    return jsonify({'ok': True, 'message': 'Update check triggered'})


@bp.route('/api/system/update-install', methods=['POST'])
def api_system_update_install():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403

    state_file = os.path.join('C:', 'ProgramData', 'FarmPOS', 'Updater', 'state.json')
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r') as f:
                state_data = json.load(f)
        except Exception as e:
            return jsonify({'error': f'Failed to read update state: {e}'}), 500
    else:
        return jsonify({'error': 'Update state not available'}), 400

    if state_data.get('state') != 'ready':
        return jsonify({'error': 'No update ready to install'}), 400

    set_setting('update_install_requested', True)
    db.session.commit()
    logger.info('Manual update install requested by user %s', session.get('user_id'))
    return jsonify({'ok': True, 'message': 'Update installation triggered'})


@bp.route('/api/system/update-settings', methods=['POST'])
def api_system_update_settings():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    if 'auto_update_enabled' in data:
        set_setting('auto_update_enabled', bool(data['auto_update_enabled']))
        logger.info('Auto-update enabled set to %s by user %s',
                    data['auto_update_enabled'], session.get('user_id'))
    if 'auto_update_minor' in data:
        set_setting('auto_update_minor', bool(data['auto_update_minor']))
        logger.info('Auto-update minor set to %s by user %s',
                    data['auto_update_minor'], session.get('user_id'))
    db.session.commit()
    return jsonify({'ok': True})

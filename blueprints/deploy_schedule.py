"""
Scheduled QA → PROD deployments.

POST /api/deploy-schedule          — schedule a deploy (datetime + description)
GET  /api/deploy-schedule          — list upcoming + recent schedules
DELETE /api/deploy-schedule/<id>   — cancel a pending schedule
POST /api/deploy-schedule/execute  — manually trigger now (admin only)
GET  /api/deploy-schedule/status   — current deploy status + env info
"""
import subprocess
import threading
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from helpers import require_role, current_user, get_setting
from models import db, DeploySchedule

bp = Blueprint('deploy_schedule', __name__)

# Background thread checks every 60s for due schedules
_scheduler_started = False
_deploy_lock = threading.Lock()


def _start_scheduler(app):
    """Start background scheduler thread — call once from create_app()."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    import time

    def _loop():
        while True:
            time.sleep(60)
            try:
                with app.app_context():
                    _check_due_schedules()
            except Exception as e:
                app.logger.error(f"Scheduler error: {e}")

    t = threading.Thread(target=_loop, daemon=True, name='deploy-scheduler')
    t.start()


def _check_due_schedules():
    """Run any pending schedules that are due."""
    now = datetime.now(timezone.utc)
    due = DeploySchedule.query.filter(
        DeploySchedule.status == 'pending',
        DeploySchedule.scheduled_at <= now,
    ).order_by(DeploySchedule.scheduled_at).all()

    for schedule in due:
        _execute_schedule(schedule)


def _execute_schedule(schedule: DeploySchedule):
    """Run deploy.sh prod for this schedule. Thread-safe."""
    if not _deploy_lock.acquire(blocking=False):
        return  # another deploy is running

    try:
        schedule.status = 'running'
        schedule.executed_at = datetime.now(timezone.utc)
        db.session.commit()

        result = subprocess.run(
            ['bash', '/home/quintusz/farmpos-docker/deploy.sh', 'prod'],
            capture_output=True, text=True, timeout=300,
        )

        schedule.status = 'done' if result.returncode == 0 else 'failed'
        schedule.result_log = (result.stdout or '') + (result.stderr or '')
        db.session.commit()

    except subprocess.TimeoutExpired:
        schedule.status = 'failed'
        schedule.result_log = 'Deploy timed out after 5 minutes'
        db.session.commit()
    except Exception as e:
        schedule.status = 'failed'
        schedule.result_log = str(e)
        db.session.commit()
    finally:
        _deploy_lock.release()


def _serialize(s):
    return {
        'id':           s.id,
        'scheduled_at': s.scheduled_at.isoformat(),
        'description':  s.description,
        'status':       s.status,
        'created_at':   s.created_at.isoformat(),
        'executed_at':  s.executed_at.isoformat() if s.executed_at else None,
        'result_log':   s.result_log,
    }


@bp.route('/api/deploy-schedule', methods=['GET'])
def api_schedule_list():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    schedules = DeploySchedule.query.order_by(DeploySchedule.scheduled_at.desc()).limit(20).all()
    return jsonify([_serialize(s) for s in schedules])


@bp.route('/api/deploy-schedule', methods=['POST'])
def api_schedule_create():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json or {}
    scheduled_at_str = data.get('scheduled_at')
    description = (data.get('description') or '').strip() or None

    if not scheduled_at_str:
        return jsonify({'error': 'scheduled_at required (ISO format)'}), 400

    try:
        scheduled_at = datetime.fromisoformat(scheduled_at_str.replace('Z', '+00:00'))
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    except Exception:
        return jsonify({'error': 'Invalid scheduled_at — use ISO format e.g. 2026-06-19T02:00:00Z'}), 400

    if scheduled_at <= datetime.now(timezone.utc):
        return jsonify({'error': 'scheduled_at must be in the future'}), 400

    # Cancel any existing pending schedule
    existing = DeploySchedule.query.filter_by(status='pending').first()
    if existing:
        existing.status = 'cancelled'
        db.session.flush()

    user = current_user()
    s = DeploySchedule(
        scheduled_at=scheduled_at,
        description=description,
        created_by=user.id if user else None,
    )
    db.session.add(s)
    db.session.commit()
    return jsonify(_serialize(s)), 201


@bp.route('/api/deploy-schedule/<int:sid>', methods=['DELETE'])
def api_schedule_cancel(sid):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    s = db.session.get(DeploySchedule, sid)
    if not s:
        return jsonify({'error': 'Not found'}), 404
    if s.status != 'pending':
        return jsonify({'error': f'Cannot cancel — status is {s.status}'}), 400
    s.status = 'cancelled'
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/deploy-schedule/execute', methods=['POST'])
def api_schedule_execute_now():
    """Immediately deploy PROD (no wait for schedule)."""
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    if not _deploy_lock.acquire(blocking=False):
        return jsonify({'error': 'A deploy is already running'}), 409
    _deploy_lock.release()

    user = current_user()
    s = DeploySchedule(
        scheduled_at=datetime.now(timezone.utc),
        description='Manual deploy now',
        created_by=user.id if user else None,
    )
    db.session.add(s)
    db.session.commit()

    t = threading.Thread(target=_execute_schedule, args=(s,), daemon=True)
    t.start()
    return jsonify({'ok': True, 'schedule_id': s.id})


@bp.route('/api/deploy-schedule/status')
def api_deploy_status():
    """Current deploy status + which env is running where."""
    import os
    is_qa = os.environ.get('APP_ENV', 'prod').lower() == 'qa'
    pending = DeploySchedule.query.filter_by(status='pending').order_by(
        DeploySchedule.scheduled_at
    ).first()
    last_done = DeploySchedule.query.filter(
        DeploySchedule.status.in_(('done', 'failed'))
    ).order_by(DeploySchedule.executed_at.desc()).first()

    return jsonify({
        'current_env': 'qa' if is_qa else 'prod',
        'pending_schedule': _serialize(pending) if pending else None,
        'last_deploy': _serialize(last_done) if last_done else None,
        'deploy_lock_held': not _deploy_lock.acquire(blocking=False) or (
            _deploy_lock.release() or False
        ),
    })

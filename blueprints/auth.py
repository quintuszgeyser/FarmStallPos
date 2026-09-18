import json
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash

from helpers import require_login, require_role, current_user, validate_password
from models import db, User, UserSession, AuditLog, LoginAttempt

bp = Blueprint('auth', __name__)


# Rev 5 P1-3 — durable per-username lockout constants.
LOCKOUT_MAX_FAILURES = 5
LOCKOUT_WINDOW = timedelta(minutes=15)

_login_attempts = {}   # {ip: [timestamp, ...]} — in-memory IP-flood guard, KEPT
# deliberately (Rev 5 P1-3 report) alongside the new durable per-username
# lockout below: this protects against request flooding (a different threat
# from account lockout) and resets on worker restart by design — that's fine
# for a flood guard, unacceptable for account lockout, which is why lockout
# itself now lives in the durable `login_attempts` table instead.


def _account_locked(username):
    """True if `username` has LOCKOUT_MAX_FAILURES+ failures within
    LOCKOUT_WINDOW, per the durable login_attempts table — not any in-process
    state, so this is genuinely durable across a worker restart/redeploy."""
    cutoff = datetime.utcnow() - LOCKOUT_WINDOW
    recent_failures = LoginAttempt.query.filter(
        LoginAttempt.username == username,
        LoginAttempt.success == False,
        LoginAttempt.attempted_at >= cutoff,
    ).count()
    return recent_failures >= LOCKOUT_MAX_FAILURES


@bp.route('/api/login', methods=['POST'])
def api_login():
    from flask import request as _req
    import time as _time
    from werkzeug.security import check_password_hash as _check

    # Brute-force guard: max 10 attempts per IP per 60s (flood guard — see module
    # docstring comment above; orthogonal to the durable per-username lockout below).
    ip   = _req.remote_addr or 'unknown'
    now  = _time.monotonic()
    wins = _login_attempts.get(ip, [])
    wins = [t for t in wins if now - t < 60]
    if len(wins) >= 10:
        return jsonify({'ok': False, 'error': 'Too many attempts — try again in a minute'}), 429
    _login_attempts[ip] = wins

    data     = _req.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    remote_ip = _req.remote_addr or 'unknown'

    # Durable per-username lockout check, BEFORE touching the password at all.
    already_locked = _account_locked(username) if username else False
    if already_locked:
        db.session.add(LoginAttempt(username=username, ip=remote_ip, success=False,
                                     attempted_at=datetime.utcnow()))
        db.session.commit()
        return jsonify({
            'ok': False,
            'error': f'Account locked after {LOCKOUT_MAX_FAILURES} failed attempts — try again in 15 minutes.',
        }), 423

    user = User.query.filter_by(username=username).first()
    # Always run check_password_hash to avoid timing-based username enumeration
    dummy = user.password_hash if user else generate_password_hash('dummy-constant')
    valid = _check(dummy, password)

    if not user or not valid or not user.active:
        _login_attempts[ip] = wins + [now]   # record failed attempt (flood guard)
        db.session.add(LoginAttempt(username=username, ip=remote_ip, success=False,
                                     attempted_at=datetime.utcnow()))
        db.session.add(AuditLog(
            event_type='login_failed', actor_user_id=None,
            target_table='users', target_id=username,
            note=f'ip={remote_ip}',
        ))
        db.session.flush()
        # Did THIS failure cross the lockout threshold? Fire the transition event
        # exactly once (the pre-check above already excluded "already locked").
        if username and _account_locked(username):
            db.session.add(AuditLog(
                event_type='account_locked', actor_user_id=None,
                target_table='users', target_id=username,
                note=f'{LOCKOUT_MAX_FAILURES} failures within {int(LOCKOUT_WINDOW.total_seconds()//60)} min; ip={remote_ip}',
            ))
        db.session.commit()
        return jsonify({'ok': False, 'error': 'Invalid credentials'}), 401

    db.session.add(LoginAttempt(username=username, ip=remote_ip, success=True,
                                 attempted_at=datetime.utcnow()))

    # Clear session before setting user_id (prevent session fixation)
    session.clear()
    session['user_id'] = user.id
    sess = UserSession(user_id=user.id, logged_in=datetime.utcnow())
    db.session.add(sess)
    db.session.commit()
    session['session_id'] = sess.id
    return jsonify({'ok': True, 'username': user.username, 'role': user.role, 'roles': user.roles,
                     'must_change_password': user.must_change_password})


@bp.route('/api/logout', methods=['POST'])
def api_logout():
    sid = session.get('session_id')
    if sid:
        sess = db.session.get(UserSession, sid)
        if sess and sess.logged_out is None:
            sess.logged_out = datetime.utcnow()
            db.session.commit()
    session.clear()
    return jsonify({'ok': True})


@bp.route('/api/me', methods=['GET'])
def api_me():
    u = current_user()
    if not u:
        return jsonify({'logged_in': False})
    return jsonify({'logged_in': True, 'username': u.username, 'role': u.role, 'roles': u.roles,
                     'must_change_password': u.must_change_password})


@bp.route('/api/users', methods=['GET'])
def api_users_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    users = User.query.order_by(User.username.asc()).all()
    return jsonify([{
        'id': u.id, 'username': u.username, 'role': u.role,
        'roles': u.roles, 'active': u.active,
    } for u in users])


@bp.route('/api/users', methods=['POST'])
def api_users_post():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data     = request.json or {}
    username = data.get('username', '').strip()
    role     = data.get('role', 'teller')
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    pw_error = validate_password(password)
    if pw_error:
        return jsonify({'error': pw_error}), 400
    valid_roles = {'admin', 'teller', 'developer', 'cctv'}
    role_set = {r.strip() for r in role.split(',') if r.strip()}
    if not role_set or not role_set.issubset(valid_roles):
        return jsonify({'error': f'Invalid role(s). Valid: {", ".join(sorted(valid_roles))}'}), 400
    role = ','.join(sorted(role_set))
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username exists'}), 409
    u = User(username=username, role=role,
             password_hash=generate_password_hash(password), active=True)
    db.session.add(u)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/users/update', methods=['POST'])
def api_users_update():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    data     = request.json or {}
    username = data.get('username')
    role     = data.get('role')
    active   = data.get('active')
    password = data.get('password')
    u = User.query.filter_by(username=username).first()
    if not u:
        return jsonify({'error': 'User not found'}), 404
    if role:
        valid_roles = {'admin', 'teller', 'developer', 'cctv'}
        role_set = {r.strip() for r in role.split(',') if r.strip()}
        if role_set and role_set.issubset(valid_roles):
            new_role = ','.join(sorted(role_set))
            if new_role != u.role:
                actor = current_user()
                db.session.add(AuditLog(
                    event_type='role_changed',
                    actor_user_id=(actor.id if actor else None),
                    target_table='users', target_id=u.username,
                    before_json=json.dumps({'role': u.role}),
                    note=f'new role: {new_role}',
                ))
            u.role = new_role
    if isinstance(active, bool):
        u.active = active
        if not active:
            now = datetime.utcnow()
            for s in UserSession.query.filter_by(user_id=u.id, logged_out=None).all():
                s.logged_out = now
    if password:
        pw_error = validate_password(password)
        if pw_error:
            return jsonify({'error': pw_error}), 400
        u.password_hash = generate_password_hash(password)
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/users/<username>', methods=['DELETE'])
def api_users_delete(username):
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    u = User.query.filter_by(username=username).first()
    if not u:
        return jsonify({'error': 'User not found'}), 404
    now = datetime.utcnow()
    for s in UserSession.query.filter_by(user_id=u.id, logged_out=None).all():
        s.logged_out = now
    db.session.delete(u)
    db.session.commit()
    return jsonify({'ok': True})



@bp.route('/api/users/change_password', methods=['POST'])
def api_users_change_password():
    if not require_login():
        return jsonify({'error': 'Unauthorized'}), 401
    u    = current_user()
    data = request.json or {}
    current_pw = data.get('current_password', '')
    new_pw     = data.get('new_password', '')
    if not check_password_hash(u.password_hash, current_pw):
        return jsonify({'error': 'Current password is incorrect'}), 400
    pw_error = validate_password(new_pw)
    if pw_error:
        return jsonify({'error': pw_error}), 400
    u.password_hash = generate_password_hash(new_pw)
    u.must_change_password = False
    db.session.commit()
    return jsonify({'ok': True})

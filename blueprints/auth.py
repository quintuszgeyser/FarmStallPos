from datetime import datetime

from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash

from helpers import require_login, require_role, current_user
from models import db, User, UserSession

bp = Blueprint('auth', __name__)


_login_attempts = {}   # {ip: [timestamp, ...]} — in-memory, resets on worker restart

@bp.route('/api/login', methods=['POST'])
def api_login():
    from flask import request as _req
    import time as _time
    from werkzeug.security import check_password_hash as _check

    # Brute-force guard: max 10 attempts per IP per 60s
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

    user = User.query.filter_by(username=username).first()
    # Always run check_password_hash to avoid timing-based username enumeration
    dummy = user.password_hash if user else generate_password_hash('dummy-constant')
    valid = _check(dummy, password)

    if not user or not valid or not user.active:
        _login_attempts[ip] = wins + [now]   # record failed attempt
        return jsonify({'ok': False, 'error': 'Invalid credentials'}), 401

    # Clear session before setting user_id (prevent session fixation)
    session.clear()
    session['user_id'] = user.id
    sess = UserSession(user_id=user.id, logged_in=datetime.utcnow())
    db.session.add(sess)
    db.session.commit()
    session['session_id'] = sess.id
    return jsonify({'ok': True, 'username': user.username, 'role': user.role, 'roles': user.roles})


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
    return jsonify({'logged_in': True, 'username': u.username, 'role': u.role, 'roles': u.roles})


@bp.route('/api/users', methods=['GET'])
def api_users_get():
    if not require_role('admin'):
        return jsonify({'error': 'Forbidden'}), 403
    users = User.query.order_by(User.username.asc()).all()
    return jsonify([{
        'username': u.username, 'role': u.role,
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
    valid_roles = {'admin', 'teller', 'developer'}
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
        valid_roles = {'admin', 'teller', 'developer'}
        role_set = {r.strip() for r in role.split(',') if r.strip()}
        if role_set and role_set.issubset(valid_roles):
            u.role = ','.join(sorted(role_set))
    if isinstance(active, bool):
        u.active = active
        if not active:
            now = datetime.utcnow()
            for s in UserSession.query.filter_by(user_id=u.id, logged_out=None).all():
                s.logged_out = now
    if password:
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
    if len(new_pw) < 1:
        return jsonify({'error': 'New password cannot be empty'}), 400
    u.password_hash = generate_password_hash(new_pw)
    db.session.commit()
    return jsonify({'ok': True})

from flask import Blueprint, request, session, redirect, render_template_string
from werkzeug.security import check_password_hash, generate_password_hash
from models import User

bp = Blueprint('cctv', __name__)

_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lady Coleen CCTV</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
  <style>
    body { background: #1a1a1a; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { max-width: 360px; width: 100%; border: none; border-radius: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
    .card-header { background: #2c2c2c; border-radius: 12px 12px 0 0; text-align: center; padding: 1.5rem; }
    .card-header h5 { color: #d4af37; margin: 0; font-size: 1.1rem; letter-spacing: 0.5px; }
    .card-header small { color: #888; font-size: 0.8rem; }
    .card-body { background: #242424; border-radius: 0 0 12px 12px; }
    .form-control { background: #333; border-color: #444; color: #eee; }
    .form-control:focus { background: #383838; border-color: #d4af37; color: #fff; box-shadow: 0 0 0 0.2rem rgba(212,175,55,0.25); }
    .btn-gold { background: #d4af37; border-color: #d4af37; color: #1a1a1a; font-weight: 600; }
    .btn-gold:hover { background: #c9a227; border-color: #c9a227; color: #1a1a1a; }
    label { color: #aaa; font-size: 0.85rem; }
    .camera-icon { font-size: 2rem; color: #d4af37; display: block; margin-bottom: 0.5rem; }
  </style>
</head>
<body>
  <div class="card">
    <div class="card-header">
      <span class="camera-icon">&#128247;</span>
      <h5>Lady Coleen CCTV</h5>
      <small>Authorised access only</small>
    </div>
    <div class="card-body p-4">
      {% if error %}<div class="alert alert-danger py-2 small">{{ error }}</div>{% endif %}
      <form method="POST" action="/cctv/login">
        <div class="mb-3">
          <label for="u">Username</label>
          <input id="u" name="username" class="form-control" autofocus autocomplete="username">
        </div>
        <div class="mb-3">
          <label for="p">Password</label>
          <input id="p" name="password" type="password" class="form-control" autocomplete="current-password">
        </div>
        <button type="submit" class="btn btn-gold w-100">Sign in</button>
      </form>
    </div>
  </div>
</body>
</html>"""


@bp.route('/cctv/login', methods=['GET'])
def cctv_login_get():
    return render_template_string(_LOGIN_HTML, error=None)


@bp.route('/cctv/login', methods=['POST'])
def cctv_login_post():
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    user = User.query.filter_by(username=username, active=True).first()
    dummy = user.password_hash if user else generate_password_hash('dummy-constant')
    valid = check_password_hash(dummy, password)
    roles = (user.role or '').split(',') if user else []
    if not user or not valid or 'cctv' not in roles:
        return render_template_string(_LOGIN_HTML, error='Invalid credentials or access not granted'), 401
    session['cctv_user'] = user.username
    return redirect('/')


@bp.route('/cctv/logout', methods=['GET', 'POST'])
def cctv_logout():
    session.pop('cctv_user', None)
    return redirect('/cctv/login')


@bp.route('/api/cctv-session-check', methods=['GET'])
def cctv_session_check():
    if session.get('cctv_user'):
        return ('', 200)
    return ('', 401)

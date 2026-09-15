from flask import Blueprint, request, session, redirect, render_template_string
from werkzeug.security import check_password_hash, generate_password_hash
from models import User
from functools import wraps

bp = Blueprint('cctv', __name__)


def _require_cctv(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('cctv_user'):
            return redirect('/cctv/login')
        return f(*args, **kwargs)
    return wrapper

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
    return redirect('/cctv/view')


_VIEW_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lady Coleen CCTV</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { width: 100%; height: 100%; overflow: hidden; background: #000; }
    #frigate-frame { position: fixed; inset: 0; width: 100%; height: 100%; border: none; }
    #cctv-hud {
      position: fixed; bottom: 16px; right: 16px; z-index: 9999;
      display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
      font-family: system-ui, sans-serif;
    }
    #settings-panel {
      background: rgba(18,18,18,0.96); border: 1px solid #3a3a3a; border-radius: 10px;
      padding: 12px 14px; display: none; min-width: 190px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.7);
    }
    .sp-title { color: #888; font-size: 0.72rem; letter-spacing: .04em; text-transform: uppercase; margin-bottom: 8px; }
    .sp-option {
      display: block; color: #ccc; font-size: 0.84rem; cursor: pointer;
      padding: 6px 10px; border-radius: 6px; background: none; border: none;
      width: 100%; text-align: left;
    }
    .sp-option:hover { background: rgba(212,175,55,0.15); color: #fff; }
    .sp-option.active { background: #d4af37; color: #111; font-weight: 600; }
    .sp-divider { border: none; border-top: 1px solid #333; margin: 8px 0; }
    #gear-btn {
      background: rgba(18,18,18,0.88); border: 1px solid #444; border-radius: 50%;
      color: #d4af37; font-size: 1.3rem; width: 44px; height: 44px; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 2px 8px rgba(0,0,0,0.5); user-select: none;
    }
    #gear-btn:hover { background: rgba(30,30,30,0.95); }
    #refresh-countdown {
      background: rgba(18,18,18,0.8); border: 1px solid #2a2a2a; border-radius: 8px;
      color: #666; font-size: 0.7rem; padding: 4px 10px;
    }
  </style>
</head>
<body>
  <iframe id="frigate-frame" src="/"></iframe>

  <div id="cctv-hud">
    <div id="refresh-countdown" style="display:none"></div>
    <div id="settings-panel">
      <div class="sp-title">Auto-refresh</div>
      <button class="sp-option" data-secs="0">Off</button>
      <button class="sp-option" data-secs="60">Every 1 minute</button>
      <button class="sp-option" data-secs="300">Every 5 minutes</button>
      <button class="sp-option" data-secs="600">Every 10 minutes</button>
      <button class="sp-option" data-secs="1800">Every 30 minutes</button>
      <hr class="sp-divider">
      <button class="sp-option" id="sp-logout" style="color:#c07070">Sign out</button>
    </div>
    <button id="gear-btn" title="Settings">&#9881;</button>
  </div>

  <script>
    const LS_KEY = 'cctv_refresh_secs';
    let _refreshHandle = null;
    let _countdownHandle = null;
    let _nextAt = 0;

    function savedSecs() { return parseInt(localStorage.getItem(LS_KEY) || '0', 10); }

    function applySetting(secs) {
      localStorage.setItem(LS_KEY, secs);
      clearTimeout(_refreshHandle);
      clearInterval(_countdownHandle);
      const cd = document.getElementById('refresh-countdown');
      if (!secs) { cd.style.display = 'none'; return; }
      _nextAt = Date.now() + secs * 1000;
      cd.style.display = 'block';
      _countdownHandle = setInterval(() => {
        const rem = Math.ceil((_nextAt - Date.now()) / 1000);
        if (rem <= 0) { cd.textContent = 'Refreshing…'; return; }
        const m = Math.floor(rem / 60), s = rem % 60;
        cd.textContent = 'Refresh in ' + (m ? m + 'm ' : '') + s + 's';
      }, 1000);
      (function schedule() {
        _refreshHandle = setTimeout(() => {
          document.getElementById('frigate-frame').src = '/';
          _nextAt = Date.now() + secs * 1000;
          schedule();
        }, secs * 1000);
      })();
    }

    function syncHighlight(secs) {
      document.querySelectorAll('.sp-option[data-secs]').forEach(b => {
        b.classList.toggle('active', parseInt(b.dataset.secs) === secs);
      });
    }

    document.querySelectorAll('.sp-option[data-secs]').forEach(b => {
      b.addEventListener('click', () => {
        const v = parseInt(b.dataset.secs);
        syncHighlight(v);
        applySetting(v);
        document.getElementById('settings-panel').style.display = 'none';
      });
    });

    document.getElementById('gear-btn').addEventListener('click', e => {
      const p = document.getElementById('settings-panel');
      p.style.display = p.style.display === 'none' ? 'block' : 'none';
      e.stopPropagation();
    });

    document.getElementById('sp-logout').addEventListener('click', () => {
      window.location.href = '/cctv/logout';
    });

    document.addEventListener('click', e => {
      const p = document.getElementById('settings-panel');
      if (p.style.display !== 'none' && !p.contains(e.target)) {
        p.style.display = 'none';
      }
    });

    const init = savedSecs();
    syncHighlight(init);
    applySetting(init);
  </script>
</body>
</html>"""


@bp.route('/cctv/view', methods=['GET'])
@_require_cctv
def cctv_view():
    return render_template_string(_VIEW_HTML)


@bp.route('/cctv/logout', methods=['GET', 'POST'])
def cctv_logout():
    session.pop('cctv_user', None)
    return redirect('/cctv/login')


@bp.route('/api/cctv-session-check', methods=['GET'])
def cctv_session_check():
    if session.get('cctv_user'):
        return ('', 200)
    return ('', 401)

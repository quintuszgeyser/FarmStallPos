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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Lady Coleen CCTV</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root { --cols: 4; --amber: #d4af37; }
    html, body { width: 100%; height: 100%; background: #000; overflow: hidden; font-family: system-ui, sans-serif; }

    #grid {
      display: grid;
      grid-template-columns: repeat(var(--cols), 1fr);
      grid-auto-rows: 1fr;
      gap: 2px;
      width: 100%;
      height: 100%;
      background: #111;
      padding: 2px;
    }

    .tile {
      position: relative;
      background: #090909;
      overflow: hidden;
      cursor: pointer;
    }

    .tile video {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      background: #090909;
    }

    .tile-gradient {
      position: absolute;
      inset: 0;
      background: linear-gradient(to top, rgba(0,0,0,0.65) 0%, transparent 35%);
      pointer-events: none;
    }

    .tile-label {
      position: absolute;
      bottom: 7px;
      left: 9px;
      color: rgba(255,255,255,0.9);
      font-size: 11px;
      font-weight: 500;
      letter-spacing: .04em;
      text-shadow: 0 1px 4px rgba(0,0,0,0.9);
      pointer-events: none;
    }

    .tile-dot {
      position: absolute;
      top: 8px;
      right: 8px;
      width: 7px;
      height: 7px;
      border-radius: 50%;
      pointer-events: none;
    }
    .tile-dot.connecting { background: #555; animation: blink 1.4s infinite; }
    .tile-dot.live       { background: #43a047; box-shadow: 0 0 5px #43a047; }
    .tile-dot.error      { background: #c62828; }

    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

    /* Fullscreen: show full frame, not cropped */
    .tile:fullscreen video,
    .tile:-webkit-full-screen video { object-fit: contain; background: #000; }

    /* HUD */
    #hud {
      position: fixed;
      bottom: max(14px, env(safe-area-inset-bottom, 14px));
      right: max(14px, env(safe-area-inset-right, 14px));
      z-index: 9999;
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 8px;
    }

    #settings-panel {
      background: rgba(12,12,12,0.97);
      border: 1px solid #252525;
      border-radius: 10px;
      padding: 13px 14px;
      display: none;
      min-width: 195px;
      box-shadow: 0 8px 28px rgba(0,0,0,0.85);
    }

    .sp-head {
      font-size: 10px;
      letter-spacing: .1em;
      text-transform: uppercase;
      color: #444;
      margin-bottom: 7px;
    }
    .sp-head + .sp-head, .sp-divider + .sp-head { margin-top: 10px; }

    .sp-cols { display: flex; gap: 5px; margin-bottom: 3px; }

    .sp-col-btn {
      flex: 1;
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 6px;
      color: #777;
      font-size: 12px;
      padding: 6px 2px;
      cursor: pointer;
      text-align: center;
    }
    .sp-col-btn:hover { color: #ccc; background: #222; }
    .sp-col-btn.active { background: var(--amber); border-color: var(--amber); color: #111; font-weight: 600; }

    .sp-action {
      display: block;
      width: 100%;
      background: none;
      border: none;
      border-radius: 6px;
      color: #999;
      font-size: 12.5px;
      padding: 7px 8px;
      cursor: pointer;
      text-align: left;
    }
    .sp-action:hover { background: rgba(255,255,255,0.05); color: #ddd; }
    .sp-action.danger { color: #b05050; }
    .sp-action.danger:hover { color: #e07070; }

    .sp-divider { border: none; border-top: 1px solid #1e1e1e; margin: 8px 0; }

    #gear-btn {
      background: rgba(12,12,12,0.9);
      border: 1px solid #2e2e2e;
      border-radius: 50%;
      color: var(--amber);
      font-size: 1.25rem;
      width: 42px;
      height: 42px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 2px 10px rgba(0,0,0,0.7);
      user-select: none;
    }
    #gear-btn:hover { background: rgba(22,22,22,0.97); }

    @media (max-width: 600px) { :root { --cols: 2; } }
  </style>
</head>
<body>

<div id="grid"></div>

<div id="hud">
  <div id="settings-panel">
    <div class="sp-head">Layout</div>
    <div class="sp-cols">
      <button class="sp-col-btn" data-cols="2">2</button>
      <button class="sp-col-btn" data-cols="3">3</button>
      <button class="sp-col-btn" data-cols="4">4</button>
    </div>
    <div class="sp-divider"></div>
    <div class="sp-head">Actions</div>
    <button class="sp-action" id="sp-reconnect">&#8635; Reconnect all</button>
    <div class="sp-divider"></div>
    <button class="sp-action" id="sp-frigate">&#128247; Open Frigate</button>
    <div class="sp-divider"></div>
    <button class="sp-action danger" id="sp-logout">Sign out</button>
  </div>
  <button id="gear-btn" title="Settings">&#9881;</button>
</div>

<script>
  // Camera list matches Frigate config
  var CAMERAS = [
    {id: 'kombuis',    label: 'Kombuis'},
    {id: 'counter',    label: 'Toonbank'},
    {id: 'rakke',      label: 'Rakke'},
    {id: 'agter',      label: 'Agter'},
    {id: 'badkamer',   label: 'Badkamer'},
    {id: 'stoepagter', label: 'Stoep Agter'},
    {id: 'stoepvoor',  label: 'Stoep Voor'},
    {id: 'Stoor',      label: 'Stoor'},
    {id: 'AgterTenk',  label: 'Agter Tenk'},
    {id: 'parkering',  label: 'Parkering'},
    {id: 'paal',       label: 'Paal'},
    {id: 'pad',        label: 'Pad'},
  ];

  var streams = {};

  // Build grid
  var grid = document.getElementById('grid');
  CAMERAS.forEach(function(cam) {
    var tile = document.createElement('div');
    tile.className = 'tile';

    var video = document.createElement('video');
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;

    var gradient = document.createElement('div');
    gradient.className = 'tile-gradient';

    var label = document.createElement('div');
    label.className = 'tile-label';
    label.textContent = cam.label;

    var dot = document.createElement('div');
    dot.className = 'tile-dot connecting';

    tile.appendChild(video);
    tile.appendChild(gradient);
    tile.appendChild(label);
    tile.appendChild(dot);
    grid.appendChild(tile);

    tile.addEventListener('click', function() {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        tile.requestFullscreen().catch(function(){});
      }
    });

    streams[cam.id] = new CamStream(cam.id, video, dot);
  });

  // WebRTC stream per camera
  function CamStream(id, video, dot) {
    this.id = id;
    this.video = video;
    this.dot = dot;
    this.pc = null;
    this.ws = null;
    this.retryDelay = 3000;
    this._timer = null;
    this.connect();
  }

  CamStream.prototype.setDot = function(state) {
    this.dot.className = 'tile-dot ' + state;
  };

  CamStream.prototype.connect = function() {
    var self = this;
    self._clear();
    self.setDot('connecting');

    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    var ws = new WebSocket(proto + '://' + location.host + '/api/ws?src=' + encodeURIComponent(self.id));
    self.ws = ws;

    var pc = new RTCPeerConnection({iceServers: []});
    self.pc = pc;

    // Video only — no audio to keep bandwidth lean
    pc.addTransceiver('video', {direction: 'recvonly'});

    pc.ontrack = function(e) {
      self.video.srcObject = e.streams[0];
      self.setDot('live');
      self.retryDelay = 3000;
    };

    pc.onconnectionstatechange = function() {
      var s = pc.connectionState;
      if (s === 'failed' || s === 'disconnected') { self.scheduleRetry(); }
    };

    pc.onicecandidate = function(e) {
      if (e.candidate && ws.readyState === 1) {
        ws.send(JSON.stringify({type: 'webrtc/candidate', value: e.candidate.candidate}));
      }
    };

    ws.onopen = function() {
      pc.createOffer().then(function(offer) {
        return pc.setLocalDescription(offer).then(function() {
          ws.send(JSON.stringify({type: 'webrtc/offer', value: offer.sdp}));
        });
      }).catch(function() { self.scheduleRetry(); });
    };

    ws.onmessage = function(e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch(_) { return; }
      if (msg.type === 'webrtc/answer') {
        pc.setRemoteDescription({type: 'answer', sdp: msg.value}).catch(function(){});
      } else if (msg.type === 'webrtc/candidate' && msg.value) {
        pc.addIceCandidate({candidate: msg.value, sdpMid: '0', sdpMLineIndex: 0}).catch(function(){});
      } else if (msg.type === 'error') {
        self.scheduleRetry();
      }
    };

    ws.onerror = function() { self.setDot('error'); };
    ws.onclose  = function() { if (self.pc === pc) self.scheduleRetry(); };
  };

  CamStream.prototype._clear = function() {
    if (this.pc) { try { this.pc.close(); } catch(_){} this.pc = null; }
    if (this.ws) { try { this.ws.close(); } catch(_){} this.ws = null; }
    this.video.srcObject = null;
  };

  CamStream.prototype.scheduleRetry = function() {
    var self = this;
    if (self._timer) return;
    self.setDot('error');
    self._clear();
    self._timer = setTimeout(function() {
      self._timer = null;
      self.retryDelay = Math.min(self.retryDelay * 1.5, 30000);
      self.connect();
    }, self.retryDelay);
  };

  CamStream.prototype.reconnect = function() {
    clearTimeout(this._timer);
    this._timer = null;
    this.retryDelay = 3000;
    this.connect();
  };

  // Layout
  var LS_COLS = 'cctv_cols';
  function applyLayout(n) {
    document.documentElement.style.setProperty('--cols', n);
    localStorage.setItem(LS_COLS, n);
    document.querySelectorAll('.sp-col-btn').forEach(function(b) {
      b.classList.toggle('active', parseInt(b.dataset.cols) === n);
    });
  }
  applyLayout(parseInt(localStorage.getItem(LS_COLS) || '4'));
  document.querySelectorAll('.sp-col-btn').forEach(function(b) {
    b.addEventListener('click', function() { applyLayout(parseInt(b.dataset.cols)); });
  });

  // Gear menu
  var gearBtn = document.getElementById('gear-btn');
  var panel   = document.getElementById('settings-panel');

  gearBtn.addEventListener('click', function(e) {
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    e.stopPropagation();
  });

  document.addEventListener('click', function(e) {
    if (panel.style.display !== 'none' && !panel.contains(e.target)) {
      panel.style.display = 'none';
    }
  });

  document.getElementById('sp-reconnect').addEventListener('click', function() {
    Object.values(streams).forEach(function(s) { s.reconnect(); });
    panel.style.display = 'none';
  });

  document.getElementById('sp-frigate').addEventListener('click', function() {
    window.open('/', '_blank');
    panel.style.display = 'none';
  });

  document.getElementById('sp-logout').addEventListener('click', function() {
    window.location.href = '/cctv/logout';
  });
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

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
    .sp-q-btn {
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
    .sp-q-btn:hover { color: #ccc; background: #222; }
    .sp-q-btn.active { background: var(--amber); border-color: var(--amber); color: #111; font-weight: 600; }
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

    /* ── Drill-down overlay ── */
    #drilldown {
      position: fixed;
      inset: 0;
      background: #0a0a0a;
      z-index: 10000;
      display: flex;
      flex-direction: column;
    }
    #drilldown[hidden] { display: none !important; }

    #dd-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      background: #0f0f0f;
      border-bottom: 1px solid #1a1a1a;
      flex-shrink: 0;
      gap: 10px;
    }
    #dd-cam-name {
      color: var(--amber);
      font-size: 14px;
      font-weight: 600;
      letter-spacing: .06em;
    }
    #dd-header-btns { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }

    #dd-live-btn {
      background: #1a2a1a;
      border: 1px solid #2a4a2a;
      border-radius: 6px;
      color: #43a047;
      font-size: 11px;
      padding: 5px 11px;
      cursor: pointer;
      white-space: nowrap;
    }
    #dd-live-btn.active { background: #43a047; color: #fff; border-color: #43a047; }
    #dd-live-btn:hover { border-color: #43a047; }

    #dd-close-btn {
      background: none;
      border: 1px solid #333;
      border-radius: 6px;
      color: #888;
      font-size: 14px;
      width: 30px;
      height: 30px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    #dd-close-btn:hover { color: #ccc; border-color: #555; }

    #dd-video-wrap {
      position: relative;
      flex: 1;
      min-height: 0;
      background: #000;
    }
    #dd-video {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
    }
    #dd-status-badge {
      position: absolute;
      top: 10px;
      left: 12px;
      font-size: 11px;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 4px;
      letter-spacing: .05em;
    }
    #dd-status-badge.live    { background: rgba(67,160,71,0.85); color: #fff; }
    #dd-status-badge.playback { background: rgba(190,100,0,0.85); color: #fff; }

    /* Controls panel */
    #dd-controls {
      flex-shrink: 0;
      background: #0d0d0d;
      border-top: 1px solid #1a1a1a;
      padding: 10px 14px 12px;
      display: flex;
      flex-direction: column;
      gap: 9px;
      max-height: 44%;
    }

    #dd-date-nav {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-shrink: 0;
    }
    .dd-nav-btn {
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 5px;
      color: #888;
      font-size: 18px;
      width: 28px;
      height: 28px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      line-height: 1;
    }
    .dd-nav-btn:hover:not(:disabled) { color: #ccc; border-color: #444; }
    .dd-nav-btn:disabled { opacity: 0.3; cursor: default; }
    #dd-date-label { color: #ddd; font-size: 13px; flex: 1; }
    #dd-rec-count  { color: #555; font-size: 11px; white-space: nowrap; }

    /* 24-hour timeline strip */
    #dd-timeline {
      display: flex;
      gap: 2px;
      flex-shrink: 0;
      height: 26px;
    }
    .dd-hour {
      flex: 1;
      border-radius: 3px;
      position: relative;
    }
    .dd-hour.empty    { background: #181818; }
    .dd-hour.has-recs { background: #1e3a1e; cursor: pointer; }
    .dd-hour.has-recs:hover { background: #2b562b; }
    .dd-hour.selected { background: var(--amber) !important; }
    .dd-hour-tip {
      position: absolute;
      bottom: calc(100% + 4px);
      left: 50%;
      transform: translateX(-50%);
      background: #222;
      color: #ccc;
      font-size: 9px;
      padding: 2px 5px;
      border-radius: 3px;
      white-space: nowrap;
      pointer-events: none;
      opacity: 0;
      z-index: 1;
    }
    .dd-hour:hover .dd-hour-tip { opacity: 1; }

    /* Clips list */
    #dd-clips-wrap {
      flex: 1;
      min-height: 0;
      overflow-y: auto;
    }
    #dd-clips-msg {
      color: #444;
      font-size: 12px;
      padding: 8px 0;
    }
    .dd-clip {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 8px;
      border-radius: 5px;
      cursor: pointer;
      border: 1px solid transparent;
    }
    .dd-clip:hover { background: #151515; }
    .dd-clip.active { background: #1c1c0c; border-color: #3a3a18; }
    .dd-clip-time { color: #aaa; font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .dd-clip-dur  { color: #555; font-size: 11px; white-space: nowrap; }
    .dd-clip-play { color: var(--amber); font-size: 15px; margin-left: auto; flex-shrink: 0; }

  </style>
</head>
<body>

<div id="grid"></div>

<div id="hud">
  <div id="settings-panel">
    <div class="sp-head">Layout</div>
    <div class="sp-cols">
      <button class="sp-col-btn" data-cols="auto">Auto</button>
      <button class="sp-col-btn" data-cols="2">2</button>
      <button class="sp-col-btn" data-cols="3">3</button>
      <button class="sp-col-btn" data-cols="4">4</button>
      <button class="sp-col-btn" data-cols="6">6</button>
    </div>
    <div class="sp-divider"></div>
    <div class="sp-head">Tile quality</div>
    <div class="sp-cols">
      <button class="sp-q-btn" data-hd="0">Smooth</button>
      <button class="sp-q-btn" data-hd="1">HD</button>
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

<!-- Drill-down overlay: live view + recordings timeline -->
<div id="drilldown" hidden>
  <div id="dd-header">
    <span id="dd-cam-name"></span>
    <div id="dd-header-btns">
      <button id="dd-live-btn">&#9679; Live</button>
      <button id="dd-close-btn">&#10005;</button>
    </div>
  </div>
  <div id="dd-video-wrap">
    <video id="dd-video" autoplay muted playsinline></video>
    <div id="dd-status-badge" class="live">&#9679; LIVE</div>
  </div>
  <div id="dd-controls">
    <div id="dd-date-nav">
      <button class="dd-nav-btn" id="dd-prev-day">&#8249;</button>
      <span id="dd-date-label"></span>
      <span id="dd-rec-count"></span>
      <button class="dd-nav-btn" id="dd-next-day">&#8250;</button>
    </div>
    <div id="dd-timeline"></div>
    <div id="dd-clips-wrap">
      <div id="dd-clips-msg">Select an hour above to view recordings</div>
      <div id="dd-clips"></div>
    </div>
  </div>
</div>

<script>
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
  ];

  var streams = {};

  // Grid tiles default to the camera substream (640x360, H264 level 2.2).
  // TV-box class decoders cap around level 4.1 and choke on the 4K level-5.0
  // mainstreams, so full res is reserved for the single-camera drill-down.
  var LS_HD = 'cctv_hd_tiles';
  var _hdTiles = localStorage.getItem(LS_HD) === '1';
  function tileSrc(id) { return _hdTiles ? id : id + '_sub'; }

  // ── WebRTC CamStream ──
  function CamStream(id, video, dot) {
    this.id = id;
    this.video = video;
    this.dot = dot;   // may be null for overlay stream
    this.pc = null;
    this.ws = null;
    this.retryDelay = 3000;
    this._timer = null;
    this.connect();
  }

  CamStream.prototype.setDot = function(state) {
    if (this.dot) this.dot.className = 'tile-dot ' + state;
  };

  CamStream.prototype.connect = function() {
    var self = this;
    self._clear();
    self.setDot('connecting');

    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    var ws = new WebSocket(proto + '://' + location.host + '/live/webrtc/api/ws?src=' + encodeURIComponent(self.id));
    self.ws = ws;

    var pc = new RTCPeerConnection({iceServers: []});
    self.pc = pc;

    pc.addTransceiver('video', {direction: 'recvonly'});

    // ontrack fires on setRemoteDescription — it does NOT mean media is flowing.
    // Only attach the stream here; the dot is driven by real connection state below.
    pc.ontrack = function(e) {
      if (e.streams && e.streams.length > 0) {
        self.video.srcObject = e.streams[0];
      } else {
        if (!self.video.srcObject) self.video.srcObject = new MediaStream();
        self.video.srcObject.addTrack(e.track);
      }
      self.video.play().catch(function(){});
    };

    pc.onconnectionstatechange = function() {
      var s = pc.connectionState;
      if (s === 'connected') {
        self.setDot('live');
        self.retryDelay = 3000;
        self.video.play().catch(function(){});
      } else if (s === 'failed' || s === 'disconnected') {
        self.scheduleRetry();
      }
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

  // ── Grid — must come after all CamStream.prototype assignments ──
  // Stream connects are staggered (not fired all at once) so a bandwidth-
  // constrained link (e.g. a relayed Tailscale path) isn't hit with every
  // camera's ICE/DTLS negotiation simultaneously.
  var grid = document.getElementById('grid');
  CAMERAS.forEach(function(cam, camIndex) {
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

    tile.addEventListener('click', function() { openDrilldown(cam); });

    setTimeout(function() {
      streams[cam.id] = new CamStream(tileSrc(cam.id), video, dot);
    }, camIndex * 600);
  });

  // ── Layout ──
  var LS_COLS = 'cctv_cols';
  var _autoMode = !localStorage.getItem(LS_COLS);

  function _bestCols() {
    var w = window.innerWidth, h = window.innerHeight, n = CAMERAS.length;
    var best = 4, bestScore = Infinity;
    [2, 3, 4, 6].forEach(function(c) {
      var rows = Math.ceil(n / c);
      var score = Math.abs((w / c) / (h / rows) - 16/9);
      if (score < bestScore) { bestScore = score; best = c; }
    });
    return best;
  }

  function _markBtns(activeCols, isAuto) {
    document.querySelectorAll('.sp-col-btn').forEach(function(b) {
      var isAutoBtn = b.dataset.cols === 'auto';
      b.classList.toggle('active', isAutoBtn ? isAuto : (!isAuto && parseInt(b.dataset.cols) === activeCols));
    });
  }

  function applyLayout(cols) {
    _autoMode = false;
    localStorage.setItem(LS_COLS, cols);
    document.documentElement.style.setProperty('--cols', cols);
    _markBtns(cols, false);
  }

  function applyAutoLayout() {
    _autoMode = true;
    localStorage.removeItem(LS_COLS);
    var n = _bestCols();
    document.documentElement.style.setProperty('--cols', n);
    _markBtns(n, true);
  }

  // Init
  if (localStorage.getItem(LS_COLS)) {
    applyLayout(parseInt(localStorage.getItem(LS_COLS)));
  } else {
    applyAutoLayout();
  }

  document.querySelectorAll('.sp-col-btn').forEach(function(b) {
    b.addEventListener('click', function() {
      if (b.dataset.cols === 'auto') { applyAutoLayout(); }
      else { applyLayout(parseInt(b.dataset.cols)); }
    });
  });

  window.addEventListener('resize', function() {
    if (_autoMode) applyAutoLayout();
  });

  // ── Gear menu ──
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

  function _markQBtns() {
    document.querySelectorAll('.sp-q-btn').forEach(function(b) {
      b.classList.toggle('active', (b.dataset.hd === '1') === _hdTiles);
    });
  }
  _markQBtns();

  document.querySelectorAll('.sp-q-btn').forEach(function(b) {
    b.addEventListener('click', function() {
      var want = b.dataset.hd === '1';
      if (want === _hdTiles) { panel.style.display = 'none'; return; }
      _hdTiles = want;
      localStorage.setItem(LS_HD, want ? '1' : '0');
      _markQBtns();
      // Re-point every tile at the other stream variant
      CAMERAS.forEach(function(cam) {
        var s = streams[cam.id];
        if (!s) return;
        s.id = tileSrc(cam.id);
        s.reconnect();
      });
      panel.style.display = 'none';
    });
  });

  document.getElementById('sp-frigate').addEventListener('click', function() {
    window.open('/', '_blank');
    panel.style.display = 'none';
  });

  document.getElementById('sp-logout').addEventListener('click', function() {
    window.location.href = '/cctv/logout';
  });

  // ── Drill-down ──
  var dd = {
    overlay:      document.getElementById('drilldown'),
    video:        document.getElementById('dd-video'),
    camName:      document.getElementById('dd-cam-name'),
    liveBtn:      document.getElementById('dd-live-btn'),
    closeBtn:     document.getElementById('dd-close-btn'),
    dateLabel:    document.getElementById('dd-date-label'),
    recCount:     document.getElementById('dd-rec-count'),
    prevDay:      document.getElementById('dd-prev-day'),
    nextDay:      document.getElementById('dd-next-day'),
    timeline:     document.getElementById('dd-timeline'),
    clipsMsg:     document.getElementById('dd-clips-msg'),
    clipsEl:      document.getElementById('dd-clips'),
    badge:        document.getElementById('dd-status-badge'),
    cam:          null,
    stream:       null,   // CamStream for overlay live view (dot=null)
    isLive:       true,
    date:         null,   // local-midnight Date for selected day
    dayRecs:      [],
    selectedHour: -1,
  };

  function ddFmtTime(ts) {
    var d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2,'0') + ':' +
           String(d.getMinutes()).padStart(2,'0') + ':' +
           String(d.getSeconds()).padStart(2,'0');
  }

  function ddFmtDur(secs) {
    secs = Math.round(secs);
    if (secs < 60) return secs + 's';
    var m = Math.floor(secs / 60), s = secs % 60;
    return m + 'm' + (s ? ' ' + s + 's' : '');
  }

  function openDrilldown(cam) {
    dd.cam = cam;
    dd.overlay.hidden = false;
    dd.camName.textContent = cam.label;
    var now = new Date();
    dd.date = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    dd.selectedHour = -1;
    ddStartLive();
    ddLoadDay();
  }

  function closeDrilldown() {
    dd.overlay.hidden = true;
    if (dd.stream) { dd.stream._clear(); dd.stream = null; }
    dd.video.src = '';
    dd.video.srcObject = null;
    dd.cam = null;
  }

  function ddStartLive() {
    if (dd.stream) { dd.stream._clear(); dd.stream = null; }
    dd.video.src = '';
    dd.video.srcObject = null;
    dd.isLive = true;
    dd.liveBtn.classList.add('active');
    dd.badge.className = 'live';
    dd.badge.innerHTML = '&#9679; LIVE';
    dd.stream = new CamStream(dd.cam.id, dd.video, null);
  }

  function ddLoadDay() {
    var d = dd.date;
    var start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, 0, 0, 0);
    var end   = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59, 999);
    var after  = Math.floor(start.getTime() / 1000);
    var before = Math.floor(end.getTime() / 1000);

    dd.dateLabel.textContent = d.toLocaleDateString('en-ZA', {
      weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'
    });

    var today = new Date();
    today = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    dd.nextDay.disabled = d >= today;

    dd.timeline.innerHTML = '';
    dd.clipsEl.innerHTML = '';
    dd.clipsMsg.textContent = 'Loading...';
    dd.recCount.textContent = '';
    dd.dayRecs = [];
    dd.selectedHour = -1;

    fetch('/api/' + encodeURIComponent(dd.cam.id) + '/recordings?after=' + after + '&before=' + before)
      .then(function(r) { return r.json(); })
      .then(function(recs) {
        dd.dayRecs = Array.isArray(recs) ? recs : [];
        ddRenderTimeline();
        var n = dd.dayRecs.length;
        dd.recCount.textContent = n ? '(' + n + ' clip' + (n !== 1 ? 's' : '') + ')' : '(no recordings)';
        dd.clipsMsg.textContent = n ? 'Select an hour above to view recordings' : 'No recordings for this day';
      })
      .catch(function() {
        dd.clipsMsg.textContent = 'Could not load recordings';
        dd.recCount.textContent = '';
      });
  }

  function ddRenderTimeline() {
    dd.timeline.innerHTML = '';
    var byHour = {};
    dd.dayRecs.forEach(function(r) {
      var h = new Date(r.start_time * 1000).getHours();
      if (!byHour[h]) byHour[h] = [];
      byHour[h].push(r);
    });

    for (var h = 0; h < 24; h++) {
      var block = document.createElement('div');
      var hasRecs = !!byHour[h];
      block.className = 'dd-hour ' + (hasRecs ? 'has-recs' : 'empty');
      if (h === dd.selectedHour) block.classList.add('selected');

      var tip = document.createElement('div');
      tip.className = 'dd-hour-tip';
      tip.textContent = String(h).padStart(2,'0') + ':00' +
                        (hasRecs ? ' · ' + byHour[h].length : '');
      block.appendChild(tip);

      if (hasRecs) {
        (function(hour, clips) {
          block.addEventListener('click', function() {
            dd.selectedHour = hour;
            ddRenderTimeline();
            ddRenderClips(clips);
          });
        })(h, byHour[h]);
      }

      dd.timeline.appendChild(block);
    }
  }

  function ddRenderClips(clips) {
    dd.clipsEl.innerHTML = '';
    dd.clipsMsg.textContent = '';
    if (!clips.length) {
      dd.clipsMsg.textContent = 'No clips for this hour';
      return;
    }
    clips.forEach(function(r) {
      var item = document.createElement('div');
      item.className = 'dd-clip';
      item.innerHTML =
        '<span class="dd-clip-time">' + ddFmtTime(r.start_time) +
        ' &ndash; ' + ddFmtTime(r.end_time) + '</span>' +
        '<span class="dd-clip-dur">' + ddFmtDur(r.end_time - r.start_time) + '</span>' +
        '<span class="dd-clip-play">&#9654;</span>';
      (function(rec, el) {
        el.addEventListener('click', function() {
          document.querySelectorAll('.dd-clip').forEach(function(c) { c.classList.remove('active'); });
          el.classList.add('active');
          ddPlayClip(rec);
        });
      })(r, item);
      dd.clipsEl.appendChild(item);
    });
  }

  function ddPlayClip(rec) {
    if (dd.stream) { dd.stream._clear(); dd.stream = null; }
    dd.video.srcObject = null;
    dd.isLive = false;
    dd.liveBtn.classList.remove('active');
    dd.badge.className = 'playback';
    dd.badge.innerHTML = '&#9654; REC';
    dd.video.src = '/recordings/' + rec.path;
    dd.video.play().catch(function(){});
  }

  dd.liveBtn.addEventListener('click', function() {
    if (!dd.isLive) ddStartLive();
  });

  dd.closeBtn.addEventListener('click', closeDrilldown);

  dd.prevDay.addEventListener('click', function() {
    var d = new Date(dd.date);
    d.setDate(d.getDate() - 1);
    dd.date = d;
    dd.selectedHour = -1;
    ddLoadDay();
  });

  dd.nextDay.addEventListener('click', function() {
    var d = new Date(dd.date);
    d.setDate(d.getDate() + 1);
    var today = new Date();
    today = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    if (d <= today) { dd.date = d; dd.selectedHour = -1; ddLoadDay(); }
  });

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && !dd.overlay.hidden) closeDrilldown();
  });

  // Auto-refresh recordings every 30s while overlay is open on today's date
  var ddRefreshTimer = null;
  function ddStartRefresh() {
    ddStopRefresh();
    ddRefreshTimer = setInterval(function() {
      if (dd.overlay.hidden || !dd.date) return;
      var t = new Date();
      var today = new Date(t.getFullYear(), t.getMonth(), t.getDate());
      if (dd.date.getTime() === today.getTime()) ddLoadDay();
    }, 30000);
  }
  function ddStopRefresh() {
    if (ddRefreshTimer) { clearInterval(ddRefreshTimer); ddRefreshTimer = null; }
  }

  // Patch open/close to manage refresh timer
  var _origOpen = openDrilldown;
  openDrilldown = function(cam) { _origOpen(cam); ddStartRefresh(); };
  var _origClose = closeDrilldown;
  closeDrilldown = function() { ddStopRefresh(); _origClose(); };

  // Stale-stream watchdog: reconnect grid tiles whose video has stopped advancing
  var _prevTimes = {};
  setInterval(function() {
    Object.keys(streams).forEach(function(id) {
      var s = streams[id];
      if (!s.video || !s.video.srcObject || s._timer) return;
      var t = s.video.currentTime;
      if (t > 0 && _prevTimes[id] !== undefined && t === _prevTimes[id]) {
        s.reconnect();
      }
      _prevTimes[id] = t;
    });
  }, 20000);
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

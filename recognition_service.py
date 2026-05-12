# -*- coding: utf-8 -*-
"""
Farm Stall — Customer Recognition Service
Runs as a standalone Windows service alongside the POS.
Listens for Frigate webhooks on port 8080, runs ANPR + face + body matching,
then logs identified customers back to the POS API.
"""

import os, sys, time, json, logging, base64, threading, requests
from datetime import datetime
from pathlib import Path

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'recognition_service.log')
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger('recognition')

# ─── Config ────────────────────────────────────────────────────────────────
POS_URL         = os.environ.get('POS_URL',     'http://127.0.0.1:5000')
POS_USER        = os.environ.get('POS_USER',    'admin')
POS_PASS        = os.environ.get('POS_PASS',    'admin123')
FRIGATE_URL     = os.environ.get('FRIGATE_URL', 'http://127.0.0.1:8971')
WEBHOOK_PORT    = int(os.environ.get('WEBHOOK_PORT', '8080'))
FACE_THRESHOLD  = float(os.environ.get('FACE_THRESHOLD',  '0.40'))
GAIT_THRESHOLD  = float(os.environ.get('GAIT_THRESHOLD',  '0.25'))

# ─── Lazy model loading ─────────────────────────────────────────────────────
_anpr_model   = None
_face_app     = None
_mp_pose      = None
_mp_pose_inst = None

def get_anpr():
    global _anpr_model
    if _anpr_model is None:
        from fast_plate_ocr import LicensePlateRecognizer
        _anpr_model = LicensePlateRecognizer('global-plates-mobile-vit-v2-model')
        logger.info('ANPR model loaded')
    return _anpr_model

def get_face_app():
    global _face_app
    if _face_app is None:
        try:
            import insightface
            from insightface.model_zoo import get_model
            # Version 0.2.1 - use retinaface detector + arcface recognizer
            _face_app = insightface.app.FaceAnalysis()
            # Manually set up models
            ctx_id = -1  # CPU
            _face_app.models = {}
            _face_app.models['detection'] = get_model('retinaface_r50_v1')
            _face_app.models['detection'].prepare(ctx_id=ctx_id, nms=0.4)
            _face_app.models['recognition'] = get_model('arcface_r100_v1')
            _face_app.models['recognition'].prepare(ctx_id=ctx_id)
            logger.info('InsightFace loaded (retinaface + arcface)')
        except Exception as e:
            logger.warning('InsightFace failed to load: %s. Face recognition disabled.', e)
            _face_app = None
    return _face_app

def get_pose():
    global _mp_pose, _mp_pose_inst
    if _mp_pose is None:
        import mediapipe as mp
        _mp_pose = mp.solutions.pose
        _mp_pose_inst = _mp_pose.Pose(static_image_mode=True, model_complexity=0, enable_segmentation=False)
        logger.info('MediaPipe Pose loaded')
    return _mp_pose, _mp_pose_inst

# ─── POS API session ────────────────────────────────────────────────────────
_pos_session = requests.Session()
_pos_logged_in = False

def pos_login():
    global _pos_logged_in
    try:
        r = _pos_session.post(f'{POS_URL}/api/login', json={'username': POS_USER, 'password': POS_PASS}, timeout=5)
        if r.ok:
            _pos_logged_in = True
            logger.info('Logged in to POS API')
        else:
            logger.warning('POS login failed: %s', r.text)
    except Exception as e:
        logger.warning('POS login error: %s', e)

def pos_post(path, payload):
    global _pos_logged_in
    if not _pos_logged_in:
        pos_login()
    try:
        r = _pos_session.post(f'{POS_URL}{path}', json=payload, timeout=10)
        if r.status_code == 401:
            pos_login()
            r = _pos_session.post(f'{POS_URL}{path}', json=payload, timeout=10)
        return r.json() if r.ok else None
    except Exception as e:
        logger.warning('POS POST %s error: %s', path, e)
        return None

def pos_get(path):
    global _pos_logged_in
    if not _pos_logged_in:
        pos_login()
    try:
        r = _pos_session.get(f'{POS_URL}{path}', timeout=10)
        return r.json() if r.ok else []
    except Exception as e:
        logger.warning('POS GET %s error: %s', path, e)
        return []

# ─── Load enrolled customers from POS ──────────────────────────────────────
_customers_cache = []
_cache_lock = threading.Lock()

def refresh_customers():
    customers = pos_get('/api/customers')
    with _cache_lock:
        _customers_cache.clear()
        _customers_cache.extend(customers)
    logger.info('Customer cache refreshed: %d customers', len(customers))

def _cache_refresh_loop():
    while True:
        try:
            refresh_customers()
        except Exception as e:
            logger.warning('Cache refresh error: %s', e)
        time.sleep(60)

# ─── Recognition helpers ────────────────────────────────────────────────────
import numpy as np

def cosine_sim(a, b):
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0

def euclidean_dist(a, b):
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    return float(np.linalg.norm(a - b))

def read_image(path):
    import cv2
    img = cv2.imread(path)
    if img is None:
        raise ValueError(f'Cannot read image: {path}')
    return img

def run_anpr(image_path):
    """Returns (plate_str, confidence) or (None, None)."""
    try:
        model = get_anpr()
        results = model.run(image_path)
        if results:
            plate, conf = results[0][0], results[0][1] if len(results[0]) > 1 else 1.0
            return plate.upper().replace(' ', ''), float(conf)
    except Exception as e:
        logger.warning('ANPR error: %s', e)
    return None, None

def run_face(image_path):
    """Returns embedding as bytes, or None."""
    try:
        face_app = get_face_app()
        if face_app is None:
            return None
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            return None
        faces = face_app.get(img)
        if not faces:
            return None
        emb = faces[0].embedding.astype(np.float32)
        return emb.tobytes()
    except Exception as e:
        logger.warning('Face error: %s', e)
    return None

def run_gait(image_path):
    """Extracts body proportion features from a single frame. Returns bytes or None."""
    try:
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            return None
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_pose, pose = get_pose()
        result = pose.process(rgb)
        if not result.pose_landmarks:
            return None
        lm = result.pose_landmarks.landmark
        # Extract 6 body proportion features from keypoints
        def pt(idx):
            return np.array([lm[idx].x, lm[idx].y])
        try:
            left_shoulder  = pt(11); right_shoulder = pt(12)
            left_hip       = pt(23); right_hip      = pt(24)
            left_ankle     = pt(27); right_ankle    = pt(28)
            left_knee      = pt(25); right_knee     = pt(26)
            nose           = pt(0)
            shoulder_width = np.linalg.norm(left_shoulder - right_shoulder)
            hip_width      = np.linalg.norm(left_hip - right_hip)
            mid_shoulder   = (left_shoulder + right_shoulder) / 2
            mid_hip        = (left_hip + right_hip) / 2
            mid_ankle      = (left_ankle + right_ankle) / 2
            torso_height   = np.linalg.norm(mid_shoulder - mid_hip)
            leg_height     = np.linalg.norm(mid_hip - mid_ankle)
            total_height   = np.linalg.norm(nose - mid_ankle)
            features = np.array([
                shoulder_width / (total_height + 1e-6),
                hip_width / (total_height + 1e-6),
                torso_height / (total_height + 1e-6),
                leg_height / (total_height + 1e-6),
                shoulder_width / (hip_width + 1e-6),
                torso_height / (leg_height + 1e-6),
            ], dtype=np.float32)
            return features.tobytes()
        except Exception:
            return None
    except Exception as e:
        logger.warning('Gait error: %s', e)
    return None

# ─── Matching ────────────────────────────────────────────────────────────────
def identify_customer(plate=None, face_bytes=None, gait_bytes=None):
    """
    Returns (customer_id, matched_signals_list, scores_dict) if 2+ signals agree.
    Returns (None, [], {}) otherwise.
    """
    from collections import Counter
    with _cache_lock:
        customers = list(_customers_cache)

    matches = {}  # signal_name -> customer_id
    scores  = {}

    # 1. Plate — exact match
    if plate:
        for c in customers:
            if plate in c.get('plates', []):
                matches['plate'] = c['id']
                scores['plate'] = 1.0
                break

    # 2. Face — cosine similarity
    if face_bytes:
        face_emb = np.frombuffer(face_bytes, dtype=np.float32)
        # Fetch face embeddings from POS
        all_faces = pos_get('/api/customers/faces_raw')  # internal endpoint added below
        best_sim, best_cid = 0.0, None
        for row in all_faces:
            try:
                stored = np.frombuffer(base64.b64decode(row['embedding_b64']), dtype=np.float32)
                sim = cosine_sim(face_emb, stored)
                if sim > best_sim:
                    best_sim, best_cid = sim, row['customer_id']
            except Exception:
                pass
        if best_cid and best_sim >= FACE_THRESHOLD:
            matches['face'] = best_cid
            scores['face'] = round(best_sim, 3)

    # 3. Gait — euclidean distance
    if gait_bytes:
        gait_feat = np.frombuffer(gait_bytes, dtype=np.float32)
        all_gaits = pos_get('/api/customers/gaits_raw')
        best_dist, best_cid = float('inf'), None
        for row in all_gaits:
            try:
                stored = np.frombuffer(base64.b64decode(row['features_b64']), dtype=np.float32)
                dist = euclidean_dist(gait_feat, stored)
                if dist < best_dist:
                    best_dist, best_cid = dist, row['customer_id']
            except Exception:
                pass
        if best_cid and best_dist < GAIT_THRESHOLD:
            matches['gait'] = best_cid
            scores['gait'] = round(1.0 - best_dist, 3)

    if len(matches) < 2:
        return None, [], {}

    # Vote: need 2+ signals agreeing on same customer
    counter = Counter(matches.values())
    top_cid, top_count = counter.most_common(1)[0]
    if top_count < 2:
        return None, [], {}

    matched_signals = [sig for sig, cid in matches.items() if cid == top_cid]
    return top_cid, matched_signals, scores

# ─── Event processing ────────────────────────────────────────────────────────
def fetch_frigate_snapshot(event_id):
    """Downloads the Frigate snapshot for an event, saves to temp file, returns path."""
    import tempfile
    url = f'{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg'
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
            tmp.write(r.content)
            tmp.close()
            return tmp.name
    except Exception as e:
        logger.warning('Snapshot fetch error: %s', e)
    return None

def process_event(event):
    label       = event.get('label', '')
    event_id    = event.get('id', '')
    camera      = event.get('camera', '')
    is_outdoor  = 'outdoor' in camera.lower()

    snapshot_path = fetch_frigate_snapshot(event_id)
    if not snapshot_path:
        logger.warning('No snapshot for event %s', event_id)
        return

    try:
        plate_str = face_bytes = gait_bytes = None
        conf = None

        if label == 'car' and is_outdoor:
            plate_str, conf = run_anpr(snapshot_path)
            if plate_str:
                logger.info('Plate detected: %s (%.2f)', plate_str, conf or 0)

        if label == 'person':
            face_bytes = run_face(snapshot_path)
            gait_bytes = run_gait(snapshot_path)

        # Log plate detection regardless of match
        if plate_str:
            cid, matched, scores = identify_customer(plate=plate_str)
            pos_post('/api/customers/log_plate', {
                'plate_number': plate_str,
                'confidence': conf,
                'customer_id': cid,
                'matched': cid is not None,
                'snapshot_path': snapshot_path,
                'camera_source': camera,
            })
            if cid and len(matched) >= 2:
                pos_post('/api/customers/identify', {
                    'customer_id': cid,
                    'matched_signals': ','.join(matched),
                    'confidence_scores': scores,
                    'camera_source': camera,
                })
                logger.info('Customer %d identified via %s', cid, matched)
                refresh_customers()
                return

        if face_bytes or gait_bytes:
            cid, matched, scores = identify_customer(face_bytes=face_bytes, gait_bytes=gait_bytes)
            if cid and len(matched) >= 2:
                pos_post('/api/customers/identify', {
                    'customer_id': cid,
                    'matched_signals': ','.join(matched),
                    'confidence_scores': scores,
                    'camera_source': camera,
                })
                logger.info('Customer %d identified via %s', cid, matched)
                refresh_customers()

    finally:
        try:
            os.unlink(snapshot_path)
        except Exception:
            pass

# ─── Frigate fallback poller ────────────────────────────────────────────────
_seen_events = set()

def poll_frigate_events():
    while True:
        try:
            r = requests.get(f'{FRIGATE_URL}/api/events?limit=20&has_snapshot=1', timeout=10)
            if r.ok:
                for ev in r.json():
                    eid = ev.get('id')
                    if eid and eid not in _seen_events:
                        _seen_events.add(eid)
                        if len(_seen_events) > 500:
                            oldest = list(_seen_events)[:100]
                            for o in oldest:
                                _seen_events.discard(o)
                        threading.Thread(target=process_event, args=(ev,), daemon=True).start()
        except Exception as e:
            logger.warning('Frigate poll error: %s', e)
        time.sleep(30)

# ─── Webhook server ─────────────────────────────────────────────────────────
from http.server import HTTPServer, BaseHTTPRequestHandler

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default HTTP logs

    def do_POST(self):
        if self.path != '/webhook/frigate':
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        try:
            payload = json.loads(body)
            event_type = payload.get('type')
            after = payload.get('after') or payload.get('before') or {}
            # Only process on 'end' (tracking complete, best snapshot available)
            if event_type == 'end':
                threading.Thread(target=process_event, args=(after,), daemon=True).start()
        except Exception as e:
            logger.warning('Webhook parse error: %s', e)

def run_webhook_server():
    server = HTTPServer(('0.0.0.0', WEBHOOK_PORT), WebhookHandler)
    logger.info('Webhook server listening on port %d', WEBHOOK_PORT)
    server.serve_forever()

# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    logger.info('Recognition service starting')
    pos_login()
    refresh_customers()

    # Background cache refresh every 60s
    threading.Thread(target=_cache_refresh_loop, daemon=True).start()

    # Background Frigate poller (fallback for missed webhooks)
    threading.Thread(target=poll_frigate_events, daemon=True).start()

    # Webhook server (blocking)
    run_webhook_server()

# -*- coding: utf-8 -*-
"""
Farm Stall — Customer Recognition Service v2.0
Production-grade multi-biometric identification with:
- Quality-gated feature extraction
- Normalized scoring with safety constraints
- Track-level identity consistency
- ROC-calibrated thresholds with segmentation
- Full decision audit trail
"""

import os, sys, time, json, logging, base64, threading, requests, hashlib, uuid
from datetime import datetime
from pathlib import Path
import collections
from collections import defaultdict
import numpy as np

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'recognition_service_v2.log')
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

class _CircularLogHandler(logging.Handler):
    """Keeps the last N log records in memory for the monitor."""
    MAX = 500
    def __init__(self):
        super().__init__()
        self._records = collections.deque(maxlen=self.MAX)
        self._lock = threading.Lock()
    def emit(self, record):
        with self._lock:
            self._records.append({
                'ts':  self.formatter.formatTime(record, '%H:%M:%S') if self.formatter else '',
                'lvl': record.levelname,
                'msg': record.getMessage(),
            })
    def get(self, n=200, level=None):
        with self._lock:
            recs = list(self._records)
        if level:
            recs = [r for r in recs if r['lvl'] == level.upper()]
        return recs[-n:]

_log_buffer = _CircularLogHandler()
_log_buffer.setFormatter(logging.Formatter('%(asctime)s'))

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(),
        _log_buffer,
    ]
)
logger = logging.getLogger('recognition_v2')

# ─── Config ────────────────────────────────────────────────────────────────
POS_URL         = os.environ.get('POS_URL',     'https://127.0.0.1:5000')
POS_USER        = os.environ.get('POS_USER',    'admin')
POS_PASS        = os.environ.get('POS_PASS',    'admin123')
FRIGATE_URL     = os.environ.get('FRIGATE_URL', 'http://127.0.0.1:8971')
WEBHOOK_PORT    = int(os.environ.get('WEBHOOK_PORT', '8080'))

# Model thresholds — overridden at startup from POS settings (GET /api/settings)
FACE_THRESHOLD  = 0.35  # Minimum cosine similarity for face match
GAIT_THRESHOLD  = 0.40  # Euclidean distance for gait match — 0.40 for 12-dim L2-normalized temporal vector

# Quality gates
FACE_QUALITY_MIN = 0.15  # Minimum face quality — recalibrated for head crops
GAIT_QUALITY_MIN = 0.45  # Mounted camera looking down scores lower
PLATE_CONF_MIN   = 0.8   # Minimum OCR confidence to use

def reload_thresholds_from_pos():
    """Pull all tunable thresholds from POS settings."""
    global FACE_THRESHOLD, FACE_QUALITY_MIN, MAX_FACE_EMBEDDINGS, MIN_ANGLE_DISTANCE
    try:
        settings = pos_get('/api/settings')
        if isinstance(settings, dict):
            if 'face_threshold' in settings:
                FACE_THRESHOLD = float(settings['face_threshold'])
            if 'face_quality_min' in settings:
                FACE_QUALITY_MIN = float(settings['face_quality_min'])
            if 'link_threshold' in settings:
                _threshold_manager.global_thresholds['link'] = float(settings['link_threshold'])
            if 'max_face_angles' in settings:
                MAX_FACE_EMBEDDINGS = int(float(settings['max_face_angles']))
            if 'min_angle_distance' in settings:
                MIN_ANGLE_DISTANCE = float(settings['min_angle_distance'])
            logger.info(f'Thresholds reloaded: face={FACE_THRESHOLD}, quality_min={FACE_QUALITY_MIN}, '
                        f'link={_threshold_manager.global_thresholds["link"]}, '
                        f'max_angles={MAX_FACE_EMBEDDINGS}, min_dist={MIN_ANGLE_DISTANCE}')
    except Exception as e:
        logger.warning(f'Could not reload thresholds from POS: {e}')

# Multi-embedding: keep this many distinct-angle embeddings per customer
MAX_FACE_EMBEDDINGS = 24
MIN_ANGLE_DISTANCE  = 0.25

# ─── Session-centric identity constants ─────────────────────────────────────
# Real-time track path: advisory only — never creates customers
STRONG_LINK_THRESHOLD    = 0.65   # face_sim threshold for teller notification
MIN_STRONG_MATCH_OBS     = 2      # consecutive face-present obs at ≥0.65 before notifying
MIN_FACE_FOR_WELCOME     = 0.55   # face_sim floor — gait/context alone cannot trigger welcome

# Session lifecycle
SESSION_IDLE_EXPIRY      = 60     # seconds idle → resolver fires
MAX_SESSION_LIFETIME     = 300    # seconds hard cap regardless of activity

# Session clustering thresholds
SESSION_JOIN_FACE_SIM    = 0.50   # min face_sim to join an existing session — 0.40 too loose on indoor camera
SESSION_MERGE_FACE_SIM   = 0.62   # min face_sim to merge two sessions — 0.50 too loose on indoor camera

# Resolver thresholds
RESOLVER_LINK_THRESHOLD  = 0.50   # resolver links to existing customer
RECENT_CUSTOMER_SIM      = 0.40   # anti-clone: link to recently-created customer
ANON_IDENTITY_SIM        = 0.45   # sim to anonymous identity → merge evidence into it

# Customer creation gates (all must pass)
MIN_FACES_TO_CREATE      = 5      # face embedding count required
MIN_HIGH_QUALITY_FACES   = 2      # of those, must be quality ≥ FACE_QUALITY_MIN_CREATE
FACE_QUALITY_MIN_CREATE  = 0.25   # quality floor — indoor camera clips score 0.22–0.35
MIN_SESSION_DURATION     = 30     # seconds session must exist before creation
CLEARLY_NOT_EXISTING     = 0.35   # best_face_sim must be BELOW this to create
                                   # 0.35–0.50 band → anonymous identity, not new customer

ANON_IDENTITY_TTL        = 86400  # 24 hours

# Versioning
WEIGHTS_VERSION = "v2.0_production"
THRESHOLD_VERSION = "v1.0_initial"

# ─── Feature Weights (production-tuned) ─────────────────────────────────────
FEATURE_WEIGHTS = {
    # Biometric (identity-grade)
    'face':         6.0,
    # Gait here is single-frame body proportions, not temporal gait — weight accordingly.
    # True temporal gait (stride cadence etc.) would warrant 3.0+.
    'gait':         1.0,

    # Support signals (cannot link alone)
    'plate':        2.0,
    'height_cat':   0.5,
    'build':        0.4,
    'hair_color':   0.3,
    'facial_hair':  0.1,

    # Contextual (capped at 1.0 total)
    'time_pattern':       0.3,
    'zone_pattern':       0.3,
    'plate_person_assoc': 0.4,
}

BIOMETRIC_FEATURES = {'face', 'gait'}
SUPPORT_FEATURES = {'plate', 'height_cat', 'build', 'hair_color', 'facial_hair'}
CONTEXT_FEATURES = {'time_pattern', 'zone_pattern', 'plate_person_assoc'}
MAX_CONTEXT_CONTRIBUTION = 0.25  # hard cap — context signals can never dominate biometrics

# ─── Lazy model loading ─────────────────────────────────────────────────────
_anpr_model   = None
_face_app     = None
_mp_pose_inst = None

# Serialise all ONNX inference calls — SCRFD/ArcFace/MediaPipe are CPU-only and
# running them from multiple threads simultaneously causes CPU spikes to 600%+.
# One inference at a time is faster overall due to cache locality.
_inference_lock = threading.Lock()

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
            from insightface.model_zoo import SCRFD, ArcFaceONNX
            import cv2
            from skimage import transform as trans

            model_dir  = os.environ.get('INSIGHTFACE_HOME', os.path.expanduser('~/.insightface/models'))
            MODEL_NAME = os.environ.get('INSIGHTFACE_MODEL', 'antelopev2')
            model_base = os.path.join(model_dir, MODEL_NAME)

            # Auto-discover detector and recognizer — prefer antelopev2 files, fall back to buffalo_l
            det_model = next(
                (os.path.join(model_base, f) for f in ['scrfd_10g_bnkps.onnx', 'det_10g.onnx']
                 if os.path.exists(os.path.join(model_base, f))), None)
            rec_model = next(
                (os.path.join(model_base, f) for f in ['glintr100.onnx', 'w600k_r50.onnx']
                 if os.path.exists(os.path.join(model_base, f))), None)

            if not det_model or not rec_model:
                logger.error(f'Face models not found in {model_base}. '
                             f'Available: {os.listdir(model_base) if os.path.isdir(model_base) else "dir missing"}')
                return None

            logger.info(f'Face models: det={os.path.basename(det_model)} rec={os.path.basename(rec_model)}')

            # Use OpenVINO GPU if available (Intel iGPU), fall back to CPU
            import onnxruntime as _ort
            _avail = _ort.get_available_providers()
            if 'OpenVINOExecutionProvider' in _avail:
                _providers = [('OpenVINOExecutionProvider', {'device_type': 'GPU'}), 'CPUExecutionProvider']
                logger.info('ONNX using OpenVINO GPU provider')
            else:
                _providers = ['CPUExecutionProvider']
                logger.info('ONNX using CPU provider (OpenVINO not available)')

            detector = SCRFD(model_file=det_model)
            detector.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=0.3)
            if hasattr(detector, 'session') and detector.session is not None:
                import onnxruntime as _ort2
                detector.session = _ort2.InferenceSession(det_model, providers=_providers)

            recognizer = ArcFaceONNX(model_file=rec_model)
            recognizer.prepare(ctx_id=-1)
            if hasattr(recognizer, 'session') and recognizer.session is not None:
                import onnxruntime as _ort3
                recognizer.session = _ort3.InferenceSession(rec_model, providers=_providers)

            class FaceApp:
                def __init__(self, det, rec):
                    self.detector = det
                    self.recognizer = rec

                def get_with_quality(self, img):
                    """
                    Extract face with quality score
                    Returns: [(embedding_bytes, quality_score, bbox), ...] or []
                    """
                    bboxes, kpss = self.detector.detect(img, input_size=(640, 640))
                    if len(bboxes) == 0 or len(kpss) == 0:
                        logger.debug(f'SCRFD: no faces in {img.shape[1]}x{img.shape[0]} image')
                        return []
                    logger.debug(f'SCRFD: {len(bboxes)} face(s) in {img.shape[1]}x{img.shape[0]}, confs={[round(float(b[4]),2) for b in bboxes]}')

                    results = []
                    img_area = img.shape[0] * img.shape[1]

                    for bbox, kps in zip(bboxes, kpss):
                        # Quality scoring
                        det_conf = bbox[4] if len(bbox) > 4 else 0.8

                        # Face size
                        face_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        size_ratio = face_area / img_area
                        # Use 2.5% threshold — crops are already head-region so
                        # face fills less of the image than a full-frame shot would
                        size_score = min(1.0, size_ratio / 0.025)

                        # Blur detection
                        face_crop = img[int(bbox[1]):int(bbox[3]), int(bbox[0]):int(bbox[2])]
                        if face_crop.size == 0:
                            continue
                        gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY) if len(face_crop.shape) == 3 else face_crop
                        blur_var = cv2.Laplacian(gray, cv2.CV_64F).var()
                        blur_score = min(1.0, blur_var / 500)

                        # Combined quality
                        quality = det_conf * 0.4 + size_score * 0.4 + blur_score * 0.2

                        # Quality gate
                        if quality < FACE_QUALITY_MIN:
                            logger.debug(f'Face quality too low: {quality:.2f}')
                            continue

                        # Align face
                        tform = trans.SimilarityTransform()
                        tform.estimate(kps, [[38.2946, 51.6963], [73.5318, 51.5014],
                                            [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]])
                        face_img = cv2.warpAffine(img, tform.params[0:2, :], (112, 112), borderValue=0.0)

                        # Ensure RGB
                        if len(face_img.shape) == 2:
                            face_img = cv2.cvtColor(face_img, cv2.COLOR_GRAY2BGR)
                        elif len(face_img.shape) == 3 and face_img.shape[2] == 4:
                            face_img = cv2.cvtColor(face_img, cv2.COLOR_BGRA2BGR)

                        # Get embedding
                        emb = self.recognizer.get_feat(face_img)[0].astype(np.float32)
                        # Encode face crop as JPEG for storage
                        _, jpeg_buf = cv2.imencode('.jpg', face_img, [cv2.IMWRITE_JPEG_QUALITY, 90])
                        results.append((emb.tobytes(), quality, bbox, jpeg_buf.tobytes()))

                    return results

            _face_app = FaceApp(detector, recognizer)
            logger.info('InsightFace loaded (SCRFD + ArcFace)')
        except Exception as e:
            logger.error(f'Face recognition unavailable: {e}')
            import traceback
            traceback.print_exc()
            _face_app = None
    return _face_app

def get_pose():
    global _mp_pose_inst
    if _mp_pose_inst is None:
        try:
            import mediapipe as mp
            import urllib.request

            model_dir = os.path.expanduser('~/.mediapipe/models')
            os.makedirs(model_dir, exist_ok=True)
            model_path = os.path.join(model_dir, 'pose_landmarker_lite.task')

            if not os.path.exists(model_path):
                logger.info('Downloading MediaPipe Pose model (~15MB)...')
                urllib.request.urlretrieve(
                    'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task',
                    model_path
                )

            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            base_options = python.BaseOptions(model_asset_path=model_path)
            options = vision.PoseLandmarkerOptions(
                base_options=base_options,
                running_mode=vision.RunningMode.IMAGE)
            _mp_pose_inst = vision.PoseLandmarker.create_from_options(options)
            logger.info('MediaPipe Pose loaded')
        except Exception as e:
            logger.error(f'MediaPipe Pose unavailable: {e}')
            _mp_pose_inst = None
    return _mp_pose_inst

# Second MediaPipe instance in VIDEO mode — for temporal gait from clips
_mp_pose_video = None
_mp_pose_model_path = None  # cached after first get_pose() call

def get_pose_video():
    """Returns a MediaPipe PoseLandmarker in VIDEO mode for temporal clip analysis."""
    global _mp_pose_video, _mp_pose_model_path
    if _mp_pose_video is None:
        try:
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            model_dir = os.path.expanduser('~/.mediapipe/models')
            model_path = os.path.join(model_dir, 'pose_landmarker_lite.task')
            if not os.path.exists(model_path):
                get_pose()  # ensure the model file is downloaded first
            if not os.path.exists(model_path):
                return None

            options = vision.PoseLandmarkerOptions(
                base_options=python.BaseOptions(model_asset_path=model_path),
                running_mode=vision.RunningMode.VIDEO)
            _mp_pose_video = vision.PoseLandmarker.create_from_options(options)
            logger.info('MediaPipe Pose (VIDEO mode) loaded for temporal gait')
        except Exception as e:
            logger.error(f'MediaPipe Pose (VIDEO) unavailable: {e}')
            _mp_pose_video = None
    return _mp_pose_video


def extract_temporal_gait_from_clip(frames_seq):
    """Extract temporal gait features from a sequence of (frame, timestamp_ms) pairs.
    Uses zero-crossing cadence analysis — no FFT, stable on noisy real-world clips.
    Returns (features_bytes, quality) or (None, 0.0).
    Feature vector: 12 × float32, L2-normalized. Incompatible with old 6-float gait.
    Creates a fresh VIDEO mode pose instance per clip — MediaPipe VIDEO mode requires
    strictly monotonically increasing timestamps across its lifetime, so reusing an
    instance across clips would fail when clip 2's timestamps restart from 0.
    """
    if len(frames_seq) < 20:
        logger.debug(f'Temporal gait: only {len(frames_seq)} frames, need ≥20, skipping')
        return None, 0.0
    logger.debug(f'Temporal gait: attempting extraction on {len(frames_seq)} frames')
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        model_dir = os.path.expanduser('~/.mediapipe/models')
        model_path = os.path.join(model_dir, 'pose_landmarker_lite.task')
        if not os.path.exists(model_path):
            get_pose()  # trigger download
        if not os.path.exists(model_path):
            return None, 0.0

        # Fresh instance per clip — avoids non-monotonic timestamp error across clips
        options = vision.PoseLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=model_path),
            running_mode=vision.RunningMode.VIDEO)
        pose = vision.PoseLandmarker.create_from_options(options)

        ankle_y_l, ankle_y_r, shoulder_w, hip_w = [], [], [], []

        for frame, ts_ms in frames_seq:
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB,
                              data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            result = pose.detect_for_video(mp_img, int(ts_ms))
            if not result.pose_landmarks:
                continue
            lm = result.pose_landmarks[0]
            # Prefer ankles (27/28), fall back to knees (25/26) when ankles not visible.
            # Knees oscillate at same frequency as ankles during walking and are
            # more visible from mounted cameras looking down at an angle.
            VIS = 0.3  # lowered from 0.5 — partial visibility is acceptable
            l_leg = lm[27] if lm[27].visibility > VIS else (lm[25] if lm[25].visibility > VIS else None)
            r_leg = lm[28] if lm[28].visibility > VIS else (lm[26] if lm[26].visibility > VIS else None)
            if l_leg:
                ankle_y_l.append(l_leg.y)
            if r_leg:
                ankle_y_r.append(r_leg.y)
            shoulder_w.append(abs(lm[11].x - lm[12].x))
            hip_w.append(abs(lm[23].x - lm[24].x))

        logger.debug(f'Temporal gait landmarks: ankle_l={len(ankle_y_l)} ankle_r={len(ankle_y_r)} '
                     f'shoulder={len(shoulder_w)} from {len(frames_seq)} frames')

        # Need at least one ankle with enough samples to compute cadence.
        # Both ankles preferred for symmetry score, but cameras often only show one.
        best_ankle = ankle_y_l if len(ankle_y_l) >= len(ankle_y_r) else ankle_y_r
        if len(best_ankle) < 10:
            logger.debug(f'Temporal gait: insufficient ankle detections (best={len(best_ankle)}, need ≥10)')
            return None, 0.0
        if len(shoulder_w) < 10:
            logger.debug(f'Temporal gait: insufficient shoulder detections ({len(shoulder_w)}, need ≥10)')
            return None, 0.0

        def zcr(s):
            """Zero-crossing rate — proxy for stride cadence."""
            d = np.array(s) - np.mean(s)
            return float(len(np.where(np.diff(np.sign(d)))[0])) / len(d)

        cl = zcr(ankle_y_l) if len(ankle_y_l) >= 5 else 0.0
        cr = zcr(ankle_y_r) if len(ankle_y_r) >= 5 else 0.0
        # If only one ankle visible, symmetry is unknown — use neutral 0.5
        if len(ankle_y_l) < 5 or len(ankle_y_r) < 5:
            sym = 0.5
        else:
            sym = 1.0 - abs(cl - cr) / max(cl + cr, 1e-6)
        # Use best-ankle cadence as the primary cadence signal
        cl = zcr(best_ankle)
        cr = cl  # single-ankle: assume symmetric
        s_st = 1.0 - np.std(shoulder_w) / (np.mean(shoulder_w) + 1e-6)
        h_st = 1.0 - np.std(hip_w)      / (np.mean(hip_w)      + 1e-6)

        features = np.array([
            cl, cr, sym,
            np.mean(shoulder_w), float(np.std(shoulder_w)),
            np.mean(hip_w),      float(np.std(hip_w)),
            s_st, h_st,
            np.mean(shoulder_w) / (np.mean(hip_w) + 1e-6),
            np.mean(ankle_y_l),  np.mean(ankle_y_r),
        ], dtype=np.float32)

        # L2-normalize so euclidean distance isn't dominated by large-scale features
        norm = np.linalg.norm(features)
        if norm > 0:
            features = features / norm

        quality = float(sym * 0.5 + max(0.0, s_st) * 0.25 + max(0.0, h_st) * 0.25)
        logger.debug(f'Temporal gait: cadence_l={cl:.3f} cadence_r={cr:.3f} '
                     f'sym={sym:.3f} quality={quality:.3f}')
        return features.tobytes(), quality

    except Exception as e:
        import traceback
        logger.debug(f'Temporal gait extraction failed: {e}\n{traceback.format_exc()}')
        return None, 0.0


# ─── POS API session ────────────────────────────────────────────────────────
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_pos_session = requests.Session()
_pos_session.verify = False
# Keep-alive pool: 10 connections, no retry storms
_adapter = HTTPAdapter(pool_connections=2, pool_maxsize=10, max_retries=0)
_pos_session.mount('http://', _adapter)
_pos_session.mount('https://', _adapter)
_pos_session.headers.update({'Connection': 'keep-alive'})
_pos_logged_in = False

_pos_last_success = time.time()   # epoch of last successful POS API call
_pos_down_warned  = False         # suppress repeated "POS down" warnings

def pos_login():
    global _pos_logged_in, _pos_last_success, _pos_down_warned
    try:
        r = _pos_session.post(f'{POS_URL}/api/login', json={'username': POS_USER, 'password': POS_PASS}, timeout=5)
        if r.ok:
            _pos_logged_in = True
            _pos_last_success = time.time()
            _pos_down_warned = False
            logger.info('Logged in to POS API')
        else:
            logger.warning(f'POS login failed: {r.text}')
    except Exception as e:
        logger.warning(f'POS login error: {e}')
        _check_pos_sustained_outage()

def _to_json_safe(obj):
    """Recursively cast numpy scalar types to Python builtins for JSON serialisation."""
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    if hasattr(obj, 'item'):   # numpy scalar (float32, int64, etc.)
        return obj.item()
    return obj

def _check_pos_sustained_outage():
    global _pos_down_warned
    if not _pos_down_warned and (time.time() - _pos_last_success) > 300:
        logger.error(f'POS has been unreachable for >5 minutes — visits and enrollments are not being recorded')
        _pos_down_warned = True

def pos_post(path, payload, retries=2):
    global _pos_logged_in, _pos_last_success, _pos_down_warned
    if not _pos_logged_in:
        pos_login()

    payload = _to_json_safe(payload)
    for attempt in range(retries + 1):
        try:
            r = _pos_session.post(f'{POS_URL}{path}', json=payload, timeout=10)
            if r.status_code == 401:
                pos_login()
                r = _pos_session.post(f'{POS_URL}{path}', json=payload, timeout=10)
            if r.ok:
                _pos_last_success = time.time()
                _pos_down_warned = False
            return r.json() if r.ok else None
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < retries:
                time.sleep(0.5)
                continue
            logger.warning(f'POS POST {path} failed after {retries} retries: {e}')
            _check_pos_sustained_outage()
            return None
        except Exception as e:
            logger.warning(f'POS POST {path} error: {e}')
            return None

def pos_get(path, retries=2):
    global _pos_logged_in, _pos_last_success, _pos_down_warned
    if not _pos_logged_in:
        pos_login()

    for attempt in range(retries + 1):
        try:
            r = _pos_session.get(f'{POS_URL}{path}', timeout=10)
            if r.status_code == 401:
                pos_login()
                r = _pos_session.get(f'{POS_URL}{path}', timeout=10)
            if r.ok:
                _pos_last_success = time.time()
                _pos_down_warned = False
            return r.json() if r.ok else []
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < retries:
                time.sleep(0.5)
                continue
            logger.warning(f'POS GET {path} failed after {retries} retries: {e}')
            _check_pos_sustained_outage()
            return []
        except Exception as e:
            logger.warning(f'POS GET {path} error: {e}')
            return []

# ─── Customer cache ─────────────────────────────────────────────────────────
_customers_cache = []
_customers_cache_map = {}  # id → customer dict, O(1) lookup for threshold decay
_signals_cache = {}       # customer_id -> signals dict, rebuilt when customer list changes
_signals_cache_ids = set()  # set of customer ids in the cache, used to detect changes
_cache_lock = threading.Lock()
_cache_rebuild_lock = threading.Lock()  # prevents concurrent full cache rebuilds

# ─── Per-customer time-decayed threshold cache ───────────────────────────────
_threshold_cache = {}        # customer_id → effective_threshold
_threshold_cache_time = 0.0  # epoch — reset to 0 to force recompute on next access

def _compute_per_customer_thresholds(link_threshold):
    """Return per-customer effective link thresholds based on days since last visit.
    Cached for 5 minutes but invalidated whenever _customers_cache_map updates.
    Long-absent customers get a relaxed threshold to account for appearance drift."""
    global _threshold_cache, _threshold_cache_time
    if time.time() - _threshold_cache_time < 300:
        return _threshold_cache
    result = {}
    for cid, c in _customers_cache_map.items():
        last = c.get('last_visit')
        days = 0
        if last:
            try:
                days = (datetime.utcnow() - datetime.fromisoformat(last.replace('Z', ''))).days
            except Exception:
                pass
        if   days > 365: eff = max(0.32, link_threshold - 0.18)
        elif days > 90:  eff = max(0.38, link_threshold - 0.12)
        elif days > 7:   eff = max(0.42, link_threshold - 0.08)
        else:            eff = link_threshold
        result[cid] = eff
    _threshold_cache      = result
    _threshold_cache_time = time.time()
    return result

def refresh_customers():
    global _signals_cache_ids, _customers_cache_map, _threshold_cache_time
    customers = pos_get('/api/customers')
    with _cache_lock:
        _customers_cache.clear()
        _customers_cache.extend(customers)
        _customers_cache_map = {c['id']: c for c in customers}
    # Invalidate signals cache so next event rebuilds with new customer set
    _signals_cache_ids = set()
    # Invalidate threshold cache — last_visit may have changed
    _threshold_cache_time = 0.0
    logger.info(f'Customer cache refreshed: {len(customers)} customers')

def _cache_refresh_loop():
    while True:
        try:
            refresh_customers()
            reload_thresholds_from_pos()
        except Exception as e:
            logger.warning(f'Cache refresh error: {e}')
        time.sleep(60)

# ─── Helper functions ───────────────────────────────────────────────────────
def cosine_sim(a, b):
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0

def euclidean_dist(a, b):
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    return float(np.linalg.norm(a - b))

def fuzzy_plate_match(plate_a, plate_b, max_distance=1):
    if not plate_a or not plate_b or len(plate_a) != len(plate_b):
        return False
    distance = sum(1 for a, b in zip(plate_a, plate_b) if a != b)
    return distance <= max_distance

def same_color_group(color_a, color_b):
    color_groups = {
        'dark': ['black', 'brown'],
        'light': ['blonde', 'gray', 'white'],
        'red': ['red', 'auburn']
    }
    for group, colors in color_groups.items():
        if color_a in colors and color_b in colors:
            return True
    return False

def adjacent_height_category(cat_a, cat_b):
    categories = ['short', 'medium', 'tall']
    try:
        idx_a = categories.index(cat_a)
        idx_b = categories.index(cat_b)
        return abs(idx_a - idx_b) == 1
    except ValueError:
        return False

# ─── Feature Extraction with Quality Gates ─────────────────────────────────

def extract_face_with_quality(image_path, person_box=None):
    """Returns (embedding_bytes, quality_score, photo_bytes) or (None, 0.0, None)

    person_box: normalised [x1,y1,x2,y2] from Frigate (0-1). When provided,
    the image is cropped to the head region (top 35% of the box, expanded by
    20%) before running face detection. This is critical when the camera sees
    the full body — the face would otherwise be too small for SCRFD to detect.
    """
    try:
        import cv2
        face_app = get_face_app()
        if not face_app:
            return None, 0.0, None

        img = cv2.imread(image_path)
        if img is None:
            return None, 0.0, None

        h, w = img.shape[:2]

        # Crop to head region when a person bounding box is available
        if person_box and len(person_box) == 4:
            bx1, by1, bx2, by2 = person_box
            box_h = by2 - by1
            # Head is roughly top 35% of the person box
            head_y1 = by1
            head_y2 = by1 + box_h * 0.35
            # Expand by 20% in all directions so we don't clip the face
            pad_x = (bx2 - bx1) * 0.20
            pad_y = box_h * 0.20
            cx1 = max(0.0, bx1 - pad_x)
            cy1 = max(0.0, head_y1 - pad_y)
            cx2 = min(1.0, bx2 + pad_x)
            cy2 = min(1.0, head_y2 + pad_y)
            px1, py1, px2, py2 = int(cx1*w), int(cy1*h), int(cx2*w), int(cy2*h)
            if px2 > px1 and py2 > py1:
                img = img[py1:py2, px1:px2]
                # Upscale small crops so the face fills the SCRFD input.
                # SCRFD uses input_size=(640,640) — a 150×180px crop gets
                # downscaled to ~150px which is too small for reliable detection.
                # Resize so the longer edge is 480px, keeping aspect ratio.
                ch, cw = img.shape[:2]
                scale = 480.0 / max(ch, cw)
                if scale > 1.0:
                    new_w, new_h = int(cw * scale), int(ch * scale)
                    img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                logger.debug(f'Face crop: box={[round(x,2) for x in person_box]} → head region {px1},{py1}-{px2},{py2} ({px2-px1}×{py2-py1}px → {img.shape[1]}×{img.shape[0]}px)')

        # CLAHE pre-processing: boost contrast on under-exposed faces before
        # passing to SCRFD. Converts to LAB, equalises the L channel only
        # (luminance), then converts back — preserves colour, lifts midtones.
        # ~2ms per image on CPU, can push quality scores from 0.19 → 0.35+.
        try:
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
            cl = clahe.apply(l)
            img = cv2.cvtColor(cv2.merge([cl, a, b]), cv2.COLOR_LAB2BGR)
        except Exception:
            pass  # fall through with original image if CLAHE fails

        with _inference_lock:
            results = face_app.get_with_quality(img)
        if not results:
            return None, 0.0, None

        # Return best quality face (embedding, quality, bbox, photo_bytes)
        best = max(results, key=lambda x: x[1])
        return best[0], best[1], best[3] if len(best) > 3 else None

    except Exception as e:
        logger.error(f'Face extraction error: {e}')
        return None, 0.0, None

def extract_gait_with_quality(image_path):
    """Returns (features_bytes, quality_score) or (None, 0.0)"""
    try:
        import cv2
        import mediapipe as mp

        pose_landmarker = get_pose()
        if not pose_landmarker:
            return None, 0.0

        img = cv2.imread(image_path)
        if img is None:
            return None, 0.0

        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        with _inference_lock:
            result = pose_landmarker.detect(mp_image)

        if not result.pose_landmarks or len(result.pose_landmarks) == 0:
            return None, 0.0

        landmarks = result.pose_landmarks[0]

        # Quality check
        key_points = [0, 11, 12, 23, 24, 25, 26, 27, 28]
        visible_count = sum(1 for idx in key_points if landmarks[idx].visibility > 0.5)
        visibility_score = visible_count / len(key_points)

        has_head = landmarks[0].visibility > 0.5
        has_feet = (landmarks[27].visibility > 0.5 or landmarks[28].visibility > 0.5)
        full_body_score = 1.0 if (has_head and has_feet) else 0.3

        quality = visibility_score * 0.6 + full_body_score * 0.4

        if quality < GAIT_QUALITY_MIN:
            return None, quality

        # Extract body proportions
        def pt(idx):
            return np.array([landmarks[idx].x, landmarks[idx].y])

        left_shoulder = pt(11); right_shoulder = pt(12)
        left_hip = pt(23); right_hip = pt(24)
        left_ankle = pt(27); right_ankle = pt(28)
        nose = pt(0)

        shoulder_width = np.linalg.norm(left_shoulder - right_shoulder)
        hip_width = np.linalg.norm(left_hip - right_hip)
        mid_shoulder = (left_shoulder + right_shoulder) / 2
        mid_hip = (left_hip + right_hip) / 2
        mid_ankle = (left_ankle + right_ankle) / 2
        torso_height = np.linalg.norm(mid_shoulder - mid_hip)
        leg_height = np.linalg.norm(mid_hip - mid_ankle)
        total_height = np.linalg.norm(nose - mid_ankle)

        features = np.array([
            shoulder_width / (total_height + 1e-6),
            hip_width / (total_height + 1e-6),
            torso_height / (total_height + 1e-6),
            leg_height / (total_height + 1e-6),
            shoulder_width / (hip_width + 1e-6),
            torso_height / (leg_height + 1e-6),
        ], dtype=np.float32)

        return features.tobytes(), quality

    except Exception as e:
        logger.error(f'Gait extraction error: {e}')
        return None, 0.0

def extract_plate_with_quality(image_path):
    """Returns (plate_str, quality_score) or (None, 0.0)"""
    try:
        model = get_anpr()
        with _inference_lock:
            results = model.run(image_path)
        if not results:
            return None, 0.0

        pred = results[0]
        plate = pred.plate if hasattr(pred, 'plate') else str(pred)
        confidence = pred.confidence if hasattr(pred, 'confidence') else 1.0

        if confidence < PLATE_CONF_MIN:
            return None, confidence

        return plate.upper().replace(' ', ''), confidence

    except Exception as e:
        logger.error(f'ANPR error: {e}')
        return None, 0.0

def extract_height_category(landmarks, img_shape):
    """Returns ('short'|'medium'|'tall', quality) or (None, 0.0)"""
    try:
        nose = landmarks[0]
        ankle_left = landmarks[27]
        ankle_right = landmarks[28]

        if nose.visibility < 0.7 or min(ankle_left.visibility, ankle_right.visibility) < 0.7:
            return None, 0.0

        # Check not at edge
        x_center = (landmarks[11].x + landmarks[12].x) / 2
        edge_distance = min(x_center, 1.0 - x_center)
        if edge_distance < 0.15:
            return None, edge_distance

        # Categorize
        ankle_y = (ankle_left.y + ankle_right.y) / 2
        height_ratio = abs(nose.y - ankle_y)

        if height_ratio < 0.65:
            category = 'short'
        elif height_ratio < 0.75:
            category = 'medium'
        else:
            category = 'tall'

        quality = min(nose.visibility, ankle_left.visibility, ankle_right.visibility) * edge_distance

        return category, quality

    except Exception as e:
        return None, 0.0

def extract_physical_attributes(image_path, person_box=None):
    """Extract physical attributes with confidence"""
    try:
        import cv2

        face_app = get_face_app()
        if not face_app:
            return None

        img = cv2.imread(image_path)
        if img is None:
            return None

        # Use full image for body measurements (gait/height need full body)
        # but crop to person box so MediaPipe doesn't try to analyse the whole scene
        if person_box and len(person_box) == 4:
            h, w = img.shape[:2]
            bx1, by1, bx2, by2 = person_box
            pad = (bx2 - bx1) * 0.05
            px1 = max(0, int((bx1 - pad) * w))
            py1 = max(0, int((by1 - pad) * h))
            px2 = min(w, int((bx2 + pad) * w))
            py2 = min(h, int((by2 + pad) * h))
            if px2 > px1 and py2 > py1:
                img = img[py1:py2, px1:px2]

        # Get face detection on the (cropped) image
        faces = face_app.detector.detect(img, input_size=(640, 640))
        if len(faces[0]) == 0:
            return None

        face_bbox = faces[0][0]
        attributes = {}

        # Hair color — sample above the face, but exclude sky-blue and wall-grey
        # pixels by requiring moderate saturation (pure background has near-zero sat).
        face_w = face_bbox[2] - face_bbox[0]
        face_h = face_bbox[3] - face_bbox[1]
        hair_y1 = max(0, int(face_bbox[1] - face_h * 0.5))
        hair_y2 = max(0, int(face_bbox[1]))
        hair_x1 = max(0, int(face_bbox[0]))
        hair_x2 = min(img.shape[1], int(face_bbox[2]))
        hair_region = img[hair_y1:hair_y2, hair_x1:hair_x2]
        if hair_region.size > 0:
            hsv_hair = cv2.cvtColor(hair_region, cv2.COLOR_BGR2HSV)
            # Keep only pixels with low saturation (natural hair colours) or red hue
            sat = hsv_hair[:, :, 1]
            val = hsv_hair[:, :, 2]
            # Exclude near-white/grey backgrounds (high val + low sat = sky/wall)
            fg_mask = ~((sat < 30) & (val > 180))
            fg_pixels = hair_region[fg_mask]
            if len(fg_pixels) > 20:
                b, g, r = np.mean(fg_pixels, axis=0)[:3]
                brightness = (r + g + b) / 3
                if brightness < 55:
                    attributes['hair_color'] = 'black'
                elif brightness < 110:
                    attributes['hair_color'] = 'brown'
                elif r > g * 1.2 and r > 120:
                    attributes['hair_color'] = 'red'
                elif brightness > 185:
                    attributes['hair_color'] = 'blonde'
                else:
                    attributes['hair_color'] = 'gray'

        # Build (from gait if available)
        pose_landmarker = get_pose()
        if pose_landmarker:
            import mediapipe as mp
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = pose_landmarker.detect(mp_image)

            if result.pose_landmarks and len(result.pose_landmarks) > 0:
                landmarks = result.pose_landmarks[0]

                # Height category
                height_cat, height_quality = extract_height_category(landmarks, img.shape)
                if height_cat:
                    attributes['height_category'] = height_cat

                # Build
                left_shoulder = landmarks[11]
                right_shoulder = landmarks[12]
                left_hip = landmarks[23]
                right_hip = landmarks[24]

                shoulder_width = abs(left_shoulder.x - right_shoulder.x)
                hip_width = abs(left_hip.x - right_hip.x)
                ratio = shoulder_width / (hip_width + 0.001)

                if ratio > 1.3:
                    attributes['build'] = 'athletic'
                elif ratio > 1.1:
                    attributes['build'] = 'average'
                elif ratio < 0.9:
                    attributes['build'] = 'heavy'
                else:
                    attributes['build'] = 'slim'

        # Facial hair — compare chin region darkness RELATIVE to mid-face to avoid
        # false positives on dark skin. A beard makes the chin darker than the cheeks;
        # the absolute level varies with skin tone.
        face_region = img[int(face_bbox[1]):int(face_bbox[3]),
                          int(face_bbox[0]):int(face_bbox[2])]
        if face_region.size > 0:
            face_height = face_bbox[3] - face_bbox[1]
            mid_region  = face_region[int(face_height * 0.35):int(face_height * 0.60), :]
            chin_region = face_region[int(face_height * 0.72):, :]
            if chin_region.size > 0 and mid_region.size > 0:
                chin_gray = cv2.cvtColor(chin_region, cv2.COLOR_BGR2GRAY)
                mid_gray  = cv2.cvtColor(mid_region, cv2.COLOR_BGR2GRAY)
                chin_val  = float(np.mean(chin_gray))
                mid_val   = float(np.mean(mid_gray))
                relative_darkness = mid_val - chin_val  # positive = chin darker than cheeks
                if relative_darkness > 25:
                    attributes['facial_hair'] = 'beard'
                elif relative_darkness > 12:
                    attributes['facial_hair'] = 'mustache'
                else:
                    attributes['facial_hair'] = 'none'

        # Overall confidence
        attributes['confidence'] = min(1.0, float(face_bbox[4]) if len(face_bbox) > 4 else 0.8)

        return attributes

    except Exception as e:
        logger.error(f'Physical attribute extraction error: {e}')
        return None

def extract_all_signals_with_quality(event):
    """
    Extract all signals from event with quality scores

    Returns: {
        'face_embedding': bytes,
        'face_quality': float,
        'gait_features': bytes,
        'gait_quality': float,
        'plate': str,
        'plate_quality': float,
        'physical_attrs': dict,
        'camera': str,
        'timestamp': datetime,
    }
    """
    label = event.get('label', '')
    event_id = event.get('id', '')
    camera = event.get('camera', '')
    is_outdoor = 'outdoor' in camera.lower()

    # Fetch snapshot
    snapshot_path = fetch_frigate_snapshot(event_id)
    if not snapshot_path:
        return None

    try:
        signals = {
            'camera': camera,
            'timestamp': datetime.utcnow(),
            'event_id': event_id,
        }

        if label == 'car' and is_outdoor:
            plate, plate_qual = extract_plate_with_quality(snapshot_path)
            if plate:
                signals['plate'] = plate
                signals['plate_quality'] = plate_qual

        if label == 'person':
            person_box = (event.get('data') or {}).get('box')

            if person_box and len(person_box) == 4:
                # Frigate can send box as [x,y,w,h] (MQTT/webhook format) or
                # [x1,y1,x2,y2] (API format). Detect [x,y,w,h] by checking if
                # x2 < x1 or y2 < y1 which is impossible in corner format.
                px, py, pw, ph = person_box
                if pw < px or ph < py:
                    # It's [x,y,w,h] — convert to [x1,y1,x2,y2]
                    person_box = [px, py, px + pw, py + ph]
                    logger.debug(f'Converted person_box [x,y,w,h]→[x1,y1,x2,y2]: {person_box}')

                # Skip edge-of-frame crops: narrower than 8% of frame width
                # produces ~20-30px face after upscale — too noisy for ArcFace.
                box_width = person_box[2] - person_box[0]
                if box_width < 0.08:
                    logger.debug(f'Skipping face extraction: person_box too narrow ({box_width:.3f} < 0.08)')
                    person_box = None  # still extract gait/attrs but skip face
            face_emb, face_qual, face_photo = extract_face_with_quality(snapshot_path, person_box)
            if face_emb:
                signals['face_embedding'] = face_emb
                signals['face_quality'] = face_qual
                signals['face_photo'] = face_photo

            gait_feat, gait_qual = extract_gait_with_quality(snapshot_path)
            if gait_feat:
                signals['gait_features'] = gait_feat
                signals['gait_quality'] = gait_qual

            # Skip second MediaPipe inference: only run physical_attributes
            # when gait did not already run pose on this same image.
            if not gait_feat:
                physical = extract_physical_attributes(snapshot_path, person_box)
                if physical:
                    signals['physical_attrs'] = physical

            # Only capture a body crop when a face was also confirmed in this snapshot.
            # Skipping when no face found avoids empty-frame body photos (person walked out).
            # person_box from Frigate already scopes to the right person even with bystanders.
            try:
                import cv2
                snap = cv2.imread(snapshot_path)
                if snap is not None and person_box and len(person_box) == 4 and face_emb:
                    sh, sw = snap.shape[:2]
                    bx1, by1, bx2, by2 = person_box
                    pad_x = (bx2 - bx1) * 0.05
                    pad_y = (by2 - by1) * 0.05
                    px1 = max(0, int((bx1 - pad_x) * sw))
                    py1 = max(0, int((by1 - pad_y) * sh))
                    px2 = min(sw, int((bx2 + pad_x) * sw))
                    py2 = min(sh, int((by2 + pad_y) * sh))
                    if px2 > px1 and py2 > py1:
                        body_crop = snap[py1:py2, px1:px2]
                        # Scale so longer edge is 400px
                        ch, cw = body_crop.shape[:2]
                        scale = 400.0 / max(ch, cw)
                        body_crop = cv2.resize(body_crop, (int(cw*scale), int(ch*scale)),
                                               interpolation=cv2.INTER_LINEAR)
                        _, jpeg_buf = cv2.imencode('.jpg', body_crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        signals['snapshot_photo'] = jpeg_buf.tobytes()
                        signals['snapshot_area'] = (bx2 - bx1) * (by2 - by1)  # for quality comparison
            except Exception as e:
                logger.debug(f'Body crop error: {e}')

        return signals

    finally:
        try:
            os.unlink(snapshot_path)
        except:
            pass

def fetch_frigate_snapshot(event_id):
    """Downloads Frigate snapshot"""
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
        logger.warning(f'Snapshot fetch error: {e}')
    return None

def fetch_frigate_clip(event_id):
    """Download event clip to temp file. Tries clip.mp4 then VOD endpoint."""
    import tempfile
    for url in [
        f'{FRIGATE_URL}/api/events/{event_id}/clip.mp4',
        f'{FRIGATE_URL}/api/vod/event/{event_id}',
    ]:
        try:
            r = requests.get(url, timeout=60, stream=True)
            if r.ok:
                content = b''.join(r.iter_content(chunk_size=65536))
                if len(content) > 1000:
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
                    tmp.write(content)
                    tmp.close()
                    return tmp.name
        except Exception as e:
            logger.debug(f'Clip fetch attempt failed ({url[:60]}): {e}')
    return None

def analyze_clip_for_best_signals(clip_path, person_box=None, n_sample=None):
    """
    Extract as many distinct-angle faces as possible from a clip.
    Samples every few frames (not just N evenly spaced) to maximise angular coverage —
    at 5fps a 10s clip = 50 frames, we sample up to 50 of them.
    Stops collecting new angles once MAX_FACE_EMBEDDINGS distinct angles found.
    """
    import cv2, tempfile

    cap = cv2.VideoCapture(clip_path)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if frame_count == 0:
        cap.release()
        return None

    # Sample densely — up to n_sample frames (default 50 for new sessions, 20 for enrichment)
    max_frames = n_sample if n_sample is not None else 50
    step = max(1, frame_count // max_frames)
    indices = list(range(0, frame_count, step))
    face_candidates = []   # (quality, embedding_bytes, photo_bytes, attrs_or_None)
    gait_candidates = []   # (quality, features_bytes) — single-frame fallback
    frames_seq      = []   # [(frame_original, timestamp_ms)] — for temporal gait
    best_body_snapshot = None
    best_body_best_face_qual = 0.0

    # Track distinct angles found so far for early exit
    distinct_seen = []

    fps = cap.get(cv2.CAP_PROP_FPS) or 5.0  # assume 5fps if not reported

    # Read the clip sequentially (not by random-seeking) so that:
    # 1. Timestamps are strictly monotonically increasing for MediaPipe VIDEO mode
    # 2. We avoid seek overhead on large clips
    frame_pos = 0
    step_set = set(indices)  # O(1) lookup

    while True:
        ret, frame = cap.read()
        if not ret or frame is None:
            break

        if frame_pos not in step_set:
            frame_pos += 1
            continue

        # Collect full-res frame + timestamp for temporal gait BEFORE downscaling
        ts_ms = int(frame_pos / fps * 1000)
        frames_seq.append((frame.copy(), ts_ms))

        frame_pos += 1

        # Early exit: if we already have MAX_FACE_EMBEDDINGS distinct angles, stop
        if len(distinct_seen) >= MAX_FACE_EMBEDDINGS:
            logger.debug(f'Clip: reached {MAX_FACE_EMBEDDINGS} distinct angles, stopping early')
            break

        # Downscale for face/gait/snapshot processing (full-res already saved in frames_seq)
        fh, fw = frame.shape[:2]
        if max(fh, fw) > 640:
            scale = 640.0 / max(fh, fw)
            frame = cv2.resize(frame, (int(fw * scale), int(fh * scale)),
                               interpolation=cv2.INTER_AREA)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        cv2.imwrite(tmp.name, frame)
        tmp.close()
        try:
            face_emb, face_qual, face_photo = extract_face_with_quality(tmp.name, None)
            if face_emb and face_qual >= FACE_QUALITY_MIN:
                new_emb = np.frombuffer(face_emb, dtype=np.float32).copy()
                n = np.linalg.norm(new_emb)
                if n > 0:
                    new_emb /= n
                is_new_angle = all(
                    float(np.dot(new_emb, prev)) < (1.0 - MIN_ANGLE_DISTANCE)
                    for prev in distinct_seen
                )
                if is_new_angle:
                    distinct_seen.append(new_emb)
                attrs = extract_physical_attributes(tmp.name)
                face_candidates.append((face_qual, face_emb, face_photo, attrs))

            # Single-frame gait (fallback if temporal gait fails)
            gait_feat, gait_qual = extract_gait_with_quality(tmp.name)
            if gait_feat and gait_qual >= GAIT_QUALITY_MIN:
                gait_candidates.append((gait_qual, gait_feat))

            # Body snapshot — only when face confirmed in same frame
            if person_box and len(person_box) == 4 and face_emb and face_qual > best_body_best_face_qual:
                h, w = frame.shape[:2]
                bx1, by1, bx2, by2 = person_box
                pad_x = (bx2 - bx1) * 0.05
                pad_y = (by2 - by1) * 0.05
                px1 = max(0, int((bx1 - pad_x) * w))
                py1 = max(0, int((by1 - pad_y) * h))
                px2 = min(w, int((bx2 + pad_x) * w))
                py2 = min(h, int((by2 + pad_y) * h))
                if px2 > px1 and py2 > py1:
                    crop = frame[py1:py2, px1:px2]
                    scale = 400.0 / max(crop.shape[:2])
                    if scale > 1.0:
                        crop = cv2.resize(crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)))
                    _, buf = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    best_body_snapshot = buf.tobytes()
                    best_body_best_face_qual = face_qual
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
        # Throttle clip inference — yield 0.2s per frame so 25 frames takes ~5s
        # instead of hammering all cores. Real-time events are unaffected.
        import time as _clip_time
        _clip_time.sleep(0.2)

    cap.release()

    if not face_candidates and not gait_candidates:
        return None

    # Select distinct-angle face embeddings from all candidates.
    # Like iPhone fingerprint enrollment: keep embeddings that are sufficiently
    # different from each other (cosine distance > 0.25) so each one covers
    # a new angle. The POS enroll/face endpoint also enforces this gate —
    # these are just the candidates we'll submit.
    face_candidates.sort(key=lambda x: x[0], reverse=True)  # best quality first

    distinct_faces = []  # [(quality, embedding_bytes, photo_bytes, attrs)]
    for cand in face_candidates:
        cand_emb = np.frombuffer(cand[1], dtype=np.float32).copy()
        n = np.linalg.norm(cand_emb)
        if n > 0:
            cand_emb /= n
        is_new = True
        for prev in distinct_faces:
            prev_emb = np.frombuffer(prev[1], dtype=np.float32).copy()
            pn = np.linalg.norm(prev_emb)
            if pn > 0:
                prev_emb /= pn
            if float(np.dot(cand_emb, prev_emb)) > (1.0 - MIN_ANGLE_DISTANCE):
                is_new = False
                break
        if is_new:
            distinct_faces.append(cand)
        if len(distinct_faces) >= MAX_FACE_EMBEDDINGS:
            break

    logger.debug(f'Clip faces: {len(face_candidates)} candidates → {len(distinct_faces)} distinct angles')

    # Build result — multiple face signals, best gait, best body snapshot
    result = {
        'camera': 'clip_analysis',
        'source': 'clip_analysis',
        'distinct_faces': distinct_faces,  # list for multi-angle enrollment
    }

    if distinct_faces:
        best_qual, best_emb, best_photo, best_attrs = distinct_faces[0]
        result['face_embedding'] = best_emb
        result['face_quality']   = float(best_qual)
        result['face_photo']     = best_photo
        if best_attrs:
            result['physical_attrs'] = best_attrs

    # Temporal gait — preferred over averaged single-frame gait
    temporal_gait_feats, temporal_gait_qual = extract_temporal_gait_from_clip(frames_seq)
    logger.debug(f'Temporal gait result: feats={temporal_gait_feats is not None} quality={temporal_gait_qual:.3f}')
    if temporal_gait_feats and temporal_gait_qual >= 0.35:
        result['gait_features'] = temporal_gait_feats
        result['gait_quality']  = temporal_gait_qual
        logger.debug(f'Clip temporal gait: quality={temporal_gait_qual:.3f} '
                     f'from {len(frames_seq)} frames')
    elif gait_candidates:
        # Fall back to averaged single-frame gait if temporal gait fails
        gait_arrays = [np.frombuffer(f, dtype=np.float32) for _, f in gait_candidates]
        avg_gait = np.mean(gait_arrays, axis=0).astype(np.float32)
        result['gait_features'] = avg_gait.tobytes()
        result['gait_quality']  = float(max(q for q, _ in gait_candidates))
        logger.debug(f'Clip gait (single-frame avg): {len(gait_candidates)} frames')

    if best_body_snapshot:
        result['snapshot_photo'] = best_body_snapshot
        result['snapshot_area']  = best_body_best_face_qual

    return result

# ─── Scoring Engine ─────────────────────────────────────────────────────────

def calculate_match_score_safe(new_signals, customer_signals, track_history=None):
    """
    Production-grade scoring with safety constraints

    Returns: (raw_score, breakdown, available_weight, passes_safety, metadata)
    """
    scores = {}
    available_weight = 0.0
    earned_score = 0.0

    biometric_score = 0.0
    biometric_weight = 0.0
    context_score = 0.0

    # === ANPR PRIOR — check plate match before face scoring =====================
    # If plate matches, we lower the effective face threshold for this candidate.
    # Floor of 0.35 — plate can never make a sub-0.35 face match succeed.
    plate_prior = False
    if new_signals.get('plate') and customer_signals.get('plates'):
        if (new_signals['plate'] in customer_signals['plates'] or
                any(fuzzy_plate_match(new_signals['plate'], p)
                    for p in customer_signals['plates'])):
            plate_prior = True
            scores['plate_prior'] = True
            logger.debug(f'Plate prior: cid={customer_signals["id"]} plate={new_signals["plate"]}')

    effective_face_threshold = (
        max(FACE_THRESHOLD - 0.10, 0.35)  # absolute floor regardless of FACE_THRESHOLD setting
        if plate_prior else FACE_THRESHOLD
    )

    # === BIOMETRIC SIGNALS ===

    # 1. FACE (with per-embedding camera boost)
    if new_signals.get('face_embedding') and customer_signals.get('face_embeddings'):
        biometric_weight += FEATURE_WEIGHTS['face']
        available_weight += FEATURE_WEIGHTS['face']

        new_face = np.frombuffer(new_signals['face_embedding'], dtype=np.float32)
        current_camera = new_signals.get('camera', '')
        best_sim = 0.0
        camera_boost_total = 0.0

        for face_entry in customer_signals['face_embeddings']:
            emb_b64    = face_entry['embedding_b64'] if isinstance(face_entry, dict) else face_entry
            stored_cam = face_entry.get('camera') if isinstance(face_entry, dict) else None
            stored = np.frombuffer(base64.b64decode(emb_b64), dtype=np.float32)
            sim = cosine_sim(new_face, stored)
            # Additive camera geometry boost: same-camera embeddings are more geometrically
            # compatible (same focal length, angle, distortion profile)
            if stored_cam and current_camera and stored_cam == current_camera:
                sim = min(1.0, sim + 0.05)
                camera_boost_total += 0.05
            best_sim = max(best_sim, sim)

        if camera_boost_total > 0:
            scores['camera_boost'] = round(camera_boost_total, 3)

        if best_sim >= effective_face_threshold:
            similarity_ratio = (best_sim - FACE_THRESHOLD) / (1.0 - FACE_THRESHOLD)
            face_score = FEATURE_WEIGHTS['face'] * similarity_ratio
            biometric_score += face_score
            earned_score += face_score
            scores['face'] = round(face_score, 2)
            scores['face_similarity'] = round(best_sim, 3)

    # 2. GAIT
    if new_signals.get('gait_features') and customer_signals.get('gait_features'):
        biometric_weight += FEATURE_WEIGHTS['gait']
        available_weight += FEATURE_WEIGHTS['gait']

        new_gait = np.frombuffer(new_signals['gait_features'], dtype=np.float32)
        best_dist = float('inf')

        for stored_gait_b64 in customer_signals['gait_features']:
            stored = np.frombuffer(base64.b64decode(stored_gait_b64), dtype=np.float32)
            # Skip if vectors have incompatible dimensions (old 6-dim vs new 12-dim)
            if stored.shape != new_gait.shape:
                continue
            dist = euclidean_dist(new_gait, stored)
            best_dist = min(best_dist, dist)

        if best_dist <= GAIT_THRESHOLD:
            similarity_ratio = 1.0 - (best_dist / GAIT_THRESHOLD)
            gait_score = FEATURE_WEIGHTS['gait'] * similarity_ratio
            biometric_score += gait_score
            earned_score += gait_score
            scores['gait'] = round(gait_score, 2)
            scores['gait_distance'] = round(best_dist, 3)

    # Calculate biometric ratio BEFORE support/context logic
    biometric_ratio = biometric_score / biometric_weight if biometric_weight > 0 else 0.0

    # Safety check: require biometric
    if biometric_weight == 0:
        return 0.0, {}, 0.0, False, {'reason': 'no_biometric'}

    # === SUPPORT SIGNALS ===

    # 3. PLATE support score (threshold reduction already handled by plate_prior above)
    if new_signals.get('plate') and customer_signals.get('plates'):
        if biometric_ratio >= 0.50:
            available_weight += FEATURE_WEIGHTS['plate']

            if new_signals['plate'] in customer_signals['plates']:
                earned_score += FEATURE_WEIGHTS['plate']
                scores['plate'] = FEATURE_WEIGHTS['plate']
            else:
                for stored_plate in customer_signals['plates']:
                    if fuzzy_plate_match(new_signals['plate'], stored_plate):
                        partial = FEATURE_WEIGHTS['plate'] * 0.7
                        earned_score += partial
                        scores['plate_fuzzy'] = round(partial, 2)
                        break

    # 4. HEIGHT CATEGORY
    if new_signals.get('physical_attrs', {}).get('height_category') and customer_signals.get('height_category'):
        available_weight += FEATURE_WEIGHTS['height_cat']

        new_cat = new_signals['physical_attrs']['height_category']
        stored_cat = customer_signals['height_category']

        if new_cat == stored_cat:
            earned_score += FEATURE_WEIGHTS['height_cat']
            scores['height_cat'] = FEATURE_WEIGHTS['height_cat']
        elif adjacent_height_category(new_cat, stored_cat):
            partial = FEATURE_WEIGHTS['height_cat'] * 0.3
            earned_score += partial
            scores['height_cat'] = round(partial, 2)

    # 5. BUILD
    if new_signals.get('physical_attrs', {}).get('build') and customer_signals.get('build'):
        available_weight += FEATURE_WEIGHTS['build']
        if new_signals['physical_attrs']['build'] == customer_signals['build']:
            earned_score += FEATURE_WEIGHTS['build']
            scores['build'] = FEATURE_WEIGHTS['build']

    # 6. HAIR COLOR
    if new_signals.get('physical_attrs', {}).get('hair_color') and customer_signals.get('hair_color'):
        available_weight += FEATURE_WEIGHTS['hair_color']

        new_color = new_signals['physical_attrs']['hair_color']
        stored_color = customer_signals['hair_color']

        if new_color == stored_color:
            earned_score += FEATURE_WEIGHTS['hair_color']
            scores['hair_color'] = FEATURE_WEIGHTS['hair_color']
        elif same_color_group(new_color, stored_color):
            partial = FEATURE_WEIGHTS['hair_color'] * 0.5
            earned_score += partial
            scores['hair_color'] = round(partial, 2)

    # 7. FACIAL HAIR
    if new_signals.get('physical_attrs', {}).get('facial_hair') and customer_signals.get('facial_hair'):
        available_weight += FEATURE_WEIGHTS['facial_hair']
        if new_signals['physical_attrs']['facial_hair'] == customer_signals['facial_hair']:
            earned_score += FEATURE_WEIGHTS['facial_hair']
            scores['facial_hair'] = FEATURE_WEIGHTS['facial_hair']

    # === CONTEXTUAL SIGNALS (only if biometric >= 60%, hard-capped at 0.25) ===
    context_score = 0.0
    if biometric_ratio >= 0.60:
        visit_count = customer_signals.get('visit_count', 0)
        hour_avg    = customer_signals.get('visit_hour_avg')
        hour_std    = float(customer_signals.get('visit_hour_std') or 99)

        if visit_count >= 5 and hour_avg is not None:
            # Weight reduced for irregular visitors (high std dev = unpredictable schedule)
            time_weight = 0.1 if hour_std > 4 else FEATURE_WEIGHTS['time_pattern']
            current_hour = datetime.now().hour
            hour_avg_f = float(hour_avg)
            # Circular hour delta (handles midnight boundary)
            hour_delta = min(abs(current_hour - hour_avg_f), 24.0 - abs(current_hour - hour_avg_f))
            time_score = max(0.0, 1.0 - hour_delta / 6.0) * time_weight
            context_score += time_score
            if time_score > 0:
                scores['time_pattern'] = round(time_score, 3)

        # Hard cap: context can never push a borderline match over the line on its own
        context_cap      = min(context_score, MAX_CONTEXT_CONTRIBUTION)
        earned_score    += context_cap
        available_weight += context_cap

    # === TRACK CONTINUITY (tie-breaker, not inflation) ===
    is_continuity_match = False
    if track_history and track_history.get('previous_customer_id') == customer_signals['id']:
        previous_confidence = track_history.get('previous_confidence', 0.0)
        if previous_confidence >= 0.70:
            is_continuity_match = True

    # === NORMALIZATION ===
    if available_weight > 0:
        normalized_score = earned_score / available_weight
    else:
        normalized_score = 0.0

    metadata = {
        'raw_score': normalized_score,
        'is_continuity_match': is_continuity_match,
        'biometric_score': biometric_score,
        'biometric_weight': biometric_weight,
        'biometric_ratio': biometric_ratio,
        'available_weight': available_weight,
        'context_score': context_score,
        'plate_prior': plate_prior,
    }

    passes_safety = (biometric_weight > 0)

    return normalized_score, scores, available_weight, passes_safety, metadata

def rank_candidates(candidates):
    """Rank using score + continuity tie-breaker"""
    def rank_key(candidate):
        customer_id, score, metadata = candidate
        is_continuity = metadata.get('is_continuity_match', False)
        return (score, is_continuity)

    return sorted(candidates, key=rank_key, reverse=True)

def get_all_customer_signals():
    """
    Returns cached signals dict, rebuilding only when the customer list changes.
    This avoids 3× N HTTP calls on every single event.
    """
    global _signals_cache, _signals_cache_ids

    with _cache_lock:
        customers = list(_customers_cache)

    current_ids = {c['id'] for c in customers}

    # Rebuild only if customer set changed
    if current_ids == _signals_cache_ids:
        return _signals_cache

    with _cache_rebuild_lock:
        # Double-checked: another thread may have rebuilt while we waited for the lock
        if current_ids == _signals_cache_ids:
            return _signals_cache
        logger.debug(f'Rebuilding signals cache for {len(customers)} customers')
        customer_signals = {}
        for customer in customers:
            cid = customer['id']
            face_embeddings = pos_get(f'/api/customers/{cid}/faces_raw') or []
            gait_features   = pos_get(f'/api/customers/{cid}/gaits_raw') or []
            attrs           = pos_get(f'/api/customers/{cid}/attributes') or {}
            if isinstance(attrs, list):
                attrs = {}
            customer_signals[cid] = {
                'id': cid,
                'face_embeddings': [
                    {'embedding_b64': f['embedding_b64'],
                     'camera': f.get('camera'),
                     'quality': f.get('quality')}
                    if isinstance(f, dict) else {'embedding_b64': f, 'camera': None, 'quality': None}
                    for f in face_embeddings
                ],
                'gait_features':   [g['features_b64']   for g in gait_features],
                'plates':          customer.get('plates', []),
                'height_category': attrs.get('height_category'),
                'build':           attrs.get('build'),
                'hair_color':      attrs.get('hair_color'),
                'facial_hair':     attrs.get('facial_hair'),
                # Temporal pattern fields (from customer dict, no extra API call)
                'visit_count':     customer.get('visit_count', 0),
                'visit_hour_avg':  customer.get('visit_hour_avg'),
                'visit_hour_std':  customer.get('visit_hour_std'),
            }

        _signals_cache     = customer_signals
        _signals_cache_ids = current_ids
    return _signals_cache

# ─── Track Identity Manager ─────────────────────────────────────────────────

class TrackIdentity:
    """Track-level identity with quality-weighted voting"""

    def __init__(self, track_id):
        self.track_id = track_id
        self.first_seen = time.time()
        self.last_seen = time.time()

        self.customer_id = None
        self.confidence = 0.0
        self.frame_observations = []
        self.customer_votes = {}  # customer_id -> [{'score': ..., 'weight': ..., 'quality': ...}]
        self.enrollment_claimed = False  # True once any thread has started enrolling this track

        self.visit_logged_at = 0.0       # epoch time of last logged visit for this track
    def add_observation(self, signals, match_results, per_customer_thresholds=None):
        """
        Add frame observation.
        per_customer_thresholds: dict of customer_id → effective link threshold (time-decayed).
        match_results: [(customer_id, score, breakdown, weight, metadata), ...]
        """
        self.last_seen = time.time()
        self.frame_observations.append({
            'signals': signals,
            'timestamp': time.time()
        })

        # Calculate frame quality from biometric qualities
        face_quality = signals.get('face_quality', 0.0)
        gait_quality = signals.get('gait_quality', 0.0)

        if face_quality > 0 and gait_quality > 0:
            frame_quality = 0.7 * face_quality + 0.3 * gait_quality
        elif face_quality > 0:
            frame_quality = face_quality
        elif gait_quality > 0:
            frame_quality = gait_quality
        else:
            frame_quality = 0.0

        # Track votes
        for customer_id, score, breakdown, weight, metadata in match_results:
            if customer_id not in self.customer_votes:
                self.customer_votes[customer_id] = []

            self.customer_votes[customer_id].append({
                'score': score,
                'weight': weight,
                'quality': frame_quality,
                'timestamp': time.time()
            })

        self._update_identity(per_customer_thresholds)

    def _update_identity(self, per_customer_thresholds=None):
        """Update identity using quality-weighted voting with per-customer time-decayed thresholds."""
        if not self.customer_votes:
            return

        # Don't update frozen tracks (oscillation prevention)
        if getattr(self, 'frozen', False):
            return

        global_thresh = _threshold_manager.global_thresholds.get('link', 0.55)
        weighted_scores = {}

        for cid, votes in self.customer_votes.items():
            total_score = sum(v['score'] * v['quality'] for v in votes)
            total_quality = sum(v['quality'] for v in votes)

            if total_quality > 0:
                weighted_scores[cid] = total_score / total_quality
            else:
                weighted_scores[cid] = np.mean([v['score'] for v in votes])

        if not weighted_scores:
            return

        best_customer = max(weighted_scores.keys(), key=lambda cid: weighted_scores[cid])
        best_score = weighted_scores[best_customer]

        # Use per-customer threshold if available — accounts for time since last visit
        if per_customer_thresholds:
            link_thresh = per_customer_thresholds.get(best_customer, global_thresh)
        else:
            link_thresh = global_thresh

        if best_score >= link_thresh:
            # Identity cooldown: freeze track after 3 flips if currently confident
            if self.customer_id and best_customer != self.customer_id:
                self.flip_count = getattr(self, 'flip_count', 0) + 1
                if self.flip_count >= 3 and self.confidence >= 0.5:
                    self.frozen = True
                    logger.warning(f'Track {self.track_id[:8]} frozen after {self.flip_count} flips '
                                   f'(confidence={self.confidence:.2f})')
                    return

            self.customer_id = best_customer
            self.confidence = best_score
        else:
            self.customer_id = None
            self.confidence = 0.0

    def has_enrollment_quality(self):
        """Check if track has sufficient quality for enrollment"""
        if len(self.frame_observations) < 3:
            return False

        qualities = []
        total_biometric_weight = 0.0

        for obs in self.frame_observations:
            signals = obs.get('signals', {})

            if signals.get('face_quality'):
                qualities.append(signals['face_quality'])
                total_biometric_weight += FEATURE_WEIGHTS['face'] * signals['face_quality']

            if signals.get('gait_quality'):
                qualities.append(signals['gait_quality'])
                total_biometric_weight += FEATURE_WEIGHTS['gait'] * signals['gait_quality']

        if not qualities:
            return False

        high_quality_count = sum(1 for q in qualities if q >= 0.5)
        medium_quality_count = sum(1 for q in qualities if q >= 0.35)

        has_sufficient_quality = (high_quality_count >= 1 or medium_quality_count >= 3)
        has_sufficient_weight = total_biometric_weight >= 3.0

        return has_sufficient_quality and has_sufficient_weight

    def get_history_context(self):
        """Get context for continuity tie-breaker"""
        if self.customer_id:
            return {
                'previous_customer_id': self.customer_id,
                'previous_confidence': self.confidence,
                'frame_count': len(self.frame_observations)
            }
        return None

    def age(self):
        return time.time() - self.first_seen

    def idle_time(self):
        return time.time() - self.last_seen

    def get_best_signal(self, signal_type):
        """Return the highest-quality observation for a given signal type."""
        best = None
        best_quality = -1.0
        for obs in self.frame_observations:
            signals = obs.get('signals', {})
            if signal_type == 'face' and signals.get('face_embedding'):
                q = signals.get('face_quality', 0.0)
                if q > best_quality:
                    best_quality = q
                    best = {
                        'embedding': signals['face_embedding'],
                        'quality': q,
                        'photo': signals.get('face_photo'),
                    }
            elif signal_type == 'gait' and signals.get('gait_features'):
                q = signals.get('gait_quality', 0.0)
                if q > best_quality:
                    best_quality = q
                    best = {'features': signals['gait_features'], 'quality': q}
        return best

    def get_evidence_summary(self):
        """Get summary for audit"""
        return {
            'frame_count': len(self.frame_observations),
            'age_seconds': self.age(),
            'customer_votes': {
                cid: {
                    'vote_count': len(votes),
                    'avg_score': np.mean([v['score'] for v in votes]),
                    'avg_quality': np.mean([v['quality'] for v in votes])
                }
                for cid, votes in self.customer_votes.items()
            }
        }

# Global track registry
_active_tracks = {}
_tracks_lock = threading.Lock()
# Limit concurrent event-processing threads to avoid fan-out at shop scale.
_event_semaphore = threading.Semaphore(3)

# ─── Daily embedding replacement cap ────────────────────────────────────────
_daily_replacements = defaultdict(int)  # customer_id → replacements today
_last_reset_date    = None

def _check_daily_reset():
    """Reset per-customer replacement counters at midnight."""
    global _last_reset_date, _daily_replacements
    from datetime import date as _date
    today = _date.today()
    if _last_reset_date != today:
        _daily_replacements.clear()
        _last_reset_date = today

# ─── Threshold Manager ──────────────────────────────────────────────────────

class ThresholdManager:
    """Manages thresholds with calibration support"""

    def __init__(self):
        self.global_thresholds = {
            'link': 0.55,      # Face-only max score at 0.45 sim ≈ 0.27; at 0.65 sim ≈ 0.46 → need lower bar
            'pending': 0.45    # Don't enroll if already matched at this level
        }
        self.segment_thresholds = {}
        self.version = "v1.0_initial"

    def get_threshold(self, threshold_type, context=None):
        """Get threshold with segment fallback"""
        # Try segment-specific
        if context:
            for segment_type in ['camera', 'quality', 'time_of_day']:
                if segment_type in context:
                    segment_key = f"{segment_type}:{context[segment_type]}"

                    if segment_key in self.segment_thresholds:
                        threshold = self.segment_thresholds[segment_key][threshold_type]
                        return threshold, segment_key

        # Fallback to global
        threshold = self.global_thresholds[threshold_type]
        return threshold, 'global'

    def get_version(self):
        return self.version

_threshold_manager = ThresholdManager()

def get_current_threshold(threshold_type, context=None):
    return _threshold_manager.get_threshold(threshold_type, context)

# ─── VisitorSession — session-centric identity accumulator ──────────────────

class VisitorSession:
    """Accumulates evidence from all sources (real-time + clips) for one visit.
    The resolver is the ONLY authority that creates persistent customer records."""

    def __init__(self):
        self.session_id      = str(uuid.uuid4())
        self.created_at      = time.time()
        self.last_evidence_at = time.time()
        # list of (embedding_bytes, quality_float, camera_str)
        self.face_embeddings  = []
        self.gait_features    = None   # (features_bytes, quality) — best seen
        self.cameras_seen     = set()
        self.source_event_ids = set()
        self.best_face_sim    = 0.0    # highest sim seen against any existing customer
        self.candidate_customer_id = None
        self.best_snapshot    = None   # (snapshot_bytes, face_quality) — for body photo
        self.best_face_photo  = None   # (face_photo_bytes, quality) — for face photo on card
        # accumulating → resolving (transitional lock) → resolved | expired
        self.status = 'accumulating'

    def add_evidence(self, face_emb=None, quality=0.0, camera=None,
                     gait=None, gait_quality=0.0, event_id=None,
                     candidate_cid=None, candidate_sim=0.0,
                     face_embeddings_list=None,
                     snapshot_photo=None, face_photo=None):
        """Add biometric evidence from a track observation or clip analysis."""
        self.last_evidence_at = time.time()
        if camera:
            self.cameras_seen.add(camera)
        if event_id:
            self.source_event_ids.add(event_id)

        # Single face embedding from real-time observation
        if face_emb and quality >= FACE_QUALITY_MIN_CREATE:
            self.face_embeddings.append((face_emb, quality, camera or ''))

        # List of distinct face embeddings from clip analysis
        if face_embeddings_list:
            for qual, emb_bytes, photo_bytes, attrs in face_embeddings_list:
                if qual >= FACE_QUALITY_MIN_CREATE:
                    self.face_embeddings.append((emb_bytes, float(qual), camera or ''))

        # Best gait wins
        if gait and gait_quality > 0:
            if self.gait_features is None or gait_quality > self.gait_features[1]:
                self.gait_features = (gait, gait_quality)

        # Update best candidate hint from real-time voting
        if candidate_cid and candidate_sim > self.best_face_sim:
            self.best_face_sim = candidate_sim
            self.candidate_customer_id = candidate_cid

        # Keep best body snapshot (highest associated face quality)
        if snapshot_photo and quality > (self.best_snapshot[1] if self.best_snapshot else -1):
            self.best_snapshot = (snapshot_photo, quality)

        # Keep best face photo (highest quality)
        if face_photo and quality > (self.best_face_photo[1] if self.best_face_photo else -1):
            self.best_face_photo = (face_photo, quality)

    @property
    def best_face_embedding(self):
        """Return highest-quality stored face embedding bytes, or None."""
        if not self.face_embeddings:
            return None
        return max(self.face_embeddings, key=lambda e: e[1])[0]

    @property
    def high_quality_face_count(self):
        return sum(1 for _, q, _ in self.face_embeddings if q >= FACE_QUALITY_MIN_CREATE)

    def duration(self):
        return self.last_evidence_at - self.created_at


# Session registry
_active_sessions    = {}   # session_id → VisitorSession
_sessions_lock      = threading.Lock()

# Anonymous identities — evidence that didn't meet creation threshold, held 24h
_anonymous_identities = {}
# anon_id → {face_embeddings, gait, cameras, created_at, last_seen_at}

# Idempotency guard — session_ids that already triggered a customer creation
_created_from_session_ids = set()

# Recently-created customer embeddings cache — for anti-clone gate (last 10 min)
_recent_customers_cache   = []   # list of {cid, embeddings, created_at}
_recent_customers_lock    = threading.Lock()


def _register_recent_customer(cid, embedding_bytes_list):
    """Called after customer creation so anti-clone gate can find it."""
    with _recent_customers_lock:
        _recent_customers_cache.append({
            'cid': cid,
            'embeddings': embedding_bytes_list,
            'created_at': time.time(),
        })
        # Keep only last 10 minutes
        cutoff = time.time() - 600
        _recent_customers_cache[:] = [c for c in _recent_customers_cache if c['created_at'] > cutoff]


def _get_recent_customer_embeddings(minutes=10):
    """Return [(cid, [emb_bytes, ...]), ...] for customers created in last N minutes."""
    cutoff = time.time() - minutes * 60
    with _recent_customers_lock:
        return [(c['cid'], c['embeddings']) for c in _recent_customers_cache if c['created_at'] > cutoff]


# ─── Profile Improvement ────────────────────────────────────────────────────

# Quality thresholds for deciding whether to upgrade a stored signal
FACE_UPGRADE_MIN   = 0.35   # only upgrade face if new quality is at least this
GAIT_UPGRADE_MIN   = 0.50

VISIT_LOG_INTERVAL   = 300  # min seconds between visit logs for the same track
TRACK_IDLE_EXPIRY    = 300  # remove tracks idle longer than this (seconds)
PROFILE_UPGRADE_INTERVAL = 300  # min seconds between photo/body upgrades per customer

# Per-customer timestamp of last photo/body upgrade — prevents upgrade on every poll
_profile_upgrade_times = {}   # customer_id -> float (epoch time)
_profile_upgrade_lock  = threading.Lock()

def _improve_customer_profile(customer_id, signals):
    """
    Called every time a known customer is seen.
    Fills in missing biometric data and upgrades to higher-quality observations.
    """
    try:
        new_face_quality = float(signals.get('face_quality', 0.0))
        has_face_embedding = bool(signals.get('face_embedding'))
        has_face_photo = bool(signals.get('face_photo'))
        has_snapshot = bool(signals.get('snapshot_photo'))

        # Fast exit: skip all network calls when there's nothing useful to update
        if (not has_face_embedding and not has_snapshot
                and not signals.get('gait_features')
                and not signals.get('physical_attrs')):
            return

        # --- Face embedding: add if missing, upgrade if meaningfully better ---
        if has_face_embedding and new_face_quality >= FACE_UPGRADE_MIN:
            # Use cached embeddings to decide whether to enroll — avoids a GET per event.
            # Fall back to API only if this customer isn't in the cache yet.
            cached = _signals_cache.get(customer_id, {})
            cached_faces = cached.get('face_embeddings', None)
            if cached_faces is None:
                existing_faces = pos_get(f'/api/customers/{customer_id}/faces_raw') or []
                cached_faces = [f['embedding_b64'] for f in existing_faces] if isinstance(existing_faces, list) else []
            enrolled_new_angle = False
            upgraded_photo_only = False
            cam_src = signals.get('camera')
            if len(cached_faces) == 0:
                # No active face embedding — add one regardless of quality
                logger.info(f'Profile [{customer_id}]: adding face embedding (quality={new_face_quality:.2f})')
                payload = {
                    'embedding_b64': base64.b64encode(signals['face_embedding']).decode(),
                    'quality': new_face_quality,
                    'camera_source': cam_src,
                }
                face_photo = signals.get('face_photo')
                if has_face_photo and face_photo and len(face_photo) >= 4000:
                    payload['photo_b64'] = base64.b64encode(face_photo).decode()
                if has_snapshot:
                    payload['body_photo_b64'] = base64.b64encode(signals['snapshot_photo']).decode()
                pos_post(f'/api/customers/{customer_id}/enroll/face', payload)
                enrolled_new_angle = True

            elif new_face_quality >= 0.60 and has_face_photo:
                # Good quality face photo — upgrade if better than what's stored.
                # Rate-limited to once per PROFILE_UPGRADE_INTERVAL per customer so
                # a customer standing in frame for 5+ min doesn't spam upgrades.
                # This is a photo-only update; it does NOT add a new embedding so
                # we must NOT invalidate the signals cache (would cause rebuild loop).
                with _profile_upgrade_lock:
                    last_upgrade = _profile_upgrade_times.get(customer_id, 0.0)
                    due_upgrade = (time.time() - last_upgrade) >= PROFILE_UPGRADE_INTERVAL
                    if due_upgrade:
                        _profile_upgrade_times[customer_id] = time.time()
                if due_upgrade:
                    _fp = signals.get('face_photo', b'')
                    if _fp and len(_fp) >= 4000:
                        logger.info(f'Profile [{customer_id}]: upgrading face photo (quality={new_face_quality:.2f})')
                        payload = {
                            'embedding_b64': base64.b64encode(signals['face_embedding']).decode(),
                            'quality': new_face_quality,
                            'photo_b64': base64.b64encode(_fp).decode(),
                            'camera_source': cam_src,
                        }
                        if has_snapshot:
                            payload['body_photo_b64'] = base64.b64encode(signals['snapshot_photo']).decode()
                        pos_post(f'/api/customers/{customer_id}/enroll/face', payload)
                        upgraded_photo_only = True

            # Only invalidate signals cache when a genuinely new angle embedding was stored.
            # Photo-only upgrades don't change embeddings so no cache invalidation needed.
            if enrolled_new_angle:
                _signals_cache_ids.clear()

        # --- Body snapshot: update at most once per PROFILE_UPGRADE_INTERVAL ---
        if has_snapshot:
            with _profile_upgrade_lock:
                last_upgrade = _profile_upgrade_times.get(customer_id, 0.0)
                due_snap = (time.time() - last_upgrade) >= PROFILE_UPGRADE_INTERVAL
                if due_snap:
                    _profile_upgrade_times[customer_id] = time.time()
            if due_snap:
                new_area = float(signals.get('snapshot_area', 0.0))
                pos_post(f'/api/customers/{customer_id}/enroll/face', {
                    'embedding_b64': base64.b64encode(bytes(512 * 4)).decode(),
                    'quality': 0.0,
                    'body_photo_b64': base64.b64encode(signals['snapshot_photo']).decode(),
                    'snapshot_area': new_area,
                    'snapshot_only': True,
                })

        # --- Gait: add if missing (use cache to avoid a GET call) ---
        new_gait_quality = float(signals.get('gait_quality', 0.0))
        if signals.get('gait_features') and new_gait_quality >= GAIT_UPGRADE_MIN:
            cached = _signals_cache.get(customer_id, {})
            existing_gaits = cached.get('gait_features', None)
            if existing_gaits is None:
                existing_gaits = pos_get(f'/api/customers/{customer_id}/gaits_raw') or []
            if not existing_gaits:
                logger.info(f'Profile [{customer_id}]: adding gait (quality={new_gait_quality:.2f})')
                pos_post(f'/api/customers/{customer_id}/enroll/gait', {
                    'features_b64': base64.b64encode(signals['gait_features']).decode(),
                    'quality': new_gait_quality,
                })

        # --- Physical attributes: fill in missing fields, upgrade on higher confidence ---
        if signals.get('physical_attrs'):
            attrs = signals['physical_attrs']
            new_conf = float(attrs.get('confidence', 0.0))
            # Only fetch existing attrs when the new observation might be worth writing.
            # This avoids a GET call on every low-quality observation.
            if new_conf >= 0.3:
                existing = pos_get(f'/api/customers/{customer_id}/attributes')
                if isinstance(existing, list):
                    existing = None
                old_conf = float((existing or {}).get('confidence') or 0.0)
                missing_fields = existing is None or not existing.get('hair_color') or not existing.get('build')
            else:
                existing = None
                old_conf = 1.0  # treat low-conf as not worth writing
                missing_fields = False
            if missing_fields or new_conf > old_conf:
                logger.info(f'Profile [{customer_id}]: updating physical attributes (conf={new_conf:.2f})')
                pos_post(f'/api/customers/{customer_id}/attributes', {
                    'hair_color':      attrs.get('hair_color'),
                    'build':           attrs.get('build'),
                    'facial_hair':     attrs.get('facial_hair'),
                    'height_category': attrs.get('height_category'),
                    'height_cm':       attrs.get('height_cm'),
                    'skin_tone':       attrs.get('skin_tone'),
                    'eye_color':       attrs.get('eye_color'),
                    'age_range':       attrs.get('age_range'),
                    'gender':          attrs.get('gender'),
                    'wearing_glasses': attrs.get('wearing_glasses'),
                    'confidence':      new_conf,
                    'camera_source':   signals.get('camera'),
                })

    except Exception as e:
        logger.warning(f'Profile improvement error for customer {customer_id}: {e}')

# ─── Session Management ─────────────────────────────────────────────────────

def _assign_to_session(face_emb, camera, event_id, ts):
    """Assign new evidence to an existing session (similarity-gated) or create one.
    Returns the session_id to use."""
    with _sessions_lock:
        best_session, best_sim = None, -1.0

        for sid, sess in list(_active_sessions.items()):
            if sess.status != 'accumulating':
                continue
            time_gap = ts - sess.last_evidence_at

            # Face similarity gate — primary assignment criterion
            # Both face_emb and stored embeddings are raw bytes; decode before cosine_sim
            if face_emb and sess.face_embeddings:
                face_arr = np.frombuffer(face_emb, dtype=np.float32)
                sim = max(
                    cosine_sim(face_arr, np.frombuffer(e[0], dtype=np.float32))
                    for e in sess.face_embeddings
                )
                if sim > SESSION_JOIN_FACE_SIM and sim > best_sim:
                    best_sim, best_session = sim, sid

            # No-face same-camera fallback — only if exactly ONE eligible recent session
            elif (not face_emb and camera and camera in sess.cameras_seen and time_gap < 30):
                eligible = [s for s in _active_sessions.values()
                            if s.status == 'accumulating'
                            and camera in s.cameras_seen
                            and ts - s.last_evidence_at < 30]
                if len(eligible) == 1 and best_session is None:
                    best_session = eligible[0].session_id

        if best_session:
            _active_sessions[best_session].last_evidence_at = ts
            return best_session

        # No match — start a new session
        sess = VisitorSession()
        _active_sessions[sess.session_id] = sess
        logger.debug(f'New session {sess.session_id[:8]} (camera={camera})')
        return sess.session_id


def _merge_overlapping_sessions():
    """Merge sessions with very similar face embeddings into one.
    Runs before the resolver so it always sees consolidated evidence.
    Uses SESSION_MERGE_FACE_SIM (0.50) — stricter than join threshold."""
    with _sessions_lock:
        open_sessions = [s for s in _active_sessions.values() if s.status == 'accumulating']

    for i, a in enumerate(open_sessions):
        for b in open_sessions[i+1:]:
            if not (a.face_embeddings and b.face_embeddings):
                continue
            sim = max(
                cosine_sim(np.frombuffer(e1[0], dtype=np.float32),
                           np.frombuffer(e2[0], dtype=np.float32))
                for e1 in a.face_embeddings
                for e2 in b.face_embeddings
            )
            if sim >= SESSION_MERGE_FACE_SIM:
                with _sessions_lock:
                    if a.status != 'accumulating' or b.status != 'accumulating':
                        continue
                    # Merge B into A — keep best-quality embeddings from both
                    a.face_embeddings.extend(b.face_embeddings)
                    a.cameras_seen.update(b.cameras_seen)
                    a.source_event_ids.update(b.source_event_ids)
                    if b.gait_features and (not a.gait_features or
                                            b.gait_features[1] > a.gait_features[1]):
                        a.gait_features = b.gait_features
                    if b.best_face_sim > a.best_face_sim:
                        a.best_face_sim = b.best_face_sim
                        a.candidate_customer_id = b.candidate_customer_id
                    a.last_evidence_at = max(a.last_evidence_at, b.last_evidence_at)
                    # Merge snapshot/face photo — keep best quality
                    if b.best_snapshot and (not a.best_snapshot or b.best_snapshot[1] > a.best_snapshot[1]):
                        a.best_snapshot = b.best_snapshot
                    if b.best_face_photo and (not a.best_face_photo or b.best_face_photo[1] > a.best_face_photo[1]):
                        a.best_face_photo = b.best_face_photo
                    b.status = 'expired'
                logger.debug(f'Sessions merged: {b.session_id[:8]} → {a.session_id[:8]} '
                             f'(sim={sim:.3f})')


def _create_customer_from_embeddings(face_embeddings, gait_features, session_id,
                                     snapshot_photo=None, face_photo=None):
    """Create a new customer in POS, enroll all face embeddings and gait.
    Returns customer_id or None on failure."""
    if session_id in _created_from_session_ids:
        logger.debug(f'Session {session_id[:8]} already created a customer, skipping')
        return None
    _created_from_session_ids.add(session_id)

    r = pos_get('/api/customers/max_number')
    next_number = (r.get('max_number') or 0) + 1 if r else 1
    customer_number_str = f'CUST-{next_number:04d}'

    new_customer = pos_post('/api/customers', {
        'name': None,
        'auto_enrolled': True,
        'customer_number': customer_number_str,
    })
    if not (new_customer and new_customer.get('id')):
        logger.error(f'Session {session_id[:8]}: customer creation failed')
        _created_from_session_ids.discard(session_id)
        return None

    cid = new_customer['id']
    logger.info(f'Session {session_id[:8]} → created {customer_number_str} (id={cid})')

    # Enroll face embeddings
    for emb_bytes, quality, camera_src in face_embeddings:
        pos_post(f'/api/customers/{cid}/enroll/face', {
            'embedding_b64': base64.b64encode(emb_bytes).decode(),
            'quality': float(quality),
            'camera_source': camera_src or None,
        })

    # Enroll gait if available
    if gait_features:
        gait_bytes, gait_qual = gait_features
        pos_post(f'/api/customers/{cid}/enroll/gait', {
            'features_b64': base64.b64encode(gait_bytes).decode(),
            'quality': float(gait_qual),
        })

    # Enroll body snapshot so the customer card has a visual
    if snapshot_photo:
        snap_payload = {
            'embedding_b64': base64.b64encode(bytes(512 * 4)).decode(),
            'quality': 0.0,
            'body_photo_b64': base64.b64encode(snapshot_photo).decode(),
            'snapshot_only': True,
        }
        if face_photo:
            snap_payload['photo_b64'] = base64.b64encode(face_photo).decode()
        pos_post(f'/api/customers/{cid}/enroll/face', snap_payload)
        logger.debug(f'Session {session_id[:8]} → body snapshot stored for cid={cid}')

    # Cache for anti-clone gate
    emb_b64_list = [base64.b64encode(e[0]).decode() for e in face_embeddings]
    _register_recent_customer(cid, emb_b64_list)

    # Insert into recognition caches
    new_entry = {'id': cid, 'auto_enrolled': True, 'customer_number': customer_number_str,
                 'name': None, 'plates': [], 'visit_count': 0, 'visit_hour_avg': None,
                 'visit_hour_std': None, 'last_visit': None}
    with _cache_lock:
        _customers_cache.append(new_entry)
        _customers_cache_map[cid] = new_entry
    global _threshold_cache_time
    _threshold_cache_time = 0.0
    with _cache_rebuild_lock:
        _signals_cache[cid] = {
            'id': cid, 'face_embeddings': [], 'gait_features': [], 'plates': [],
            'height_category': None, 'build': None, 'hair_color': None, 'facial_hair': None,
            'visit_count': 0, 'visit_hour_avg': None, 'visit_hour_std': None,
        }
        _signals_cache_ids.add(cid)

    return cid


def _log_session_visit(cid, session, dwell_seconds=None):
    """Log a visit for a resolved session."""
    payload = {
        'customer_id': cid,
        'matched_signals': 'session_resolved',
        'confidence_scores': {
            'session_face_sim': float(session.best_face_sim),
            'session_cameras': len(session.cameras_seen),
            'session_faces': len(session.face_embeddings),
        },
        'camera_source': (list(session.cameras_seen) or [None])[0],
    }
    if dwell_seconds:
        payload['dwell_seconds'] = int(dwell_seconds)
    pos_post('/api/customers/identify', payload)


def _resolve_session(session):
    """Single authority for customer creation. 5-step decision.
    Sets session.status to 'resolved' or 'expired'."""

    # Atomic: prevent double-resolution races
    with _sessions_lock:
        if session.status != 'accumulating':
            return
        session.status = 'resolving'

    try:
        best_emb = session.best_face_embedding
        all_cust_sigs = get_all_customer_signals()

        # Re-score all customers against all session embeddings for best match
        if best_emb and all_cust_sigs:
            for cid, csigs in all_cust_sigs.items():
                if not csigs.get('face_embeddings'):
                    continue
                for face_entry in csigs['face_embeddings']:
                    emb_b64 = face_entry['embedding_b64'] if isinstance(face_entry, dict) else face_entry
                    stored = np.frombuffer(base64.b64decode(emb_b64), dtype=np.float32)
                    for sess_emb, _, _ in session.face_embeddings:
                        sim = float(cosine_sim(
                            np.frombuffer(sess_emb, dtype=np.float32), stored))
                        if sim > session.best_face_sim:
                            session.best_face_sim = sim
                            session.candidate_customer_id = cid

        # ── Step 1: Link to existing customer ────────────────────────────────
        if (session.best_face_sim >= RESOLVER_LINK_THRESHOLD
                and session.candidate_customer_id):
            linked_cid = session.candidate_customer_id
            _log_session_visit(linked_cid, session, dwell_seconds=session.duration())
            session.status = 'resolved'
            logger.info(f'Resolver: session {session.session_id[:8]} → '
                        f'linked cid={linked_cid} '
                        f'face_sim={session.best_face_sim:.3f}')
            # Re-queue session source events for clip enrichment to build more angles
            with _clip_queue_lock:
                for eid in session.source_event_ids:
                    if (len(_clip_analysis_queue) < MAX_CLIP_QUEUE
                            and not any(j[0] == eid for j in _clip_analysis_queue)):
                        _clip_analysis_queue.append((eid, linked_cid, None))
                        logger.debug(f'Re-queued clip {eid[:12]} for enrichment of linked cid={linked_cid}')
            return

        # ── Step 2: Anti-clone — recent customer suppression ─────────────────
        if best_emb:
            best_emb_arr = np.frombuffer(best_emb, dtype=np.float32)
            for recent_cid, recent_emb_list in _get_recent_customer_embeddings(minutes=10):
                for emb_b64 in recent_emb_list:
                    try:
                        stored = np.frombuffer(base64.b64decode(emb_b64), dtype=np.float32)
                        sim = float(cosine_sim(best_emb_arr, stored))
                        if sim > RECENT_CUSTOMER_SIM:
                            _log_session_visit(recent_cid, session)
                            session.status = 'resolved'
                            logger.info(f'Resolver: session {session.session_id[:8]} → '
                                        f'anti-clone cid={recent_cid} sim={sim:.3f}')
                            return
                    except Exception:
                        pass

        # ── Step 3: Anonymous identity matching ───────────────────────────────
        if best_emb:
            best_emb_arr = np.frombuffer(best_emb, dtype=np.float32)
            best_anon_id, best_anon_sim = None, -1.0
            now = time.time()
            for anon_id, anon in list(_anonymous_identities.items()):
                if now - anon.get('last_seen_at', anon['created_at']) > ANON_IDENTITY_TTL:
                    continue
                for emb_bytes, _, _ in anon.get('face_embeddings', []):
                    try:
                        stored = np.frombuffer(emb_bytes, dtype=np.float32)
                        sim = float(cosine_sim(best_emb_arr, stored))
                        if sim > ANON_IDENTITY_SIM and sim > best_anon_sim:
                            best_anon_sim, best_anon_id = sim, anon_id
                    except Exception:
                        pass

            if best_anon_id:
                anon = _anonymous_identities[best_anon_id]
                # Merge session evidence into anonymous identity
                anon['face_embeddings'].extend(session.face_embeddings)
                if session.gait_features:
                    if not anon.get('gait') or session.gait_features[1] > anon['gait'][1]:
                        anon['gait'] = session.gait_features
                anon['cameras'].update(session.cameras_seen)
                anon['last_seen_at'] = now

                # Re-evaluate combined evidence against creation gates
                high_q = [e for e in anon['face_embeddings'] if e[1] >= FACE_QUALITY_MIN_CREATE]
                if (len(anon['face_embeddings']) >= MIN_FACES_TO_CREATE
                        and len(high_q) >= MIN_HIGH_QUALITY_FACES
                        and session.best_face_sim < CLEARLY_NOT_EXISTING):
                    snap = anon.get('best_snapshot') or (session.best_snapshot[0] if session.best_snapshot else None)
                    fp   = anon.get('best_face_photo') or (session.best_face_photo[0] if session.best_face_photo else None)
                    cid = _create_customer_from_embeddings(
                        anon['face_embeddings'], anon.get('gait'), session.session_id,
                        snapshot_photo=snap, face_photo=fp)
                    if cid:
                        _log_session_visit(cid, session)
                        del _anonymous_identities[best_anon_id]
                        session.status = 'resolved'
                        logger.info(f'Resolver: session {session.session_id[:8]} → '
                                    f'anon {best_anon_id[:8]} promoted to cid={cid}')
                        with _clip_queue_lock:
                            for eid in session.source_event_ids:
                                if (len(_clip_analysis_queue) < MAX_CLIP_QUEUE
                                        and not any(j[0] == eid for j in _clip_analysis_queue)):
                                    _clip_analysis_queue.append((eid, cid, None))
                                    logger.debug(f'Re-queued clip {eid[:12]} for enrichment of promoted cid={cid}')
                        return
                # Not yet promotable — keep anon, expire this session
                session.status = 'expired'
                logger.debug(f'Resolver: session {session.session_id[:8]} → '
                             f'merged into anon {best_anon_id[:8]}, not yet promotable')
                return

        # ── Step 4: Safe to create new customer? ──────────────────────────────
        high_q = [e for e in session.face_embeddings if e[1] >= FACE_QUALITY_MIN_CREATE]
        safe_to_create = (
            len(session.face_embeddings) >= MIN_FACES_TO_CREATE
            and len(high_q) >= MIN_HIGH_QUALITY_FACES
            and session.duration() >= MIN_SESSION_DURATION
            and session.best_face_sim < CLEARLY_NOT_EXISTING
        )
        if safe_to_create:
            snap = session.best_snapshot[0] if session.best_snapshot else None
            fp   = session.best_face_photo[0] if session.best_face_photo else None
            cid = _create_customer_from_embeddings(
                session.face_embeddings, session.gait_features, session.session_id,
                snapshot_photo=snap, face_photo=fp)
            if cid:
                _log_session_visit(cid, session, dwell_seconds=session.duration())
                session.status = 'resolved'
                logger.info(f'Resolver: session {session.session_id[:8]} → '
                            f'new customer cid={cid} '
                            f'(faces={len(session.face_embeddings)} '
                            f'hq={len(high_q)} dur={session.duration():.0f}s)')
                # Re-queue session's source events for clip enrichment now that customer exists.
                # The clip loop will match them to this new customer and enroll more angles.
                with _clip_queue_lock:
                    for eid in session.source_event_ids:
                        if (len(_clip_analysis_queue) < MAX_CLIP_QUEUE
                                and not any(j[0] == eid for j in _clip_analysis_queue)):
                            _clip_analysis_queue.append((eid, cid, None))
                            logger.debug(f'Re-queued clip {eid[:12]} for enrichment of new cid={cid}')
                return

        # ── Step 5: Insufficient evidence ─────────────────────────────────────
        if session.face_embeddings:
            anon_id = str(uuid.uuid4())
            _anonymous_identities[anon_id] = {
                'face_embeddings': list(session.face_embeddings),
                'gait': session.gait_features,
                'cameras': set(session.cameras_seen),
                'created_at': session.created_at,
                'last_seen_at': time.time(),
            }
            logger.debug(f'Resolver: session {session.session_id[:8]} → '
                         f'anonymous identity {anon_id[:8]} '
                         f'(faces={len(session.face_embeddings)} '
                         f'best_sim={session.best_face_sim:.3f})')
        else:
            logger.debug(f'Resolver: session {session.session_id[:8]} → discarded (no face)')
        session.status = 'expired'

    except Exception as e:
        logger.error(f'Resolver error for session {session.session_id[:8]}: {e}')
        import traceback; traceback.print_exc()
        session.status = 'expired'


# ─── Event Processing ───────────────────────────────────────────────────────

def process_event(event):
    """Process Frigate event with track-based identification"""
    try:
        # Extract signals
        signals = extract_all_signals_with_quality(event)
        if not signals:
            return
        # event_id is Frigate's stable identifier for one person detection session.
        # Using it as track_id directly supports multiple simultaneous people per camera.
        # multiple Frigate event IDs for the same physical person in frame.
        camera = signals.get('camera', 'unknown')
        event_id = event.get('id', str(uuid.uuid4()))

        # Each Frigate event_id is stable for the lifetime of one person detection.
        # Use it directly as the track key — one track per person, no camera-level
        # single-slot that would overwrite a second person entering the same camera.
        with _tracks_lock:
            if event_id not in _active_tracks:
                _active_tracks[event_id] = TrackIdentity(event_id)
            track = _active_tracks[event_id]
            track_id = event_id

        # Get all customer signals
        all_customer_signals = get_all_customer_signals()

        # Match against all customers
        match_results = []
        for customer_id, customer_signals in all_customer_signals.items():
            score, breakdown, weight, safe, metadata = calculate_match_score_safe(
                signals,
                customer_signals,
                track_history=track.get_history_context()
            )

            if safe:
                match_results.append((customer_id, score, breakdown, weight, metadata))

        # Compute per-customer time-decayed thresholds (cached, 5-min TTL)
        context = {
            'camera': signals.get('camera'),
            'quality': 'high' if max(signals.get('face_quality', 0), signals.get('gait_quality', 0)) >= 0.8 else 'medium',
            'time_of_day': 'morning' if 6 <= datetime.now().hour < 12 else 'afternoon'
        }
        link_threshold, link_source = get_current_threshold('link', context)
        per_customer_thresholds = _compute_per_customer_thresholds(link_threshold)

        # Update track with per-customer thresholds so _update_identity uses correct floor per candidate
        track.add_observation(signals, match_results, per_customer_thresholds)

        # link_threshold already computed above; re-use it
        pending_threshold, pending_source = get_current_threshold('pending', context)

        # Decision logic
        if track.customer_id and track.confidence >= link_threshold:
            # Resolve merged_into chain: the tracked customer may have been merged
            # after the track was created. Follow to the active primary so visits
            # and profile improvements land on the right customer.
            resolved_id = track.customer_id
            resolved = pos_get(f'/api/customers/{resolved_id}')
            if isinstance(resolved, dict) and not resolved.get('active', True) and resolved.get('merged_into'):
                primary_id = resolved['merged_into']
                logger.info(f'Track {track_id[:8]} resolved merged customer {resolved_id} → primary {primary_id}')
                resolved_id = primary_id
                track.customer_id = primary_id

            # Collect best face_sim and context_score for safety checks + logging
            best_face_sim = 0.0
            best_scores_breakdown = {}
            best_match_meta = {}
            for cid_m, score_m, breakdown_m, _, meta_m in match_results:
                if cid_m == resolved_id:
                    best_face_sim = float(breakdown_m.get('face_similarity', 0.0))
                    best_scores_breakdown = breakdown_m
                    best_match_meta = meta_m
                    break

            plate_prior = best_match_meta.get('plate_prior', False)
            context_score_val = best_match_meta.get('context_score', 0.0)

            # Safety checks when relaxed threshold was used (long-absent customer)
            eff_threshold = per_customer_thresholds.get(resolved_id, link_threshold)
            if eff_threshold < link_threshold:
                if best_face_sim < 0.30:
                    logger.warning(f'Track {track_id[:8]} face_sim_floor_failed sim={best_face_sim:.3f} '
                                   f'threshold={eff_threshold:.3f}')
                    return
                # Weak face + context without strong corroborating signal → reject
                if best_face_sim < 0.40 and context_score_val > 0:
                    has_strong = (plate_prior or
                                  float(best_scores_breakdown.get('gait', 0)) >= 0.2)
                    if not has_strong:
                        logger.debug(f'Track {track_id[:8]} weak_face+context_no_strong_signal '
                                     f'sim={best_face_sim:.3f}')
                        return

            # Structured confidence explanation log — primary diagnostic tool for tuning
            cam_boost = float(best_scores_breakdown.get('camera_boost', 0.0))
            logger.info(
                f'Track {track_id[:8]} → cid={resolved_id} | '
                f'face_sim={best_face_sim:.3f} threshold={eff_threshold:.3f} '
                f'plate_prior={plate_prior} '
                f'time_pattern={float(best_scores_breakdown.get("time_pattern", 0.0)):.3f} '
                f'camera_boost={cam_boost:.3f} '
                f'final_score={track.confidence:.3f}'
            )

            # Build confidence scores — all values must be plain Python float, not
            # numpy.float32, or json.dumps will raise "not JSON serializable".
            conf_scores = {'track_confidence': float(track.confidence)}
            if signals.get('face_quality'):
                conf_scores['face'] = float(signals['face_quality'])
            if signals.get('gait_quality'):
                conf_scores['gait'] = float(signals['gait_quality'])
            for cid_m, score_m, breakdown_m, _, _ in match_results:
                if cid_m == resolved_id:
                    if 'face_similarity' in breakdown_m:
                        conf_scores['face_similarity'] = float(breakdown_m['face_similarity'])
                    if 'gait_distance' in breakdown_m:
                        conf_scores['gait_distance'] = float(breakdown_m['gait_distance'])
                    break

            # Real-time: notify teller only when STABLE + FACE-DOMINANT match.
            # Advisory only — does not create customers.
            # Requires MIN_STRONG_MATCH_OBS consecutive obs at face_sim ≥ STRONG_LINK_THRESHOLD
            # AND face_sim ≥ MIN_FACE_FOR_WELCOME (gait/context alone cannot trigger welcome).
            face_sim_this_obs = best_face_sim
            face_present_for_welcome = (
                signals.get('face_quality', 0) > 0
                and face_sim_this_obs >= MIN_FACE_FOR_WELCOME
            )
            if face_sim_this_obs >= STRONG_LINK_THRESHOLD and face_present_for_welcome:
                track._strong_obs = getattr(track, '_strong_obs', 0) + 1
            else:
                track._strong_obs = 0

            if getattr(track, '_strong_obs', 0) >= MIN_STRONG_MATCH_OBS:
                # Stable confirmed match — log visit (teller notification) rate-limited
                if time.time() - track.visit_logged_at >= VISIT_LOG_INTERVAL:
                    identify_payload = {
                        'customer_id': resolved_id,
                        'matched_signals': 'track_consensus',
                        'confidence_scores': conf_scores,
                        'camera_source': signals.get('camera'),
                    }
                    start_time = event.get('start_time')
                    end_time_ev = event.get('end_time')
                    if event.get('_is_ended') and start_time and end_time_ev:
                        identify_payload['dwell_seconds'] = int(end_time_ev - start_time)
                    pos_post('/api/customers/identify', identify_payload)
                    track.visit_logged_at = time.time()

                # Continuous profile improvement — fill in missing or upgrade quality
                _improve_customer_profile(resolved_id, signals)
            else:
                logger.debug(f'Track {track_id[:8]} waiting for stable match '
                             f'(strong_obs={getattr(track, "_strong_obs", 0)}/{MIN_STRONG_MATCH_OBS} '
                             f'face_sim={face_sim_this_obs:.3f})')

        else:
            logger.debug(f'Track {track_id[:8]} pending '
                         f'(age={track.age():.1f}s confidence={track.confidence:.3f})')

        # ── Feed evidence into VisitorSession (all tracks, regardless of link status) ──
        # The session resolver is the sole authority for customer creation.
        # Use FACE_QUALITY_MIN (global) not FACE_QUALITY_MIN_CREATE — sessions accumulate
        # all usable evidence; the creation gate is applied only at resolve time.
        _face_qual_now = float(signals.get('face_quality', 0))
        face_emb_for_session = signals.get('face_embedding') if _face_qual_now >= FACE_QUALITY_MIN else None
        session_id = _assign_to_session(face_emb_for_session, camera, event_id, time.time())
        sess = _active_sessions.get(session_id)
        if sess:
            # Pass candidate hint only if track has a confirmed linked identity
            _candidate_cid = track.customer_id if (track.customer_id and track.confidence >= RESOLVER_LINK_THRESHOLD) else None
            _candidate_sim = best_face_sim if track.customer_id else 0.0  # best_face_sim only set when linked
            sess.add_evidence(
                face_emb=face_emb_for_session,
                quality=_face_qual_now,
                camera=camera,
                gait=signals.get('gait_features'),
                gait_quality=float(signals.get('gait_quality', 0)),
                event_id=event_id,
                candidate_cid=_candidate_cid,
                candidate_sim=_candidate_sim,
                snapshot_photo=signals.get('snapshot_photo'),
                face_photo=signals.get('face_photo'),
            )

        # Queue clip analysis for ended events (enrichment path)
        if event.get('_is_ended'):
            person_box = (event.get('data') or {}).get('box')
            with _clip_queue_lock:
                if (len(_clip_analysis_queue) < MAX_CLIP_QUEUE
                        and not any(j[0] == event_id for j in _clip_analysis_queue)):
                    _clip_analysis_queue.append((event_id, track.customer_id, person_box))
                    logger.debug(f'Queued clip analysis for event {event_id[:12]} '
                                 f'customer={track.customer_id} session={session_id[:8]}')

    except Exception as e:
        logger.error(f'Event processing error: {e}')
        import traceback
        traceback.print_exc()

# ─── Frigate Integration ────────────────────────────────────────────────────

_seen_events = {}  # event_id -> timestamp; insertion-ordered for FIFO eviction
_active_event_last_processed = {}  # event_id -> timestamp; throttles re-processing of still-active events
ACTIVE_EVENT_REPROCESS_INTERVAL = 90  # seconds between re-runs for the same active event
_clip_analysis_queue = collections.deque()  # [(event_id, customer_id, person_box)]
_clip_queue_lock = threading.Lock()
MAX_CLIP_QUEUE = 50
_clips_enriched = set()  # (event_id, customer_id) pairs already processed — prevents re-queue spam

def _clip_analysis_loop():
    """Background thread: post-event clip enrichment. Multiple workers share the queue."""
    import time as _t
    while True:
        _check_daily_reset()  # reset per-customer replacement cap at midnight
        with _clip_queue_lock:
            if _clip_analysis_queue:
                event_id, customer_id, person_box = _clip_analysis_queue.popleft()
            else:
                event_id = None
        if event_id is None:
            _t.sleep(5)  # idle — no work available
            continue

        _t.sleep(1)  # brief yield between jobs to keep CPU below saturation

        for event_id, customer_id, person_box in [(event_id, customer_id, person_box)]:
            # Skip if this (event, customer) pair was already fully processed
            dedup_key = (event_id, customer_id)
            if dedup_key in _clips_enriched:
                logger.debug(f'Clip skip (already enriched): {event_id[:12]} cid={customer_id}')
                continue
            _clips_enriched.add(dedup_key)
            if len(_clips_enriched) > 1000:
                # Evict oldest half — set has no order so just clear the excess
                excess = list(_clips_enriched)[:500]
                for k in excess:
                    _clips_enriched.discard(k)

            clip_path = fetch_frigate_clip(event_id)
            if not clip_path:
                logger.debug(f'Clip not available for {event_id[:12]}')
                continue
            try:
                # For known customers, check if further enrichment is worth the CPU cost.
                # Skip entirely if already at max angles AND has gait stored.
                _n_sample = 25  # reduced from 50 — enough for multi-angle coverage
                _need_gait = True
                if customer_id is not None:
                    cust_sigs = _signals_cache.get(customer_id, {})
                    stored_angles = len(cust_sigs.get('face_embeddings', []))
                    has_gait = bool(cust_sigs.get('gait_features'))
                    if stored_angles >= MAX_FACE_EMBEDDINGS and has_gait:
                        logger.debug(f'Clip skip: cid={customer_id} already at max angles+gait')
                        continue
                    # Partial enrichment: fewer frames, skip gait if already stored
                    _n_sample = 10
                    _need_gait = not has_gait

                signals = analyze_clip_for_best_signals(clip_path, person_box, n_sample=_n_sample)
                if not signals:
                    continue

                distinct_faces = signals.pop('distinct_faces', [])
                clip_camera = signals.get('camera') or signals.get('source')

                # --- If clip matched an existing customer: enrich their profile ---
                # Use RESOLVER_LINK_THRESHOLD (not the stricter real-time link threshold)
                # so clips can enrich customers even with moderate face similarity.
                if customer_id is None and signals.get('face_embedding'):
                    all_sigs = get_all_customer_signals()
                    best_match_id = None
                    best_match_score = 0.0
                    for cid_cand, cust_sigs in all_sigs.items():
                        score, _, _, safe, _ = calculate_match_score_safe(signals, cust_sigs)
                        if safe and score > best_match_score:
                            best_match_score = score
                            best_match_id = cid_cand
                    if best_match_id and best_match_score >= RESOLVER_LINK_THRESHOLD:
                        customer_id = best_match_id
                        logger.info(f'Clip matched existing customer={customer_id} '
                                    f'(score={best_match_score:.3f}) for event {event_id[:12]}')

                # --- Unresolved: feed evidence into VisitorSession, NOT directly creating a customer ---
                if customer_id is None:
                    # Session resolver will decide what to do with this evidence
                    face_emb_for_sess = signals.get('face_embedding') if signals.get('face_quality', 0) >= FACE_QUALITY_MIN_CREATE else None
                    sess_id = _assign_to_session(face_emb_for_sess, clip_camera, event_id, time.time())
                    sess = _active_sessions.get(sess_id)
                    if sess:
                        gait_for_sess = signals.get('gait_features')
                        gait_q = float(signals.get('gait_quality', 0))
                        sess.add_evidence(
                            face_emb=face_emb_for_sess,
                            quality=float(signals.get('face_quality', 0)),
                            camera=clip_camera,
                            gait=gait_for_sess,
                            gait_quality=gait_q,
                            event_id=event_id,
                            face_embeddings_list=distinct_faces,
                        )
                        logger.debug(f'Clip evidence → session {sess_id[:8]}: '
                                     f'{len(distinct_faces)} angles from {event_id[:12]}')
                    continue  # Do NOT proceed to enrichment for unresolved clips

                # Submit each distinct face angle directly.
                # For established customers (visit_count > 2) and within daily cap,
                # pass replace_if_better=True so low-quality stored embeddings can be
                # upgraded when the clip produces a sharper shot of the same angle.
                cust_visit_count = _signals_cache.get(customer_id, {}).get('visit_count', 0)
                can_replace = (cust_visit_count > 2 and
                               _daily_replacements[customer_id] < 3)

                angles_added = 0
                best_attrs = None
                clip_camera = signals.get('camera') or signals.get('source')

                # Verify each clip face actually matches this customer before storing.
                # Clips can contain multiple people — don't enrich with wrong-person faces.
                cust_sigs = _signals_cache.get(customer_id, {})
                cust_stored_embs = []
                for fe in cust_sigs.get('face_embeddings', [])[:10]:  # top 10 stored
                    emb_b64 = fe['embedding_b64'] if isinstance(fe, dict) else fe
                    try:
                        e = np.frombuffer(base64.b64decode(emb_b64), dtype=np.float32)
                        n = np.linalg.norm(e)
                        if n > 0:
                            cust_stored_embs.append(e / n)
                    except Exception:
                        pass

                for qual, emb_bytes, photo_bytes, attrs in distinct_faces:
                    # Gate: face must match customer at ≥FACE_THRESHOLD before enrolling
                    if cust_stored_embs:
                        clip_emb = np.frombuffer(emb_bytes, dtype=np.float32)
                        cn = np.linalg.norm(clip_emb)
                        if cn > 0:
                            clip_emb = clip_emb / cn
                        best_match = max(float(np.dot(clip_emb, se)) for se in cust_stored_embs)
                        if best_match < FACE_THRESHOLD:
                            logger.debug(f'Clip face rejected for cid={customer_id}: sim={best_match:.3f} < {FACE_THRESHOLD}')
                            continue
                    payload = {
                        'embedding_b64': base64.b64encode(emb_bytes).decode(),
                        'quality': float(qual),
                        'camera_source': clip_camera,
                    }
                    # Only attach face photo if it's large enough to be a real face.
                    # ArcFace-aligned 112×112 crops: hands/objects ~2-3KB, real faces ≥4KB.
                    if photo_bytes and len(photo_bytes) >= 4000:
                        payload['photo_b64'] = base64.b64encode(photo_bytes).decode()
                    if can_replace:
                        payload['replace_if_better'] = True
                    result = pos_post(f'/api/customers/{customer_id}/enroll/face', payload)
                    if result and can_replace:
                        _daily_replacements[customer_id] += 1
                    angles_added += 1
                    if attrs and (best_attrs is None or
                                  float(attrs.get('confidence', 0)) >
                                  float((best_attrs or {}).get('confidence', 0))):
                        best_attrs = attrs

                # Invalidate cache: new/replaced angles just stored
                if angles_added > 0:
                    _signals_cache_ids.clear()

                # Gait — enroll only if customer doesn't have one yet
                if _need_gait and signals.get('gait_features'):
                    existing_gaits = pos_get(f'/api/customers/{customer_id}/gaits_raw') or []
                    if not existing_gaits:
                        pos_post(f'/api/customers/{customer_id}/enroll/gait', {
                            'features_b64': base64.b64encode(signals['gait_features']).decode(),
                            'quality': float(signals.get('gait_quality', 0.5)),
                        })

                # Body snapshot
                if signals.get('snapshot_photo'):
                    pos_post(f'/api/customers/{customer_id}/enroll/face', {
                        'embedding_b64': base64.b64encode(bytes(512 * 4)).decode(),
                        'quality': 0.0,
                        'body_photo_b64': base64.b64encode(signals['snapshot_photo']).decode(),
                        'snapshot_only': True,
                    })

                # Best physical attributes — one write total
                attrs_to_write = best_attrs or signals.get('physical_attrs')
                if attrs_to_write and float(attrs_to_write.get('confidence', 0)) >= 0.3:
                    pos_post(f'/api/customers/{customer_id}/attributes', attrs_to_write)

                logger.info(
                    f'Clip enrichment: customer={customer_id} '
                    f'angles={angles_added} '
                    f'gait={bool(signals.get("gait_features"))}'
                )
            except Exception as e:
                logger.warning(f'Clip analysis failed for {event_id[:12]}: {e}')
            finally:
                try:
                    os.unlink(clip_path)
                except Exception:
                    pass

def _track_cleanup_loop():
    """Background thread: expire stale tracks to prevent memory growth."""
    while True:
        time.sleep(60)
        with _tracks_lock:
            expired = [tid for tid, t in list(_active_tracks.items())
                       if t.idle_time() > TRACK_IDLE_EXPIRY]
            for tid in expired:
                del _active_tracks[tid]
            if expired:
                logger.debug(f'Track cleanup: removed {len(expired)} stale tracks, {len(_active_tracks)} remaining')


def _session_resolver_loop():
    """Background thread: runs every 10s.
    1. Merges overlapping sessions (face_sim ≥ SESSION_MERGE_FACE_SIM)
    2. Resolves sessions idle ≥ SESSION_IDLE_EXPIRY or age ≥ MAX_SESSION_LIFETIME
    3. Evicts expired sessions and stale anonymous identities"""
    while True:
        time.sleep(10)
        try:
            now = time.time()

            # Step 1: consolidate sessions before resolving
            _merge_overlapping_sessions()

            # Step 2: find sessions ready to resolve
            with _sessions_lock:
                to_resolve = [
                    sess for sess in list(_active_sessions.values())
                    if sess.status == 'accumulating' and (
                        now - sess.last_evidence_at > SESSION_IDLE_EXPIRY or
                        now - sess.created_at > MAX_SESSION_LIFETIME
                    )
                ]

            for sess in to_resolve:
                try:
                    _resolve_session(sess)
                except Exception as e:
                    logger.error(f'Session resolver error: {e}')
                    sess.status = 'expired'

            # Step 3: evict expired sessions
            with _sessions_lock:
                expired_sids = [k for k, v in list(_active_sessions.items())
                                if v.status in ('resolved', 'expired')]
                for sid in expired_sids:
                    del _active_sessions[sid]

            # Step 4: evict stale anonymous identities (TTL based on last_seen_at)
            stale = [k for k, v in list(_anonymous_identities.items())
                     if now - v.get('last_seen_at', v['created_at']) > ANON_IDENTITY_TTL]
            for k in stale:
                del _anonymous_identities[k]

            if to_resolve:
                logger.debug(f'Session resolver: processed {len(to_resolve)} sessions, '
                             f'{len(_active_sessions)} active, '
                             f'{len(_anonymous_identities)} anonymous identities')

        except Exception as e:
            logger.error(f'Session resolver loop error: {e}')


def poll_frigate_events():
    """Background poller for Frigate events"""
    import time as time_module
    logger.info('Frigate poller thread started')
    while True:
        try:
            logger.debug('Polling Frigate for events...')
            r = requests.get(f'{FRIGATE_URL}/api/events?limit=20&has_snapshot=1', timeout=10)
            if r.ok:
                events = r.json()
                new_count = 0
                recent_count = 0
                now = time_module.time()

                for ev in events:
                    eid = ev.get('id')
                    end_time = ev.get('end_time')
                    label = ev.get('label')

                    # Only process persons and outdoor cars (for ANPR)
                    if label not in ('person', 'car'):
                        continue

                    # Active event (no end_time yet) — throttle re-processing to avoid
                    # running SCRFD inference on every 30s poll for someone standing still.
                    if not end_time:
                        recent_count += 1
                        last = _active_event_last_processed.get(eid, 0)
                        if (now - last) < ACTIVE_EVENT_REPROCESS_INTERVAL:
                            continue
                        _active_event_last_processed[eid] = now
                        # Evict old entries to prevent unbounded growth
                        if len(_active_event_last_processed) > 200:
                            oldest_keys = list(_active_event_last_processed.keys())[:50]
                            for k in oldest_keys:
                                _active_event_last_processed.pop(k, None)
                        new_count += 1
                        logger.info(f'Processing active event {eid[:20]} (label={label} camera={ev.get("camera")})')
                        def _run_active(e=ev):
                            with _event_semaphore:
                                process_event(e)
                        threading.Thread(target=_run_active, daemon=True).start()

                    # Ended event within last 60 seconds — process once
                    elif (now - end_time) <= 60:
                        recent_count += 1
                        if eid and eid not in _seen_events:
                            new_count += 1
                            _seen_events[eid] = now
                            if len(_seen_events) > 500:
                                oldest = list(_seen_events.keys())[:100]
                                for o in oldest:
                                    _seen_events.pop(o, None)
                            logger.info(f'Processing ended event {eid[:20]} (label={label} camera={ev.get("camera")})')
                            ev['_is_ended'] = True  # signal process_event to queue clip analysis
                            def _run_ended(e=ev):
                                with _event_semaphore:
                                    process_event(e)
                            threading.Thread(target=_run_ended, daemon=True).start()

                logger.debug(f'Frigate poll complete: {len(events)} total, {recent_count} recent, {new_count} new')
        except Exception as e:
            logger.warning(f'Frigate poll error: %s', e)
        time_module.sleep(30)

# ─── Webhook Server ─────────────────────────────────────────────────────────

from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _get_status_snapshot():
    """Return a live snapshot of the recognition service state for the monitor."""
    import psutil, os as _os
    proc = psutil.Process(_os.getpid())
    mem_mb = proc.memory_info().rss / 1024 / 1024
    cpu_pct = proc.cpu_percent(interval=0.1)

    with _sessions_lock:
        sessions = [
            {
                'id': s.session_id[:8],
                'status': s.status,
                'faces': len(s.face_embeddings),
                'cameras': list(s.cameras_seen),
                'age_s': round(time.time() - s.created_at),
                'idle_s': round(time.time() - s.last_evidence_at),
                'best_sim': round(s.best_face_sim, 3),
                'candidate_cid': s.candidate_customer_id,
                'events': len(s.source_event_ids),
            }
            for s in _active_sessions.values()
        ]

    anon_list = [
        {
            'id': aid[:8],
            'faces': len(a['face_embeddings']),
            'cameras': list(a.get('cameras', set())),
            'age_s': round(time.time() - a['created_at']),
            'last_seen_s': round(time.time() - a.get('last_seen_at', a['created_at'])),
        }
        for aid, a in list(_anonymous_identities.items())
    ]

    with _clip_queue_lock:
        queue_depth = len(_clip_analysis_queue)
        queue_items = [
            {'event_id': j[0][:12], 'customer_id': j[1]}
            for j in list(_clip_analysis_queue)[:10]
        ]

    cache_size = len(_signals_cache)

    return {
        'ok': True,
        'uptime_s': round(time.time() - _startup_time_epoch),
        'cpu_pct': round(cpu_pct, 1),
        'mem_mb': round(mem_mb, 1),
        'onnx_providers': [],  # filled below
        'sessions': sessions,
        'sessions_total': len(sessions),
        'anon_identities': anon_list,
        'anon_total': len(anon_list),
        'clip_queue_depth': queue_depth,
        'clip_queue_items': queue_items,
        'customer_cache_size': cache_size,
        'active_tracks': len(_active_tracks),
        'created_from_sessions': len(_created_from_session_ids),
    }


_startup_time_epoch = time.time()


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == '/status':
            try:
                data = _get_status_snapshot()
                try:
                    import onnxruntime as _ort
                    data['onnx_providers'] = _ort.get_available_providers()
                except Exception:
                    pass
                self._send_json(data)
            except Exception as e:
                self._send_json({'error': str(e)}, 500)

        elif parsed.path == '/logs':
            n     = int(qs.get('n', ['200'])[0])
            level = qs.get('level', [None])[0]
            search = qs.get('q', [None])[0]
            recs = _log_buffer.get(n=n, level=level)
            if search:
                recs = [r for r in recs if search.lower() in r['msg'].lower()]
            self._send_json({'logs': recs, 'total': len(recs)})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body_raw = self.rfile.read(length)
        try:
            body_data = json.loads(body_raw) if body_raw else {}
        except Exception:
            body_data = {}

        if parsed.path == '/webhook/frigate':
            self.send_response(200)
            self.end_headers()
            try:
                payload = body_data
                event_type = payload.get('type')
                after = payload.get('after') or payload.get('before') or {}
                if event_type in ('update', 'end') and after.get('label') in ('person', 'car'):
                    if event_type == 'end':
                        after['_is_ended'] = True
                    def _run_webhook(e=after):
                        with _event_semaphore:
                            process_event(e)
                    threading.Thread(target=_run_webhook, daemon=True).start()
            except Exception as e:
                logger.warning(f'Webhook parse error: {e}')
            return

        elif parsed.path == '/control/clear_queue':
            with _clip_queue_lock:
                cleared = len(_clip_analysis_queue)
                _clip_analysis_queue.clear()
            logger.info(f'Monitor: clip queue cleared ({cleared} items)')
            self._send_json({'ok': True, 'cleared': cleared})

        elif parsed.path == '/control/flush_sessions':
            with _sessions_lock:
                n = len(_active_sessions)
                for s in _active_sessions.values():
                    s.status = 'expired'
            logger.info(f'Monitor: flushed {n} active sessions')
            self._send_json({'ok': True, 'flushed': n})

        elif parsed.path == '/control/clear_anon':
            n = len(_anonymous_identities)
            _anonymous_identities.clear()
            logger.info(f'Monitor: cleared {n} anonymous identities')
            self._send_json({'ok': True, 'cleared': n})

        elif parsed.path == '/control/sync_cache':
            _signals_cache_ids.clear()
            logger.info('Monitor: customer cache invalidated — will rebuild on next poll')
            self._send_json({'ok': True})

        elif parsed.path == '/control/requeue_clip':
            event_id = body_data.get('event_id')
            customer_id = body_data.get('customer_id')
            if not event_id:
                self._send_json({'error': 'event_id required'}, 400)
                return
            with _clip_queue_lock:
                _clip_analysis_queue.append((event_id, customer_id, None))
            logger.info(f'Monitor: re-queued clip {event_id[:12]} for cid={customer_id}')
            self._send_json({'ok': True})

        elif parsed.path == '/control/resync_customer':
            cid = body_data.get('customer_id')
            if not cid:
                self._send_json({'error': 'customer_id required'}, 400)
                return
            if cid in _signals_cache:
                del _signals_cache[cid]
            _signals_cache_ids.discard(cid)
            logger.info(f'Monitor: evicted cid={cid} from signals cache')
            self._send_json({'ok': True})

        else:
            self._send_json({'error': 'Not found'}, 404)
            return


def _reindex_customer_embeddings(cid):
    """Re-run ArcFace on stored face photos for a customer and re-enroll fresh embeddings.
    Called nightly for customers not seen in >90 days to keep embeddings current."""
    faces_raw = pos_get(f'/api/customers/{cid}/faces_raw') or []
    if not faces_raw:
        return
    face_app = get_face_app()
    if not face_app:
        return

    new_embeddings = []
    for face_entry in faces_raw:
        # faces_raw doesn't include the raw photo — we need to fetch via the photo endpoint
        pass  # Will use enroll/face with replace_if_better for now; full photo re-embedding
              # requires a separate endpoint. Use existing embeddings + replace_if_better only.

    # For now: trigger profile improvement by posting the existing embeddings back with
    # replace_if_better=True — the endpoint will upgrade lower-quality angles
    logger.debug(f'Nightly reindex: cid={cid} — triggering replace_if_better on {len(faces_raw)} embeddings')
    for face_entry in faces_raw:
        if not isinstance(face_entry, dict):
            continue
        quality = float(face_entry.get('quality') or 0.0)
        if quality <= 0:
            continue  # no quality metadata yet, skip
        # Re-submit with replace_if_better — will upgrade any same-angle lower-quality row
        pos_post(f'/api/customers/{cid}/enroll/face', {
            'embedding_b64': face_entry['embedding_b64'],
            'quality': quality,
            'camera_source': face_entry.get('camera'),
            'replace_if_better': True,
        })


def _nightly_reindex_loop():
    """Nightly background job: re-select best embeddings for stale customers (>90 days absent).
    Runs at 02:00, processes max 50 customers per night with 2s throttle between each."""
    from datetime import timedelta
    while True:
        now = datetime.now()
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())

        try:
            customers = pos_get('/api/customers') or []
            stale = []
            for c in customers:
                last = c.get('last_visit')
                if not last:
                    stale.append(c)
                    continue
                try:
                    if (datetime.utcnow() - datetime.fromisoformat(last.replace('Z', ''))).days > 90:
                        stale.append(c)
                except Exception:
                    pass

            logger.info(f'Nightly reindex: {len(stale)} stale customers, processing max 50')
            for c in stale[:50]:
                try:
                    _reindex_customer_embeddings(c['id'])
                    time.sleep(2)
                except Exception as e:
                    logger.warning(f'Reindex failed cid={c["id"]}: {e}')
        except Exception as e:
            logger.warning(f'Nightly reindex loop error: {e}')


def run_webhook_server():
    server = ThreadedHTTPServer(('0.0.0.0', WEBHOOK_PORT), WebhookHandler)
    logger.info(f'Webhook server listening on port {WEBHOOK_PORT}')
    server.serve_forever()

# ─── Entry Point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logger.info('Recognition service v2.0 starting')
    logger.info(f'Weights version: {WEIGHTS_VERSION}')
    logger.info(f'Threshold version: {THRESHOLD_VERSION}')

    pos_login()
    refresh_customers()
    reload_thresholds_from_pos()

    # Force a full signal cache rebuild on startup so embeddings are loaded
    # before any events are processed. refresh_customers() already invalidates
    # _signals_cache_ids, so the next get_all_customer_signals() will rebuild.
    refresh_customers()
    get_all_customer_signals()
    logger.info('Signal cache primed')

    # Build a set of recently-enrolled customer numbers (last 5 min) so that
    # if we restart while people are still in frame, we don't re-enroll them.
    # process_event checks this set before enrolling a new track.
    _startup_recent_customers = set()
    try:
        import time as _st
        cutoff = _st.time() - 300
        for c in _customers_cache:
            first_seen = c.get('first_seen')
            if first_seen:
                try:
                    from datetime import timezone
                    fs_dt = datetime.fromisoformat(first_seen.replace('Z', '+00:00'))
                    fs_epoch = fs_dt.replace(tzinfo=timezone.utc).timestamp() if fs_dt.tzinfo else fs_dt.timestamp()
                    if fs_epoch >= cutoff:
                        _startup_recent_customers.add(c.get('customer_number'))
                except Exception:
                    pass
        if _startup_recent_customers:
            logger.info(f'Startup: {len(_startup_recent_customers)} recently enrolled customers — will skip re-enrollment: {_startup_recent_customers}')
    except Exception as e:
        logger.warning(f'Startup recent-customer check failed: {e}')

    # Store globally so process_event can check on first poll
    globals()['_startup_skip_numbers'] = _startup_recent_customers

    # Background cache refresh
    threading.Thread(target=_cache_refresh_loop, daemon=True).start()

    # Brief delay before starting the Frigate poller — lets models load and
    # signal cache settle before processing any events.
    import time as _startup_time
    _startup_time.sleep(5)

    # Background Frigate poller
    threading.Thread(target=poll_frigate_events, daemon=True).start()

    # Background clip enrichment — 2 workers drain the queue without saturating the CPU
    for _i in range(2):
        threading.Thread(target=_clip_analysis_loop, daemon=True, name=f'clip-worker-{_i}').start()

    # Background track cleanup (prevents _active_tracks memory growth)
    threading.Thread(target=_track_cleanup_loop, daemon=True).start()

    # Session resolver — single authority for customer creation
    threading.Thread(target=_session_resolver_loop, daemon=True).start()
    logger.info('Session resolver started (idle_expiry=60s max_lifetime=300s)')

    # Nightly reindex — refreshes embeddings for customers not seen in >90 days
    threading.Thread(target=_nightly_reindex_loop, daemon=True).start()

    # Webhook server (blocking)
    run_webhook_server()

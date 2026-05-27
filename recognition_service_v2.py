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
from collections import defaultdict
import numpy as np

LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'recognition_service_v2.log')
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(),
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
GAIT_THRESHOLD  = 0.25  # Maximum euclidean distance for gait match

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
# Default = 24 for a 6-camera shop (6 cameras × ~4 meaningful angles each)
# Configurable from POS Settings → Recognition tab without restart
MAX_FACE_EMBEDDINGS = 24
MIN_ANGLE_DISTANCE  = 0.25   # min cosine distance to count as a genuinely new angle

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
MAX_CONTEXT_CONTRIBUTION = 1.0

# ─── Lazy model loading ─────────────────────────────────────────────────────
_anpr_model   = None
_face_app     = None
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
            from insightface.model_zoo import SCRFD, ArcFaceONNX
            import cv2
            from skimage import transform as trans

            model_dir = os.environ.get('INSIGHTFACE_HOME', os.path.expanduser('~/.insightface/models'))
            det_model = os.path.join(model_dir, 'buffalo_l', 'det_10g.onnx')
            rec_model = os.path.join(model_dir, 'buffalo_l', 'w600k_r50.onnx')

            if not os.path.exists(det_model) or not os.path.exists(rec_model):
                logger.error('Face models not found. Run: python download_face_models.py')
                return None

            detector = SCRFD(model_file=det_model)
            detector.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=0.3)

            recognizer = ArcFaceONNX(model_file=rec_model)
            recognizer.prepare(ctx_id=-1)

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
_signals_cache = {}       # customer_id -> signals dict, rebuilt when customer list changes
_signals_cache_ids = set()  # set of customer ids in the cache, used to detect changes
_cache_lock = threading.Lock()
_cache_rebuild_lock = threading.Lock()  # prevents concurrent full cache rebuilds

def refresh_customers():
    global _signals_cache_ids
    customers = pos_get('/api/customers')
    with _cache_lock:
        _customers_cache.clear()
        _customers_cache.extend(customers)
    # Invalidate signals cache so next event rebuilds with new customer set
    _signals_cache_ids = set()
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

            # Skip edge-of-frame crops: if person box is narrower than 8% of frame
            # width, the face will be ~20-30px after upscale — too noisy for ArcFace.
            if person_box and len(person_box) == 4:
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

    # Sample densely — every 2nd frame up to 50 samples
    step = max(1, frame_count // 50)
    indices = list(range(0, frame_count, step))
    face_candidates = []   # (quality, embedding_bytes, photo_bytes, attrs_or_None)
    gait_candidates = []   # (quality, features_bytes)
    best_body_snapshot = None
    best_body_best_face_qual = 0.0  # body frame chosen by face quality, not box area

    # Track distinct angles found so far for early exit
    distinct_seen = []

    for idx in indices:
        # Early exit: if we already have MAX_FACE_EMBEDDINGS distinct angles, stop
        if len(distinct_seen) >= MAX_FACE_EMBEDDINGS:
            logger.debug(f'Clip: reached {MAX_FACE_EMBEDDINGS} distinct angles, stopping early')
            break

        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        cv2.imwrite(tmp.name, frame)
        tmp.close()
        try:
            # Face — do NOT pass person_box for clips: the trigger-frame box is stale
            # as the person moves through the clip. Let SCRFD find the face freely.
            # We pick the best-quality face found in each frame regardless of position.
            face_emb, face_qual, face_photo = extract_face_with_quality(tmp.name, None)
            if face_emb and face_qual >= FACE_QUALITY_MIN:
                # Check if this is a genuinely new angle vs what we've already found
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

            # Gait
            gait_feat, gait_qual = extract_gait_with_quality(tmp.name)
            if gait_feat and gait_qual >= GAIT_QUALITY_MIN:
                gait_candidates.append((gait_qual, gait_feat))

            # Body snapshot — only take this frame's body crop when a face was also
            # confirmed in the SAME frame. This guarantees face and body always match,
            # and skips frames where the person has walked out of shot.
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

    if gait_candidates:
        gait_arrays = [np.frombuffer(f, dtype=np.float32) for _, f in gait_candidates]
        avg_gait = np.mean(gait_arrays, axis=0).astype(np.float32)
        result['gait_features'] = avg_gait.tobytes()
        result['gait_quality']  = float(max(q for q, _ in gait_candidates))
        logger.debug(f'Clip gait: {len(gait_candidates)} frames averaged')

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

    # === BIOMETRIC SIGNALS ===

    # 1. FACE
    if new_signals.get('face_embedding') and customer_signals.get('face_embeddings'):
        biometric_weight += FEATURE_WEIGHTS['face']
        available_weight += FEATURE_WEIGHTS['face']

        new_face = np.frombuffer(new_signals['face_embedding'], dtype=np.float32)
        best_sim = 0.0

        for stored_face_b64 in customer_signals['face_embeddings']:
            stored = np.frombuffer(base64.b64decode(stored_face_b64), dtype=np.float32)
            sim = cosine_sim(new_face, stored)
            best_sim = max(best_sim, sim)

        if best_sim >= FACE_THRESHOLD:
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

    # 3. PLATE (only if biometric >= 50%)
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

    # === CONTEXTUAL SIGNALS (only if biometric >= 60%, capped) ===
    if biometric_ratio >= 0.60:
        # TODO: Implement time_pattern, zone_pattern, plate_person_assoc
        # For now, context is 0
        pass

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
                'face_embeddings': [f['embedding_b64'] for f in face_embeddings],
                'gait_features':   [g['features_b64']   for g in gait_features],
                'plates':          customer.get('plates', []),
                'height_category': attrs.get('height_category'),
                'build':           attrs.get('build'),
                'hair_color':      attrs.get('hair_color'),
                'facial_hair':     attrs.get('facial_hair'),
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
    def add_observation(self, signals, match_results):
        """
        Add frame observation

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

        self._update_identity()

    def _update_identity(self):
        """Update identity using quality-weighted voting"""
        if not self.customer_votes:
            return

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

        link_thresh = _threshold_manager.global_thresholds.get('link', 0.55)
        if best_score >= link_thresh:
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
_event_semaphore = threading.Semaphore(20)

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
            if len(cached_faces) == 0:
                # No active face embedding — add one regardless of quality
                logger.info(f'Profile [{customer_id}]: adding face embedding (quality={new_face_quality:.2f})')
                payload = {
                    'embedding_b64': base64.b64encode(signals['face_embedding']).decode(),
                    'quality': new_face_quality,
                }
                if has_face_photo:
                    payload['photo_b64'] = base64.b64encode(signals['face_photo']).decode()
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
                    logger.info(f'Profile [{customer_id}]: upgrading face photo (quality={new_face_quality:.2f})')
                    payload = {
                        'embedding_b64': base64.b64encode(signals['face_embedding']).decode(),
                        'quality': new_face_quality,
                        'photo_b64': base64.b64encode(signals['face_photo']).decode(),
                    }
                    if has_snapshot:
                        payload['body_photo_b64'] = base64.b64encode(signals['snapshot_photo']).decode()
                    pos_post(f'/api/customers/{customer_id}/enroll/face', payload)
                    upgraded_photo_only = True

            # Only invalidate signals cache when a genuinely new angle embedding was stored.
            # Photo-only upgrades don't change embeddings so no cache invalidation needed.
            if enrolled_new_angle:
                global _signals_cache_ids
                _signals_cache_ids = set()

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

        # Update track
        track.add_observation(signals, match_results)

        # Get context for threshold selection
        context = {
            'camera': signals.get('camera'),
            'quality': 'high' if max(signals.get('face_quality', 0), signals.get('gait_quality', 0)) >= 0.8 else 'medium',
            'time_of_day': 'morning' if 6 <= datetime.now().hour < 12 else 'afternoon'
        }

        link_threshold, link_source = get_current_threshold('link', context)
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
                # Update track so future events don't need to re-resolve
                track.customer_id = primary_id

            # Log face_similarity alongside track confidence for threshold tuning
            best_face_sim = 0.0
            for cid_m, score_m, breakdown_m, _, _ in match_results:
                if cid_m == resolved_id and 'face_similarity' in breakdown_m:
                    best_face_sim = float(breakdown_m['face_similarity'])
                    break
            sim_str = f' face_sim={best_face_sim:.3f}' if best_face_sim > 0 else ''
            logger.info(f'Track {track_id[:8]} linked to customer {resolved_id} (confidence={track.confidence:.3f}{sim_str})')

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
                    if 'face' in breakdown_m:
                        conf_scores['face_score'] = float(breakdown_m['face'])
                    break

            # Log visit — at most once per VISIT_LOG_INTERVAL per track
            if time.time() - track.visit_logged_at >= VISIT_LOG_INTERVAL:
                identify_payload = {
                    'customer_id': resolved_id,
                    'matched_signals': 'track_consensus',
                    'confidence_scores': conf_scores,
                    'camera_source': signals.get('camera'),
                }
                # Include dwell time for ended events (Frigate start_time / end_time are Unix timestamps)
                start_time = event.get('start_time')
                end_time_ev = event.get('end_time')
                if event.get('_is_ended') and start_time and end_time_ev:
                    identify_payload['dwell_seconds'] = int(end_time_ev - start_time)
                pos_post('/api/customers/identify', identify_payload)
                track.visit_logged_at = time.time()

            # Continuous profile improvement — fill in missing or upgrade quality
            _improve_customer_profile(resolved_id, signals)

        elif track.age() >= 30 and track.has_enrollment_quality():
            if track.confidence < pending_threshold:
                # Guard against multiple threads enrolling the same track simultaneously
                with _tracks_lock:
                    if track.enrollment_claimed:
                        logger.debug(f'Track {track_id[:8]} enrollment already claimed, skipping')
                        return
                    track.enrollment_claimed = True  # atomic claim under lock

                logger.info(f'Track {track_id[:8]} ready for enrollment (age={track.age():.1f}s, quality=ok)')

                # Auto-enroll new customer — retry up to 3x on customer_number collision
                new_customer = None
                customer_number_str = None
                next_number = 1
                try:
                    for _attempt in range(3):
                        r = pos_get('/api/customers/max_number')
                        next_number = (r.get('max_number') or 0) + 1 if r else 1
                        customer_number_str = f'CUST-{next_number:04d}'
                        # Skip if this customer_number was recently enrolled (post-restart dedup)
                        skip_nums = globals().get('_startup_skip_numbers', set())
                        if customer_number_str in skip_nums:
                            logger.info(f'Track {track_id[:8]} skipping re-enrollment of {customer_number_str} (enrolled in last 5 min)')
                            skip_nums.discard(customer_number_str)
                            break
                        new_customer = pos_post('/api/customers', {
                            'name': None,
                            'auto_enrolled': True,
                            'customer_number': customer_number_str,
                            'first_seen': datetime.now().isoformat()
                        })
                        if new_customer and new_customer.get('id'):
                            break  # success
                        if new_customer and new_customer.get('error') == 'customer_number_conflict':
                            logger.warning(f'customer_number collision on {customer_number_str}, retrying...')
                            time.sleep(0.1 * (_attempt + 1))
                            continue
                        break  # unexpected response — don't retry
                    if not new_customer:
                        logger.error(f'Auto-enrollment failed: POS returned None for customer POST (POS down?), track={track_id[:8]}')
                    if new_customer and new_customer.get('id'):
                        customer_id = new_customer['id']
                        logger.info(f'Auto-enrolled customer {customer_number_str} (id={customer_id})')
                        # Enroll face if available
                        best_face = track.get_best_signal('face')
                        if best_face and best_face.get('embedding'):
                            payload = {
                                'embedding_b64': base64.b64encode(best_face['embedding']).decode(),
                                'quality': float(best_face.get('quality', 0.0)),
                            }
                            if best_face.get('photo'):
                                payload['photo_b64'] = base64.b64encode(best_face['photo']).decode()
                            pos_post(f'/api/customers/{customer_id}/enroll/face', payload)
                            logger.info(f'   Face enrolled for customer #{next_number}')

                        # Enroll gait if available
                        best_gait = track.get_best_signal('gait')
                        if best_gait and best_gait.get('features'):
                            pos_post(f'/api/customers/{customer_id}/enroll/gait', {
                                'features_b64': base64.b64encode(best_gait['features']).decode(),
                                'quality': float(best_gait.get('quality', 0.0))
                            })
                            logger.info(f'   Gait enrolled for customer #{next_number}')

                        # Always store body snapshot so every customer card has
                        # a visual — face crop if available, full-body otherwise
                        if signals.get('snapshot_photo'):
                            payload = {
                                'embedding_b64': base64.b64encode(bytes(512 * 4)).decode(),
                                'quality': 0.0,
                                'body_photo_b64': base64.b64encode(signals['snapshot_photo']).decode(),
                                'snapshot_only': True,
                            }
                            # Include face crop as photo if we have one
                            if best_face and best_face.get('photo'):
                                payload['photo_b64'] = base64.b64encode(best_face['photo']).decode()
                            pos_post(f'/api/customers/{customer_id}/enroll/face', payload)
                            logger.info(f'   Body snapshot stored for customer #{next_number}')

                        # Enroll physical attributes if extracted
                        if signals.get('physical_attrs'):
                            pos_post(f'/api/customers/{customer_id}/attributes', signals['physical_attrs'])
                            logger.info(f'   Attributes enrolled for customer #{next_number}')

                        # Link track to new customer
                        track.customer_id = customer_id
                        track.confidence = 1.0

                        # Insert new customer into caches directly — avoids a full
                        # O(N×3) refresh_customers() rebuild just for one new entry.
                        new_entry = {
                            'id': customer_id,
                            'auto_enrolled': True,
                            'customer_number': customer_number_str,
                            'name': None,
                            'plates': [],
                        }
                        with _cache_lock:
                            _customers_cache.append(new_entry)
                        with _cache_rebuild_lock:
                            _signals_cache[customer_id] = {
                                'id': customer_id,
                                'face_embeddings': [],
                                'gait_features': [],
                                'plates': [],
                                'height_category': None,
                                'build': None,
                                'hair_color': None,
                                'facial_hair': None,
                            }

                        # Log visit
                        enroll_identify = {
                            'customer_id': customer_id,
                            'matched_signals': 'auto_enrollment',
                            'confidence_scores': {'auto_enroll': 1.0},
                            'camera_source': signals.get('camera'),
                        }
                        start_time = event.get('start_time')
                        end_time_ev = event.get('end_time')
                        if start_time and end_time_ev:
                            enroll_identify['dwell_seconds'] = int(end_time_ev - start_time)
                        pos_post('/api/customers/identify', enroll_identify)

                except Exception as e:
                    logger.error(f'Auto-enrollment failed: {e}')
                    import traceback
                    traceback.print_exc()
                    # Release the claim so the next event can retry enrollment
                    with _tracks_lock:
                        track.enrollment_claimed = False

        else:
            logger.debug(f'Track {track_id[:8]} pending (age={track.age():.1f}s, confidence={track.confidence:.3f})')

        # Queue clip analysis for all ended events — both resolved (profile enrichment)
        # and unresolved (first-time visitor who got a poor real-time snapshot).
        # customer_id may be None for unresolved tracks; _clip_analysis_loop handles both.
        if event.get('_is_ended'):
            person_box = (event.get('data') or {}).get('box')
            with _clip_queue_lock:
                if (len(_clip_analysis_queue) < MAX_CLIP_QUEUE
                        and not any(j[0] == event_id for j in _clip_analysis_queue)):
                    _clip_analysis_queue.append((event_id, track.customer_id, person_box))
                    logger.debug(f'Queued clip analysis for event {event_id[:12]} customer={track.customer_id}')

    except Exception as e:
        logger.error(f'Event processing error: {e}')
        import traceback
        traceback.print_exc()

# ─── Frigate Integration ────────────────────────────────────────────────────

_seen_events = {}  # event_id -> timestamp; insertion-ordered for FIFO eviction
_clip_analysis_queue = []      # [(event_id, customer_id, person_box)]
_clip_queue_lock = threading.Lock()
MAX_CLIP_QUEUE = 50

def _clip_analysis_loop():
    """Background thread: post-event clip enrichment."""
    import time as _t
    while True:
        _t.sleep(10)
        with _clip_queue_lock:
            jobs = list(_clip_analysis_queue)
            _clip_analysis_queue.clear()

        for event_id, customer_id, person_box in jobs:
            clip_path = fetch_frigate_clip(event_id)
            if not clip_path:
                logger.debug(f'Clip not available for {event_id[:12]}')
                continue
            try:
                signals = analyze_clip_for_best_signals(clip_path, person_box)
                if not signals:
                    continue

                distinct_faces = signals.pop('distinct_faces', [])

                # --- Unresolved track: try to identify from clip signals first ---
                if customer_id is None and signals.get('face_embedding'):
                    all_sigs = get_all_customer_signals()
                    best_match_id = None
                    best_match_score = 0.0
                    link_thresh = _threshold_manager.global_thresholds.get('link', 0.55)
                    for cid_cand, cust_sigs in all_sigs.items():
                        score, _, _, safe, _ = calculate_match_score_safe(signals, cust_sigs)
                        if safe and score > best_match_score:
                            best_match_score = score
                            best_match_id = cid_cand
                    if best_match_id and best_match_score >= link_thresh:
                        customer_id = best_match_id
                        logger.info(f'Clip resolved unresolved track {event_id[:12]} → customer={customer_id} (score={best_match_score:.3f})')
                    else:
                        # Still unresolved — enroll as new customer from clip
                        logger.info(f'Clip enrolling new customer from unresolved track {event_id[:12]}')
                        r = pos_get('/api/customers/max_number')
                        next_number = (r.get('max_number') or 0) + 1 if r else 1
                        customer_number_str = f'CUST-{next_number:04d}'
                        new_customer = pos_post('/api/customers', {
                            'name': None,
                            'auto_enrolled': True,
                            'customer_number': customer_number_str,
                        })
                        if new_customer and new_customer.get('id'):
                            customer_id = new_customer['id']
                            logger.info(f'Clip enrolled new customer {customer_number_str} (id={customer_id})')
                            refresh_customers()
                        else:
                            logger.warning(f'Clip enrollment failed for {event_id[:12]}')
                            continue

                if customer_id is None:
                    continue

                # Submit each distinct face angle directly — avoids N redundant
                # faces_raw/gaits_raw/attributes GETs that _improve_customer_profile
                # would make for each of the N angles.
                angles_added = 0
                best_attrs = None
                for qual, emb_bytes, photo_bytes, attrs in distinct_faces:
                    payload = {
                        'embedding_b64': base64.b64encode(emb_bytes).decode(),
                        'quality': float(qual),
                    }
                    if photo_bytes:
                        payload['photo_b64'] = base64.b64encode(photo_bytes).decode()
                    pos_post(f'/api/customers/{customer_id}/enroll/face', payload)
                    angles_added += 1
                    if attrs and (best_attrs is None or float(attrs.get('confidence', 0)) > float((best_attrs or {}).get('confidence', 0))):
                        best_attrs = attrs

                # Invalidate cache: new angles were just added
                if angles_added > 0:
                    global _signals_cache_ids
                    _signals_cache_ids = set()

                # Gait — enroll once from averaged clip gait
                if signals.get('gait_features'):
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

                    # Active event (no end_time yet) — process every poll.
                    if not end_time:
                        recent_count += 1
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


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

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

            # Process both updates (track building) and end events (final snapshot)
            # Accept 'person' for recognition and 'car' for ANPR plate reading
            if event_type in ('update', 'end') and after.get('label') in ('person', 'car'):
                if event_type == 'end':
                    after['_is_ended'] = True
                def _run_webhook(e=after):
                    with _event_semaphore:
                        process_event(e)
                threading.Thread(target=_run_webhook, daemon=True).start()
        except Exception as e:
            logger.warning(f'Webhook parse error: {e}')

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

    # Background clip enrichment (post-event quality improvement)
    threading.Thread(target=_clip_analysis_loop, daemon=True).start()

    # Background track cleanup (prevents _active_tracks memory growth)
    threading.Thread(target=_track_cleanup_loop, daemon=True).start()

    # Webhook server (blocking)
    run_webhook_server()

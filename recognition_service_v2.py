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

# Model thresholds
FACE_THRESHOLD  = 0.40  # Minimum cosine similarity for face match
GAIT_THRESHOLD  = 0.25  # Maximum euclidean distance for gait match

# Quality gates
FACE_QUALITY_MIN = 0.5   # Minimum face quality to use
GAIT_QUALITY_MIN = 0.6   # Minimum gait quality to use
PLATE_CONF_MIN   = 0.8   # Minimum OCR confidence to use

# Versioning
WEIGHTS_VERSION = "v2.0_production"
THRESHOLD_VERSION = "v1.0_initial"

# ─── Feature Weights (production-tuned) ─────────────────────────────────────
FEATURE_WEIGHTS = {
    # Biometric (identity-grade)
    'face':         6.0,
    'gait':         3.0,

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
            detector.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=0.5)

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
                        return []

                    results = []
                    img_area = img.shape[0] * img.shape[1]

                    for bbox, kps in zip(bboxes, kpss):
                        # Quality scoring
                        det_conf = bbox[4] if len(bbox) > 4 else 0.8

                        # Face size
                        face_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        size_ratio = face_area / img_area
                        size_score = min(1.0, size_ratio / 0.05)

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
                        results.append((emb.tobytes(), quality, bbox))

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
_pos_session = requests.Session()
_pos_session.verify = False
_pos_logged_in = False

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def pos_login():
    global _pos_logged_in
    try:
        r = _pos_session.post(f'{POS_URL}/api/login', json={'username': POS_USER, 'password': POS_PASS}, timeout=5)
        if r.ok:
            _pos_logged_in = True
            logger.info('Logged in to POS API')
        else:
            logger.warning(f'POS login failed: {r.text}')
    except Exception as e:
        logger.warning(f'POS login error: {e}')

def pos_post(path, payload, retries=2):
    global _pos_logged_in
    if not _pos_logged_in:
        pos_login()

    for attempt in range(retries + 1):
        try:
            r = _pos_session.post(f'{POS_URL}{path}', json=payload, timeout=10)
            if r.status_code == 401:
                pos_login()
                r = _pos_session.post(f'{POS_URL}{path}', json=payload, timeout=10)
            return r.json() if r.ok else None
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < retries:
                time.sleep(0.5)
                continue
            logger.warning(f'POS POST {path} failed after {retries} retries: {e}')
            return None
        except Exception as e:
            logger.warning(f'POS POST {path} error: {e}')
            return None

def pos_get(path, retries=2):
    global _pos_logged_in
    if not _pos_logged_in:
        pos_login()

    for attempt in range(retries + 1):
        try:
            r = _pos_session.get(f'{POS_URL}{path}', timeout=10)
            return r.json() if r.ok else []
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < retries:
                time.sleep(0.5)
                continue
            logger.warning(f'POS GET {path} failed after {retries} retries: {e}')
            return []
        except Exception as e:
            logger.warning(f'POS GET {path} error: {e}')
            return []

# ─── Customer cache ─────────────────────────────────────────────────────────
_customers_cache = []
_cache_lock = threading.Lock()

def refresh_customers():
    customers = pos_get('/api/customers')
    with _cache_lock:
        _customers_cache.clear()
        _customers_cache.extend(customers)
    logger.info(f'Customer cache refreshed: {len(customers)} customers')

def _cache_refresh_loop():
    while True:
        try:
            refresh_customers()
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

def extract_face_with_quality(image_path):
    """Returns (embedding_bytes, quality_score) or (None, 0.0)"""
    try:
        import cv2
        face_app = get_face_app()
        if not face_app:
            return None, 0.0

        img = cv2.imread(image_path)
        if img is None:
            return None, 0.0

        results = face_app.get_with_quality(img)
        if not results:
            return None, 0.0

        # Return best quality face
        best = max(results, key=lambda x: x[1])
        return best[0], best[1]

    except Exception as e:
        logger.error(f'Face extraction error: {e}')
        return None, 0.0

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

def extract_physical_attributes(image_path):
    """Extract physical attributes with confidence"""
    try:
        import cv2

        face_app = get_face_app()
        if not face_app:
            return None

        img = cv2.imread(image_path)
        if img is None:
            return None

        # Get face detection
        faces = face_app.detector.detect(img, input_size=(640, 640))
        if len(faces[0]) == 0:
            return None

        face_bbox = faces[0][0]
        attributes = {}

        # Hair color
        hair_region = img[max(0, int(face_bbox[1] - 50)):int(face_bbox[1]),
                          int(face_bbox[0]):int(face_bbox[2])]
        if hair_region.size > 0:
            avg_color = cv2.mean(hair_region)[:3]
            b, g, r = avg_color
            brightness = (r + g + b) / 3

            if brightness < 50:
                attributes['hair_color'] = 'black'
            elif brightness < 100:
                attributes['hair_color'] = 'brown'
            elif brightness > 180:
                attributes['hair_color'] = 'blonde' if r <= g else 'red'
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

        # Facial hair
        face_region = img[int(face_bbox[1]):int(face_bbox[3]),
                          int(face_bbox[0]):int(face_bbox[2])]
        if face_region.size > 0:
            face_height = face_bbox[3] - face_bbox[1]
            chin_region = face_region[int(face_height * 0.7):, :]
            if chin_region.size > 0:
                chin_darkness = np.mean(cv2.cvtColor(chin_region, cv2.COLOR_BGR2GRAY))
                if chin_darkness < 80:
                    attributes['facial_hair'] = 'beard'
                elif chin_darkness < 120:
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
            face_emb, face_qual = extract_face_with_quality(snapshot_path)
            if face_emb:
                signals['face_embedding'] = face_emb
                signals['face_quality'] = face_qual

            gait_feat, gait_qual = extract_gait_with_quality(snapshot_path)
            if gait_feat:
                signals['gait_features'] = gait_feat
                signals['gait_quality'] = gait_qual

            physical = extract_physical_attributes(snapshot_path)
            if physical:
                signals['physical_attrs'] = physical

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
    Fetch all customer signals from POS

    Returns: {customer_id: {face_embeddings: [...], gait_features: [...], plates: [...], ...}}
    """
    with _cache_lock:
        customers = list(_customers_cache)

    customer_signals = {}

    for customer in customers:
        cid = customer['id']

        # Fetch biometric signals
        face_embeddings = pos_get(f'/api/customers/{cid}/faces_raw') or []
        gait_features = pos_get(f'/api/customers/{cid}/gaits_raw') or []
        plates = customer.get('plates', [])

        # Fetch physical attributes
        attrs = pos_get(f'/api/customers/{cid}/attributes') or {}

        customer_signals[cid] = {
            'id': cid,
            'face_embeddings': [f['embedding_b64'] for f in face_embeddings],
            'gait_features': [g['features_b64'] for g in gait_features],
            'plates': plates,
            'height_category': attrs.get('height_category'),
            'build': attrs.get('build'),
            'hair_color': attrs.get('hair_color'),
            'facial_hair': attrs.get('facial_hair'),
        }

    return customer_signals

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

        if best_score >= 0.70:
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

        high_quality_count = sum(1 for q in qualities if q >= 0.8)
        medium_quality_count = sum(1 for q in qualities if q >= 0.6)

        has_sufficient_quality = (high_quality_count >= 1 or medium_quality_count >= 3)
        has_sufficient_weight = total_biometric_weight >= 5.0

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
                    best = {'embedding': signals['face_embedding'], 'quality': q}
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

# ─── Threshold Manager ──────────────────────────────────────────────────────

class ThresholdManager:
    """Manages thresholds with calibration support"""

    def __init__(self):
        self.global_thresholds = {
            'link': 0.75,      # Conservative initial
            'pending': 0.60
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

# ─── Event Processing ───────────────────────────────────────────────────────

def process_event(event):
    """Process Frigate event with track-based identification"""
    try:
        # Extract signals
        signals = extract_all_signals_with_quality(event)
        if not signals:
            return

        # Generate track ID
        track_id = event.get('id', str(uuid.uuid4()))

        with _tracks_lock:
            if track_id not in _active_tracks:
                _active_tracks[track_id] = TrackIdentity(track_id)
            track = _active_tracks[track_id]

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
            logger.info(f'Track {track_id[:8]} linked to customer {track.customer_id} (confidence={track.confidence:.3f})')

            # Log visit
            pos_post('/api/customers/identify', {
                'customer_id': track.customer_id,
                'matched_signals': 'track_consensus',
                'confidence_scores': {'track_confidence': track.confidence},
                'camera_source': signals.get('camera'),
            })

        elif track.age() >= 60 and track.has_enrollment_quality():
            if track.confidence < pending_threshold:
                logger.info(f'Track {track_id[:8]} ready for enrollment (age={track.age():.1f}s, quality=ok)')

                # Auto-enroll new customer
                try:
                    # Get next customer number in CUST-XXXX format
                    r = pos_get('/api/customers/max_number')
                    if r and r.get('max_number') is not None:
                        next_number = r['max_number'] + 1
                    else:
                        next_number = 1
                    customer_number_str = f'CUST-{next_number:04d}'

                    # Create customer with auto_enrolled flag
                    new_customer = pos_post('/api/customers', {
                        'name': None,
                        'auto_enrolled': True,
                        'customer_number': customer_number_str,
                        'first_seen': datetime.now().isoformat()
                    })

                    if new_customer and new_customer.get('id'):
                        customer_id = new_customer['id']
                        logger.info(f'Auto-enrolled customer {customer_number_str} (id={customer_id})')

                        # Enroll face if available
                        best_face = track.get_best_signal('face')
                        if best_face and best_face.get('embedding'):
                            pos_post(f'/api/customers/{customer_id}/enroll/face', {
                                'embedding_b64': base64.b64encode(best_face['embedding']).decode(),
                                'quality': best_face.get('quality', 0.0)
                            })
                            logger.info(f'   Face enrolled for customer #{next_number}')

                        # Enroll gait if available
                        best_gait = track.get_best_signal('gait')
                        if best_gait and best_gait.get('features'):
                            pos_post(f'/api/customers/{customer_id}/enroll/gait', {
                                'features_b64': base64.b64encode(best_gait['features']).decode(),
                                'quality': best_gait.get('quality', 0.0)
                            })
                            logger.info(f'   Gait enrolled for customer #{next_number}')

                        # Enroll physical attributes if extracted
                        if signals.get('attributes'):
                            pos_post(f'/api/customers/{customer_id}/attributes', signals['attributes'])
                            logger.info(f'   Attributes enrolled for customer #{next_number}')

                        # Link track to new customer
                        track.customer_id = customer_id
                        track.confidence = 1.0

                        # Refresh customer cache to include new customer
                        refresh_customers()

                        # Log visit
                        pos_post('/api/customers/identify', {
                            'customer_id': customer_id,
                            'matched_signals': 'auto_enrollment',
                            'confidence_scores': {'auto_enroll': 1.0},
                            'camera_source': signals.get('camera'),
                        })

                except Exception as e:
                    logger.error(f'Auto-enrollment failed: {e}')
                    import traceback
                    traceback.print_exc()

        else:
            logger.debug(f'Track {track_id[:8]} pending (age={track.age():.1f}s, confidence={track.confidence:.3f})')

    except Exception as e:
        logger.error(f'Event processing error: {e}')
        import traceback
        traceback.print_exc()

# ─── Frigate Integration ────────────────────────────────────────────────────

_seen_events = set()

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

                    if label != 'person':
                        continue

                    # Active event (no end_time yet) — process every poll to build up track
                    if not end_time:
                        recent_count += 1
                        new_count += 1
                        logger.info(f'Processing active event {eid[:20]} (camera={ev.get("camera")})')
                        threading.Thread(target=process_event, args=(ev,), daemon=True).start()

                    # Ended event within last 60 seconds — process once
                    elif (now - end_time) <= 60:
                        recent_count += 1
                        if eid and eid not in _seen_events:
                            new_count += 1
                            _seen_events.add(eid)
                            if len(_seen_events) > 500:
                                oldest = list(_seen_events)[:100]
                                for o in oldest:
                                    _seen_events.discard(o)
                            logger.info(f'Processing ended event {eid[:20]} (camera={ev.get("camera")})')
                            threading.Thread(target=process_event, args=(ev,), daemon=True).start()

                logger.debug(f'Frigate poll complete: {len(events)} total, {recent_count} recent, {new_count} new')
        except Exception as e:
            logger.warning(f'Frigate poll error: %s', e)
        time_module.sleep(30)

# ─── Webhook Server ─────────────────────────────────────────────────────────

from http.server import HTTPServer, BaseHTTPRequestHandler

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
            if event_type in ('update', 'end') and after.get('label') == 'person':
                threading.Thread(target=process_event, args=(after,), daemon=True).start()
        except Exception as e:
            logger.warning(f'Webhook parse error: {e}')

def run_webhook_server():
    server = HTTPServer(('0.0.0.0', WEBHOOK_PORT), WebhookHandler)
    logger.info(f'Webhook server listening on port {WEBHOOK_PORT}')
    server.serve_forever()

# ─── Entry Point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logger.info('Recognition service v2.0 starting')
    logger.info(f'Weights version: {WEIGHTS_VERSION}')
    logger.info(f'Threshold version: {THRESHOLD_VERSION}')

    pos_login()
    refresh_customers()

    # Background cache refresh
    threading.Thread(target=_cache_refresh_loop, daemon=True).start()

    # Background Frigate poller
    threading.Thread(target=poll_frigate_events, daemon=True).start()

    # Webhook server (blocking)
    run_webhook_server()

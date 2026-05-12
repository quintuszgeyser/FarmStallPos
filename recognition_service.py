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
POS_URL         = os.environ.get('POS_URL',     'https://127.0.0.1:5000')
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
            import os
            from insightface.model_zoo import SCRFD, ArcFaceONNX

            model_dir = os.path.expanduser('~/.insightface/models')
            det_model = os.path.join(model_dir, 'det_10g.onnx')
            rec_model = os.path.join(model_dir, 'w600k_r50.onnx')

            if not os.path.exists(det_model) or not os.path.exists(rec_model):
                logger.error('Face models not found. Run: python download_face_models.py')
                _face_app = None
                return None

            # Initialize detector and recognizer
            detector = SCRFD(model_file=det_model)
            detector.prepare(ctx_id=-1, input_size=(640, 640), det_thresh=0.5)

            recognizer = ArcFaceONNX(model_file=rec_model)
            recognizer.prepare(ctx_id=-1)

            # Wrapper to match expected API
            class FaceApp:
                def __init__(self, det, rec):
                    self.detector = det
                    self.recognizer = rec

                def get(self, img):
                    bboxes, kpss = self.detector.detect(img, input_size=(640, 640))
                    if len(bboxes) == 0 or len(kpss) == 0:
                        return []
                    # Get embedding for first face - ArcFaceONNX needs image + landmarks
                    import cv2
                    from skimage import transform as trans
                    # Align face using landmarks
                    tform = trans.SimilarityTransform()
                    tform.estimate(kpss[0], [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]])
                    face_img = cv2.warpAffine(img, tform.params[0:2, :], (112, 112), borderValue=0.0)
                    # Get embedding - needs numpy array
                    face_img_np = np.array([face_img])
                    emb = self.recognizer.get_feat(face_img_np)[0]
                    face = type('Face', (), {'embedding': emb})()
                    return [face]

            _face_app = FaceApp(detector, recognizer)
            logger.info('InsightFace loaded (SCRFD + ArcFace)')
        except Exception as e:
            logger.warning('Face recognition unavailable: %s', e)
            import traceback
            traceback.print_exc()
            _face_app = None
    return _face_app

def get_pose():
    global _mp_pose, _mp_pose_inst
    if _mp_pose is None:
        try:
            import mediapipe as mp
            import os
            import urllib.request

            # Download pose model if missing
            model_dir = os.path.expanduser('~/.mediapipe/models')
            os.makedirs(model_dir, exist_ok=True)
            model_path = os.path.join(model_dir, 'pose_landmarker_lite.task')

            if not os.path.exists(model_path):
                logger.info('Downloading MediaPipe Pose model (~15MB)...')
                urllib.request.urlretrieve(
                    'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task',
                    model_path
                )

            # MediaPipe v0.10+ uses tasks API
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            # Create pose landmarker
            base_options = python.BaseOptions(model_asset_path=model_path)
            options = vision.PoseLandmarkerOptions(
                base_options=base_options,
                running_mode=vision.RunningMode.IMAGE)
            _mp_pose_inst = vision.PoseLandmarker.create_from_options(options)
            _mp_pose = type('Pose', (), {})()  # Dummy
            logger.info('MediaPipe Pose loaded')
        except Exception as e:
            logger.warning('MediaPipe Pose unavailable: %s. Body recognition disabled.', e)
            _mp_pose_inst = None
    return _mp_pose, _mp_pose_inst

# ─── POS API session ────────────────────────────────────────────────────────
_pos_session = requests.Session()
_pos_session.verify = False  # Disable SSL verification for localhost HTTPS
_pos_logged_in = False

# Suppress InsecureRequestWarning
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
            # fast-plate-ocr now returns PlatePrediction objects
            pred = results[0]
            plate = pred.plate if hasattr(pred, 'plate') else str(pred)
            conf = pred.confidence if hasattr(pred, 'confidence') else 1.0
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
        import mediapipe as mp
        img = cv2.imread(image_path)
        if img is None:
            return None
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_pose, pose_landmarker = get_pose()
        if pose_landmarker is None:
            return None

        # Convert to MediaPipe Image
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = pose_landmarker.detect(mp_image)
        if not result.pose_landmarks or len(result.pose_landmarks) == 0:
            return None
        lm = result.pose_landmarks[0]  # v0.10+ returns list
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

# ─── Physical Attribute Extraction ──────────────────────────────────────────
def extract_physical_attributes(image_path):
    """
    Extracts visual attributes from person image using InsightFace + MediaPipe.
    Returns dict with estimated physical characteristics.
    """
    try:
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            return None

        # Get face analysis from InsightFace (already loaded)
        face_app = get_face_app()
        if not face_app:
            return None

        faces = face_app.detector.detect(img, input_size=(640, 640))
        if len(faces[0]) == 0:
            return None

        face_bbox = faces[0][0]  # First face bounding box
        face_landmarks = faces[1][0]  # Facial keypoints

        attributes = {}

        # 1. Height estimation (from body proportions + MediaPipe)
        mp_pose, pose_landmarker = get_pose()
        if pose_landmarker:
            import mediapipe as mp
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = pose_landmarker.detect(mp_image)

            if result.pose_landmarks and len(result.pose_landmarks) > 0:
                landmarks = result.pose_landmarks[0]

                # Calculate height from head-to-foot ratio
                nose = landmarks[0]
                left_ankle = landmarks[27]
                right_ankle = landmarks[28]

                pixel_height = abs(nose.y - (left_ankle.y + right_ankle.y) / 2)
                # Rough calibration: 1.0 normalized units ≈ 170cm average person
                estimated_height = int(pixel_height * 170 / 1.0)
                attributes['height_cm'] = max(150, min(220, estimated_height))

                # 4. Build/body type (from gait features if available)
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

        # 2. Hair color (from top of face bbox)
        hair_region = img[max(0, int(face_bbox[1] - 50)):int(face_bbox[1]),
                          int(face_bbox[0]):int(face_bbox[2])]
        if hair_region.size > 0:
            avg_color = cv2.mean(hair_region)[:3]  # BGR
            hair_color = classify_hair_color(avg_color)
            attributes['hair_color'] = hair_color

        # 3. Skin tone (from face region)
        face_region = img[int(face_bbox[1]):int(face_bbox[3]),
                          int(face_bbox[0]):int(face_bbox[2])]
        if face_region.size > 0:
            avg_skin = cv2.mean(face_region)[:3]
            skin_tone = classify_skin_tone(avg_skin)
            attributes['skin_tone'] = skin_tone

        # 5. Age range (basic heuristic from face texture)
        face_gray = cv2.cvtColor(face_region, cv2.COLOR_BGR2GRAY)
        texture_variance = np.var(face_gray)

        if texture_variance < 100:
            attributes['age_range'] = '18-25'
        elif texture_variance < 200:
            attributes['age_range'] = '26-35'
        elif texture_variance < 300:
            attributes['age_range'] = '36-50'
        else:
            attributes['age_range'] = '51-65'

        # 6. Gender (basic heuristic)
        face_width = face_bbox[2] - face_bbox[0]
        face_height = face_bbox[3] - face_bbox[1]
        face_ratio = face_width / face_height

        if face_ratio > 0.8:
            attributes['gender'] = 'male'
        else:
            attributes['gender'] = 'female'

        # 7. Glasses detection
        if len(face_landmarks) >= 2:
            left_eye = face_landmarks[0]
            right_eye = face_landmarks[1]
            eye_region_y = int((left_eye[1] + right_eye[1]) / 2)
            eye_region = face_region[max(0, eye_region_y-20):eye_region_y+20, :]

            if eye_region.size > 0:
                eye_brightness = np.mean(cv2.cvtColor(eye_region, cv2.COLOR_BGR2GRAY))
                attributes['wearing_glasses'] = eye_brightness > 150

        # 8. Facial hair
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

        logger.info(f'Extracted attributes: height={attributes.get("height_cm")}cm, '
                    f'hair={attributes.get("hair_color")}, build={attributes.get("build")}')

        return attributes

    except Exception as e:
        logger.warning(f'Attribute extraction error: {e}')
        import traceback
        traceback.print_exc()
    return None

def classify_hair_color(bgr_color):
    """Classify hair color from average BGR values."""
    b, g, r = bgr_color
    brightness = (r + g + b) / 3

    if brightness < 50:
        return 'black'
    elif brightness < 100:
        return 'brown'
    elif brightness > 180:
        if r > g and r > b:
            return 'red'
        else:
            return 'blonde'
    else:
        return 'gray'

def classify_skin_tone(bgr_color):
    """Classify skin tone from average BGR values."""
    b, g, r = bgr_color
    brightness = (r + g + b) / 3

    if brightness < 80:
        return 'dark'
    elif brightness < 120:
        return 'brown'
    elif brightness < 160:
        return 'tan'
    elif brightness < 200:
        return 'medium'
    elif brightness < 230:
        return 'light'
    else:
        return 'very_light'

# ─── Matching (Weighted Multi-Signal Voting) ────────────────────────────────
def identify_customer_weighted(plate=None, face_bytes=None, gait_bytes=None, physical_attrs=None):
    """
    Weighted multi-signal voting using ALL features.
    Returns (customer_id, total_score, feature_breakdown) or (None, 0.0, {})
    """
    from collections import defaultdict

    with _cache_lock:
        customers = list(_customers_cache)

    # Track scores per customer
    customer_scores = defaultdict(lambda: {'total': 0.0, 'features': {}})

    # 1. PLATE MATCHING (weight: 3.0)
    if plate:
        for c in customers:
            if plate in c.get('plates', []):
                customer_scores[c['id']]['total'] += 3.0
                customer_scores[c['id']]['features']['plate'] = 3.0
                break

    # 2. FACE MATCHING (weight: 3.0, scaled by similarity)
    if face_bytes:
        face_emb = np.frombuffer(face_bytes, dtype=np.float32)
        all_faces = pos_get('/api/customers/faces_raw')

        for row in all_faces:
            try:
                stored = np.frombuffer(base64.b64decode(row['embedding_b64']), dtype=np.float32)
                sim = cosine_sim(face_emb, stored)

                if sim >= FACE_THRESHOLD:  # 0.40
                    # Scale from threshold to 1.0 → 0.0 to 3.0 points
                    score = 3.0 * ((sim - FACE_THRESHOLD) / (1.0 - FACE_THRESHOLD))
                    cid = row['customer_id']
                    customer_scores[cid]['total'] += score
                    customer_scores[cid]['features']['face'] = round(score, 2)
            except Exception:
                pass

    # 3. GAIT MATCHING (weight: 2.0, scaled by distance)
    if gait_bytes:
        gait_feat = np.frombuffer(gait_bytes, dtype=np.float32)
        all_gaits = pos_get('/api/customers/gaits_raw')

        for row in all_gaits:
            try:
                stored = np.frombuffer(base64.b64decode(row['features_b64']), dtype=np.float32)
                dist = euclidean_dist(gait_feat, stored)

                if dist <= GAIT_THRESHOLD:  # 0.25
                    # Scale from 0 to threshold → 2.0 to 0.0 points
                    score = 2.0 * (1.0 - dist / GAIT_THRESHOLD)
                    cid = row['customer_id']
                    customer_scores[cid]['total'] += score
                    customer_scores[cid]['features']['gait'] = round(score, 2)
            except Exception:
                pass

    # 4. PHYSICAL ATTRIBUTE MATCHING (weights: 0.3 - 1.0)
    if physical_attrs:
        for c in customers:
            cid = c['id']
            stored_attrs = pos_get(f'/api/customers/{cid}/attributes')

            if not stored_attrs:
                continue

            # Gender (1.0)
            if physical_attrs.get('gender') and physical_attrs['gender'] == stored_attrs.get('gender'):
                customer_scores[cid]['total'] += 1.0
                customer_scores[cid]['features']['gender'] = 1.0

            # Height (1.0, ± 5cm tolerance)
            if physical_attrs.get('height_cm') and stored_attrs.get('height_cm'):
                height_diff = abs(physical_attrs['height_cm'] - stored_attrs['height_cm'])
                if height_diff <= 5:
                    score = 1.0 * (1.0 - height_diff / 5.0)
                    customer_scores[cid]['total'] += score
                    customer_scores[cid]['features']['height'] = round(score, 2)

            # Hair color (0.8)
            if physical_attrs.get('hair_color') and physical_attrs['hair_color'] == stored_attrs.get('hair_color'):
                customer_scores[cid]['total'] += 0.8
                customer_scores[cid]['features']['hair_color'] = 0.8

            # Skin tone (0.8)
            if physical_attrs.get('skin_tone') and physical_attrs['skin_tone'] == stored_attrs.get('skin_tone'):
                customer_scores[cid]['total'] += 0.8
                customer_scores[cid]['features']['skin_tone'] = 0.8

            # Build (0.6)
            if physical_attrs.get('build') and physical_attrs['build'] == stored_attrs.get('build'):
                customer_scores[cid]['total'] += 0.6
                customer_scores[cid]['features']['build'] = 0.6

            # Age range (0.5, exact or adjacent)
            if physical_attrs.get('age_range') and stored_attrs.get('age_range'):
                age_ranges = ['18-25', '26-35', '36-50', '51-65', '65+']
                try:
                    idx_new = age_ranges.index(physical_attrs['age_range'])
                    idx_stored = age_ranges.index(stored_attrs['age_range'])
                    age_diff = abs(idx_new - idx_stored)

                    if age_diff == 0:
                        customer_scores[cid]['total'] += 0.5
                        customer_scores[cid]['features']['age_range'] = 0.5
                    elif age_diff == 1:
                        customer_scores[cid]['total'] += 0.25
                        customer_scores[cid]['features']['age_range'] = 0.25
                except ValueError:
                    pass

            # Glasses (0.3)
            if physical_attrs.get('wearing_glasses') is not None and stored_attrs.get('wearing_glasses') is not None:
                if physical_attrs['wearing_glasses'] == stored_attrs['wearing_glasses']:
                    customer_scores[cid]['total'] += 0.3
                    customer_scores[cid]['features']['glasses'] = 0.3

            # Facial hair (0.3)
            if physical_attrs.get('facial_hair') and physical_attrs['facial_hair'] == stored_attrs.get('facial_hair'):
                customer_scores[cid]['total'] += 0.3
                customer_scores[cid]['features']['facial_hair'] = 0.3

    # Find best match
    if not customer_scores:
        return None, 0.0, {}

    best_cid = max(customer_scores.keys(), key=lambda cid: customer_scores[cid]['total'])
    best_score = customer_scores[best_cid]['total']

    # Threshold: 5.0 points required for identification
    if best_score >= 5.0:
        return best_cid, best_score, customer_scores[best_cid]['features']

    return None, best_score, customer_scores[best_cid]['features']

# ─── Legacy Matching (Keep for backward compatibility) ──────────────────────
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
        plate_str = face_bytes = gait_bytes = physical_attrs = None
        conf = None

        if label == 'car' and is_outdoor:
            plate_str, conf = run_anpr(snapshot_path)
            if plate_str:
                logger.info('Plate detected: %s (%.2f)', plate_str, conf or 0)

        if label == 'person':
            face_bytes = run_face(snapshot_path)
            gait_bytes = run_gait(snapshot_path)
            physical_attrs = extract_physical_attributes(snapshot_path)

        # Use weighted voting system
        if plate_str or face_bytes or gait_bytes:
            cid, score, features = identify_customer_weighted(
                plate=plate_str,
                face_bytes=face_bytes,
                gait_bytes=gait_bytes,
                physical_attrs=physical_attrs
            )

            # Log plate detection regardless of match
            if plate_str:
                pos_post('/api/customers/log_plate', {
                    'plate_number': plate_str,
                    'confidence': conf,
                    'customer_id': cid,
                    'matched': cid is not None,
                    'snapshot_path': snapshot_path,
                    'camera_source': camera,
                })

            if cid:
                # Customer identified
                logger.info(f'Customer {cid} identified (score={score:.2f}, features={features})')

                # Log visit
                pos_post('/api/customers/identify', {
                    'customer_id': cid,
                    'matched_signals': ','.join(features.keys()),
                    'confidence_scores': features,
                    'camera_source': camera,
                })

                # Store/update physical attributes if detected
                if physical_attrs:
                    pos_post(f'/api/customers/{cid}/attributes', {
                        **physical_attrs,
                        'camera_source': camera
                    })

                # Check if detected at till (for purchase linking)
                is_till = 'till' in camera.lower() or 'checkout' in camera.lower() or 'counter' in camera.lower()
                if is_till:
                    pos_post('/api/till/detect', {
                        'customer_id': cid,
                        'camera_source': camera
                    })
                    logger.info(f'Customer {cid} detected at till')

                refresh_customers()
            else:
                # Not identified - check if we should auto-enroll
                logger.info(f'No identification (score={score:.2f}, features={features})')

                # Auto-enrollment logic: need 2+ biometric signals OR 1 signal + strong physical profile
                signal_count = sum([
                    1 if plate_str else 0,
                    1 if face_bytes else 0,
                    1 if gait_bytes else 0
                ])

                strong_physical = (
                    physical_attrs and
                    physical_attrs.get('gender') and
                    physical_attrs.get('height_cm') and
                    physical_attrs.get('hair_color')
                )

                if signal_count >= 2 or (signal_count == 1 and strong_physical):
                    # Auto-enroll new customer
                    logger.info(f'Auto-enrolling new customer (signals={signal_count}, physical={strong_physical})')

                    # Get next customer number
                    max_num = pos_get('/api/customers/max_number')
                    customer_number = f"CUST-{(max_num + 1):04d}"

                    # Create customer
                    customer_data = {
                        'name': None,
                        'auto_enrolled': True,
                        'customer_number': customer_number,
                        'first_seen': datetime.utcnow().isoformat()
                    }

                    new_customer = pos_post('/api/customers', customer_data)
                    if new_customer and new_customer.get('id'):
                        new_cid = new_customer['id']
                        logger.info(f'Created customer {customer_number} (ID={new_cid})')

                        # Enroll available signals
                        if plate_str:
                            pos_post(f'/api/customers/{new_cid}/enroll/plate', {
                                'plate_number': plate_str
                            })
                            logger.info(f'  Enrolled plate: {plate_str}')

                        if face_bytes:
                            face_b64 = base64.b64encode(face_bytes).decode()
                            pos_post(f'/api/customers/{new_cid}/enroll/face', {
                                'embedding_b64': face_b64
                            })
                            logger.info(f'  Enrolled face')

                        if gait_bytes:
                            gait_b64 = base64.b64encode(gait_bytes).decode()
                            pos_post(f'/api/customers/{new_cid}/enroll/gait', {
                                'features_b64': gait_b64
                            })
                            logger.info(f'  Enrolled gait')

                        # Store physical attributes
                        if physical_attrs:
                            pos_post(f'/api/customers/{new_cid}/attributes', {
                                **physical_attrs,
                                'camera_source': camera
                            })
                            logger.info(f'  Stored physical attributes: {physical_attrs.keys()}')

                        # Refresh customer cache
                        refresh_customers()
                    else:
                        logger.warning(f'Failed to create customer: {new_customer}')
                else:
                    logger.info(f'Insufficient signals for auto-enrollment (signals={signal_count}, physical={strong_physical})')

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

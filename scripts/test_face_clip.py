"""
Test face extraction with a Frigate clip (video or image).
Usage: python test_face_clip.py <clip_path>
"""
import sys
import cv2
import numpy as np
from insightface.model_zoo import get_model
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def test_face_extraction(clip_path):
    logger.info(f'Testing face extraction on: {clip_path}')

    # Frigate clips often have no extension - try adding .mp4
    import os
    if not os.path.exists(clip_path):
        logger.error(f'File not found: {clip_path}')
        return

    # Try to open as video first
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        # Try with .mp4 extension
        clip_with_ext = clip_path + '.mp4'
        logger.info(f'Trying with .mp4 extension: {clip_with_ext}')
        cap = cv2.VideoCapture(clip_with_ext)

    if not cap.isOpened():
        # Try to read as image instead
        logger.info('Not a video, trying to read as image...')
        img = cv2.imread(clip_path)
        if img is None:
            logger.error(f'Failed to open as video or image: {clip_path}')
            return
        logger.info(f'Image loaded: shape={img.shape}, dtype={img.dtype}')
    else:
        # Get middle frame from video
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_count == 0:
            logger.error('No frames in video')
            cap.release()
            return

        mid_frame = frame_count // 2
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid_frame)
        ret, img = cap.read()
        cap.release()

        if not ret or img is None:
            logger.error('Failed to read frame')
            return

        logger.info(f'Frame loaded: shape={img.shape}, dtype={img.dtype}')

    # Initialize face detector
    logger.info('Loading face detector (SCRFD)...')
    detector = get_model('C:/Users/Quintusz/.insightface/models/det_10g.onnx')
    detector.prepare(ctx_id=0, input_size=(640, 640))

    # Initialize face recognizer
    logger.info('Loading face recognizer (ArcFace)...')
    recognizer = get_model('C:/Users/Quintusz/.insightface/models/w600k_r50.onnx')
    recognizer.prepare(ctx_id=0)

    # Detect faces
    logger.info('Detecting faces...')
    bboxes, kpss = detector.detect(img, input_size=(640, 640))

    if len(bboxes) == 0:
        logger.warning('No faces detected')
        return

    logger.info(f'Detected {len(bboxes)} face(s)')

    # Extract embedding for first face
    from skimage import transform as trans

    # Ensure input image is BGR (3 channels)
    logger.info(f'[DEBUG] Input img shape: {img.shape}, dtype: {img.dtype}')
    if len(img.shape) == 2:  # Grayscale
        logger.warning('Input is grayscale, converting to BGR...')
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        logger.info(f'[DEBUG] After GRAY2BGR: {img.shape}')
    elif img.shape[2] == 4:  # RGBA
        logger.warning('Input is RGBA, converting to BGR...')
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        logger.info(f'[DEBUG] After BGRA2BGR: {img.shape}')

    logger.info('Aligning face...')
    tform = trans.SimilarityTransform()
    tform.estimate(kpss[0], [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]])

    face_img = cv2.warpAffine(img, tform.params[0:2, :], (112, 112), borderValue=0.0)
    logger.info(f'[DEBUG] After warpAffine: shape={face_img.shape}, dtype={face_img.dtype}')

    # Check channels
    if len(face_img.shape) == 2:
        logger.warning('Face image is grayscale after warpAffine, converting to BGR...')
        face_img = cv2.cvtColor(face_img, cv2.COLOR_GRAY2BGR)
        logger.info(f'[DEBUG] After GRAY2BGR: shape={face_img.shape}, dtype={face_img.dtype}')
    elif len(face_img.shape) == 3 and face_img.shape[2] == 4:
        logger.warning('Face image is RGBA after warpAffine, converting to BGR...')
        face_img = cv2.cvtColor(face_img, cv2.COLOR_BGRA2BGR)
        logger.info(f'[DEBUG] After BGRA2BGR: shape={face_img.shape}, dtype={face_img.dtype}')
    elif len(face_img.shape) == 3 and face_img.shape[2] != 3:
        logger.error(f'Unexpected channel count: {face_img.shape[2]}')
        if face_img.shape[2] == 1:
            logger.warning('Trying to squeeze and convert single channel...')
            face_img = cv2.cvtColor(face_img[:,:,0], cv2.COLOR_GRAY2BGR)
            logger.info(f'[DEBUG] After squeeze+GRAY2BGR: shape={face_img.shape}')
        else:
            logger.error(f'Cannot handle {face_img.shape[2]} channels')
            return

    logger.info(f'[DEBUG] Final face_img shape before recognizer: {face_img.shape}, dtype={face_img.dtype}')

    # Try different input formats to find what works
    logger.info('Extracting face embedding...')

    # Try 1: Pass face_img directly (H, W, C)
    logger.info(f'[ATTEMPT 1] Passing face_img directly: {face_img.shape}')
    try:
        emb = recognizer.get_feat(face_img)[0]
        logger.info(f'SUCCESS with direct face_img!')
        logger.info(f'Embedding shape: {emb.shape}')
        logger.info(f'Embedding sample: {emb[:10]}')
        return emb
    except Exception as e:
        logger.warning(f'Attempt 1 failed: {e}')

    # Try 2: Add batch dimension (N, H, W, C)
    face_img_batch = np.array([face_img])  # (1, 112, 112, 3)
    logger.info(f'[ATTEMPT 2] With batch dimension: {face_img_batch.shape}')
    try:
        emb = recognizer.get_feat(face_img_batch)[0]
        logger.info(f'SUCCESS with batch dimension!')
        logger.info(f'Embedding shape: {emb.shape}')
        logger.info(f'Embedding sample: {emb[:10]}')
        return emb
    except Exception as e:
        logger.warning(f'Attempt 2 failed: {e}')

    # Try 3: Transpose to channels-first (N, C, H, W)
    face_img_transposed = np.transpose(face_img, (2, 0, 1))  # (3, 112, 112)
    face_img_np = np.array([face_img_transposed])  # (1, 3, 112, 112)
    logger.info(f'[ATTEMPT 3] Channels-first: {face_img_np.shape}')
    try:
        emb = recognizer.get_feat(face_img_np)[0]
        logger.info(f'SUCCESS! Embedding shape: {emb.shape}')
        logger.info(f'Embedding sample: {emb[:10]}')
        return emb
    except AssertionError as e:
        logger.error(f'FAILED with AssertionError: {e}')
        logger.error(f'Final shapes - face_img: {face_img.shape}, face_img_np: {face_img_np.shape}')
        import traceback
        traceback.print_exc()
    except Exception as e:
        logger.error(f'FAILED: {e}')
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python test_face_clip.py <clip_path>')
        print('Example: python test_face_clip.py D:/frigate/storage/clips/indoor-1778606425.54264-lh5hd0')
        sys.exit(1)

    test_face_extraction(sys.argv[1])

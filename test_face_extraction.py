"""
Test face extraction with a Frigate snapshot or any image file.
Usage: python test_face_extraction.py <image_path>
"""
import sys
import cv2
import numpy as np
from insightface.app import FaceAnalysis
from insightface.model_zoo import get_model
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def test_face_extraction(image_path):
    logger.info(f'Testing face extraction on: {image_path}')

    # Load image
    img = cv2.imread(image_path)
    if img is None:
        logger.error(f'Failed to load image: {image_path}')
        return

    logger.info(f'Image loaded: shape={img.shape}, dtype={img.dtype}')

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

    logger.info('Aligning face...')
    tform = trans.SimilarityTransform()
    tform.estimate(kpss[0], [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]])

    face_img = cv2.warpAffine(img, tform.params[0:2, :], (112, 112), borderValue=0.0)
    logger.info(f'[DEBUG] After warpAffine: shape={face_img.shape}, dtype={face_img.dtype}')

    # Check channels
    if len(face_img.shape) == 2:
        logger.warning('Face image is grayscale, converting to BGR...')
        face_img = cv2.cvtColor(face_img, cv2.COLOR_GRAY2BGR)
        logger.info(f'[DEBUG] After GRAY2BGR: shape={face_img.shape}, dtype={face_img.dtype}')
    elif len(face_img.shape) == 3 and face_img.shape[2] == 4:
        logger.warning('Face image is RGBA, converting to BGR...')
        face_img = cv2.cvtColor(face_img, cv2.COLOR_BGRA2BGR)
        logger.info(f'[DEBUG] After BGRA2BGR: shape={face_img.shape}, dtype={face_img.dtype}')
    elif len(face_img.shape) == 3 and face_img.shape[2] != 3:
        logger.error(f'Unexpected channel count: {face_img.shape[2]}')
        return

    logger.info(f'[DEBUG] Final face_img shape before recognizer: {face_img.shape}, dtype={face_img.dtype}')

    # Prepare for recognizer
    face_img_np = np.array([face_img])
    logger.info(f'[DEBUG] face_img_np shape: {face_img_np.shape}, dtype={face_img_np.dtype}')

    # Extract embedding
    logger.info('Extracting face embedding...')
    try:
        emb = recognizer.get_feat(face_img_np)[0]
        logger.info(f'SUCCESS! Embedding shape: {emb.shape}')
        logger.info(f'Embedding sample: {emb[:10]}')
    except Exception as e:
        logger.error(f'FAILED: {e}')
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python test_face_extraction.py <image_path>')
        print('Example: python test_face_extraction.py snapshots/test.jpg')
        sys.exit(1)

    test_face_extraction(sys.argv[1])

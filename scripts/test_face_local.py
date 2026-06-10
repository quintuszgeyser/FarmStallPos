"""
Test face extraction locally using the recognition_service module.
Usage: python test_face_local.py <clip_path>
"""
import sys
import cv2

# Mock the POS API calls since we're testing offline
def mock_pos_get(path):
    return []

def mock_pos_post(path, data):
    return {'ok': True}

# Patch the recognition_service functions
import recognition_service
recognition_service.pos_get = mock_pos_get
recognition_service.pos_post = mock_pos_post

def test_face_extraction(clip_path):
    print(f'Testing face extraction on: {clip_path}')

    # Try to open as video first
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        print(f'Failed to open clip: {clip_path}')
        return

    # Get middle frame from video
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if frame_count == 0:
        print('No frames in video')
        cap.release()
        return

    print(f'Video has {frame_count} frames, getting middle frame...')
    mid_frame = frame_count // 2
    cap.set(cv2.CAP_PROP_POS_FRAMES, mid_frame)
    ret, img = cap.read()
    cap.release()

    if not ret or img is None:
        print('Failed to read frame')
        return

    print(f'Frame loaded: shape={img.shape}, dtype={img.dtype}')

    # Save frame as temp image
    temp_path = 'temp_test_frame.jpg'
    cv2.imwrite(temp_path, img)
    print(f'Saved temp frame to: {temp_path}')

    # Use recognition_service's run_face function
    print('Running face extraction...')
    try:
        face_bytes = recognition_service.run_face(temp_path)
        if face_bytes:
            import numpy as np
            embedding = np.frombuffer(face_bytes, dtype=np.float32)
            print(f'SUCCESS! Extracted face embedding')
            print(f'Embedding shape: {embedding.shape}')
            print(f'Embedding sample: {embedding[:10]}')
        else:
            print('No face detected or extraction failed')
    except Exception as e:
        print(f'FAILED: {e}')
        import traceback
        traceback.print_exc()
    finally:
        import os
        if os.path.exists(temp_path):
            os.remove(temp_path)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python test_face_local.py <clip_path>')
        print('Example: python test_face_local.py C:/Users/CP368103/Downloads/indoor_*.mp4')
        sys.exit(1)

    test_face_extraction(sys.argv[1])

"""Test SCRFD and ArcFaceONNX directly"""
from insightface.model_zoo import SCRFD, ArcFaceONNX
import traceback
import os

print("Testing direct model initialization:\n")

# Test SCRFD (face detector)
print("1. Testing SCRFD face detector...")
try:
    detector = SCRFD(model_file=None)
    print("  ✓ SCRFD created")
    print(f"  Type: {type(detector)}")
    print(f"  Dir: {[x for x in dir(detector) if not x.startswith('_')]}")
except Exception as e:
    print(f"  ✗ Failed: {e}")
    traceback.print_exc()

print()

# Test ArcFaceONNX (face recognizer)
print("2. Testing ArcFaceONNX recognizer...")
try:
    recognizer = ArcFaceONNX(model_file=None)
    print("  ✓ ArcFaceONNX created")
    print(f"  Type: {type(recognizer)}")
    print(f"  Dir: {[x for x in dir(recognizer) if not x.startswith('_')]}")
except Exception as e:
    print(f"  ✗ Failed: {e}")
    traceback.print_exc()

print("\n3. Checking what model files would be needed:")
model_dir = os.path.expanduser('~/.insightface/models')
print(f"  Model directory: {model_dir}")
print(f"  Exists: {os.path.exists(model_dir)}")

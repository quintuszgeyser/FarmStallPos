"""Test FaceAnalysis initialization with different model packs"""
from insightface.app import FaceAnalysis
import traceback

# Common model pack names for v0.2.1
model_packs = [
    'antelopev2',
    'antelope',
    'buffalo_l',
    'buffalo_sc',
]

print("Testing FaceAnalysis initialization:\n")
for pack in model_packs:
    try:
        print(f"Trying '{pack}'...")
        app = FaceAnalysis(name=pack)
        print(f"  ✓ Created FaceAnalysis(name='{pack}')")
        print(f"  Models: {app.models.keys() if hasattr(app, 'models') else 'N/A'}")

        # Try to prepare
        try:
            app.prepare(ctx_id=-1, nms=0.4)
            print(f"  ✓ Prepared successfully!")
            break  # Success!
        except Exception as e:
            print(f"  ✗ prepare() failed: {e}")
    except Exception as e:
        print(f"  ✗ Failed: {e}")
    print()

"""Download insightface ONNX models manually"""
import os
import urllib.request
import zipfile

model_dir = os.path.expanduser('~/.insightface/models/buffalo_l')
os.makedirs(model_dir, exist_ok=True)

print(f"Downloading models to: {model_dir}\n")

# Buffalo_l model pack from insightface releases
models_url = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
zip_path = os.path.join(os.path.dirname(model_dir), 'buffalo_l.zip')

try:
    print("Downloading buffalo_l.zip (~100MB)...")
    urllib.request.urlretrieve(models_url, zip_path)
    print("✓ Downloaded")

    print("\nExtracting models...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(os.path.dirname(model_dir))
    print("✓ Extracted")

    print(f"\nModel files in {model_dir}:")
    for f in os.listdir(model_dir):
        size_mb = os.path.getsize(os.path.join(model_dir, f)) / (1024*1024)
        print(f"  - {f} ({size_mb:.1f} MB)")

    # Clean up zip
    os.remove(zip_path)
    print("\n✓ Models ready!")

except Exception as e:
    print(f"\n✗ Error: {e}")
    import traceback
    traceback.print_exc()

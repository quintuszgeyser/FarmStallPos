"""Download insightface ONNX models manually"""
import os
import urllib.request
import zipfile

model_dir = os.path.expanduser('~/.insightface/models/buffalo_l')
os.makedirs(model_dir, exist_ok=True)

print("=" * 80)
print("InsightFace Model Download")
print("=" * 80)
print(f"\nModel directory: {model_dir}\n")

# Check if models already exist
required_files = ['det_10g.onnx', 'genderage.onnx', 'w600k_r50.onnx']
existing = [f for f in required_files if os.path.exists(os.path.join(model_dir, f))]

if len(existing) == len(required_files):
    print("✓ All required models already exist:")
    for f in os.listdir(model_dir):
        size_mb = os.path.getsize(os.path.join(model_dir, f)) / (1024*1024)
        print(f"  - {f} ({size_mb:.1f} MB)")
    print("\n✓ No download needed!")
    exit(0)

print(f"Missing models: {len(required_files) - len(existing)}/{len(required_files)}")
print("Downloading buffalo_l model pack (~100MB)...\n")

# Buffalo_l model pack from insightface releases
models_url = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
zip_path = os.path.join(os.path.dirname(model_dir), 'buffalo_l.zip')

try:
    # Use urllib with proxy detection
    print(f"Downloading from: {models_url}")
    print("This may take a few minutes...")

    def progress_hook(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            percent = min(100, downloaded * 100 / total_size)
            mb_downloaded = downloaded / (1024*1024)
            mb_total = total_size / (1024*1024)
            print(f"\r  Progress: {percent:.1f}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)", end='')

    urllib.request.urlretrieve(models_url, zip_path, reporthook=progress_hook)
    print("\n✓ Downloaded")

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
    print("\nNext step: Restart FarmPOS-Recognition service")
    print("  PowerShell: Restart-Service FarmPOS-Recognition")

except urllib.error.URLError as e:
    print(f"\n✗ Network error: {e}")
    print("\nTroubleshooting:")
    print("  1. Check internet connection")
    print("  2. If behind corporate proxy, configure proxy environment variables:")
    print("     $env:HTTP_PROXY = 'http://proxy.company.com:8080'")
    print("     $env:HTTPS_PROXY = 'http://proxy.company.com:8080'")
    print("  3. Or download manually from:")
    print(f"     {models_url}")
    print(f"     Extract to: {model_dir}")

except Exception as e:
    print(f"\n✗ Error: {e}")
    import traceback
    traceback.print_exc()

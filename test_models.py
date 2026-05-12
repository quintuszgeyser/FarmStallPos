"""Test available model names"""
from insightface.model_zoo import get_model
import os

# Try common model names from insightface v0.2.1
model_names = [
    'arcface_r100_v1',
    'retinaface_r50_v1',
    'retinaface_mnet025_v1',
    'retinaface_mnet025_v2',
    'buffalo_l',
    'buffalo_m',
    'buffalo_s',
    'buffalo_sc',
]

print("Testing model availability:\n")
for name in model_names:
    try:
        model = get_model(name)
        if model:
            print(f"✓ {name}: {type(model).__name__}")
        else:
            print(f"✗ {name}: returned None")
    except Exception as e:
        print(f"✗ {name}: {type(e).__name__} - {e}")

print("\n\nChecking model directory:")
model_dir = os.path.expanduser('~/.insightface/models')
if os.path.exists(model_dir):
    print(f"Contents of {model_dir}:")
    for item in os.listdir(model_dir):
        print(f"  - {item}")
else:
    print(f"{model_dir} does not exist yet")

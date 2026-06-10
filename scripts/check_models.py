"""Check what models were extracted"""
import os

model_base = os.path.expanduser('~/.insightface/models')
print(f"Checking: {model_base}\n")

if os.path.exists(model_base):
    for root, dirs, files in os.walk(model_base):
        level = root.replace(model_base, '').count(os.sep)
        indent = ' ' * 2 * level
        print(f'{indent}{os.path.basename(root)}/')
        subindent = ' ' * 2 * (level + 1)
        for file in files:
            filepath = os.path.join(root, file)
            size_mb = os.path.getsize(filepath) / (1024*1024)
            print(f'{subindent}{file} ({size_mb:.1f} MB)')
else:
    print(f"Directory does not exist: {model_base}")

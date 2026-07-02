"""
One-time migration: generate _thumb (64px) and _small (300px) variants
for all existing product images that don't have them yet.

Run inside the container:
    docker exec farmpos-app python reprocess_product_images.py
"""
import os, sys, re
from PIL import Image as _PIL, ImageOps, ImageEnhance

IMG_DIR = os.path.join(os.path.dirname(__file__), 'static', 'product_images')
SIZES   = [(64, '_thumb'), (300, '_small'), (800, '')]


def needs_processing(base):
    return not all(
        os.path.exists(os.path.join(IMG_DIR, f'{base}{s}.jpg'))
        for _, s in SIZES
    )


def process(base):
    src = os.path.join(IMG_DIR, f'{base}.jpg')
    try:
        img = _PIL.open(src)
        img = ImageOps.exif_transpose(img).convert('RGB')
    except Exception as e:
        print(f'  ERROR opening {base}.jpg: {e}')
        return False

    for dim, suffix in SIZES:
        dest = os.path.join(IMG_DIR, f'{base}{suffix}.jpg')
        if os.path.exists(dest) and suffix:
            continue  # already exists
        if suffix:
            resized = ImageOps.fit(img, (dim, dim), method=_PIL.LANCZOS)
        else:
            resized = img.copy()
            resized.thumbnail((dim, dim), _PIL.LANCZOS)
        resized = ImageEnhance.Sharpness(resized).enhance(1.2)
        tmp = dest + '.tmp'
        resized.save(tmp, 'JPEG', quality=82, optimize=True, progressive=True)
        os.replace(tmp, dest)
    return True


if not os.path.isdir(IMG_DIR):
    print(f'No image directory found at {IMG_DIR} - nothing to do.')
    sys.exit(0)

# Find all base images (e.g. 16_abc12345.jpg) - exclude _thumb/_small variants
base_re = re.compile(r'^(\d+_[a-f0-9]+)\.jpg$')
bases = [m.group(1) for f in os.listdir(IMG_DIR) if (m := base_re.match(f))]

print(f'Found {len(bases)} base image(s) in {IMG_DIR}')
done = skipped = errors = 0

for base in sorted(bases):
    if not needs_processing(base):
        print(f'  SKIP {base} (all variants exist)')
        skipped += 1
        continue
    print(f'  PROCESS {base}…', end=' ')
    if process(base):
        print('OK')
        done += 1
    else:
        errors += 1

print(f'\nDone: {done} processed, {skipped} skipped, {errors} errors')

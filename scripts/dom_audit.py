import re, sys, os

base = os.path.dirname(os.path.dirname(__file__))
js   = open(os.path.join(base, 'static', 'main.js'),        encoding='utf-8').read()
html = open(os.path.join(base, 'templates', 'index.html'),  encoding='utf-8').read()

js_ids  = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js))
js_ids |= set(re.findall(r"querySelector\(['\"]#([a-zA-Z0-9_-]+)['\"]\)", js))
html_ids = set(re.findall(r'id=["\']([^"\'> ]+)["\']', html))

missing = sorted(js_ids - html_ids)
print(f"JS references {len(js_ids)} IDs, HTML defines {len(html_ids)} IDs")
print(f"\nIn JS but NOT in HTML - {len(missing)} mismatches:")
for m in missing:
    print(f"  MISSING: {m}")
if not missing:
    print("  (none - clean!)")

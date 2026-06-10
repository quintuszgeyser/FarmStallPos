import re, os
from collections import Counter

base = os.path.dirname(os.path.dirname(__file__))
c = open(os.path.join(base, 'templates', 'index.html'), encoding='utf-8').read()

vals = re.findall(r'style=["\'](.*?)["\']', c)
print("Repeated inline style values (count > 1):")
for v, n in Counter(vals).most_common(30):
    if n > 1:
        print(f"  {n:3}x  {v}")

# Count comment blocks
comment_blocks = re.findall(r'<!--.*?-->', c, re.DOTALL)
large = [b for b in comment_blocks if len(b) > 200]
print(f"\nTotal comment blocks: {len(comment_blocks)}")
print(f"Large blocks (>200 chars): {len(large)}")
for b in large[:5]:
    print(f"  [{len(b)} chars] {b[:80].strip()}...")

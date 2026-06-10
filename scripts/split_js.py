"""
Split main.js into ES modules.

Strategy:
- Parse section boundaries by the ═══ divider pairs.
- Each section becomes a module file under static/modules/<name>.js
- main.js keeps: STATE, api(), toast(), show()/hide(), displayQty/Cost,
  _globalMarkupPct, the inactivity timer, and the DOMContentLoaded init block.
- Every module exports its functions and imports { STATE, api, toast, show, hide }
  from '../main.js'.
- index.html gets a single <script type="module" src="/static/main.js">.
"""

import re, os, sys

BASE    = os.path.dirname(os.path.dirname(__file__))
JS_PATH = os.path.join(BASE, 'static', 'main.js')
MOD_DIR = os.path.join(BASE, 'static', 'modules')
os.makedirs(MOD_DIR, exist_ok=True)

src = open(JS_PATH, encoding='utf-8').read()
lines = src.splitlines(keepends=True)

DIVIDER = '═'   # ═

# ── Find section boundaries ────────────────────────────────────────────────
sections = []   # [(name, start_line_0idx, end_line_0idx_exclusive)]
i = 0
while i < len(lines):
    if DIVIDER in lines[i] and i + 1 < len(lines) and DIVIDER in lines[i + 1]:
        # The name line sits just before the first divider
        name_line = lines[i - 1].strip().lstrip('/ ').strip() if i > 0 else 'UNKNOWN'
        start = i - 1  # include the name comment
        # find next section start or EOF
        j = i + 2
        while j < len(lines):
            if DIVIDER in lines[j] and j + 1 < len(lines) and DIVIDER in lines[j + 1]:
                break
            j += 1
        end = j - 1 if j < len(lines) else len(lines)
        sections.append((name_line, start, end))
        i = j
    else:
        i += 1

# ── Determine which sections go into modules ───────────────────────────────
# The first 3 sections (STATE, UNIT SYSTEM, HELPERS) stay in main.js.
CORE_SECTIONS = {'STATE', 'UNIT SYSTEM', 'HELPERS', 'VISIBILITY & AUTH', 'APP INIT'}

# Slug a section name to a filename
def slug(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')

IMPORT_LINE = "import { STATE, api, toast, show, hide, displayQty, displayCost } from '../main.js';\n"

module_files = []   # [(module_name, filename)] in order

for name, start, end in sections:
    body = ''.join(lines[start:end])
    s = slug(name)
    if not s:
        continue
    filename = f'{s}.js'
    filepath = os.path.join(MOD_DIR, filename)
    module_text = f'// Module: {name}\n{IMPORT_LINE}\n{body}\n'
    open(filepath, 'w', encoding='utf-8').write(module_text)
    module_files.append((name, filename))
    print(f'  wrote modules/{filename}  ({end-start} lines)  [{name}]')

# ── Rebuild main.js ────────────────────────────────────────────────────────
# Keep the preamble (before first section) + core sections + dynamic imports for all modules.
if sections:
    preamble_end = sections[0][1]
    preamble = ''.join(lines[:preamble_end])
else:
    preamble = src

core_body = []
for name, start, end in sections:
    if any(c in name.upper() for c in CORE_SECTIONS):
        core_body.append(''.join(lines[start:end]))

dynamic_imports = '\n'.join(
    f"import './modules/{fn}';"
    for _, fn in module_files
)

new_main = (
    preamble +
    '\n'.join(core_body) +
    '\n\n// ─── Module imports ────────────────────────────────────\n' +
    dynamic_imports + '\n'
)

open(JS_PATH, 'w', encoding='utf-8').write(new_main)
print(f'\nmain.js rewritten: {len(new_main.splitlines())} lines ({len(module_files)} modules imported)')
print(f'Modules directory: {MOD_DIR}')

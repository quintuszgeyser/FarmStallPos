#!/usr/bin/env python3
"""
Farm POS DB integrity check - run after every migration to confirm schema and row counts.

Usage:
    python scripts/db_integrity.py [database_url]

    database_url defaults to DATABASE_URL env var, then
    postgresql://farmstall:FarmStall@localhost:5432/farm_pos

Saves a baseline snapshot to scripts/.db_baseline.json on first run.
On subsequent runs, compares counts against baseline and warns on unexpected drops.

Exit code 0 = schema intact. Non-zero = missing tables or catastrophic row count drop.
"""

import os
import sys
import json

try:
    from sqlalchemy import create_engine, text, inspect as sa_inspect
except ImportError:
    print('ERROR: sqlalchemy not installed. Run: pip install sqlalchemy psycopg')
    sys.exit(1)

DB_URL = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.getenv('DATABASE_URL', 'postgresql://farmstall:FarmStall@localhost:5432/farm_pos')
)

# Rewrite postgres:// → postgresql+psycopg://
if DB_URL.startswith('postgres://'):
    DB_URL = DB_URL.replace('postgres://', 'postgresql+psycopg://', 1)
elif DB_URL.startswith('postgresql://') and '+psycopg://' not in DB_URL:
    DB_URL = 'postgresql+psycopg://' + DB_URL.split('://', 1)[1]

REQUIRED_TABLES = [
    'users',
    'products',
    'product_images',
    'sales',
    'purchases',
    'stock_batches',
    'stock_consumption',
    'stock_adjustments',
    'recipe_lines',
    'suppliers',
    'customers',
    'customer_faces',
    'customer_plates',
    'customer_gaits',
    'customer_visits',
    'plate_detections',
    'kitchen_orders',
    'invoices',
    'special_lines',
    'specials',
    'settings',
    'user_sessions',
]

BASELINE_FILE = os.path.join(os.path.dirname(__file__), '.db_baseline.json')

failures = []

def section(title):
    print(f'\n── {title}')

try:
    engine = create_engine(DB_URL)
    inspector = sa_inspect(engine)
except Exception as e:
    print(f'ERROR: Cannot connect to database: {e}')
    sys.exit(1)

section('Schema check')
existing = set(inspector.get_table_names())
for table in REQUIRED_TABLES:
    if table in existing:
        print(f'  ✓  {table}')
    else:
        print(f'  ✗  {table}  MISSING')
        failures.append(f'Missing table: {table}')

section('Row counts')
counts = {}
with engine.connect() as conn:
    for table in REQUIRED_TABLES:
        if table not in existing:
            counts[table] = None
            continue
        try:
            n = conn.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar()
            counts[table] = n
            print(f'  {table:30s}  {n:>8} rows')
        except Exception as e:
            print(f'  ✗  {table}: query failed - {e}')
            failures.append(f'Count query failed: {table}')

section('Baseline comparison')
if os.path.exists(BASELINE_FILE):
    with open(BASELINE_FILE) as f:
        baseline = json.load(f)
    for table, current in counts.items():
        if current is None:
            continue
        prev = baseline.get(table)
        if prev is None:
            print(f'  +  {table}: new table ({current} rows)')
        elif current < prev * 0.9 and prev > 10:
            # Warn if row count drops more than 10% (only meaningful for larger tables)
            print(f'  ⚠  {table}: {prev} → {current} rows (dropped >{int((1 - current/prev)*100)}%)')
            failures.append(f'Row count drop: {table} {prev} → {current}')
        else:
            delta = current - prev
            sign = '+' if delta >= 0 else ''
            print(f'  ✓  {table}: {prev} → {current} ({sign}{delta})')
else:
    print(f'  No baseline found - saving current counts as baseline to {BASELINE_FILE}')
    with open(BASELINE_FILE, 'w') as f:
        json.dump(counts, f, indent=2)
    print('  Baseline saved. Re-run to compare.')

print()
if failures:
    print(f'FAILED - {len(failures)} issue(s):')
    for f in failures:
        print(f'  • {f}')
    sys.exit(1)
else:
    print('DB integrity OK.')

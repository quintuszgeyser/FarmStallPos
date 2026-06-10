#!/usr/bin/env python3
"""
Farm POS smoke test — run after every deploy to confirm core routes are alive.

Usage:
    python scripts/smoke_test.py [base_url]

    base_url defaults to http://localhost:5000
    For production: python scripts/smoke_test.py https://localhost:5443

Exit code 0 = all checks passed. Non-zero = something broke.
"""

import sys
import json
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else 'http://localhost:5000'
ADMIN_USER = sys.argv[2] if len(sys.argv) > 2 else 'admin'
ADMIN_PASS = sys.argv[3] if len(sys.argv) > 3 else 'admin123'

s = requests.Session()
s.verify = False  # self-signed cert on prod

failures = []

def check(label, r, expected=200):
    ok = r.status_code == expected
    mark = '✓' if ok else '✗'
    print(f'  {mark}  {label:45s}  {r.status_code}')
    if not ok:
        failures.append(f'{label}: expected {expected}, got {r.status_code}')
        try:
            print(f'       → {r.json()}')
        except Exception:
            print(f'       → {r.text[:200]}')

def section(title):
    print(f'\n── {title}')


section('Health')
check('/health',      s.get(f'{BASE}/health'))
check('/__version',   s.get(f'{BASE}/__version'))

section('Auth')
r = s.post(f'{BASE}/api/login', json={'username': ADMIN_USER, 'password': ADMIN_PASS})
check('POST /api/login', r)
check('GET  /api/me',    s.get(f'{BASE}/api/me'))

section('Core reads (admin)')
for path in [
    '/api/products',
    '/api/suppliers',
    '/api/stock/ingredients',
    '/api/stock/adjustments',
    '/api/users',
    '/api/settings',
    '/api/specials',
    '/api/kitchen/orders',
    '/api/transactions',
    '/api/customers',
    '/api/invoices',
    '/api/stats',
    '/api/kiosk/tablets',
]:
    check(f'GET  {path}', s.get(f'{BASE}{path}'))

section('Exports')
for path in [
    '/admin/export/products',
    '/admin/export/transactions',
]:
    check(f'GET  {path}', s.get(f'{BASE}{path}'))

section('Teller read (no auth required after login)')
check('GET  /api/till/active_customer', s.get(f'{BASE}/api/till/active_customer'))

section('Logout')
check('POST /api/logout', s.post(f'{BASE}/api/logout'))

section('Auth guard (must get 401/403 after logout)')
r = s.get(f'{BASE}/api/products')
if r.status_code in (401, 403):
    print(f'  ✓  auth guard after logout                            {r.status_code}')
else:
    print(f'  ✗  auth guard after logout                            {r.status_code} (expected 401/403)')
    failures.append(f'auth guard: expected 401/403, got {r.status_code}')

print()
if failures:
    print(f'FAILED — {len(failures)} check(s) failed:')
    for f in failures:
        print(f'  • {f}')
    sys.exit(1)
else:
    print(f'All checks passed against {BASE}')

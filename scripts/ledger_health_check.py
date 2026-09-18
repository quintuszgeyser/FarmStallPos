#!/usr/bin/env python3
"""
Rev 5 P3-4 — the CLI/cron half of ledger health.

Runs the same invariant suite as scripts/reconcile.py (imports its checks
directly rather than re-implementing them) and writes a small status file
that /api/health reads on every request — the same shape backup.sh already
uses for backup_status.json (see app.py's _api_health): a status file an
external cron writes, that the app surfaces as a plain-language warning to
admins with zero extra infrastructure. Intended to run nightly via cron,
same as backup.sh; add it to the appliance crontab alongside that job.

Read-only — see scripts/reconcile.py's own docstring for why that property
matters (this can run against production safely).

Usage:
    DATABASE_URL=... python scripts/ledger_health_check.py [--out PATH]

Exit code is 1 if the overall invariant suite fails, 0 otherwise — same
convention as reconcile.py, so this can gate a CI/cron job on its own.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.reconcile import _make_session, run_all

DEFAULT_OUT = '/app/config/ledger_health_status.json'


def build_status(results, generated_at):
    by_id = {r.id: r for r in results}
    computable = [r for r in results if r.status in ('PASS', 'FAIL')]
    failing = [r.id for r in computable if r.status == 'FAIL']
    unresolved_violations = sum(len(r.violations) for r in computable if r.status == 'FAIL')

    inv11 = by_id.get('INV-11')
    inv11_status = inv11.status if inv11 else 'NOT_YET_COMPUTABLE'
    inv11_coverage = None
    if inv11 and inv11.status in ('PASS', 'FAIL'):
        inv11_coverage = {'checked': inv11.checked_count, 'skipped': inv11.skipped_count}

    return {
        'generated_at': generated_at.isoformat(),
        'overall_status': 'FAIL' if failing else 'PASS',
        'invariants_computable': len(computable),
        'invariants_passing': len(computable) - len(failing),
        'invariants_failing': failing,
        'unresolved_violation_count': unresolved_violations,
        'inv11_status': inv11_status,
        'inv11_coverage': inv11_coverage,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default=DEFAULT_OUT, help=f'status file path (default {DEFAULT_OUT})')
    args = parser.parse_args()

    session = _make_session()
    try:
        results = run_all(session)
    finally:
        session.close()

    generated_at = datetime.now(timezone.utc)
    status = build_status(results, generated_at)

    os.makedirs(os.path.dirname(args.out), exist_ok=True) if os.path.dirname(args.out) else None
    with open(args.out, 'w') as f:
        json.dump(status, f, indent=2)

    print(json.dumps(status, indent=2))
    sys.exit(1 if status['overall_status'] == 'FAIL' else 0)


if __name__ == '__main__':
    main()

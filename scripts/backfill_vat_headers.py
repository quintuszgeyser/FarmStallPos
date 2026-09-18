#!/usr/bin/env python3
"""
Rev 5 P1-1b — legacy VAT header backfill.

Run explicitly, on demand. This is NOT wired into strong_migrate()/app
startup — per Section 9's staged-migration spirit, populating sale_headers
for historical sales is a reviewable, operator-run action, not something
that should happen silently on the next deploy.

Like scripts/reconcile.py, this connects via a plain SQLAlchemy engine bound
to DATABASE_URL and never calls create_app()/strong_migrate() — running it
can never trigger a schema migration as a side effect. It DOES require the
P1-1 schema (sale_headers table, sales.vat_* columns) to already exist; run
it only after that schema has been deployed (i.e. after the app has booted
at least once on the target database).

What this computes, and why it's honest about its limits
----------------------------------------------------------
For any sale_id in `sales` with no existing `sale_headers` row, this
reconstructs a header using the EXACT SAME flat-rate-on-whole-basket formula
the (buggy) receipt endpoints have always shown:

    vat_amount = round(total_incl * rate/100 / (1 + rate/100), 2)

using vat_registered/vat_rate from CURRENT settings — because the rate and
registration status actually in effect at the time of each historical sale
were never recorded anywhere, they CANNOT be reconstructed. This script does
NOT claim to recover "true" historical VAT; it freezes the same (flawed)
number the old code would show today, so that number stops moving around
every time settings change in the future. Decimal + ROUND_HALF_UP replace
the old code's float + implicit-rounding path — a genuine stability
improvement — but the underlying total being taxed is unchanged: the
historical Sale.unit_price * qty, never reinterpreted.

The per-line sales.vat_classification/vat_rate/amount_excl/vat_amount/
amount_incl columns are deliberately left NULL for these rows. A flat-rate
sale never had a real per-product VAT breakdown computed at the time, and
synthesizing one now (e.g. by applying today's Product.vat_type backwards
over historical lines) would misrepresent history as more precise than it
ever was. Likewise the three sale_headers subtotal buckets (standard/
zero_rated/exempt) are left at 0 for legacy_flat rows — not because nothing
was sold, but because which bucket each line belonged to was never recorded.
Only total_excl_vat/total_vat/total_incl_vat are meaningful for a
legacy_flat header; any reader must check vat_method before trusting the
subtotal buckets (this is exactly what P1-1b's "present as VAT originally
recorded, never as verified" requirement means in practice).

Idempotency
-----------
Each sale_id is backfilled in its OWN short transaction (not one giant
transaction for the whole run), using `INSERT ... WHERE NOT EXISTS` guarded
additionally by sale_headers.sale_id's DB-level UNIQUE constraint. A second
full run finds nothing left to do. A row that already exists — whether from
the new checkout path (vat_method='per_line') or a prior backfill run — is
never touched; this script only ever INSERTs, never UPDATEs. Per-sale_id
transactions also mean a single unexpected failure doesn't lose progress
already committed for earlier sales (checkpoint-friendly, per Section 14's
spirit for data-repair-adjacent scripts, even though this isn't P4-1 itself).
"""
import argparse
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

CENTS = Decimal('0.01')


def _db_url():
    url = os.environ.get('DATABASE_URL', 'sqlite:///pos.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql+psycopg://', 1)
    elif url.startswith('postgresql://') and '+psycopg://' not in url:
        url = 'postgresql+psycopg://' + url.split('://', 1)[1]
    return url


def _current_vat_settings(conn):
    vat_registered = (conn.execute(sa.text(
        "SELECT value FROM settings WHERE key = 'vat_registered'"
    )).scalar() or 'false') == 'true'
    vat_rate_raw = conn.execute(sa.text(
        "SELECT value FROM settings WHERE key = 'vat_rate'"
    )).scalar()
    vat_rate = Decimal(str(vat_rate_raw)) if vat_rate_raw not in (None, '') else Decimal('15')
    return vat_registered, vat_rate


def _pending_sale_ids(conn):
    return conn.execute(sa.text("""
        SELECT DISTINCT s.sale_id
        FROM sales s
        LEFT JOIN sale_headers h ON h.sale_id = s.sale_id
        WHERE h.id IS NULL
    """)).scalars().all()


def run(engine, dry_run=False):
    summary = {'backfilled': 0, 'already_present': 0, 'errors': []}

    with engine.connect() as conn:
        with conn.begin():
            vat_registered, vat_rate = _current_vat_settings(conn)
            pending = _pending_sale_ids(conn)

    for sale_id in pending:
        with engine.connect() as conn:
            with conn.begin():
                lines = conn.execute(sa.text(
                    "SELECT qty, unit_price FROM sales WHERE sale_id = :sid"
                ), {'sid': sale_id}).all()
            if not lines:
                summary['errors'].append((sale_id, 'no line rows found (race?)'))
                continue

            total_incl = sum((Decimal(str(q)) * Decimal(str(p))) for q, p in lines)
            total_incl = total_incl.quantize(CENTS, rounding=ROUND_HALF_UP)

            if vat_registered:
                vat_amount = (
                    total_incl * vat_rate / Decimal('100') / (1 + vat_rate / Decimal('100'))
                ).quantize(CENTS, rounding=ROUND_HALF_UP)
            else:
                vat_amount = Decimal('0.00')
            total_excl = total_incl - vat_amount

            if dry_run:
                summary['backfilled'] += 1
                continue

            try:
                with conn.begin():
                    # created_at is set explicitly here rather than relied on as a DB-level
                    # default: SaleHeader's ORM column only declares a Python-side
                    # `default=datetime.utcnow` (models.py), and db.create_all() — which
                    # runs before strong_migrate()'s hand-written DDL and creates the table
                    # first on a fresh database — does not carry that through as a server
                    # DEFAULT. Relying on either created the exact NOT NULL violation this
                    # comment now documents (caught by tests/test_backfill_vat.py).
                    result = conn.execute(sa.text("""
                        INSERT INTO sale_headers
                            (sale_id, standard_rated_subtotal, zero_rated_subtotal, exempt_subtotal,
                             total_excl_vat, total_vat, total_incl_vat,
                             vat_rate_snapshot, vat_registered_snapshot, vat_method, created_at)
                        SELECT CAST(:sale_id AS VARCHAR(64)), 0, 0, 0, :excl, :vat, :incl, :rate, :reg, 'legacy_flat', NOW()
                        WHERE NOT EXISTS (SELECT 1 FROM sale_headers WHERE sale_id = :sale_id)
                    """), {
                        'sale_id': sale_id, 'excl': total_excl, 'vat': vat_amount, 'incl': total_incl,
                        'rate': vat_rate, 'reg': vat_registered,
                    })
                if result.rowcount == 1:
                    summary['backfilled'] += 1
                else:
                    summary['already_present'] += 1
            except IntegrityError:
                # Lost a race to a concurrent writer (new checkout, or another backfill
                # run) between the pending-list read and this insert — not an error,
                # the row is exactly what we would have written. Never overwrite it.
                summary['already_present'] += 1

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true', help='report what would be backfilled without writing')
    args = parser.parse_args()

    engine = sa.create_engine(_db_url())
    summary = run(engine, dry_run=args.dry_run)

    print(f"{'DRY RUN — ' if args.dry_run else ''}backfilled: {summary['backfilled']}")
    print(f"already had a header (skipped): {summary['already_present']}")
    if summary['errors']:
        print(f"errors: {len(summary['errors'])}")
        for sid, msg in summary['errors']:
            print(f"  {sid}: {msg}")
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Rev 5 P0-2 / Section 6 — the invariant suite.

Read-only, always. This script never calls create_app() or strong_migrate() —
it opens a plain SQLAlchemy engine/session bound to DATABASE_URL directly, so
running it against a production database can NEVER trigger a schema migration
or any other write as a side effect. That property matters more than
convenience here: P0-3 requires running this against production, and P4-1
requires running it against a restored copy — neither may mutate anything.

Pre-P2-0 (movement ledger) scope, per Rev 5 P0-2's note:
    INV-2, INV-4, INV-5, INV-6, INV-8, INV-9 are implemented against the
    CURRENT schema (stock_batches / stock_consumption / stock_adjustments /
    consignment_liabilities), because they don't require the unified movement
    ledger to compute.
    INV-1, INV-3, INV-11 require the movement ledger (P2-0) and are reported
    as NOT_YET_COMPUTABLE — present in the registry so it is already complete,
    not silently omitted.
    INV-7 (VAT closure) requires the sale_headers table (P1-1) and is also
    NOT_YET_COMPUTABLE.
    INV-10 (transaction immutability) is a DB-trigger + CI concern per Rev 5
    Section 6, not a reconcile.py check — reported as NOT_APPLICABLE_HERE.

Discrepancies against the pasted Rev 5 text found while implementing this
script (Operating Rule 16 — recorded, not silently resolved):

  * INV-5 (consignment closure): Rev 5's formula nets liabilities against
    "unsettled consignment consumption, net of returned quantity." Returns are
    not linked to their originating consumption record until P2-2 ships, so
    there is no way to compute the "net of returned quantity" term today. This
    implementation checks per-batch/per-supplier liability-vs-consumption
    agreement WITHOUT the return netting term, and is documented as a reduced
    proxy that P2-2 will make exact.

  * INV-6 (COGS agreement): StockConsumption.sale_id is scoped to the whole
    checkout transaction, not to an individual Sale line — a basket with two
    different stock_item products sharing one sale_id can only be
    disambiguated by ingredient_id, which breaks down the moment the SAME
    product appears on two separate lines of the same sale, or the sale
    involves a made-to-order recipe (whose ingredient consumption cannot be
    attributed back to a specific recipe Sale line pre-ledger — this is
    exactly the ambiguity Rev 5 cites as motivating P2-0). This
    implementation checks stock_item / produced-recipe lines only, skips any
    sale_id+product_id pair that appears more than once (ambiguous), and
    reports made-to-order recipe COGS as NOT_YET_COMPUTABLE.

  * INV-8 (value closure): Rev 5's formula compares the batch-derived
    valuation total against "the inventory valuation report total." No such
    report exists anywhere in this codebase today (grepped blueprints/stats.py
    and found nothing) — there is no second, independently-computed number to
    close against. This implementation instead reports the batch-derived
    valuation as a numeric baseline (per product, and total) plus a sanity
    check (no negative remaining quantity, no negative cost), and documents
    that true "closure" against a second source is not possible until such a
    report exists.

  * INV-9 (allocation closure): Rev 5 names `allocated_shipping` as if it
    represents the batch's full overhead allocation. Reading the actual
    receive code (blueprints/suppliers.py ~1660-1716), `allocated_shipping`
    is only the shipping-typed SUBSET of a broader per-batch overhead
    allocation (`share`); the real overhead applied to final_cost_incl_vat is
    the `additional_costs` JSON total (which nets in the discount entry
    appended for audit purposes). Checking literally against
    `allocated_shipping` would false-positive on every batch with a
    non-shipping overhead cost. This implementation reconstructs the real
    overhead from `additional_costs` instead.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Product, StockBatch, StockConsumption, StockAdjustment,
    ConsignmentLiability, Sale,
)

Q2 = Decimal('0.01')


def _d(x):
    return Decimal(str(x)) if x is not None else None


def _q2(x):
    return _d(x).quantize(Q2, rounding=ROUND_HALF_UP) if x is not None else None


def _make_session():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise SystemExit('DATABASE_URL is required (read-only — never falls back to sqlite default).')
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql+psycopg://', 1)
    elif db_url.startswith('postgresql://') and '+psycopg://' not in db_url:
        db_url = 'postgresql+psycopg://' + db_url.split('://', 1)[1]
    engine = create_engine(db_url)
    return sessionmaker(bind=engine)()


class Result:
    def __init__(self, inv_id, name, severity, blocks, tolerance):
        self.id = inv_id
        self.name = name
        self.severity = severity
        self.blocks = blocks       # gates this invariant blocks when failing
        self.tolerance = tolerance
        self.status = None         # PASS | FAIL | NOT_YET_COMPUTABLE | NOT_APPLICABLE_HERE
        self.checked_count = 0
        self.skipped_count = 0
        self.violations = []       # list of dicts — per-entity detail, never total-only
        self.note = None

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'severity': self.severity,
            'blocks': self.blocks, 'tolerance': self.tolerance,
            'status': self.status, 'checked_count': self.checked_count,
            'skipped_count': self.skipped_count,
            'violation_count': len(self.violations),
            'violations': self.violations, 'note': self.note,
        }


def check_inv2_no_orphan_movement(session):
    r = Result('INV-2', 'No orphan movement', 'high', ['deployment', 'pilot_readiness'], 'zero')
    r.note = ('Pre-P2-0 proxy: checks StockConsumption.batch_id against stock_batches. '
              'FK-enforced (StockConsumption.batch_id is NOT NULL with a real FK), so this '
              'is expected to always pass unless referential integrity was bypassed '
              '(e.g. a hard delete outside the ORM). Superseded by the real movement.batch_id '
              'check once P2-0 ships.')
    batch_ids = {b.id for b in session.query(StockBatch.id).all()}
    consumptions = session.query(StockConsumption).all()
    r.checked_count = len(consumptions)
    for c in consumptions:
        if c.batch_id not in batch_ids:
            r.violations.append({
                'stock_consumption_id': c.id, 'sale_id': c.sale_id,
                'ingredient_id': c.ingredient_id, 'orphaned_batch_id': c.batch_id,
            })
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


def check_inv4_no_free_stock(session):
    r = Result('INV-4', 'No free stock', 'high', ['deployment', 'pilot_readiness'], 'zero')
    batches = session.query(StockBatch).filter(StockBatch.batch_type == 'normal').all()
    r.checked_count = len(batches)
    for b in batches:
        if _d(b.cost_per_base_unit) == Decimal('0'):
            r.violations.append({
                'batch_id': b.id, 'product_id': b.product_id,
                'qty_remaining_base': str(b.qty_remaining_base),
                'purchased_at': b.purchased_at.isoformat() if b.purchased_at else None,
            })
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


def check_inv5_consignment_closure(session):
    r = Result('INV-5', 'Consignment closure', 'high', ['pilot_readiness'], 'zero')
    r.note = ('REDUCED PROXY — see module docstring. Compares, per (supplier_id, batch_id) '
              'and per (supplier_id, product_id) for batch_id IS NULL (simple consignment '
              'products), Sigma ConsignmentLiability.qty_consumed (status != voided) against '
              'Sigma StockConsumption.qty_consumed_base for that batch. Does NOT net returned '
              'quantity (no consumption<->return link exists pre-P2-2) — a partial return will '
              'currently show as a mismatch here, which is a known limitation, not a false '
              'invariant violation you should act on before P2-2 ships.')
    liabilities = session.query(ConsignmentLiability).filter(
        ConsignmentLiability.status != 'voided'
    ).all()
    by_batch = {}
    for lib in liabilities:
        key = ('batch', lib.batch_id) if lib.batch_id else ('product', lib.supplier_id, lib.product_id)
        by_batch.setdefault(key, Decimal('0'))
        by_batch[key] += _d(lib.qty_consumed)

    consumptions = session.query(StockConsumption).join(
        StockBatch, StockConsumption.batch_id == StockBatch.id
    ).filter(StockBatch.ownership_type == 'CONSIGNMENT').all()
    consumed_by_batch = {}
    for c in consumptions:
        consumed_by_batch.setdefault(('batch', c.batch_id), Decimal('0'))
        consumed_by_batch[('batch', c.batch_id)] += _d(c.qty_consumed_base)

    r.checked_count = len(set(by_batch) | set(consumed_by_batch))
    all_keys = set(by_batch) | set(consumed_by_batch)
    for key in all_keys:
        if key[0] != 'batch':
            continue  # simple-product (no-batch) consignment consumption isn't tracked in StockConsumption at all
        liability_qty = by_batch.get(key, Decimal('0'))
        consumed_qty = consumed_by_batch.get(key, Decimal('0'))
        if liability_qty != consumed_qty:
            r.violations.append({
                'batch_id': key[1], 'liability_qty_outstanding_plus_settled': str(liability_qty),
                'consumed_qty': str(consumed_qty), 'difference': str(liability_qty - consumed_qty),
            })
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


def check_inv6_cogs_agrees(session):
    r = Result('INV-6', 'COGS agrees', 'high', ['pilot_readiness'], 'zero')
    r.note = ('Restricted to stock_item / produced-recipe Sale lines — see module docstring '
              'for why made-to-order recipe lines and duplicate product-per-sale lines are '
              'NOT_YET_COMPUTABLE pre-P2-0 (StockConsumption.sale_id is basket-scoped, not '
              'line-scoped, so attribution is ambiguous in those cases).')
    products = {p.id: p for p in session.query(Product).all()}
    sales = session.query(Sale).filter(
        Sale.voided.is_(False),
        Sale.product_id.isnot(None),
        (Sale.payment_method.is_(None)) | (Sale.payment_method != 'return'),
    ).all()

    # Group by (sale_id, product_id) to detect ambiguous duplicates.
    by_key = {}
    for s in sales:
        prod = products.get(s.product_id)
        if not prod or not (prod.product_type == 'stock_item' or
                             (prod.product_type == 'recipe' and prod.is_produced)):
            continue
        by_key.setdefault((s.sale_id, s.product_id), []).append(s)

    consumption_sums = {}
    if by_key:
        rows = session.query(StockConsumption).filter(
            StockConsumption.sale_id.in_({k[0] for k in by_key})
        ).all()
        for c in rows:
            key = (c.sale_id, c.ingredient_id)
            consumption_sums.setdefault(key, Decimal('0'))
            consumption_sums[key] += _d(c.qty_consumed_base) * _d(c.cost_per_base_unit)

    for key, rows in by_key.items():
        if len(rows) > 1:
            r.skipped_count += 1
            continue  # ambiguous — same product appears twice in one sale_id
        r.checked_count += 1
        sale_row = rows[0]
        expected = consumption_sums.get(key, Decimal('0')).quantize(Q2, rounding=ROUND_HALF_UP)
        actual = (_q2(sale_row.cogs) if sale_row.cogs is not None else Decimal('0.00'))
        if expected != actual:
            r.violations.append({
                'sale_id': key[0], 'product_id': key[1], 'sale_row_id': sale_row.id,
                'sale_cogs': str(actual), 'consumption_derived_cogs': str(expected),
                'difference': str(actual - expected),
            })
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


def check_inv8_value_baseline(session):
    r = Result('INV-8', 'Value closure', 'medium', ['pilot_readiness'], 'zero')
    r.note = ('NOT A TRUE CLOSURE — see module docstring. No inventory valuation report exists '
              'in this codebase to close against, so this reports the batch-derived valuation '
              'as a baseline plus a sanity check (no negative remaining qty, no negative cost) '
              'rather than a two-source agreement.')
    batches = session.query(StockBatch).all()
    r.checked_count = len(batches)
    total_value = Decimal('0')
    by_product = {}
    for b in batches:
        qty = _d(b.qty_remaining_base)
        cost = _d(b.cost_per_base_unit)
        if qty < 0:
            r.violations.append({'batch_id': b.id, 'product_id': b.product_id,
                                  'issue': 'negative qty_remaining_base', 'value': str(qty)})
            continue
        if cost < 0:
            r.violations.append({'batch_id': b.id, 'product_id': b.product_id,
                                  'issue': 'negative cost_per_base_unit', 'value': str(cost)})
            continue
        value = (qty * cost).quantize(Q2, rounding=ROUND_HALF_UP)
        total_value += value
        by_product[b.product_id] = by_product.get(b.product_id, Decimal('0')) + value
    r.note += f' Baseline total inventory value: R{total_value.quantize(Q2)} across {len(by_product)} products.'
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


def check_inv9_allocation_closure(session):
    r = Result('INV-9', 'Allocation closure', 'medium', ['pilot_readiness'], '0.01 (see note)')
    r.note = ('Only checks batches created via the VAT-aware purchase-run costing path '
              '(base_cost_incl_vat IS NOT NULL) — receive/stocktake/write-off/return batches '
              "don't populate these columns and are out of scope for this invariant. Formula "
              'reconstructed from actual suppliers.py code, not the literal Rev 5 column name — '
              'see module docstring discrepancy note. Tolerance 0.01 because these columns are '
              'independently rounded (quantize to 2dp/4dp) at several points during purchase-run '
              'allocation, not because exact equality is unattainable in principle.')
    batches = session.query(StockBatch).filter(StockBatch.base_cost_incl_vat.isnot(None)).all()
    r.checked_count = len(batches)
    for b in batches:
        base_total = _d(b.base_cost_total) or Decimal('0')
        vat_amount = _d(b.vat_amount) or Decimal('0')
        base_incl_vat = _d(b.base_cost_incl_vat)
        final_incl_vat = _d(b.final_cost_incl_vat)
        cost_per_unit = _d(b.cost_per_base_unit)
        qty_purchased = _d(b.qty_purchased_base)

        expected_base_incl_vat = _q2(base_total + vat_amount)
        if _q2(base_incl_vat) != expected_base_incl_vat:
            r.violations.append({
                'batch_id': b.id, 'check': 'base_cost_incl_vat == base_cost_total + vat_amount',
                'stored': str(_q2(base_incl_vat)), 'expected': str(expected_base_incl_vat),
            })
            continue

        overhead_total = Decimal('0')
        if b.additional_costs:
            try:
                entries = json.loads(b.additional_costs)
                overhead_total = sum((_d(e.get('amount', 0)) for e in entries), Decimal('0'))
            except (ValueError, TypeError):
                r.violations.append({'batch_id': b.id, 'check': 'additional_costs JSON parse',
                                      'error': 'unparseable additional_costs'})
                continue
        expected_final = _q2(base_incl_vat + overhead_total)
        if final_incl_vat is not None and _q2(final_incl_vat) != expected_final:
            r.violations.append({
                'batch_id': b.id,
                'check': 'final_cost_incl_vat == base_cost_incl_vat + sum(additional_costs)',
                'stored': str(_q2(final_incl_vat)), 'expected': str(expected_final),
            })
            continue

        if final_incl_vat is not None and qty_purchased and qty_purchased != 0:
            expected_cost_per_unit = _q2(final_incl_vat / qty_purchased)
            if _q2(cost_per_unit) != expected_cost_per_unit:
                r.violations.append({
                    'batch_id': b.id,
                    'check': 'cost_per_base_unit == final_cost_incl_vat / qty_purchased_base',
                    'stored': str(_q2(cost_per_unit)), 'expected': str(expected_cost_per_unit),
                })
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


def not_yet_computable(inv_id, name, reason):
    r = Result(inv_id, name, 'n/a', ['cutover'], 'n/a')
    r.status = 'NOT_YET_COMPUTABLE'
    r.note = reason
    return r


def not_applicable_here(inv_id, name, reason):
    r = Result(inv_id, name, 'n/a', [], 'n/a')
    r.status = 'NOT_APPLICABLE_HERE'
    r.note = reason
    return r


def run_all(session):
    results = [
        check_inv2_no_orphan_movement(session),
        not_yet_computable('INV-1', 'Quantity closure', 'Requires the P2-0 movement ledger.'),
        not_yet_computable('INV-3', 'Typed source resolves', 'Requires the P2-0 movement ledger.'),
        check_inv4_no_free_stock(session),
        check_inv5_consignment_closure(session),
        check_inv6_cogs_agrees(session),
        not_yet_computable('INV-7', 'VAT closure', 'Requires the P1-1 sale_headers table.'),
        check_inv8_value_baseline(session),
        check_inv9_allocation_closure(session),
        not_applicable_here('INV-10', 'Transaction immutability',
                             'Enforced via DB trigger + CI per Rev 5 Section 6, not a reconcile.py check.'),
        not_yet_computable('INV-11', 'Projection rebuild', 'Requires the P2-0 movement ledger.'),
    ]
    return results


def render_human(results, generated_at):
    lines = [f'Rev 5 invariant suite — {generated_at.isoformat()}', '=' * 60]
    any_fail = False
    for r in results:
        marker = {'PASS': 'PASS', 'FAIL': 'FAIL', 'NOT_YET_COMPUTABLE': 'N/A (pre-P2-0)',
                   'NOT_APPLICABLE_HERE': 'N/A (not a reconcile.py check)'}[r.status]
        lines.append(f'{r.id} {r.name} — {marker}')
        if r.status not in ('NOT_YET_COMPUTABLE', 'NOT_APPLICABLE_HERE'):
            lines.append(f'    checked={r.checked_count} skipped={r.skipped_count} '
                         f'violations={len(r.violations)} tolerance={r.tolerance}')
        if r.note:
            lines.append(f'    note: {r.note}')
        if r.violations:
            any_fail = True
            for v in r.violations[:20]:
                lines.append(f'    VIOLATION: {v}')
            if len(r.violations) > 20:
                lines.append(f'    ... and {len(r.violations) - 20} more (see JSON output)')
        lines.append('')
    lines.append('OVERALL: ' + ('FAIL' if any_fail else 'PASS'))
    return '\n'.join(lines), any_fail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json-out', help='path to write machine-readable JSON report')
    args = parser.parse_args()

    session = _make_session()
    try:
        results = run_all(session)
    finally:
        session.close()

    generated_at = datetime.now(timezone.utc)
    human_text, any_fail = render_human(results, generated_at)
    print(human_text)

    json_report = {
        'generated_at': generated_at.isoformat(),
        'overall_status': 'FAIL' if any_fail else 'PASS',
        'invariants': [r.to_dict() for r in results],
    }
    if args.json_out:
        with open(args.json_out, 'w') as f:
            json.dump(json_report, f, indent=2, default=str)
        print(f'\nJSON report written to {args.json_out}')
    else:
        print('\n--- JSON ---')
        print(json.dumps(json_report, indent=2, default=str))

    sys.exit(1 if any_fail else 0)


if __name__ == '__main__':
    main()

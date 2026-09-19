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
    INV-1 (quantity closure) still requires full movement-ledger cutover —
    the per-product formula in Rev 5's contract is arithmetically the same
    statement as INV-11's per-batch check (both reduce to "signed sum of
    movement qty_delta equals the live balance"), so implementing it
    separately here would just restate INV-11 at a coarser grain without new
    signal; it remains NOT_YET_COMPUTABLE until INV-11 passes clean and this
    note gets revisited.
    INV-3 (typed source resolves) IS now implemented — see
    check_inv3_typed_source_resolves. Dual-write coverage across every
    write_stock_movement call site went to 100% this session (closing the
    two gaps in auto_produce_on_negative — see helpers.py), which is what
    makes this check meaningful rather than reporting everything as
    "not yet covered." Several source_type values resolve against a proxy
    key rather than a literal business-record lookup — see that function's
    own docstring for exactly which, and why each one is safe.
    INV-11 (projection rebuild) is now implemented — see
    check_inv11_projection_rebuild. It is the P2-0a cutover gate: it runs
    throughout the dual-write ramp-up (expect it to FAIL, entirely accounted
    for by skipped_count, until coverage is complete — see that function's
    docstring) and keeps running after cutover as a standing canary.
    INV-7 (VAT closure) is now implemented (Rev 5 P1-1 Wave B), against the
    sale_headers table — see check_inv7_vat_closure for scope (per_line
    headers only; legacy_flat headers are reported via skipped_count, not
    checked, since they have no per-line data to close against by design).
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
    allocation (`share`). Checking literally against `allocated_shipping`
    would false-positive on every batch with a non-shipping overhead cost.
    This implementation reconstructs the real overhead from
    `additional_costs` instead.

    CORRECTION (found via the P4-1 dry-run's deviation-size check on the
    2026-09-19 production baseline — see reports/p0-3-baseline-prod-
    20260919.json's commit for the discovery): an earlier version of this
    function summed every additional_costs entry and compared against
    final_cost_incl_vat directly, which produced 47 false-positive
    violations, all off by exactly one batch's discount amount. The actual
    receive code's own comment states the real formula: "ex_vat + vat =
    incl_vat -> + overheads - discount = final_cost". additional_costs'
    entries are all positive overhead (shipping etc.) EXCEPT a type=
    'discount' entry, which suppliers.py appends purely "for audit trail" —
    it is a display duplicate of the line-level share already folded into
    `allocated_discount` (batch_addl's own comment says so explicitly), not
    an independent additive term. Summing it in AND separately subtracting
    allocated_discount double-subtracts that line-level share. The fix:
    sum additional_costs entries excluding type='discount', then subtract
    allocated_discount once. Verified exactly against batch 667 (shipping
    5.93, allocated_discount 36.00, base_incl_vat 233.54 -> 203.47, matching
    the stored value to the cent) and batch 668 (same shape, different
    numbers) before trusting the fix.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from models import (
    Product, StockBatch, StockConsumption, StockAdjustment,
    ConsignmentLiability, Sale, SaleHeader, StockMovement, SupplierInvoice,
)

Q2 = Decimal('0.01')
Q4 = Decimal('0.0001')


def _d(x):
    return Decimal(str(x)) if x is not None else None


def _q2(x):
    return _d(x).quantize(Q2, rounding=ROUND_HALF_UP) if x is not None else None


def _q4(x):
    return _d(x).quantize(Q4, rounding=ROUND_HALF_UP) if x is not None else None


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
              'CORRECTION (found investigating the 563-violation P0-3 baseline): despite '
              'models.py declaring ForeignKey(\'stock_batches.id\'), no FK constraint on '
              'this column actually existed in the database — confirmed via pg_constraint. '
              'strong_migrate() now adds fk_stock_consumption_batch NOT VALID, which stops '
              'new orphans without failing on the historical ones; VALIDATE CONSTRAINT after '
              'Phase 4 repair is the acceptance test that this invariant is closed for good. '
              'Superseded by the real movement.batch_id check once P2-0 ships.')
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


_WRITEOFF_REVERSAL_SOURCE_ID = re.compile(r'^adj-(?:edit|del)-(\d+)$')


def check_inv3_typed_source_resolves(session):
    """Every movement's (source_type, source_id) should resolve to an existing
    record of that business type. Unlike INV-2 (a real FK), nothing in the
    schema enforces this — source_id is a free-text string precisely because
    the tables it points at don't share a key space (Sale.sale_id is a UUID,
    StockBatch.id/StockAdjustment.id/SupplierInvoice.id are integers).

    Resolution rule per source_type, derived from reading every
    write_stock_movement call site rather than assumed from the column
    comment:
      sale / return  -> a Sale row exists with that sale_id (the return's own
                         new sale_id, for 'return' — see transactions.py's
                         return endpoint, which stamps return_uuid onto the
                         Sale rows it creates for the return transaction).
      production     -> a StockBatch exists with that produce_ref (covers both
                         the finished-goods output batch and, for the
                         ingredient-consumption movements consume_fifo writes
                         during a produce run, the same produce_uuid passed
                         through as its `sale_id` parameter).
      receipt        -> a StockBatch.id or StockBatch.import_run_id matches.
                         source_id is deliberately NULL for
                         absorb_neg_placeholder's two call sites (no batch
                         exists yet at that point in the caller — see its
                         docstring) and reported via skipped_count, not a
                         violation.
      stocktake      -> a StockBatch.id matches (the variance batch itself).
      reconciliation -> a SupplierInvoice.id or StockBatch.id matches
                         (void_unconsumed_batch is called with either,
                         depending on caller — suppliers.py's invoice
                         update/delete pass an invoice id, stock.py's batch
                         delete / opening-import undo pass the batch's own id).
      writeoff       -> two shapes exist under one source_type, found by
                         reading every is_writeoff=True consume_fifo call
                         site, not by assumption:
                           - WRITEOFF_REVERSAL movements (stock.py's
                             adjustment edit/delete) stamp 'adj-edit-<id>' /
                             'adj-del-<id>', a real StockAdjustment.id.
                           - the original write-off CONSUMPTION movements
                             (api_stock_writeoff, the stocktake negative-
                             variance path, archive's own write-off) stamp a
                             synthetic 'wo-<uuid>' / 'archive-wo-<uuid>' /
                             'adj-<uuid>' token that is never persisted
                             anywhere else — it only ever correlates that
                             movement to its sibling StockConsumption rows
                             sharing the same sale_id, not to an independently
                             resolvable business record. Reported via
                             skipped_count with this note, not a violation:
                             there is no bug to find here, just a source_id
                             shape this invariant cannot check.
      migration      -> exempt by the ledger's own design (StockMovement's
                         docstring: "Migration backfill rows that cannot be
                         classified... get source_type='migration'... per
                         Rev 5's visible, not hidden rule").
    """
    r = Result('INV-3', 'Typed source resolves', 'high', ['pilot_readiness'], 'zero, excluding documented proxies')
    sale_ids = {row[0] for row in session.query(Sale.sale_id).distinct().all()}
    produce_refs = {row[0] for row in session.query(StockBatch.produce_ref)
                     .filter(StockBatch.produce_ref.isnot(None)).distinct().all()}
    batch_ids = {row[0] for row in session.query(StockBatch.id).all()}
    import_run_ids = {row[0] for row in session.query(StockBatch.import_run_id)
                       .filter(StockBatch.import_run_id.isnot(None)).distinct().all()}
    adjustment_ids = {row[0] for row in session.query(StockAdjustment.id).all()}
    supplier_invoice_ids = {row[0] for row in session.query(SupplierInvoice.id).all()}

    def _as_int(s):
        try:
            return int(s)
        except (TypeError, ValueError):
            return None

    movements = session.query(StockMovement).all()
    for m in movements:
        if m.source_type == 'migration':
            r.skipped_count += 1
            continue
        if m.source_type in ('sale', 'return'):
            r.checked_count += 1
            if m.source_id not in sale_ids:
                r.violations.append({
                    'movement_id': m.id, 'source_type': m.source_type,
                    'source_id': m.source_id, 'issue': 'no Sale row with this sale_id',
                })
        elif m.source_type == 'production':
            r.checked_count += 1
            if m.source_id not in produce_refs:
                r.violations.append({
                    'movement_id': m.id, 'source_type': m.source_type,
                    'source_id': m.source_id, 'issue': 'no StockBatch with this produce_ref',
                })
        elif m.source_type == 'receipt':
            if m.source_id is None:
                r.skipped_count += 1  # absorb_neg_placeholder's documented best-effort case
                continue
            r.checked_count += 1
            sid_int = _as_int(m.source_id)
            if (sid_int not in batch_ids) and (m.source_id not in import_run_ids):
                r.violations.append({
                    'movement_id': m.id, 'source_type': m.source_type, 'source_id': m.source_id,
                    'issue': 'source_id is not a live StockBatch.id or StockBatch.import_run_id',
                })
        elif m.source_type == 'stocktake':
            r.checked_count += 1
            if _as_int(m.source_id) not in batch_ids:
                r.violations.append({
                    'movement_id': m.id, 'source_type': m.source_type,
                    'source_id': m.source_id, 'issue': 'no StockBatch with this id',
                })
        elif m.source_type == 'reconciliation':
            r.checked_count += 1
            sid_int = _as_int(m.source_id)
            if (sid_int not in supplier_invoice_ids) and (sid_int not in batch_ids):
                r.violations.append({
                    'movement_id': m.id, 'source_type': m.source_type, 'source_id': m.source_id,
                    'issue': 'source_id is not a live SupplierInvoice.id or StockBatch.id',
                })
        elif m.source_type == 'writeoff':
            match = _WRITEOFF_REVERSAL_SOURCE_ID.match(m.source_id or '')
            if not match:
                r.skipped_count += 1  # synthetic correlation-only token — see docstring
                continue
            r.checked_count += 1
            if int(match.group(1)) not in adjustment_ids:
                r.violations.append({
                    'movement_id': m.id, 'source_type': m.source_type, 'source_id': m.source_id,
                    'issue': 'adj-edit-/adj-del- id is not a live StockAdjustment.id',
                })
        else:
            r.checked_count += 1
            r.violations.append({
                'movement_id': m.id, 'source_type': m.source_type,
                'source_id': m.source_id, 'issue': 'unrecognised source_type',
            })
    r.note = (f'{r.skipped_count} of {len(movements)} movements are documented proxies not checked here: '
              "migration rows (exempt by design), absorb_neg_placeholder's NULL-source_id receipts, and "
              "writeoff CONSUMPTION movements whose source_id is a correlation-only token with no "
              'independently resolvable record — see this function\'s docstring for all three.')
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


CONFIRMED_FREE_MARKER = 'OWNER-CONFIRMED-FREE:'
CONFIRMED_FREE_CONSIGNMENT_MARKER = 'OWNER-CONFIRMED-CONSIGNMENT-FREE:'


def check_inv4_no_free_stock(session):
    r = Result('INV-4', 'No free stock', 'high', ['deployment', 'pilot_readiness'], 'zero')
    r.note = (f'A batch is not a violation when cost_adjustment_reason starts with '
              f'{CONFIRMED_FREE_MARKER!r} — the plan\'s own text calls for "a deliberate '
              f'zero-cost option for genuinely free stock as an explicit UI choice" (P2-3\'s '
              f'section); this is that choice, made explicit and dated rather than an '
              f'unexplained zero this check would otherwise flag forever.')
    batches = session.query(StockBatch).filter(StockBatch.batch_type == 'normal').all()
    for b in batches:
        is_zero_cost = _d(b.cost_per_base_unit) == Decimal('0')
        is_confirmed_free = bool(b.cost_adjustment_reason) and b.cost_adjustment_reason.startswith(CONFIRMED_FREE_MARKER)
        if is_zero_cost and is_confirmed_free:
            r.skipped_count += 1
            continue
        r.checked_count += 1
        if is_zero_cost:
            r.violations.append({
                'batch_id': b.id, 'product_id': b.product_id,
                'qty_remaining_base': str(b.qty_remaining_base),
                'purchased_at': b.purchased_at.isoformat() if b.purchased_at else None,
            })
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


_WRITEOFF_SALE_ID = re.compile(r'^(wo-|archive-wo-|adj-)')


def check_inv5_consignment_closure(session):
    r = Result('INV-5', 'Consignment closure', 'high', ['pilot_readiness'], 'zero')
    r.note = ('REDUCED PROXY — see module docstring. Compares, per (supplier_id, batch_id) '
              'and per (supplier_id, product_id) for batch_id IS NULL (simple consignment '
              'products), Sigma ConsignmentLiability.qty_consumed (status != voided) against '
              'Sigma StockConsumption.qty_consumed_base for that batch. Does NOT net returned '
              'quantity (no consumption<->return link exists pre-P2-2) — a partial return will '
              'currently show as a mismatch here, which is a known limitation, not a false '
              'invariant violation you should act on before P2-2 ships.'
              ' CORRECTION: found investigating a real production discrepancy that turned '
              'out to be two false-positive sources, not corruption. (1) ConsignmentLiability '
              'rows with sale_id IS NULL are legitimate opening-balance entries — production '
              'has four, all timestamped 2026-08-19 09:35:00.619177, entered when consignment '
              'tracking started, to capture consumption that happened before per-sale liability '
              'existed. They have no StockConsumption counterpart by design and are excluded '
              'from this comparison entirely (reported separately, in unlinked_liability_qty). '
              '(2) StockConsumption rows from a write-off (sale_id matching wo-/archive-wo-/'
              'adj-) never get a matching liability — write-offs are absorbed as the store\'s '
              'own loss, per consume_fifo\'s own comment: "supplier is not owed for spoilage/'
              'damage." Summing them into consumed_qty made a batch look underpaid when the '
              'true consumption (sales only) matched its liability exactly. A batch whose '
              f'cost_adjustment_reason contains {CONFIRMED_FREE_CONSIGNMENT_MARKER!r} is also '
              'skipped, not failed — the owner\'s explicit call that a specific gap involves no '
              'real money owed (e.g. stock given away, not sold on consignment terms), mirroring '
              'INV-4\'s CONFIRMED_FREE_MARKER for the same reason: a dated, sourced decision '
              'beats an unexplained mismatch this check would otherwise flag forever.')
    liabilities = session.query(ConsignmentLiability).filter(
        ConsignmentLiability.status != 'voided'
    ).all()
    by_batch = {}
    unlinked_by_batch = {}
    for lib in liabilities:
        key = ('batch', lib.batch_id) if lib.batch_id else ('product', lib.supplier_id, lib.product_id)
        if lib.sale_id is None:
            unlinked_by_batch.setdefault(key, Decimal('0'))
            unlinked_by_batch[key] += _d(lib.qty_consumed)
            continue
        by_batch.setdefault(key, Decimal('0'))
        by_batch[key] += _d(lib.qty_consumed)

    consumptions = session.query(StockConsumption).join(
        StockBatch, StockConsumption.batch_id == StockBatch.id
    ).filter(StockBatch.ownership_type == 'CONSIGNMENT').all()
    consumed_by_batch = {}
    for c in consumptions:
        if c.sale_id and _WRITEOFF_SALE_ID.match(c.sale_id):
            continue  # write-off: absorbed as the store's own loss, never owed to the supplier
        consumed_by_batch.setdefault(('batch', c.batch_id), Decimal('0'))
        consumed_by_batch[('batch', c.batch_id)] += _d(c.qty_consumed_base)

    all_keys = set(by_batch) | set(consumed_by_batch)
    r.checked_count = len(all_keys)
    for key in all_keys:
        if key[0] != 'batch':
            continue  # simple-product (no-batch) consignment consumption isn't tracked in StockConsumption at all
        liability_qty = by_batch.get(key, Decimal('0'))
        consumed_qty = consumed_by_batch.get(key, Decimal('0'))
        if liability_qty != consumed_qty:
            batch = session.get(StockBatch, key[1])
            if (batch and batch.cost_adjustment_reason
                    and CONFIRMED_FREE_CONSIGNMENT_MARKER in batch.cost_adjustment_reason):
                r.skipped_count += 1
                continue
            r.violations.append({
                'batch_id': key[1], 'liability_qty_outstanding_plus_settled': str(liability_qty),
                'consumed_qty': str(consumed_qty), 'difference': str(liability_qty - consumed_qty),
                'unlinked_liability_qty': str(unlinked_by_batch.get(key, Decimal('0'))),
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
              'as a baseline plus a sanity check (no unexpected negative remaining qty, no '
              'negative cost) rather than a two-source agreement. '
              "CORRECTION (found while building the P4-1 apply step, before anything was "
              "written): this originally flagged EVERY negative qty_remaining_base as a "
              "violation, including batch_type='negative_placeholder' rows — which are "
              "supposed to be negative by design (they represent stock owed, per P2-1's "
              "oversell handling) and are supposed to carry cost_per_base_unit=0 by design "
              "(the real cost estimate lives in estimated_unit_cost instead — see StockBatch's "
              "own model comment). All 19 of the production baseline's INV-8 violations turned "
              "out to be legitimate pre-existing negative_placeholder rows dated across "
              "2026-08-17 to 2026-09-19, not oversell corruption; 'repairing' them would have "
              "created a second placeholder per product, breaking the one-aggregate-per-product "
              "assumption absorb_neg_placeholder relies on. Skipped from this check entirely now, "
              "matching how the rest of the app already excludes this batch_type from valuation "
              "and FIFO-cost queries.")
    batches = session.query(StockBatch).filter(StockBatch.batch_type != 'negative_placeholder').all()
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
                # type='discount' entries are an audit-trail duplicate of (part of) the same
                # amount already stored in allocated_discount below — see suppliers.py's
                # receive code: "Append per-line discount to batch_addl for audit trail"
                # appends the line-level share into additional_costs purely for display,
                # while allocated_discount already carries that same line-level share plus
                # the invoice-level share. Summing every entry here and also subtracting
                # allocated_discount would double-subtract the line-level portion.
                overhead_total = sum((_d(e.get('amount', 0)) for e in entries
                                      if e.get('type') != 'discount'), Decimal('0'))
            except (ValueError, TypeError):
                r.violations.append({'batch_id': b.id, 'check': 'additional_costs JSON parse',
                                      'error': 'unparseable additional_costs'})
                continue
        allocated_discount = _d(b.allocated_discount) or Decimal('0')
        expected_final = _q2(base_incl_vat + overhead_total - allocated_discount)
        if final_incl_vat is not None and _q2(final_incl_vat) != expected_final:
            r.violations.append({
                'batch_id': b.id,
                'check': ('final_cost_incl_vat == base_cost_incl_vat + '
                          'sum(non-discount additional_costs) - allocated_discount'),
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


def check_inv7_vat_closure(session):
    r = Result('INV-7', 'VAT closure', 'high', ['pilot_readiness'], 'zero')
    r.note = ('Only checks vat_method=per_line headers (P1-1 checkout-forward sales) — '
              'formula: sum of per-line rounded vat_amount == header.total_vat, and '
              'header subtotals (standard+zero_rated+exempt) sum to total_excl_vat, and '
              'total_excl_vat + total_vat == total_incl_vat. legacy_flat headers '
              '(scripts/backfill_vat_headers.py) have no per-line data to check against '
              'by design — reported via skipped_count with the reason, not silently '
              'ignored and not treated as a violation.')
    headers = session.query(SaleHeader).all()
    per_line = [h for h in headers if h.vat_method == 'per_line']
    r.skipped_count = len(headers) - len(per_line)
    r.checked_count = len(per_line)
    for h in per_line:
        lines = session.query(Sale).filter(
            Sale.sale_id == h.sale_id, Sale.voided == False
        ).all()
        line_vat_sum = _q2(sum((_d(l.vat_amount) or Decimal('0') for l in lines), Decimal('0')))
        header_total_vat = _q2(h.total_vat)
        if line_vat_sum != header_total_vat:
            r.violations.append({
                'sale_id': h.sale_id, 'check': 'sum(line.vat_amount) == header.total_vat',
                'stored': str(header_total_vat), 'expected': str(line_vat_sum),
            })
            continue

        subtotal_sum = _q2(
            (_d(h.standard_rated_subtotal) or Decimal('0'))
            + (_d(h.zero_rated_subtotal) or Decimal('0'))
            + (_d(h.exempt_subtotal) or Decimal('0'))
        )
        total_excl = _q2(h.total_excl_vat)
        if subtotal_sum != total_excl:
            r.violations.append({
                'sale_id': h.sale_id,
                'check': 'standard+zero_rated+exempt subtotals == total_excl_vat',
                'stored': str(total_excl), 'expected': str(subtotal_sum),
            })
            continue

        expected_incl = _q2((_d(h.total_excl_vat) or Decimal('0')) + (_d(h.total_vat) or Decimal('0')))
        total_incl = _q2(h.total_incl_vat)
        if total_incl != expected_incl:
            r.violations.append({
                'sale_id': h.sale_id, 'check': 'total_excl_vat + total_vat == total_incl_vat',
                'stored': str(total_incl), 'expected': str(expected_incl),
            })
    r.status = 'FAIL' if r.violations else 'PASS'
    return r


def check_inv11_projection_rebuild(session):
    """Rev 5 P2-0a — the cutover gate. Independent SQL aggregate: SUM(qty_delta) per
    batch from stock_movements, compared against the live StockBatch.qty_remaining_base.
    Deliberately does NOT reuse consume_fifo/reverse_fifo/write_stock_movement or any
    other part of the write path — a rebuild that reused the writer would agree with
    itself while both are wrong, which is the failure this check exists to catch.

    This does not mean the ledger is authoritative yet, and a FAIL here during the
    dual-write ramp-up is expected, not alarming, PROVIDED it is entirely accounted for
    by skipped_count (batches with zero movements — no write path has touched them
    since dual-write started: pre-existing batches, or paths not yet wired, see
    write_stock_movement's docstring in helpers.py for the coverage map). A violation
    is a batch that DOES have movements whose sum disagrees with the live quantity —
    that is a real bug in the dual-write, the exact thing this gate is for.
    """
    r = Result('INV-11', 'Projection rebuild', 'critical', ['cutover'], 'zero, excluding not-yet-covered batches')
    ledger_sums = dict(
        session.query(StockMovement.batch_id, func.sum(StockMovement.qty_delta))
        .group_by(StockMovement.batch_id)
        .all()
    )
    batches = session.query(StockBatch).all()
    for b in batches:
        ledger_sum = ledger_sums.get(b.id)
        if ledger_sum is None:
            r.skipped_count += 1
            continue
        r.checked_count += 1
        live_qty = _d(b.qty_remaining_base)
        if _q4(ledger_sum) != _q4(live_qty):
            r.violations.append({
                'batch_id': b.id, 'product_id': b.product_id,
                'ledger_sum': str(_q4(ledger_sum)), 'live_qty_remaining_base': str(_q4(live_qty)),
            })
    r.note = (f'{r.skipped_count} of {len(batches)} batches have zero movements (not yet '
              f'covered by dual-write — see docstring). {r.checked_count} batches checked '
              f'against the ledger sum.')
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
        check_inv3_typed_source_resolves(session),
        check_inv4_no_free_stock(session),
        check_inv5_consignment_closure(session),
        check_inv6_cogs_agrees(session),
        check_inv7_vat_closure(session),
        check_inv8_value_baseline(session),
        check_inv9_allocation_closure(session),
        not_applicable_here('INV-10', 'Transaction immutability',
                             'Enforced via DB trigger + CI per Rev 5 Section 6, not a reconcile.py check.'),
        check_inv11_projection_rebuild(session),
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

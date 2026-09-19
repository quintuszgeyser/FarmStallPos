"""P4-1 apply-step characterization tests.

Locks in a fix found during rehearsal against production, before anything
was written: apply_inv4 must skip a batch whose base_cost_incl_vat is a
literal 0.0000 (not NULL) — such a batch is also in INV-9's checked set, and
setting cost_per_base_unit alone would desync it from base_cost_incl_vat/
final_cost_incl_vat (both still 0), turning a resolved INV-4 violation into
a fresh INV-9 one. See scripts/p4_apply.py's apply_inv4 for the full story.
"""
import sys
from decimal import Decimal

sys.path.insert(0, 'scripts')

from models import StockMovement, db  # noqa: E402
from scripts.p4_apply import apply_inv4, apply_inv9_correction  # noqa: E402
from scripts.reconcile import check_inv4_no_free_stock, check_inv9_allocation_closure  # noqa: E402
from tests.factories import make_product, make_stock_batch  # noqa: E402
from tests.helpers import D  # noqa: E402


def test_apply_inv4_skips_a_batch_with_a_real_nonzero_vat_chain_value(db_session):
    # base_cost_total holds a REAL, non-zero number that disagrees with
    # cost_per_base_unit=0 — there's something on the row that a joint estimate-based
    # update would silently overwrite, so this must still be a manual-review case.
    product = make_product(product_type='stock_item', name='P4 Apply Real VAT Data')
    zero_cost_batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=D('115.00'), final_cost_incl_vat=D('115.00'), base_cost_total=D('100.00'),
    )
    make_stock_batch(product, batch_type='normal', cost_per_base_unit=D('5.00'))
    db_session.flush()

    log = []
    applied = apply_inv4(db_session, zero_cost_batch.id, 'test-run', log)

    assert applied is False
    assert log[0]['action'] == 'SKIPPED'
    assert 'holds a real non-zero value' in log[0]['reason']
    assert zero_cost_batch.cost_per_base_unit == D('0')  # untouched


def test_apply_inv4_fills_in_an_all_zero_vat_chain_via_weighted_average(db_session):
    # base_cost_total/base_cost_incl_vat/final_cost_incl_vat are all exactly zero, not
    # NULL — nothing on the row to lose, so a joint update is safe once a cost is found.
    product = make_product(product_type='stock_item', name='P4 Apply All-Zero VAT Chain')
    zero_cost_batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=D('0'), final_cost_incl_vat=D('0'), base_cost_total=D('0'),
        qty_purchased_base=D('100'),
    )
    make_stock_batch(product, batch_type='normal', cost_per_base_unit=D('5.00'))
    db_session.flush()

    log = []
    applied = apply_inv4(db_session, zero_cost_batch.id, 'test-run', log)

    assert applied is True
    assert log[0]['is_estimate'] is False
    assert zero_cost_batch.cost_per_base_unit == D('5.000000')
    assert zero_cost_batch.base_cost_total == D('500.0000')  # 5.00 * 100
    assert zero_cost_batch.base_cost_incl_vat == D('500.0000')
    assert zero_cost_batch.final_cost_incl_vat == D('500.0000')

    db_session.flush()
    result = check_inv4_no_free_stock(db_session)
    assert zero_cost_batch.id not in {v['batch_id'] for v in result.violations}
    inv9 = check_inv9_allocation_closure(db_session)
    assert zero_cost_batch.id not in {v['batch_id'] for v in inv9.violations}


def test_apply_inv4_falls_back_to_price_margin_estimate_with_no_comparable_batch(db_session):
    product = make_product(product_type='stock_item', name='P4 Apply Price Margin Estimate',
                            price=D('94.00'), margin_pct=D('10.80'))
    zero_cost_batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=None, qty_purchased_base=D('10'),
    )
    # No other batch of this product exists — forces the estimate fallback.
    db_session.flush()

    log = []
    applied = apply_inv4(db_session, zero_cost_batch.id, 'test-run', log)

    assert applied is True
    assert log[0]['is_estimate'] is True
    assert zero_cost_batch.cost_per_base_unit == D('84.837545')  # 94.00 / 1.108


def test_apply_inv4_skips_when_all_zero_and_no_estimate_available(db_session):
    product = make_product(product_type='stock_item', name='P4 Apply Nothing To Go On',
                            price=None, margin_pct=None)
    zero_cost_batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=D('0'), final_cost_incl_vat=D('0'), base_cost_total=D('0'),
    )
    db_session.flush()

    log = []
    applied = apply_inv4(db_session, zero_cost_batch.id, 'test-run', log)

    assert applied is False
    assert log[0]['action'] == 'SKIPPED'
    assert 'no price+margin_pct' in log[0]['reason']


def test_apply_inv4_applies_when_base_cost_incl_vat_is_genuinely_null(db_session):
    product = make_product(product_type='stock_item', name='P4 Apply Null VAT Cols')
    zero_cost_batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=None,
    )
    make_stock_batch(product, batch_type='normal', cost_per_base_unit=D('4.00'))
    db_session.flush()

    log = []
    applied = apply_inv4(db_session, zero_cost_batch.id, 'test-run', log)

    assert applied is True
    assert log[0]['action'] == 'APPLIED'
    assert zero_cost_batch.cost_per_base_unit == D('4.000000')

    db_session.flush()
    result = check_inv4_no_free_stock(db_session)
    assert zero_cost_batch.id not in {v['batch_id'] for v in result.violations}


def test_recosting_a_batch_with_populated_vat_cols_would_have_broken_inv9(db_session):
    # Documents WHY the skip in apply_inv4 exists: directly setting cost_per_base_unit
    # on a batch with base_cost_incl_vat/final_cost_incl_vat still at 0 creates a fresh
    # INV-9 violation that a target-list-only check would miss.
    product = make_product(product_type='stock_item', name='P4 Apply INV9 Side Effect')
    batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=D('0'), final_cost_incl_vat=D('0'), base_cost_total=D('0'),
        qty_purchased_base=D('100'),
    )
    db_session.flush()
    before = check_inv9_allocation_closure(db_session)
    assert batch.id not in {v['batch_id'] for v in before.violations}

    batch.cost_per_base_unit = D('3.500000')  # the naive, unguarded fix
    db_session.flush()

    after = check_inv9_allocation_closure(db_session)
    assert batch.id in {v['batch_id'] for v in after.violations}


def test_apply_inv9_correction_fixes_incl_vat_fields_leaving_cost_per_unit_unchanged(db_session):
    # Reproduces the invoice-58 shape: cost_per_base_unit already matches
    # base_cost_total/qty; only base/final_cost_incl_vat are wrong.
    product = make_product(product_type='stock_item', name='P4 INV9 Overhead Only')
    batch = make_stock_batch(
        product, batch_type='normal', base_cost_total=D('1100.00'), vat_amount=None,
        base_cost_incl_vat=D('1600.00'), final_cost_incl_vat=D('1600.00'),
        cost_per_base_unit=D('0.055000'), qty_purchased_base=D('20000'),
        qty_remaining_base=D('17040'),
    )
    db_session.flush()

    log = []
    applied = apply_inv9_correction(db_session, batch.id, 'test-run', log)

    assert applied is True
    assert batch.base_cost_incl_vat == D('1100.00')
    assert batch.final_cost_incl_vat == D('1100.00')
    assert batch.cost_per_base_unit == D('0.055000') or str(batch.cost_per_base_unit).startswith('0.0550')
    assert log[0]['cost_per_unit_changed'] is False

    db_session.flush()
    result = check_inv9_allocation_closure(db_session)
    assert batch.id not in {v['batch_id'] for v in result.violations}


def test_apply_inv9_correction_fixes_cost_per_unit_when_batch_fully_unconsumed(db_session):
    # Reproduces batch 588's shape: base/final_cost_incl_vat already correct;
    # cost_per_base_unit was computed from the ex-VAT total instead.
    product = make_product(product_type='stock_item', name='P4 INV9 Cost Per Unit')
    batch = make_stock_batch(
        product, batch_type='normal', base_cost_total=D('446.06'), vat_amount=D('54.60'),
        base_cost_incl_vat=D('500.66'), final_cost_incl_vat=D('500.66'),
        cost_per_base_unit=D('223.030000'), qty_purchased_base=D('2'), qty_remaining_base=D('2'),
    )
    db_session.flush()

    log = []
    applied = apply_inv9_correction(db_session, batch.id, 'test-run', log)

    assert applied is True
    assert batch.cost_per_base_unit == D('250.33')
    assert log[0]['cost_per_unit_changed'] is True


def test_apply_inv9_correction_skips_cost_per_unit_change_on_a_partially_consumed_batch(db_session):
    product = make_product(product_type='stock_item', name='P4 INV9 Partially Consumed')
    batch = make_stock_batch(
        product, batch_type='normal', base_cost_total=D('446.06'), vat_amount=D('54.60'),
        base_cost_incl_vat=D('500.66'), final_cost_incl_vat=D('500.66'),
        cost_per_base_unit=D('223.030000'), qty_purchased_base=D('2'), qty_remaining_base=D('1'),
    )
    db_session.flush()

    log = []
    applied = apply_inv9_correction(db_session, batch.id, 'test-run', log)

    assert applied is False
    assert log[0]['action'] == 'SKIPPED'
    assert batch.cost_per_base_unit == D('223.030000')  # untouched


def test_apply_writes_an_opening_balance_backfill_for_a_first_movement_with_live_qty(db_session):
    product = make_product(product_type='stock_item', name='P4 Opening Balance Backfill')
    batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=None, qty_purchased_base=D('50'), qty_remaining_base=D('30'),
    )
    make_stock_batch(product, batch_type='normal', cost_per_base_unit=D('9.00'))
    db_session.flush()

    log = []
    apply_inv4(db_session, batch.id, 'test-run', log)
    db_session.flush()

    movements = db.session.query(StockMovement).filter(StockMovement.batch_id == batch.id).all()
    types = {m.movement_type for m in movements}
    assert 'OPENING_BALANCE_BACKFILL' in types
    backfill = next(m for m in movements if m.movement_type == 'OPENING_BALANCE_BACKFILL')
    assert backfill.qty_delta == D('30')
    assert backfill.source_type == 'migration'


def test_apply_does_not_backfill_when_batch_is_fully_sold_out(db_session):
    product = make_product(product_type='stock_item', name='P4 No Backfill Needed')
    batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=None, qty_purchased_base=D('50'), qty_remaining_base=D('0'),
    )
    make_stock_batch(product, batch_type='normal', cost_per_base_unit=D('9.00'))
    db_session.flush()

    log = []
    apply_inv4(db_session, batch.id, 'test-run', log)
    db_session.flush()

    movements = db.session.query(StockMovement).filter(StockMovement.batch_id == batch.id).all()
    assert len(movements) == 1
    assert movements[0].movement_type == 'COST_CORRECTION'

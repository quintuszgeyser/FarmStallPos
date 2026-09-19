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

from scripts.p4_apply import apply_inv4  # noqa: E402
from scripts.reconcile import check_inv4_no_free_stock, check_inv9_allocation_closure  # noqa: E402
from tests.factories import make_product, make_stock_batch  # noqa: E402
from tests.helpers import D  # noqa: E402


def test_apply_inv4_skips_a_batch_with_populated_base_cost_incl_vat(db_session):
    product = make_product(product_type='stock_item', name='P4 Apply Populated VAT Cols')
    zero_cost_batch = make_stock_batch(
        product, batch_type='normal', cost_per_base_unit=D('0'),
        base_cost_incl_vat=D('0'), final_cost_incl_vat=D('0'), base_cost_total=D('0'),
    )
    # Give the product one other positive-cost batch so a weighted average IS computable —
    # proves the skip is because of base_cost_incl_vat, not a missing average.
    make_stock_batch(product, batch_type='normal', cost_per_base_unit=D('5.00'))
    db_session.flush()

    log = []
    applied = apply_inv4(db_session, zero_cost_batch.id, 'test-run', log)

    assert applied is False
    assert log[0]['action'] == 'SKIPPED'
    assert 'base_cost_incl_vat is populated' in log[0]['reason']
    assert zero_cost_batch.cost_per_base_unit == D('0')  # untouched


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

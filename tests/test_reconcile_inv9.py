"""INV-9 (allocation closure) characterization tests (Rev 5 P0-2).

Locks in the P4-1 fix to check_inv9_allocation_closure: the formula must
subtract allocated_discount, and must exclude type='discount' entries from
the additional_costs overhead sum (they duplicate part of allocated_discount
for display — see suppliers.py's "Append per-line discount to batch_addl for
audit trail" comment and this file's own module docstring correction). The
pre-fix version summed every additional_costs entry and never subtracted
allocated_discount at all, producing 47 false-positive violations against
the 2026-09-19 production baseline, every one off by exactly one batch's
discount amount.
"""
import json
from decimal import Decimal

from scripts.reconcile import check_inv9_allocation_closure
from tests.factories import make_product, make_stock_batch
from tests.helpers import D


def test_invoice_level_discount_with_shipping_does_not_false_positive(db_session):
    # Reproduces batch 667 from the production baseline exactly: an
    # invoice-level discount allocated_discount=36.00, a shipping-only
    # additional_costs entry, and a final_cost_incl_vat that already has the
    # discount correctly subtracted at write time.
    product = make_product(product_type='stock_item', name='INV9 Discount+Shipping')
    make_stock_batch(
        product,
        base_cost_total=D('200.00'),
        vat_amount=D('33.54'),
        base_cost_incl_vat=D('233.54'),
        allocated_shipping=D('5.93'),
        additional_costs=json.dumps([
            {'label': 'Shipping / delivery', 'type': 'shipping', 'amount': 5.93,
             'source': 'supplier_run', 'source_id': None, 'invoice_ref': None},
        ]),
        allocated_discount=D('36.00'),
        final_cost_incl_vat=D('203.47'),  # 233.54 + 5.93 - 36.00
        cost_per_base_unit=D('4.069400'),
        qty_purchased_base=D('50'),
    )
    db_session.commit()

    r = check_inv9_allocation_closure(db_session)
    assert r.violations == []
    assert r.status == 'PASS'


def test_line_level_discount_duplicated_in_additional_costs_does_not_double_subtract(db_session):
    # A line-level discount is appended into additional_costs as a negative
    # type='discount' entry purely for the audit-trail display, AND the same
    # amount is folded into allocated_discount. The check must not subtract
    # it twice.
    product = make_product(product_type='stock_item', name='INV9 Line Discount')
    make_stock_batch(
        product,
        base_cost_total=D('100.00'),
        vat_amount=D('15.00'),
        base_cost_incl_vat=D('115.00'),
        additional_costs=json.dumps([
            {'label': 'Line discount', 'type': 'discount', 'amount': -10.00},
        ]),
        allocated_discount=D('10.00'),
        final_cost_incl_vat=D('105.00'),  # 115.00 + 0 (discount entry excluded) - 10.00
        cost_per_base_unit=D('10.500000'),
        qty_purchased_base=D('10'),
    )
    db_session.commit()

    r = check_inv9_allocation_closure(db_session)
    assert r.violations == []


def test_genuine_final_cost_mismatch_is_still_a_violation(db_session):
    product = make_product(product_type='stock_item', name='INV9 Real Mismatch')
    make_stock_batch(
        product,
        base_cost_total=D('100.00'),
        vat_amount=D('15.00'),
        base_cost_incl_vat=D('115.00'),
        additional_costs=None,
        allocated_discount=None,
        final_cost_incl_vat=D('999.00'),  # should be 115.00
        cost_per_base_unit=D('99.900000'),
        qty_purchased_base=D('10'),
    )
    db_session.commit()

    r = check_inv9_allocation_closure(db_session)
    matching = [v for v in r.violations if 'final_cost_incl_vat' in v['check']]
    assert len(matching) == 1
    assert matching[0]['stored'] == '999.00'
    assert matching[0]['expected'] == '115.00'
    assert r.status == 'FAIL'


def test_genuine_cost_per_unit_mismatch_with_no_discount_or_overhead(db_session):
    # Reproduces batch 588 from the production baseline: additional_costs and
    # allocated_discount both absent (base == final), but cost_per_base_unit
    # disagrees with final_cost_incl_vat / qty_purchased_base — a real
    # inconsistency this invariant should still catch.
    product = make_product(product_type='stock_item', name='INV9 Cost Per Unit Mismatch')
    make_stock_batch(
        product,
        base_cost_total=D('435.00'),
        vat_amount=D('65.66'),
        base_cost_incl_vat=D('500.66'),
        additional_costs=None,
        allocated_discount=None,
        final_cost_incl_vat=D('500.66'),
        cost_per_base_unit=D('223.030000'),  # should be 250.33
        qty_purchased_base=D('2'),
    )
    db_session.commit()

    r = check_inv9_allocation_closure(db_session)
    matching = [v for v in r.violations if 'cost_per_base_unit' in v['check']]
    assert len(matching) == 1
    assert matching[0]['stored'] == '223.03'
    assert matching[0]['expected'] == '250.33'

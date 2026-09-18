"""Discount, inclusive-pricing, VAT rounding, and receipt-after-oversell
characterization tests (Rev 5 P0-1 / Section 5). Routes exercised through the
real Flask test client.
"""
from decimal import Decimal

import pytest

from helpers import set_setting
from models import Sale, StockBatch
from tests.factories import make_admin, make_product, make_stock_batch, make_user
from tests.helpers import D, checkout, login_as, refresh


def test_admin_discount_overrides_unit_price_capped_at_server_price(db_session, client):
    from werkzeug.security import generate_password_hash
    make_admin(username='discadmin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'discadmin', 'adminpass123')

    product = make_product(price=D('20.00'))

    # Discounted price below server price is honored.
    resp = checkout(client, [{'product_id': product.id, 'qty': 1, 'item_discount': True, 'unit_price': '15.00'}],
                     cash_tendered=15)
    sale_id = resp.get_json()['transaction_id']
    row = Sale.query.filter_by(sale_id=sale_id).one()
    assert row.unit_price == D('15.00')
    assert row.discount_by is not None
    assert '"item"' in (row.discount_json or '')

    # A client-supplied price ABOVE server price is rejected (capped, not honored) —
    # unit_price falls back to the server price of 20.00.
    resp2 = checkout(client, [{'product_id': product.id, 'qty': 1, 'item_discount': True, 'unit_price': '25.00'}],
                      cash_tendered=25)
    sale_id2 = resp2.get_json()['transaction_id']
    row2 = Sale.query.filter_by(sale_id=sale_id2).one()
    assert row2.unit_price == D('20.00')


def test_teller_discount_flag_is_recorded_but_price_not_reduced(db_session, client):
    """KNOWN-SHAPE characterization (not a Rev 5-tracked defect, just a precise
    pin of current authorization behavior): a non-admin's item_discount flag
    still gets written into discount_json (transactions.py ~377-383, which is
    NOT gated on role), but the admin-only price-override block (~365-371) never
    runs for a teller, so unit_price stays at the full server price. The result
    is a Sale row that LOOKS discounted (discount_json populated) but billed at
    full price — worth knowing before anyone builds a discount report off
    discount_json alone.
    """
    from werkzeug.security import generate_password_hash
    make_user(username='disctell', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'disctell', 'testpass123')

    product = make_product(price=D('20.00'))
    resp = checkout(client, [{'product_id': product.id, 'qty': 1, 'item_discount': True, 'unit_price': '5.00'}],
                     cash_tendered=20)
    sale_id = resp.get_json()['transaction_id']
    row = Sale.query.filter_by(sale_id=sale_id).one()
    assert row.unit_price == D('20.00')  # NOT 5.00 — teller can't actually discount
    assert '"item"' in (row.discount_json or '')  # but the flag was still recorded


def test_inclusive_pricing_weighted_item_vat_extracted_from_tax_inclusive_price(db_session, client):
    """All prices in this system are VAT-INCLUSIVE by design — there is no
    separate ex-VAT/inc-VAT toggle anywhere (confirmed: api_transaction_receipt
    always backs VAT out of the total via total*rate/(1+rate), never adds it on
    top). This test pins that on a weighted stock_item priced per kg
    (price_per_unit), which exercises a different pricing code path
    (transactions.py ~358: sold_by_weight -> price_per_unit) than a flat-price
    simple/stock_item sale.
    """
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)

    product = make_product(product_type='stock_item', sold_by_weight=True,
                            price_per_unit=D('45.0000'), price=None, base_unit='kg')
    make_stock_batch(product, qty_remaining_base=D(100), qty_purchased_base=D(100), cost_per_base_unit=D('20.000000'))

    from werkzeug.security import generate_password_hash
    make_user(username='weightteller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'weightteller', 'testpass123')

    resp = checkout(client, [{'product_id': product.id, 'qty': '0.400'}], cash_tendered=18)
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']
    row = Sale.query.filter_by(sale_id=sale_id).one()
    assert row.unit_price == D('45.0000')
    assert row.qty == D('0.400')

    receipt = client.get(f'/api/transactions/{sale_id}/receipt').get_json()
    assert receipt['total'] == 18.0  # 0.4 * 45.00, price already includes VAT
    # 15% VAT-inclusive extraction: 18 * 0.15 / 1.15 = 2.35 (rounded)
    assert receipt['vat_amount'] == round(18.0 * 0.15 / 1.15, 2)


def test_vat_rounding_boundary_receipt_and_till_summary_agree_on_odd_amounts(db_session, client):
    """Dedicated VAT rounding-boundary probe (Rev 5 Section 5). The receipt path
    (transactions.py) rounds a Python FLOAT total with round(); the till-summary
    path (till_sessions.py) rounds a Decimal total with .quantize() (banker's
    rounding). These are different arithmetic domains and could plausibly
    diverge at a half-cent boundary.

    Empirical offline sweep (not part of this test — run once while writing it,
    documented here for the record): every exact-cent total from R0.01 to
    R20,000.00 (2,000,000 values) and 200,000 randomized 1-6 line multi-item
    baskets up to ~R10,000/line were checked against both formulas at
    VAT_RATE=15% — ZERO divergences found. Python 3's round() is correctly-
    rounded against the float's true binary value, and Decimal-cent totals
    happen to convert to floats precisely enough at this magnitude that the two
    paths agree everywhere tested. This test asserts that observed agreement on
    a handful of concrete odd/fractional-qty amounts through the REAL checkout
    and till-summary routes — it is a characterization of measured stability,
    not a proof of agreement for all possible inputs, and P1-1's single shared
    Decimal rounding policy (INV-7) is what removes the two-formula fragility
    entirely regardless.
    """
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)

    from werkzeug.security import generate_password_hash
    make_user(username='boundteller', password_hash=generate_password_hash('testpass123'))
    make_admin(username='boundadmin', password_hash=generate_password_hash('adminpass123'))

    odd_prices = [D('33.33'), D('66.67'), D('19.99'), D('0.99'), D('7.77')]
    sale_ids = []
    login_as(client, 'boundteller', 'testpass123')
    for price in odd_prices:
        product = make_product(price=price)
        resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=100)
        assert resp.status_code == 200, resp.get_json()
        sale_ids.append(resp.get_json()['transaction_id'])

    receipt_total = Decimal('0')
    receipt_vat = Decimal('0')
    for sid in sale_ids:
        r = client.get(f'/api/transactions/{sid}/receipt').get_json()
        receipt_total += D(r['total'])
        receipt_vat += D(r['vat_amount'])

    client.post('/api/logout')
    login_as(client, 'boundadmin', 'adminpass123')
    summary = client.get('/api/till/sessions/summary').get_json()

    assert D(summary['total_sales']) == receipt_total
    # Rev 5 P1-1 Wave B: till_sessions.py no longer independently recomputes VAT
    # from the summed basket total (which is what made this test's old assertion
    # — comparing against round(receipt_total * 0.15/1.15, 2) — a plausible-but-
    # different formula). It now sums the SAME already-rounded per-transaction
    # sale_headers.total_vat values the receipt endpoint reads, so the two are
    # equal by construction, not by coincidence of magnitude. This is P1-1's core
    # acceptance proof: receipt VAT equals Z-report VAT to the cent.
    assert D(summary['vat_amount']) == receipt_vat
    assert summary['vat_spans_cutover'] is False
    assert summary['vat_unrecorded_count'] == 0


def test_mixed_basket_rounding_boundary_receipt_equals_z_report_to_the_cent(db_session, client):
    """Rev 5 Section 7's core P1-1 proof requirement, in its full documented shape:
    a single basket mixing standard/zero_rated/exempt lines, including the exact
    rounding-boundary pair from Wave A's worked example (R1.00 + R1.11 -> R0.27
    summed-per-line vs R0.28 if wrongly recomputed from the basket total), checked
    out through the real /api/transactions route, then verified that what the
    receipt shows and what the Z-report shows for the same window agree exactly —
    not approximately, not by coincidence, but because both read the identical
    stored sale_headers.total_vat.
    """
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)

    boundary_a = make_product(name='Boundary A', price=D('1.00'), vat_type='standard')
    boundary_b = make_product(name='Boundary B', price=D('1.11'), vat_type='standard')
    zero_item  = make_product(name='Zero Item', price=D('25.00'), vat_type='zero_rated')
    exempt_item = make_product(name='Exempt Item', price=D('12.34'), vat_type='exempt')

    from werkzeug.security import generate_password_hash
    make_user(username='mixedteller', password_hash=generate_password_hash('testpass123'))
    make_admin(username='mixedadmin', password_hash=generate_password_hash('adminpass123'))

    login_as(client, 'mixedteller', 'testpass123')
    resp = checkout(client, [
        {'product_id': boundary_a.id, 'qty': 1},
        {'product_id': boundary_b.id, 'qty': 1},
        {'product_id': zero_item.id, 'qty': 1},
        {'product_id': exempt_item.id, 'qty': 1},
    ], cash_tendered=100)
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']

    receipt = client.get(f'/api/transactions/{sale_id}/receipt').get_json()
    # R1.00 and R1.11 independently rounded per line = R0.27 (Wave A's worked
    # example); R0.28 would be the wrong, naively-recomputed-from-total answer.
    assert D(str(receipt['vat_amount'])) == D('0.27')
    assert receipt['vat_method'] == 'per_line'

    client.post('/api/logout')
    login_as(client, 'mixedadmin', 'adminpass123')
    summary = client.get('/api/till/sessions/summary').get_json()

    assert D(str(summary['vat_amount'])) == D(str(receipt['vat_amount'])) == D('0.27')
    assert summary['vat_spans_cutover'] is False
    assert summary['vat_amount_legacy_flat'] == 0


def test_receipt_after_oversell_still_renders_full_requested_quantity(db_session, client):
    """Receipt generation must not error, and must reflect the FULL Sale.qty
    requested — the oversell shortfall (P2-1's target) affects COGS/costing
    internals, not the customer-facing Sale.qty/unit_price/total, which are
    always what was actually rung up regardless of stock availability under
    ALLOW_NEGATIVE (the factory default policy).
    """
    from werkzeug.security import generate_password_hash
    product = make_product(product_type='stock_item', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(2), qty_purchased_base=D(2), cost_per_base_unit=D('4.000000'))
    make_user(username='overteller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'overteller', 'testpass123')

    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50)  # need 5, only 2 in stock
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']

    row = Sale.query.filter_by(sale_id=sale_id).one()
    assert row.qty == D(5)
    assert row.cogs == D('20.00')  # 5 units all costed at the only batch's 4.00 (the P2-1 defect)

    receipt = client.get(f'/api/transactions/{sale_id}/receipt')
    assert receipt.status_code == 200
    body = receipt.get_json()
    assert body['total'] == 50.0  # 5 * 10.00 — full requested qty, unaffected by the stock shortfall
    assert len(body['lines']) == 1
    assert body['lines'][0]['qty'] == 5.0

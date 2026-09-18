"""Rev 5 P1-1 — VAT snapshot proof tests.

These exercise the NEW checkout-time VAT stamping (blueprints/transactions.py
api_transactions_post) and the sale_headers aggregate it writes. They do NOT
touch the three receipt-rendering endpoints or till_sessions.py's Z-report —
those still read from current settings via the old flat-rate formula until
Wave B rewires them (see the P1-1 checkout-snapshot commit message). The
Wave-1 test pinning that old behavior
(tests/test_checkout.py::test_mixed_vat_basket_applies_flat_rate_ignoring_
product_vat_type) is unaffected by anything here and still passes unchanged.
"""
from decimal import Decimal

from werkzeug.security import generate_password_hash

from helpers import set_setting
from models import Sale, SaleHeader
from tests.factories import make_product, make_user
from tests.helpers import D, checkout, login_as


def _teller(client, username='vat_teller'):
    make_user(username=username, password_hash=generate_password_hash('testpass123'))
    login_as(client, username, 'testpass123')


def test_standard_rated_line_computes_vat_and_persists_header(db_session, client):
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)
    product = make_product(name='Standard Widget', price=D('11.50'), vat_type='standard')
    _teller(client)

    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=20)
    sale_id = resp.get_json()['transaction_id']

    line = Sale.query.filter_by(sale_id=sale_id).one()
    # 11.50 / 1.15 = 10.00 exactly -> no rounding ambiguity, a clean sanity check.
    assert line.vat_classification == 'standard'
    assert line.vat_rate == D('15')
    assert line.amount_excl == D('10.00')
    assert line.vat_amount == D('1.50')
    assert line.amount_incl == D('11.50')
    assert line.amount_excl + line.vat_amount == line.amount_incl

    header = SaleHeader.query.filter_by(sale_id=sale_id).one()
    assert header.vat_method == 'per_line'
    assert header.standard_rated_subtotal == D('10.00')
    assert header.zero_rated_subtotal == D('0.00')
    assert header.exempt_subtotal == D('0.00')
    assert header.total_excl_vat == D('10.00')
    assert header.total_vat == D('1.50')
    assert header.total_incl_vat == D('11.50')
    assert header.vat_rate_snapshot == D('15')
    assert header.vat_registered_snapshot is True
    assert header.cash_tendered == D('20.00')


def test_zero_rated_line_has_zero_vat(db_session, client):
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)
    product = make_product(name='Zero Rated Bread', price=D('10.00'), vat_type='zero_rated')
    _teller(client, 'vat_teller_zero')

    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=10)
    sale_id = resp.get_json()['transaction_id']

    line = Sale.query.filter_by(sale_id=sale_id).one()
    assert line.vat_classification == 'zero_rated'
    assert line.vat_rate == D('0')
    assert line.vat_amount == D('0.00')
    assert line.amount_excl == line.amount_incl == D('10.00')

    header = SaleHeader.query.filter_by(sale_id=sale_id).one()
    assert header.zero_rated_subtotal == D('10.00')
    assert header.standard_rated_subtotal == D('0.00')
    assert header.total_vat == D('0.00')


def test_exempt_line_has_zero_vat_and_classification_recorded(db_session, client):
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)
    product = make_product(name='Exempt Financial Service Fee', price=D('50.00'), vat_type='exempt')
    _teller(client, 'vat_teller_exempt')

    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=50)
    sale_id = resp.get_json()['transaction_id']

    line = Sale.query.filter_by(sale_id=sale_id).one()
    assert line.vat_classification == 'exempt'
    assert line.vat_amount == D('0.00')

    header = SaleHeader.query.filter_by(sale_id=sale_id).one()
    assert header.exempt_subtotal == D('50.00')
    assert header.zero_rated_subtotal == D('0.00')
    assert header.standard_rated_subtotal == D('0.00')
    assert header.total_vat == D('0.00')


def test_mixed_basket_header_buckets_and_total_vat_only_standard(db_session, client):
    """The fix for the Wave-1 known-defect test: a basket mixing all three
    classifications must tax ONLY the standard-rated line, and the header must
    report each classification's subtotal in its own bucket, not commingled.
    """
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)
    standard = make_product(name='Standard Item', price=D('11.50'), vat_type='standard')
    zero = make_product(name='Zero Rated Item', price=D('10.00'), vat_type='zero_rated')
    exempt = make_product(name='Exempt Item', price=D('20.00'), vat_type='exempt')
    _teller(client, 'vat_teller_mixed')

    resp = checkout(client, [
        {'product_id': standard.id, 'qty': 1},
        {'product_id': zero.id, 'qty': 1},
        {'product_id': exempt.id, 'qty': 1},
    ], cash_tendered=50)
    sale_id = resp.get_json()['transaction_id']

    header = SaleHeader.query.filter_by(sale_id=sale_id).one()
    assert header.standard_rated_subtotal == D('10.00')   # 11.50 excl. VAT
    assert header.zero_rated_subtotal == D('10.00')
    assert header.exempt_subtotal == D('20.00')
    assert header.total_excl_vat == D('40.00')             # 10 + 10 + 20
    assert header.total_vat == D('1.50')                   # only the standard line
    assert header.total_incl_vat == D('41.50')             # 11.50 + 10.00 + 20.00


def test_vat_not_registered_zeroes_amounts_but_keeps_classification(db_session, client):
    set_setting('vat_registered', 'false')
    set_setting('vat_rate', 15)
    product = make_product(name='Standard Item Unregistered', price=D('11.50'), vat_type='standard')
    _teller(client, 'vat_teller_unreg')

    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=20)
    sale_id = resp.get_json()['transaction_id']

    line = Sale.query.filter_by(sale_id=sale_id).one()
    # Classification is still recorded for audit purposes even though no VAT is charged —
    # this is NOT the same thing as "zero_rated"; the product IS standard-rated, the shop
    # just isn't VAT-registered right now.
    assert line.vat_classification == 'standard'
    assert line.vat_amount == D('0.00')
    assert line.amount_excl == line.amount_incl == D('11.50')

    header = SaleHeader.query.filter_by(sale_id=sale_id).one()
    assert header.vat_registered_snapshot is False
    assert header.total_vat == D('0.00')


def test_rounding_boundary_per_line_sum_differs_from_naive_basket_total(db_session, client):
    """Section 7's required rounding-boundary proof.

    Two standard-rated lines, R1.00 and R1.11 (VAT-inclusive, 15%):
      line 1: 1.00 / 1.15 = 0.869565... -> round to 0.87 excl -> vat = 0.13
      line 2: 1.11 / 1.15 = 0.965217... -> round to 0.97 excl -> vat = 0.14
      per-line VAT sum = 0.13 + 0.14 = 0.27

    A basket-level (naive) computation instead does:
      total incl = 2.11 -> 2.11 / 1.15 = 1.834782... -> round to 1.83 excl -> vat = 0.28

    0.27 != 0.28 — this is exactly the boundary Section 7 requires proving:
    "calculate and round VAT per line, then sum the rounded line values. Never
    round the sum." The header must show 0.27, not 0.28.
    """
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)
    item_a = make_product(name='Boundary Item A', price=D('1.00'), vat_type='standard')
    item_b = make_product(name='Boundary Item B', price=D('1.11'), vat_type='standard')
    _teller(client, 'vat_teller_boundary')

    resp = checkout(client, [
        {'product_id': item_a.id, 'qty': 1},
        {'product_id': item_b.id, 'qty': 1},
    ], cash_tendered=5)
    sale_id = resp.get_json()['transaction_id']

    lines = {l.unit_price: l for l in Sale.query.filter_by(sale_id=sale_id).all()}
    assert lines[D('1.00')].vat_amount == D('0.13')
    assert lines[D('1.00')].amount_excl == D('0.87')
    assert lines[D('1.11')].vat_amount == D('0.14')
    assert lines[D('1.11')].amount_excl == D('0.97')

    header = SaleHeader.query.filter_by(sale_id=sale_id).one()
    assert header.total_vat == D('0.27')          # per-line sum — the correct value
    assert header.total_vat != D('0.28')           # what a naive basket-level recompute would give
    assert header.total_excl_vat == D('1.84')      # 0.87 + 0.97
    assert header.total_incl_vat == D('2.11')      # matches the actual amount charged

"""Checkout / void / VAT / receipt characterization tests (Rev 5 P0-1 / Section 5).

Routes are exercised through the real Flask test client, not by calling
internals directly. Every test asserts actual DB state after the call.
"""
from decimal import Decimal

import pytest

from helpers import set_setting
from models import AuditLog, Sale, StockBatch
from tests.factories import make_admin, make_product, make_user
from tests.helpers import D, checkout, login_as, refresh


def _make_stock_item(price='10.00', cost='5.000000', qty='10'):
    product = make_product(product_type='stock_item', price=D(price))
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(qty), qty_remaining_base=D(qty),
        cost_per_base_unit=D(cost),
    )
    from models import db
    db.session.add(batch)
    db.session.flush()
    return product, batch


def test_sale_checkout_creates_sale_row_and_decrements_stock(db_session, client):
    from werkzeug.security import generate_password_hash
    user = make_user(username='teller1', password_hash=generate_password_hash('testpass123'))

    product, batch = _make_stock_item(price='10.00', cost='5.000000', qty='10')

    login_as(client, 'teller1', 'testpass123')
    resp = checkout(client, [{'product_id': product.id, 'qty': 2}], cash_tendered=20)

    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['ok'] is True
    sale_id = body['transaction_id']

    rows = Sale.query.filter_by(sale_id=sale_id).all()
    assert len(rows) == 1
    assert rows[0].qty == D(2)
    assert rows[0].unit_price == D('10.00')
    assert rows[0].cogs == D('10.00')  # 2 * 5.00
    assert rows[0].voided is False

    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(8)


def test_void_restores_stock_and_writes_audit_log(db_session, client):
    from werkzeug.security import generate_password_hash
    admin = make_admin(username='admin1', password_hash=generate_password_hash('adminpass123'))

    product, batch = _make_stock_item(price='10.00', cost='5.000000', qty='10')

    login_as(client, 'admin1', 'adminpass123')
    resp = checkout(client, [{'product_id': product.id, 'qty': 3}], cash_tendered=30)
    sale_id = resp.get_json()['transaction_id']

    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(7)

    void_resp = client.post(f'/api/transactions/{sale_id}/void', json={'reason': 'testing void'})
    assert void_resp.status_code == 200, void_resp.get_json()

    rows = Sale.query.filter_by(sale_id=sale_id).all()
    assert all(r.voided is True for r in rows)
    assert rows[0].void_reason == 'testing void'

    refresh(db_session, batch)
    assert batch.qty_remaining_base == D(10)  # fully restored

    audit_rows = AuditLog.query.filter_by(event_type='sale_void', target_id=sale_id).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].actor_user_id == admin.id


@pytest.mark.known_defect
def test_mixed_vat_basket_applies_flat_rate_ignoring_product_vat_type(db_session, client):
    """KNOWN DEFECT — Rev 5 P1-1/P1-1b fixes this. TODAY, Product.vat_type is
    stored and UI-editable but read by nothing: the receipt endpoint applies a
    single flat rate to the WHOLE basket total regardless of each line's
    vat_type (transactions.py api_transaction_receipt). This test pins that
    flat-rate behavior on a basket mixing a 'standard' and a 'zero' rated item
    so P1-1's commit is the one that changes this assertion.
    """
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)

    standard = make_product(name='Standard Item', price=D('11.50'), vat_type='standard')
    zero = make_product(name='Zero Rated Item', price=D('10.00'), vat_type='zero')

    from werkzeug.security import generate_password_hash
    make_user(username='teller2', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'teller2', 'testpass123')

    resp = checkout(client, [
        {'product_id': standard.id, 'qty': 1},
        {'product_id': zero.id, 'qty': 1},
    ], cash_tendered=25)
    sale_id = resp.get_json()['transaction_id']

    receipt = client.get(f'/api/transactions/{sale_id}/receipt').get_json()

    total = D('11.50') + D('10.00')  # 21.50 — both lines summed with no per-line VAT split
    expected_flat_vat = round(float(total) * (15 / 100) / (1 + 15 / 100), 2)

    assert receipt['total'] == float(total)
    # DEFECT: VAT is computed on the FULL basket total (both lines), even though
    # the zero-rated line should contribute nothing to the VAT amount.
    assert receipt['vat_amount'] == expected_flat_vat
    assert receipt['vat_amount'] > 0  # the zero-rated line is still being taxed


def test_receipt_and_till_summary_vat_agreement_for_a_single_sale(db_session, client):
    """EXPLORATORY (Rev 5 Section 5 'receipt totals vs Z-report totals') — the
    receipt endpoint (transactions.py) computes VAT with Python float `round()`
    on a float total; the till summary endpoint (till_sessions.py) computes the
    same formula with Decimal.quantize() on a Decimal total. This test picks an
    amount and records whether the two currently AGREE or DIVERGE — it does not
    assume either outcome, per the Rev 5 instruction to find out empirically.
    """
    set_setting('vat_registered', 'true')
    set_setting('vat_rate', 15)

    product = make_product(name='VAT Probe Item', price=D('19.99'))
    from werkzeug.security import generate_password_hash
    make_user(username='teller3', password_hash=generate_password_hash('testpass123'))
    make_admin(username='admin3', password_hash=generate_password_hash('adminpass123'))

    login_as(client, 'teller3', 'testpass123')
    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=20)
    sale_id = resp.get_json()['transaction_id']
    receipt = client.get(f'/api/transactions/{sale_id}/receipt').get_json()

    client.post('/api/logout')
    login_as(client, 'admin3', 'adminpass123')
    summary = client.get('/api/till/sessions/summary').get_json()

    # Single sale in the period, so the two totals describe the same underlying
    # amount computed by two different code paths (float round() vs Decimal quantize()).
    assert receipt['total'] == summary['total_sales']
    # Empirically observed at R19.99/15%: float-round and Decimal-quantize AGREE.
    # This is a characterization of TODAY'S behavior, not a guarantee for all
    # amounts — P1-1 replaces both call sites with one shared Decimal rounding
    # policy (INV-7), which is exactly what removes this fragility.
    assert receipt['vat_amount'] == summary['vat_amount']

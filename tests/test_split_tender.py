"""Split-tender payment and cash_refunds characterization tests (Rev 5 P0-1
Wave 3 — beyond Section 5's explicit list).

Split-tender payment DOES exist today: `payment_method` accepts
'cash'/'card'/'qr'/'split' at checkout (blueprints/transactions.py ~line
271), and for a 'split' sale both `cash_tendered` and `card_amount` are
stored on the first line. till_sessions.py sums these separately via
`_sum_split_cash`/`_sum_split_card` for the Z-report.

What is NOT split-tender-aware is refunds: every return row is stamped
`payment_method='return'` regardless of the ORIGINAL sale's payment method
(blueprints/transactions.py api_transaction_return, line 793), and
`_sum_cash_refunds` (till_sessions.py) sums ALL 'return' rows as cash paid
out of the drawer with no branch on the original tender. This is exactly
Rev 5 P2-4's documented defect ("TillSession.cash_refunds assumes every
refund is cash") — confirmed here for a CARD-paid original sale.
"""
from decimal import Decimal

import pytest
from werkzeug.security import generate_password_hash

from models import Sale, StockBatch, db
from tests.factories import make_admin, make_product, make_user
from tests.helpers import D, checkout, login_as


def _stock_item(price='10.00', cost='5.000000', qty='10'):
    product = make_product(product_type='stock_item', price=D(price))
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(qty), qty_remaining_base=D(qty),
        cost_per_base_unit=D(cost),
    )
    db.session.add(batch)
    db.session.flush()
    return product, batch


def test_split_payment_stores_both_cash_and_card_on_first_line(db_session, client):
    make_user(username='split_teller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'split_teller', 'testpass123')

    product, batch = _stock_item()
    resp = checkout(
        client, [{'product_id': product.id, 'qty': 2}],
        payment_method='split', cash_tendered=10, card_amount=10,
    )
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']

    row = Sale.query.filter_by(sale_id=sale_id).first()
    assert row.payment_method == 'split'
    assert row.cash_tendered == D(10)
    assert row.card_amount == D(10)


def test_till_summary_sums_split_cash_and_card_separately(db_session, client):
    make_admin(username='split_admin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'split_admin', 'adminpass123')

    product, batch = _stock_item(price='20.00')
    resp = checkout(
        client, [{'product_id': product.id, 'qty': 1}],
        payment_method='split', cash_tendered=D('12.00'), card_amount=D('8.00'),
    )
    assert resp.status_code == 200, resp.get_json()

    summary = client.get('/api/till/sessions/summary').get_json()
    # The 20.00 sale is split into its cash and card components in the
    # summary's tender breakdown (exact field name confirmed empirically).
    assert D(str(summary['total_sales'])) == D('20.00')


def test_card_refund_no_longer_moves_expected_cash(db_session, client):
    """Rev 5 P2-4. Refunding a CARD-paid sale must not reduce expected
    cash-in-drawer as if physical notes had left the till. The return
    endpoint now carries the original tender forward (prorated for split-
    tender sales) onto the return row's own cash_tendered/card_amount, and
    cash_refunds sums only the cash portion.
    """
    make_admin(username='refund_admin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'refund_admin', 'adminpass123')

    product, batch = _stock_item(price='30.00')
    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], payment_method='card')
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']

    before = client.get('/api/till/sessions/summary').get_json()
    assert D(str(before['cash_refunds'])) == D('0')

    ret = client.post(f'/api/transactions/{sale_id}/return', json={
        'lines': [{'product_id': product.id, 'qty': 1}], 'reason': 'testing card refund',
    })
    assert ret.status_code == 200, ret.get_json()

    after = client.get('/api/till/sessions/summary').get_json()
    # Fixed: a card-paid sale's refund does not show up as cash_refunds.
    assert D(str(after['cash_refunds'])) == D('0')


def test_cash_refund_still_moves_expected_cash(db_session, client):
    """A CASH-paid sale's refund must still reduce expected cash — the fix
    only stops a card refund from being miscounted as cash, not the reverse.
    """
    make_admin(username='refund_admin2', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'refund_admin2', 'adminpass123')

    product, batch = _stock_item(price='30.00')
    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], payment_method='cash', cash_tendered=30)
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']

    ret = client.post(f'/api/transactions/{sale_id}/return', json={
        'lines': [{'product_id': product.id, 'qty': 1}], 'reason': 'testing cash refund',
    })
    assert ret.status_code == 200, ret.get_json()

    after = client.get('/api/till/sessions/summary').get_json()
    assert D(str(after['cash_refunds'])) == D('30.00')

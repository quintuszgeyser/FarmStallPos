"""Inventory-policy (STRICT / WARN / ALLOW_NEGATIVE) and force-override
characterization tests (Rev 5 P0-1 Wave 3 — beyond Section 5's explicit list).

Mechanism (blueprints/transactions.py api_transactions, ~lines 294-345):
a pre-flight, read-only pass aggregates cart quantities per product BEFORE any
DB write. Per product: STRICT with insufficient stock -> added to
`_sale_blocks`; WARN with insufficient stock -> added to `_sale_warns`.
If `_sale_blocks` is non-empty, an admin passing `force=True` clears it
(bypasses STRICT); a teller passing force=True does NOT bypass STRICT — only
`u.has_role('admin')` is checked. If `_sale_warns` is non-empty and force is
not set, the sale is blocked with a distinct {'warn': True, ...} 409 payload
(not the same shape as a STRICT block) requiring the caller to resubmit with
force=True to proceed.
"""
from decimal import Decimal

import pytest
from werkzeug.security import generate_password_hash

from models import Sale, StockBatch, db
from tests.factories import make_admin, make_product, make_user
from tests.helpers import D, checkout, login_as


def _stock_item(policy, qty_on_hand='2'):
    product = make_product(product_type='stock_item', price=D('10.00'), inventory_policy=policy)
    batch = StockBatch(
        product_id=product.id, qty_purchased_base=D(qty_on_hand), qty_remaining_base=D(qty_on_hand),
        cost_per_base_unit=D('5.000000'),
    )
    db.session.add(batch)
    db.session.flush()
    return product, batch


def test_strict_policy_blocks_teller_oversell(db_session, client):
    make_user(username='strict_teller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'strict_teller', 'testpass123')

    product, batch = _stock_item('STRICT', qty_on_hand='2')
    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50)

    assert resp.status_code == 409
    body = resp.get_json()
    assert body['error'] == 'Sale blocked: insufficient stock.'
    assert body['blocked'][0]['product_id'] == product.id
    assert body['blocked'][0]['available'] == 2.0
    assert body['blocked'][0]['needed'] == 5.0
    # No Sale row written, no stock touched — the pre-flight check runs before any write.
    assert Sale.query.filter_by(product_id=product.id).count() == 0


def test_strict_policy_teller_cannot_bypass_with_force(db_session, client):
    make_user(username='strict_teller2', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'strict_teller2', 'testpass123')

    product, batch = _stock_item('STRICT', qty_on_hand='2')
    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50, force=True)

    # DEFECT-ADJACENT BUT INTENTIONAL PER CODE: force=True alone does not
    # bypass STRICT — only `force and u.has_role('admin')` does. A teller's
    # force flag is silently ignored, not rejected with a different error.
    assert resp.status_code == 409
    assert resp.get_json()['error'] == 'Sale blocked: insufficient stock.'


def test_strict_policy_admin_can_bypass_with_force(db_session, client):
    make_admin(username='strict_admin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'strict_admin', 'adminpass123')

    product, batch = _stock_item('STRICT', qty_on_hand='2')
    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50, force=True)

    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']
    rows = Sale.query.filter_by(sale_id=sale_id).all()
    assert len(rows) == 1
    assert rows[0].qty == D(5)


def test_warn_policy_blocks_without_force_then_succeeds_with_force(db_session, client):
    make_admin(username='warn_admin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'warn_admin', 'adminpass123')

    product, batch = _stock_item('WARN', qty_on_hand='2')

    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50)
    assert resp.status_code == 409
    body = resp.get_json()
    # WARN produces a DIFFERENT response shape than STRICT — {'warn': True, ...}
    # rather than {'error': ..., 'blocked': [...]}. A caller distinguishing on
    # 'error' presence alone would mishandle this.
    assert body.get('warn') is True
    assert body['warnings'][0]['product_id'] == product.id
    assert Sale.query.filter_by(product_id=product.id).count() == 0

    resp2 = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50, force=True)
    assert resp2.status_code == 200, resp2.get_json()


def test_warn_policy_force_bypass_available_to_teller_not_just_admin(db_session, client):
    make_user(username='warn_teller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'warn_teller', 'testpass123')

    product, batch = _stock_item('WARN', qty_on_hand='1')
    resp = checkout(client, [{'product_id': product.id, 'qty': 3}], cash_tendered=30, force=True)

    # Confirms the asymmetry: STRICT's force bypass is admin-gated, WARN's is not.
    assert resp.status_code == 200, resp.get_json()


def test_allow_negative_policy_never_blocks(db_session, client):
    make_user(username='an_teller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'an_teller', 'testpass123')

    product, batch = _stock_item('ALLOW_NEGATIVE', qty_on_hand='1')
    resp = checkout(client, [{'product_id': product.id, 'qty': 10}], cash_tendered=100)

    assert resp.status_code == 200, resp.get_json()
    # Shortfall posts to a negative-placeholder batch (this is the known P2-1
    # defect area — see tests/test_fifo.py's oversell tests — not re-asserted
    # here, this test's scope is the policy gate itself, not the placeholder
    # mechanics).

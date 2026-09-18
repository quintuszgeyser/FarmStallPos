"""Consignment sale and reversal characterization tests (Rev 5 P0-1 / Section 5).
Routes exercised through the real Flask test client.
"""
from werkzeug.security import generate_password_hash

from models import ConsignmentLiability
from tests.factories import make_admin, make_product, make_stock_batch, make_supplier, make_user
from tests.helpers import D, checkout, login_as, refresh


def _login_admin(client, username='consignadmin'):
    make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')


def test_consignment_sale_of_stock_item_creates_liability_per_batch_consumption(db_session, client):
    supplier = make_supplier(name='Consignment Orchard')
    product = make_product(product_type='stock_item', price=D('20.00'), is_consignment=True,
                            consignment_supplier_id=supplier.id, settlement_basis='FIXED_COST',
                            consignment_cost_per_unit=D('6.000000'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('8.000000'), ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    make_user(username='teller_c', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'teller_c', 'testpass123')

    resp = checkout(client, [{'product_id': product.id, 'qty': 4}], cash_tendered=80)
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']

    liabilities = ConsignmentLiability.query.filter_by(sale_id=sale_id).all()
    assert len(liabilities) == 1
    lib = liabilities[0]
    assert lib.supplier_id == supplier.id
    assert lib.status == 'outstanding'
    assert lib.qty_consumed == 4.0
    assert lib.unit_cost == 6.0        # FIXED_COST from product.consignment_cost_per_unit, not batch cost (8.00)
    assert lib.amount_owed == 24.0     # 4 * 6.00


def test_consignment_sale_pct_of_sale_basis_uses_sale_price(db_session, client):
    supplier = make_supplier(name='Pct Orchard')
    product = make_product(product_type='stock_item', price=D('50.00'), is_consignment=True,
                            consignment_supplier_id=supplier.id, settlement_basis='PCT_OF_SALE',
                            consignment_pct=D('40.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('8.000000'), ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    make_user(username='teller_pct', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'teller_pct', 'testpass123')

    resp = checkout(client, [{'product_id': product.id, 'qty': 2}], cash_tendered=100)
    sale_id = resp.get_json()['transaction_id']

    lib = ConsignmentLiability.query.filter_by(sale_id=sale_id).one()
    # 40% of R50.00 sale price = R20.00/unit owed, * 2 units = R40.00
    assert lib.unit_cost == 20.0
    assert lib.amount_owed == 40.0
    assert lib.sale_price_at_time == 50.0
    assert lib.settlement_percent_at_time == 40.0


def test_void_of_consignment_sale_voids_the_liability(db_session, client):
    """Void IS wired to reverse_consignment_liabilities (transactions.py:857),
    unlike return. A full-sale void correctly voids every liability that sale
    created — this is NOT the P2-2 "wrong for a partial return" bug, since void
    is inherently whole-sale (there is no partial void endpoint). Pins today's
    correct behavior here so it isn't confused with the return-side gap.
    """
    supplier = make_supplier(name='Void Orchard')
    product = make_product(product_type='stock_item', price=D('20.00'), is_consignment=True,
                            consignment_supplier_id=supplier.id, settlement_basis='FIXED_COST',
                            consignment_cost_per_unit=D('6.000000'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('8.000000'), ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    _login_admin(client)

    resp = checkout(client, [{'product_id': product.id, 'qty': 3}], cash_tendered=60)
    sale_id = resp.get_json()['transaction_id']

    lib = ConsignmentLiability.query.filter_by(sale_id=sale_id).one()
    assert lib.status == 'outstanding'

    void = client.post(f'/api/transactions/{sale_id}/void', json={'reason': 'wrong item rung up'})
    assert void.status_code == 200, void.get_json()

    refresh(db_session, lib)
    assert lib.status == 'voided'
    assert lib.settled_at is not None

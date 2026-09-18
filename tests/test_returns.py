"""Return characterization tests (Rev 5 P0-1 / Section 5) — full return, partial
return, the recipe cross-ingredient over-restore defect, and consignment-liability
handling on return. Routes exercised through the real Flask test client.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest
from werkzeug.security import generate_password_hash

from models import ConsignmentLiability, Sale, StockBatch, StockConsumption
from tests.factories import make_admin, make_product, make_recipe_line, make_stock_batch, make_supplier
from tests.helpers import D, checkout, login_as, refresh


def _login_admin(client, username='returnadmin'):
    admin = make_admin(username=username, password_hash=generate_password_hash('adminpass123'))
    login_as(client, username, 'adminpass123')
    return admin


def test_full_return_stock_item_restores_via_new_batch_not_original(db_session, client):
    """The stock_item return branch (transactions.py ~814) does NOT restore
    quantity onto the original consumed batch — it creates a brand NEW StockBatch
    for the returned qty. This is a deliberate design (Rev 5 P2-2 acknowledges it
    as one of the "four reversal implementations" and does not flag it as the
    defect to fix — the defect is the *recipe* branch below). This test just pins
    that mechanism precisely so nobody "fixes" it by accident while working P2-2.
    """
    product = make_product(product_type='stock_item', price=D('10.00'))
    original_batch = make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                                       cost_per_base_unit=D('4.000000'))
    _login_admin(client)

    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50)
    sale_id = resp.get_json()['transaction_id']
    refresh(db_session, original_batch)
    assert original_batch.qty_remaining_base == D(5)

    ret = client.post(f'/api/transactions/{sale_id}/return',
                       json={'lines': [{'product_id': product.id, 'qty': 5}], 'reason': 'customer changed mind'})
    assert ret.status_code == 200, ret.get_json()
    return_id = ret.get_json()['return_id']

    # Original batch is untouched by the return.
    refresh(db_session, original_batch)
    assert original_batch.qty_remaining_base == D(5)

    # A distinct new batch was created for the returned qty instead.
    new_batches = StockBatch.query.filter_by(product_id=product.id).filter(StockBatch.id != original_batch.id).all()
    assert len(new_batches) == 1
    assert new_batches[0].qty_remaining_base == D(5)
    assert new_batches[0].qty_purchased_base == D(5)

    return_rows = Sale.query.filter_by(sale_id=return_id).all()
    assert len(return_rows) == 1
    assert return_rows[0].qty == D(-5)
    assert return_rows[0].payment_method == 'return'
    assert return_rows[0].original_sale_id == sale_id


def test_partial_return_stock_item_caps_at_remaining_returnable_qty(db_session, client):
    product = make_product(product_type='stock_item', price=D('10.00'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10), cost_per_base_unit=D('4.000000'))
    _login_admin(client)

    resp = checkout(client, [{'product_id': product.id, 'qty': 4}], cash_tendered=40)
    sale_id = resp.get_json()['transaction_id']

    # Return 3 of the 4 — allowed.
    ret1 = client.post(f'/api/transactions/{sale_id}/return',
                        json={'lines': [{'product_id': product.id, 'qty': 3}], 'reason': 'partial'})
    assert ret1.status_code == 200, ret1.get_json()

    # Returning the remaining 1 more (total would be 4) — still allowed, exactly exhausts it.
    ret2 = client.post(f'/api/transactions/{sale_id}/return',
                        json={'lines': [{'product_id': product.id, 'qty': 1}], 'reason': 'rest of it'})
    assert ret2.status_code == 200, ret2.get_json()

    # A third return attempt of even 1 more unit is rejected — nothing left to return.
    ret3 = client.post(f'/api/transactions/{sale_id}/return',
                        json={'lines': [{'product_id': product.id, 'qty': 1}], 'reason': 'too many'})
    assert ret3.status_code == 400
    assert 'exceeds original' in ret3.get_json()['error']


def test_partial_return_recipe_restores_only_its_own_share_of_shared_ingredient(db_session, client):
    """Rev 5 P2-2. Selling Bread (needs 1 flour) and Cake (needs 2 flour) in the
    SAME sale, then returning only the Bread, restores exactly Bread's own 1
    unit of flour — not Cake's too. Fixed by computing the restore quantity
    directly from the recipe formula (qty_base * qty returned) into a NEW
    batch, rather than trying to disambiguate shared StockConsumption history.
    """
    flour = make_product(product_type='stock_item', name='Flour', price=D('1.00'))
    make_stock_batch(flour, qty_remaining_base=D(100), qty_purchased_base=D(100), cost_per_base_unit=D('1.000000'))

    bread = make_product(product_type='recipe', name='Bread', is_produced=False, price=D('10.00'))
    cake = make_product(product_type='recipe', name='Cake', is_produced=False, price=D('20.00'))
    make_recipe_line(bread, flour, qty_base=D(1))
    make_recipe_line(cake, flour, qty_base=D(2))

    _login_admin(client)

    # One sale containing both: consumes 1 (bread) + 2 (cake) = 3 units of flour.
    resp = checkout(client, [
        {'product_id': bread.id, 'qty': 1},
        {'product_id': cake.id, 'qty': 1},
    ], cash_tendered=30)
    sale_id = resp.get_json()['transaction_id']

    flour_consumed = sum(D(c.qty_consumed_base) for c in
                          StockConsumption.query.filter_by(sale_id=sale_id, ingredient_id=flour.id).all())
    assert flour_consumed == D(3)

    flour_batch = StockBatch.query.filter_by(product_id=flour.id).one()
    refresh(db_session, flour_batch)
    assert flour_batch.qty_remaining_base == D(97)  # 100 - 3

    # Return ONLY the Bread (which alone needs 1 unit of flour).
    ret = client.post(f'/api/transactions/{sale_id}/return',
                       json={'lines': [{'product_id': bread.id, 'qty': 1}], 'reason': 'wrong item'})
    assert ret.status_code == 200, ret.get_json()

    # The ORIGINAL flour batch is untouched — restoration goes to a new batch.
    refresh(db_session, flour_batch)
    assert flour_batch.qty_remaining_base == D(97)

    new_batches = StockBatch.query.filter_by(product_id=flour.id).filter(
        StockBatch.id != flour_batch.id).all()
    assert len(new_batches) == 1
    assert new_batches[0].qty_remaining_base == D(1)  # only Bread's own share


def test_return_of_consignment_item_reverses_liability_via_compensating_credit(db_session, client):
    """Rev 5 P2-2.
    Was previously a FINDING (see git history) that the return path never
    reversed consignment liability at all, worse than Rev 5's original
    description ("voids EVERY outstanding liability" implied over-reversal;
    actual behavior was zero reversal). Rev 5 P2-2 fixes it: the ORIGINAL
    liability row is left untouched (still 'outstanding' — a preserved audit
    record of what was actually charged), and a compensating credit row (same
    sale_id/product_id, negative qty_consumed/amount_owed) is added alongside
    it, so aggregates that sum amount_owed for status='outstanding' net to
    zero without needing to know anything changed.
    """
    supplier = make_supplier(name='Consignment Farm')
    product = make_product(product_type='stock_item', price=D('10.00'), is_consignment=True,
                            consignment_supplier_id=supplier.id, settlement_basis='FIXED_COST',
                            consignment_cost_per_unit=D('3.000000'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'), ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    _login_admin(client)

    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50)
    sale_id = resp.get_json()['transaction_id']

    liabilities = ConsignmentLiability.query.filter_by(sale_id=sale_id).all()
    assert len(liabilities) == 1
    assert liabilities[0].status == 'outstanding'
    assert liabilities[0].amount_owed == pytest.approx(15.0)  # 5 * 3.00
    original_id = liabilities[0].id

    ret = client.post(f'/api/transactions/{sale_id}/return',
                       json={'lines': [{'product_id': product.id, 'qty': 5}], 'reason': 'full return'})
    assert ret.status_code == 200, ret.get_json()

    refresh(db_session, liabilities[0])
    # The original charge is untouched — preserved as an audit record.
    assert liabilities[0].status == 'outstanding'
    assert liabilities[0].amount_owed == pytest.approx(15.0)

    all_for_sale = ConsignmentLiability.query.filter_by(
        sale_id=sale_id, status='outstanding').all()
    assert len(all_for_sale) == 2  # the original charge + one compensating credit
    credit = [l for l in all_for_sale if l.id != original_id][0]
    assert credit.qty_consumed == pytest.approx(-5.0)
    assert credit.amount_owed == pytest.approx(-15.0)

    # Net outstanding for this sale is now zero — a full return fully offsets it.
    net = sum(Decimal(str(l.amount_owed)) for l in all_for_sale)
    assert net == D('0.00')


def test_partial_return_of_consignment_item_reverses_only_the_returned_portion(db_session, client):
    """Rev 5 P2-2 proof criterion: a partial return reverses exactly the
    returned portion, pro-rata — not the whole sale's liability."""
    supplier = make_supplier(name='Consignment Farm 2')
    product = make_product(product_type='stock_item', price=D('10.00'), is_consignment=True,
                            consignment_supplier_id=supplier.id, settlement_basis='FIXED_COST',
                            consignment_cost_per_unit=D('3.000000'))
    make_stock_batch(product, qty_remaining_base=D(10), qty_purchased_base=D(10),
                      cost_per_base_unit=D('4.000000'), ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    _login_admin(client)

    resp = checkout(client, [{'product_id': product.id, 'qty': 5}], cash_tendered=50)
    sale_id = resp.get_json()['transaction_id']
    original = ConsignmentLiability.query.filter_by(sale_id=sale_id).one()
    assert original.amount_owed == pytest.approx(15.0)  # 5 * 3.00

    # Return only 2 of the 5 units.
    ret = client.post(f'/api/transactions/{sale_id}/return',
                       json={'lines': [{'product_id': product.id, 'qty': 2}], 'reason': 'partial return'})
    assert ret.status_code == 200, ret.get_json()

    all_for_sale = ConsignmentLiability.query.filter_by(
        sale_id=sale_id, status='outstanding').all()
    net = sum(Decimal(str(l.amount_owed)) for l in all_for_sale)
    # 2/5 of the original 15.00 is credited back -> net owed is 9.00 (3 units still sold).
    assert net == D('9.00')

    # Returning the remaining 3 units in a SEPARATE call must bring net to exactly
    # zero, not go negative — proves the ratio is against the true original qty
    # (5), not a shrinking remainder, across repeated partial returns.
    ret2 = client.post(f'/api/transactions/{sale_id}/return',
                        json={'lines': [{'product_id': product.id, 'qty': 3}], 'reason': 'rest of it'})
    assert ret2.status_code == 200, ret2.get_json()
    all_for_sale2 = ConsignmentLiability.query.filter_by(
        sale_id=sale_id, status='outstanding').all()
    net2 = sum(Decimal(str(l.amount_owed)) for l in all_for_sale2)
    assert net2 == D('0.00')

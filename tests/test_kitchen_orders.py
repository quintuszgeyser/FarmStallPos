"""Kitchen-order side-effect characterization tests (Rev 5 P0-1 Wave 3 —
beyond Section 5's explicit list).

Product.is_prepared=True gates kitchen-order creation via
helpers.collect_kitchen_items, called from the checkout route
(blueprints/transactions.py api_transactions, ~lines 463-477). A non-prepared
product creates no KitchenOrder row at all.
"""
from decimal import Decimal

from werkzeug.security import generate_password_hash

from models import KitchenOrder, StockBatch, db
from tests.factories import make_product, make_recipe_line, make_user
from tests.helpers import D, checkout, login_as


def test_prepared_product_creates_kitchen_order_with_ingredients(db_session, client):
    flour = make_product(name='Flour', product_type='stock_item', price=D('1.00'), base_unit='g')
    db.session.add(StockBatch(
        product_id=flour.id, qty_purchased_base=D('1000'), qty_remaining_base=D('1000'),
        cost_per_base_unit=D('0.01'),
    ))
    db.session.flush()

    bread = make_product(name='Fresh Bread', product_type='recipe', price=D('15.00'), is_prepared=True)
    make_recipe_line(bread, flour, qty_base=D('200'))

    make_user(username='kitchen_teller', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'kitchen_teller', 'testpass123')

    resp = checkout(client, [{'product_id': bread.id, 'qty': 2}], cash_tendered=30)
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']
    assert resp.get_json()['kitchen_orders'] == 1

    orders = KitchenOrder.query.filter_by(sale_id=sale_id).all()
    assert len(orders) == 1
    ko = orders[0]
    assert ko.product_id == bread.id
    assert ko.product_name == 'Fresh Bread'
    assert ko.qty == D(2)
    assert ko.status == 'pending'

    import json
    ingredients = json.loads(ko.ingredients)
    assert len(ingredients) == 1
    assert ingredients[0]['name'] == 'Flour'
    # qty_base (200) * sale qty (2) = 400
    assert ingredients[0]['qty'] == 400.0


def test_non_prepared_product_creates_no_kitchen_order(db_session, client):
    product = make_product(name='Bottled Water', price=D('12.00'), is_prepared=False)

    make_user(username='kitchen_teller2', password_hash=generate_password_hash('testpass123'))
    login_as(client, 'kitchen_teller2', 'testpass123')

    resp = checkout(client, [{'product_id': product.id, 'qty': 1}], cash_tendered=15)
    assert resp.status_code == 200, resp.get_json()
    sale_id = resp.get_json()['transaction_id']

    assert resp.get_json()['kitchen_orders'] == 0
    assert KitchenOrder.query.filter_by(sale_id=sale_id).count() == 0

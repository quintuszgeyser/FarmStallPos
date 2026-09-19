"""
Plain factory functions for characterization tests (Rev 5 P0-1).

Every function persists (add + flush) a real ORM row with sane defaults for
required columns, taken directly from models.py, so a test only needs to
override what it actually cares about. Money/quantity defaults are Decimal,
matching the real column types — never float, per CLAUDE.md convention.

Functions flush (not commit) by default so callers inside a single test keep
everything on one SAVEPOINT; call `db.session.commit()` explicitly in a test
only when exercising a route/helper that itself must observe committed state
(the app commits internally throughout, so this is rarely needed here).
"""
from datetime import datetime, UTC
from decimal import Decimal

from werkzeug.security import generate_password_hash

from models import (
    db,
    User, Product, Supplier, RecipeLine,
    StockBatch, StockConsumption, StockMovement, TillSession, Customer,
)

_seq = {'n': 0}


def _next(prefix):
    _seq['n'] += 1
    return f'{prefix}{_seq["n"]}'


def make_user(**overrides):
    defaults = dict(
        username=_next('user'),
        password_hash=generate_password_hash('testpass123'),
        role='teller',
        active=True,
    )
    defaults.update(overrides)
    user = User(**defaults)
    db.session.add(user)
    db.session.flush()
    return user


def make_admin(**overrides):
    overrides.setdefault('role', 'admin')
    return make_user(**overrides)


def make_supplier(**overrides):
    defaults = dict(name=_next('Supplier '))
    defaults.update(overrides)
    supplier = Supplier(**defaults)
    db.session.add(supplier)
    db.session.flush()
    return supplier


def make_product(**overrides):
    """product_type='simple' (default) | 'stock_item' | 'recipe'.

    stock_item: FIFO-tracked via StockBatch, needs base_unit — defaults to
    'unit' (conversion factor 1 in blueprints/stock.py's _UNIT_CONV).
    recipe: made-to-order by default (is_produced=False); pass
    is_produced=True for a batch-produced recipe (consumes its own FIFO batches
    like a stock_item once produced).
    """
    defaults = dict(
        name=_next('Product '),
        price=Decimal('10.00'),
        product_type='simple',
        stock_qty=0,
        sold_by_weight=False,
        is_for_sale=True,
        vat_type='standard',
        is_produced=False,
        batch_size=Decimal('1'),
        inventory_policy='ALLOW_NEGATIVE',
        is_consignment=False,
        settlement_basis='FIXED_COST',
    )
    if overrides.get('product_type') in ('stock_item', 'recipe') or defaults['product_type'] == 'stock_item':
        defaults.setdefault('base_unit', 'unit')
    defaults.update(overrides)
    product = Product(**defaults)
    db.session.add(product)
    db.session.flush()
    return product


def make_stock_batch(product, **overrides):
    now = datetime.now(UTC).replace(tzinfo=None)
    defaults = dict(
        product_id=product.id,
        qty_purchased_base=Decimal('10'),
        qty_remaining_base=Decimal('10'),
        cost_per_base_unit=Decimal('5.000000'),
        purchased_at=now,
        ownership_type='NORMAL',
        batch_type='normal',
    )
    defaults.update(overrides)
    batch = StockBatch(**defaults)
    db.session.add(batch)
    db.session.flush()
    return batch


def make_stock_consumption(sale_id, ingredient, batch, **overrides):
    now = datetime.now(UTC).replace(tzinfo=None)
    defaults = dict(
        sale_id=sale_id,
        ingredient_id=ingredient.id if hasattr(ingredient, 'id') else ingredient,
        batch_id=batch.id if hasattr(batch, 'id') else batch,
        qty_consumed_base=Decimal('1'),
        cost_per_base_unit=Decimal('5.000000'),
        consumed_at=now,
    )
    defaults.update(overrides)
    consumption = StockConsumption(**defaults)
    db.session.add(consumption)
    db.session.flush()
    return consumption


def make_stock_movement(batch, **overrides):
    now = datetime.now(UTC).replace(tzinfo=None)
    defaults = dict(
        movement_type='RECEIPT',
        batch_id=batch.id if hasattr(batch, 'id') else batch,
        qty_delta=Decimal('10'),
        unit_cost=Decimal('5.000000'),
        source_type='receipt',
        source_id=None,
        source_line_id=None,
        created_at=now,
    )
    defaults.update(overrides)
    movement = StockMovement(**defaults)
    db.session.add(movement)
    db.session.flush()
    return movement


def make_recipe_line(recipe_product, ingredient_product, **overrides):
    defaults = dict(
        product_id=recipe_product.id,
        ingredient_id=ingredient_product.id,
        qty_base=Decimal('1'),
    )
    defaults.update(overrides)
    line = RecipeLine(**defaults)
    db.session.add(line)
    db.session.flush()
    return line


def make_customer(**overrides):
    defaults = dict(name=_next('Customer '), active=True, is_pos_customer=True)
    defaults.update(overrides)
    customer = Customer(**defaults)
    db.session.add(customer)
    db.session.flush()
    return customer


def make_till_session(user=None, **overrides):
    now = datetime.now(UTC).replace(tzinfo=None)
    uid = user.id if user else None
    defaults = dict(
        opened_at=now,
        closed_at=now,
        opened_by=uid,
        closed_by=uid,
        opening_float=Decimal('0'),
        counted_cash=Decimal('0'),
        pos_cash_sales=Decimal('0'),
        pos_card_sales=Decimal('0'),
        pos_total_sales=Decimal('0'),
        expected_cash=Decimal('0'),
        over_under=Decimal('0'),
        void_total=Decimal('0'),
        cash_refunds=Decimal('0'),
    )
    defaults.update(overrides)
    session_row = TillSession(**defaults)
    db.session.add(session_row)
    db.session.flush()
    return session_row

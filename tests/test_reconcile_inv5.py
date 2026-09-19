"""INV-5 (consignment closure) characterization tests (Rev 5 P0-2).

Locks in two false-positive fixes found investigating the 2026-09-19
production baseline: an opening-balance ConsignmentLiability row (sale_id
IS NULL, entered when consignment tracking started to capture pre-tracking
consumption) must not be expected to have a matching StockConsumption row,
and a write-off's StockConsumption row (sale_id matching wo-/archive-wo-/
adj-) must not be expected to have a matching liability — write-offs are
the store's own absorbed loss, never owed to the supplier. Four of six
production violations turned out to be entirely explained by these two
patterns, not real supplier-liability corruption.
"""
from scripts.reconcile import check_inv5_consignment_closure
from tests.factories import make_product, make_stock_batch, make_supplier
from tests.helpers import D


def _make_liability(supplier, product, batch, **overrides):
    from models import ConsignmentLiability, db
    defaults = dict(
        supplier_id=supplier.id, product_id=product.id, batch_id=batch.id,
        sale_id='some-sale-id', qty_consumed=D('1'), unit_cost=D('5.000000'),
        amount_owed=D('5.00'), status='outstanding',
    )
    defaults.update(overrides)
    liab = ConsignmentLiability(**defaults)
    db.session.add(liab)
    db.session.flush()
    return liab


def _make_consumption(batch, ingredient, **overrides):
    from models import StockConsumption, db
    defaults = dict(
        sale_id='some-sale-id', ingredient_id=ingredient.id, batch_id=batch.id,
        qty_consumed_base=D('1'), cost_per_base_unit=D('5.000000'),
    )
    defaults.update(overrides)
    cons = StockConsumption(**defaults)
    db.session.add(cons)
    db.session.flush()
    return cons


def test_matched_sale_ids_reconcile_cleanly(db_session):
    supplier = make_supplier()
    product = make_product(product_type='stock_item', name='INV5 Matched')
    batch = make_stock_batch(product, ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    _make_liability(supplier, product, batch, sale_id='sale-a', qty_consumed=D('3'))
    _make_consumption(batch, product, sale_id='sale-a', qty_consumed_base=D('3'))
    db_session.commit()

    r = check_inv5_consignment_closure(db_session)
    assert batch.id not in {v['batch_id'] for v in r.violations}


def test_opening_balance_liability_with_no_sale_id_does_not_false_positive(db_session):
    supplier = make_supplier()
    product = make_product(product_type='stock_item', name='INV5 Opening Balance')
    batch = make_stock_batch(product, ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    _make_liability(supplier, product, batch, sale_id='sale-a', qty_consumed=D('3'))
    _make_consumption(batch, product, sale_id='sale-a', qty_consumed_base=D('3'))
    # Opening-balance entry: real liability, no matching consumption by design.
    _make_liability(supplier, product, batch, sale_id=None, qty_consumed=D('50'))
    db_session.commit()

    r = check_inv5_consignment_closure(db_session)
    assert batch.id not in {v['batch_id'] for v in r.violations}


def test_writeoff_consumption_does_not_false_positive(db_session):
    supplier = make_supplier()
    product = make_product(product_type='stock_item', name='INV5 Writeoff')
    batch = make_stock_batch(product, ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    _make_liability(supplier, product, batch, sale_id='sale-a', qty_consumed=D('3'))
    _make_consumption(batch, product, sale_id='sale-a', qty_consumed_base=D('3'))
    # Write-off: real consumption, no liability by design (absorbed as store's own loss).
    _make_consumption(batch, product, sale_id='wo-1234abcd', qty_consumed_base=D('10'))
    db_session.commit()

    r = check_inv5_consignment_closure(db_session)
    assert batch.id not in {v['batch_id'] for v in r.violations}


def test_a_genuinely_missing_liability_is_still_a_violation(db_session):
    supplier = make_supplier()
    product = make_product(product_type='stock_item', name='INV5 Real Gap')
    batch = make_stock_batch(product, ownership_type='CONSIGNMENT', supplier_id=supplier.id)
    _make_consumption(batch, product, sale_id='sale-real', qty_consumed_base=D('7'))
    # No matching liability at all — a real gap, not a write-off, not unlinked.
    db_session.commit()

    r = check_inv5_consignment_closure(db_session)
    matching = [v for v in r.violations if v['batch_id'] == batch.id]
    assert len(matching) == 1
    assert matching[0]['difference'] == '-7.0000'

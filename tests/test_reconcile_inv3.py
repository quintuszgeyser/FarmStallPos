"""INV-3 (typed source resolves) characterization tests (Rev 5 P0-2, wired
up once P2-0 dual-write coverage reached 100%). See
check_inv3_typed_source_resolves's own docstring in scripts/reconcile.py for
the full per-source_type resolution rules this exercises.
"""
from datetime import date
from decimal import Decimal

from models import StockAdjustment, SupplierInvoice, db
from scripts.reconcile import check_inv3_typed_source_resolves
from tests.factories import make_product, make_stock_batch, make_stock_movement, make_supplier
from tests.helpers import D


def _make_sale(sale_id, product):
    from models import Sale
    s = Sale(sale_id=sale_id, product_id=product.id, qty=D(1), unit_price=D('10.00'))
    db.session.add(s)
    db.session.flush()
    return s


def test_sale_movement_resolves_against_a_real_sale_id(db_session):
    product = make_product(product_type='stock_item', name='INV3 Sale')
    batch = make_stock_batch(product)
    _make_sale('real-sale-uuid', product)
    make_stock_movement(batch, movement_type='SALE', qty_delta=D(-1),
                         source_type='sale', source_id='real-sale-uuid')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert not any(v['source_id'] == 'real-sale-uuid' for v in r.violations)


def test_sale_movement_with_a_bogus_sale_id_is_a_violation(db_session):
    product = make_product(product_type='stock_item', name='INV3 Bad Sale')
    batch = make_stock_batch(product)
    make_stock_movement(batch, movement_type='SALE', qty_delta=D(-1),
                         source_type='sale', source_id='no-such-sale')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    matching = [v for v in r.violations if v['source_id'] == 'no-such-sale']
    assert len(matching) == 1
    assert r.status == 'FAIL'


def test_return_movement_resolves_against_the_return_transactions_own_sale_id(db_session):
    product = make_product(product_type='stock_item', name='INV3 Return')
    batch = make_stock_batch(product)
    _make_sale('return-uuid-1', product)  # the NEW sale_id the return itself creates
    make_stock_movement(batch, movement_type='RETURN_SALEABLE', qty_delta=D(1),
                         source_type='return', source_id='return-uuid-1')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert not any(v['source_id'] == 'return-uuid-1' for v in r.violations)


def test_production_movement_resolves_against_a_batchs_produce_ref(db_session):
    product = make_product(product_type='stock_item', name='INV3 Flour')
    flour_batch = make_stock_batch(product)
    bread = make_product(product_type='recipe', name='INV3 Bread', is_produced=True)
    output_batch = make_stock_batch(bread, produce_ref='produce-uuid-1')
    make_stock_movement(flour_batch, movement_type='PRODUCTION', qty_delta=D(-1),
                         source_type='production', source_id='produce-uuid-1')
    make_stock_movement(output_batch, movement_type='PRODUCTION_OUTPUT', qty_delta=D(4),
                         source_type='production', source_id='produce-uuid-1')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert not any(v['source_id'] == 'produce-uuid-1' for v in r.violations)


def test_production_movement_with_no_matching_produce_ref_is_a_violation(db_session):
    product = make_product(product_type='stock_item', name='INV3 Bad Production')
    batch = make_stock_batch(product)
    make_stock_movement(batch, movement_type='PRODUCTION', qty_delta=D(-1),
                         source_type='production', source_id='no-such-produce-run')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert any(v['source_id'] == 'no-such-produce-run' for v in r.violations)


def test_receipt_movement_resolves_against_batch_id_or_import_run_id(db_session):
    product = make_product(product_type='stock_item', name='INV3 Receipt')
    batch = make_stock_batch(product)
    make_stock_movement(batch, movement_type='RECEIPT', qty_delta=D(10),
                         source_type='receipt', source_id=str(batch.id))

    imported_batch = make_stock_batch(product, import_run_id='import-run-1')
    make_stock_movement(imported_batch, movement_type='RECEIPT', qty_delta=D(10),
                         source_type='receipt', source_id='import-run-1')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert not any(v['source_id'] in (str(batch.id), 'import-run-1') for v in r.violations)


def test_receipt_movement_with_null_source_id_is_skipped_not_a_violation(db_session):
    """absorb_neg_placeholder's documented best-effort case — no batch exists
    yet at that point in the caller, so there's nothing to cite."""
    product = make_product(product_type='stock_item', name='INV3 Null Receipt')
    batch = make_stock_batch(product, batch_type='negative_placeholder', qty_remaining_base=D(-2))
    make_stock_movement(batch, movement_type='RECEIPT_ABSORB_SHORTFALL', qty_delta=D(2),
                         source_type='receipt', source_id=None)
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert r.skipped_count >= 1
    assert not any(v.get('movement_id') for v in r.violations if v['source_type'] == 'receipt')


def test_reconciliation_movement_resolves_against_supplier_invoice_or_batch(db_session):
    supplier = make_supplier(name='INV3 Supplier')
    inv = SupplierInvoice(supplier_id=supplier.id, date=date.today(), status='posted')
    db.session.add(inv)
    db.session.flush()

    product = make_product(product_type='stock_item', name='INV3 Reconciliation')
    voided_by_invoice = make_stock_batch(product, qty_remaining_base=D(0), qty_purchased_base=D(0))
    make_stock_movement(voided_by_invoice, movement_type='RECEIPT_VOID', qty_delta=D(-5),
                         source_type='reconciliation', source_id=str(inv.id))

    self_voided_batch = make_stock_batch(product, qty_remaining_base=D(0), qty_purchased_base=D(0))
    make_stock_movement(self_voided_batch, movement_type='RECEIPT_VOID', qty_delta=D(-3),
                         source_type='reconciliation', source_id=str(self_voided_batch.id))
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert not any(v['source_id'] in (str(inv.id), str(self_voided_batch.id)) for v in r.violations)


def test_writeoff_reversal_resolves_against_a_real_stock_adjustment(db_session):
    product = make_product(product_type='stock_item', name='INV3 Writeoff Reversal')
    batch = make_stock_batch(product)
    adj = StockAdjustment(product_id=product.id, adjustment_type='writeoff', qty_change_base=D(-2),
                           system_qty_before=D(10), reason='test')
    db.session.add(adj)
    db.session.flush()
    make_stock_movement(batch, movement_type='WRITEOFF_REVERSAL', qty_delta=D(2),
                         source_type='writeoff', source_id=f'adj-del-{adj.id}')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert not any(v['source_id'] == f'adj-del-{adj.id}' for v in r.violations)


def test_writeoff_reversal_with_a_bogus_adjustment_id_is_a_violation(db_session):
    product = make_product(product_type='stock_item', name='INV3 Bad Writeoff Reversal')
    batch = make_stock_batch(product)
    make_stock_movement(batch, movement_type='WRITEOFF_REVERSAL', qty_delta=D(2),
                         source_type='writeoff', source_id='adj-del-999999')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert any(v['source_id'] == 'adj-del-999999' for v in r.violations)


def test_writeoff_consumption_synthetic_token_is_skipped_not_a_violation(db_session):
    """The original write-off CONSUMPTION movement's source_id (e.g.
    'wo-<uuid>') is a correlation-only token with no independently
    resolvable record — see the function's docstring."""
    product = make_product(product_type='stock_item', name='INV3 Writeoff Consumption')
    batch = make_stock_batch(product)
    make_stock_movement(batch, movement_type='WRITEOFF', qty_delta=D(-1),
                         source_type='writeoff', source_id='wo-1234abcd')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert r.skipped_count >= 1
    assert not any(v['source_id'] == 'wo-1234abcd' for v in r.violations)


def test_migration_movement_is_exempt(db_session):
    product = make_product(product_type='stock_item', name='INV3 Migration')
    batch = make_stock_batch(product)
    make_stock_movement(batch, movement_type='RECEIPT', qty_delta=D(5),
                         source_type='migration', source_id=None,
                         note='unclassifiable backfill row')
    db_session.commit()

    r = check_inv3_typed_source_resolves(db_session)
    assert r.skipped_count >= 1
    assert not r.violations

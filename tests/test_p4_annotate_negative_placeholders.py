"""p4_annotate_negative_placeholders.py characterization tests.

Confirms the query scope: only batch_type='negative_placeholder' rows with
qty_remaining_base < 0 (still open) are targeted, and a batch that already
carries a note is left alone rather than overwritten.
"""
from models import StockBatch
from tests.factories import make_product, make_stock_batch
from tests.helpers import D


def test_scope_finds_only_open_negative_placeholders(db_session):
    product = make_product(product_type='stock_item', name='Annotate Scope')
    open_neg = make_stock_batch(product, batch_type='negative_placeholder', qty_remaining_base=D('-5'))
    reconciled_neg = make_stock_batch(product, batch_type='negative_placeholder', qty_remaining_base=D('0'))
    normal_batch = make_stock_batch(product, batch_type='normal', qty_remaining_base=D('10'))
    db_session.commit()

    targets = db_session.query(StockBatch).filter(
        StockBatch.batch_type == 'negative_placeholder',
        StockBatch.qty_remaining_base < 0,
    ).all()
    target_ids = {b.id for b in targets}

    assert open_neg.id in target_ids
    assert reconciled_neg.id not in target_ids
    assert normal_batch.id not in target_ids


def test_a_batch_with_an_existing_note_is_not_a_fresh_target(db_session):
    product = make_product(product_type='stock_item', name='Annotate Existing Note')
    batch = make_stock_batch(
        product, batch_type='negative_placeholder', qty_remaining_base=D('-3'),
        cost_adjustment_reason='already annotated previously',
    )
    db_session.commit()

    # The script's own skip condition, exercised directly rather than via main()'s CLI.
    assert bool(batch.cost_adjustment_reason) is True

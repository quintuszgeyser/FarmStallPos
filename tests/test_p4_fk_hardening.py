"""Rev 5 Phase 4 prerequisite — stock_consumption.batch_id FK hardening.

models.py has declared ForeignKey('stock_batches.id') on this column since the
table was created, but the P0-3 baseline run (reports/p0-3-baseline-qa-
20260919.json) found 563 rows that violate it, which is only possible because
no constraint actually existed in the database (confirmed via pg_constraint).
strong_migrate() now adds fk_stock_consumption_batch as NOT VALID: it closes
the gap for new writes without failing at migration time on the historical
corruption. See app.py's Rev 5 Phase 4 prerequisite comment and reconcile.py's
INV-2 note for the full story.
"""
from sqlalchemy import text

from models import db


def test_fk_stock_consumption_batch_exists_and_is_not_validated_yet(db_session):
    row = db.session.execute(text("""
        SELECT convalidated
        FROM pg_constraint
        WHERE conrelid = 'stock_consumption'::regclass
          AND conname = 'fk_stock_consumption_batch'
          AND contype = 'f'
    """)).fetchone()
    assert row is not None, (
        "fk_stock_consumption_batch is missing — strong_migrate() should have "
        "added it (NOT VALID) on stock_consumption.batch_id"
    )
    # NOT VALID until Phase 4 repair cleans the 563 historical orphans and
    # VALIDATE CONSTRAINT is run as that repair's acceptance test. If this
    # flips to True, the migration comment recording that fact is the thing
    # to update, not this assertion.
    assert row[0] is False


def test_fk_stock_consumption_batch_blocks_new_orphans(db_session):
    import pytest
    from sqlalchemy.exc import IntegrityError

    from tests.factories import make_product, make_stock_batch, make_stock_consumption

    product = make_product(product_type='stock_item', name='FK Guard Item')
    batch = make_stock_batch(product)
    make_stock_consumption('sale-fk-guard-test', product, batch)

    db.session.delete(batch)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()

"""Rev 5 P1-1b — legacy VAT header backfill script tests.

Runs scripts/backfill_vat_headers.py as a real subprocess against the test
database (same precedent as tests/test_security.py's boot-guard tests) — it's
a standalone, operator-run script, not something imported and called
in-process, so exercising it as a real process is the faithful thing to do.

This also sidesteps the db_session fixture's SAVEPOINT-based isolation, which
intentionally hides uncommitted data from any OTHER connection (including a
subprocess's own engine) — data the backfill script needs to see must be
committed for real, via `app.app_context()` directly (same pattern as
tests/test_locking.py), with manual cleanup in a `finally` block.
"""
import os
import subprocess
import sys
from datetime import datetime, UTC

from helpers import set_setting
from models import Product, Sale, SaleHeader, db
from tests.conftest import TEST_DATABASE_URL, REPO_ROOT
from tests.helpers import D


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _run_backfill():
    env = os.environ.copy()
    env['DATABASE_URL'] = TEST_DATABASE_URL
    return subprocess.run(
        [sys.executable, 'scripts/backfill_vat_headers.py'],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


def _cleanup(app, sale_id):
    with app.app_context():
        product_ids = [r.product_id for r in Sale.query.filter_by(sale_id=sale_id).all()]
        Sale.query.filter_by(sale_id=sale_id).delete()
        SaleHeader.query.filter_by(sale_id=sale_id).delete()
        if product_ids:
            Product.query.filter(Product.id.in_(product_ids)).delete(synchronize_session=False)
        db.session.commit()


def test_backfill_creates_legacy_flat_header_for_sale_with_no_header(app):
    sale_id = 'backfill-test-basic'
    try:
        with app.app_context():
            set_setting('vat_registered', 'true')
            set_setting('vat_rate', 15)
            product = Product(name='Backfill Test Product ' + sale_id, product_type='simple',
                               price=D('11.50'), is_for_sale=True)
            db.session.add(product)
            db.session.flush()
            db.session.add(Sale(sale_id=sale_id, date_time=_now(), product_id=product.id,
                                 qty=D(1), unit_price=D('11.50'), payment_method='cash'))
            db.session.commit()
            assert SaleHeader.query.filter_by(sale_id=sale_id).first() is None

        result = _run_backfill()
        assert result.returncode == 0, result.stderr

        with app.app_context():
            header = SaleHeader.query.filter_by(sale_id=sale_id).one()
            assert header.vat_method == 'legacy_flat'
            # 11.50 incl -> excl = round(11.50/1.15, 2) = 10.00, vat = 1.50
            assert header.total_incl_vat == D('11.50')
            assert header.total_vat == D('1.50')
            assert header.total_excl_vat == D('10.00')
            # Subtotal buckets are deliberately meaningless for legacy_flat rows — must stay 0.
            assert header.standard_rated_subtotal == D('0.00')
            assert header.zero_rated_subtotal == D('0.00')
            assert header.exempt_subtotal == D('0.00')
            # Per-line columns deliberately left NULL — no fabricated breakdown for history.
            line = Sale.query.filter_by(sale_id=sale_id).one()
            assert line.vat_classification is None
            assert line.amount_excl is None
    finally:
        _cleanup(app, sale_id)


def test_backfill_is_idempotent_second_run_changes_nothing(app):
    sale_id = 'backfill-test-idempotent'
    try:
        with app.app_context():
            product = Product(name='Backfill Idempotency Product ' + sale_id, product_type='simple',
                               price=D('20.00'), is_for_sale=True)
            db.session.add(product)
            db.session.flush()
            db.session.add(Sale(sale_id=sale_id, date_time=_now(), product_id=product.id,
                                 qty=D(1), unit_price=D('20.00'), payment_method='cash'))
            db.session.commit()

        r1 = _run_backfill()
        assert r1.returncode == 0, r1.stderr

        with app.app_context():
            h1 = SaleHeader.query.filter_by(sale_id=sale_id).one()
            snapshot = (h1.id, h1.total_excl_vat, h1.total_vat, h1.total_incl_vat, h1.vat_method)

        r2 = _run_backfill()
        assert r2.returncode == 0, r2.stderr

        with app.app_context():
            rows = SaleHeader.query.filter_by(sale_id=sale_id).all()
            assert len(rows) == 1, 'second run must not create a duplicate header'
            h2 = rows[0]
            after = (h2.id, h2.total_excl_vat, h2.total_vat, h2.total_incl_vat, h2.vat_method)
            assert after == snapshot, 'second run must not change an existing header at all'
    finally:
        _cleanup(app, sale_id)


def test_backfill_never_touches_a_sale_that_already_has_a_per_line_header(app):
    sale_id = 'backfill-test-untouched'
    try:
        with app.app_context():
            product = Product(name='Already Migrated Product ' + sale_id, product_type='simple',
                               price=D('10.00'), is_for_sale=True)
            db.session.add(product)
            db.session.flush()
            db.session.add(Sale(
                sale_id=sale_id, date_time=_now(), product_id=product.id,
                qty=D(1), unit_price=D('10.00'), payment_method='cash',
                vat_classification='standard', vat_rate=D('15'),
                amount_excl=D('8.70'), vat_amount=D('1.30'), amount_incl=D('10.00'),
            ))
            db.session.add(SaleHeader(
                sale_id=sale_id, standard_rated_subtotal=D('8.70'),
                zero_rated_subtotal=D('0'), exempt_subtotal=D('0'),
                total_excl_vat=D('8.70'), total_vat=D('1.30'), total_incl_vat=D('10.00'),
                vat_method='per_line',
            ))
            db.session.commit()

        result = _run_backfill()
        assert result.returncode == 0, result.stderr

        with app.app_context():
            headers = SaleHeader.query.filter_by(sale_id=sale_id).all()
            assert len(headers) == 1
            assert headers[0].vat_method == 'per_line'   # untouched — not overwritten to legacy_flat
            assert headers[0].total_vat == D('1.30')       # untouched value
            line = Sale.query.filter_by(sale_id=sale_id).one()
            assert line.vat_classification == 'standard'  # untouched
    finally:
        _cleanup(app, sale_id)

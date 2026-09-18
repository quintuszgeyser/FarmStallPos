"""CSV bulk-import / stock interaction characterization test (Rev 5 P0-1
Wave 3 — beyond Section 5's explicit list).

NEW FINDING, not described anywhere in Rev 5 — flagged here for the
principal to classify per Operating Rule 17, not self-assigned an ID.

blueprints/imports.py's own template header comment documents `stock_qty`
as: "[OPTIONAL] [Integer] [default: 0] — Starting stock for NEW products
only. Ignored on update." In reality `stock_qty` is listed in `ALL_COLS`
(the parser reads it into `raw`) but is NEVER copied into the `fields` dict
built by `_build_product_fields` (~imports.py:900-953), so `_apply_fields`'s
generic `setattr(product, k, v)` loop never touches it — for new OR
existing products. The column is silently a no-op in both directions,
contradicting its own documentation.

This is compounded by `_validate_row` (imports.py:154) rejecting any
product_type other than 'stock_item'/'recipe' — 'simple' (the only product
type where `Product.stock_qty` is actually the live stock counter, per
CLAUDE.md's product-type table) cannot even be created via CSV import. So
`stock_qty` in the importer is dead for every product type that could
exist post-import: stock_item/recipe track stock via stock_batches, not
Product.stock_qty, and 'simple' products aren't importable at all.
"""
import io

from werkzeug.security import generate_password_hash

from models import Product
from tests.factories import make_admin
from tests.helpers import login_as


def test_csv_import_stock_qty_column_is_silently_ignored_for_new_products(db_session, client):
    make_admin(username='import_admin', password_hash=generate_password_hash('adminpass123'))
    login_as(client, 'import_admin', 'adminpass123')

    csv_body = (
        "name,product_type,unit_type,price,stock_qty\n"
        "CSV Import Probe,stock_item,unit,9.99,50\n"
    )
    resp = client.post(
        '/api/products/import?mode=import',
        data={'file': (io.BytesIO(csv_body.encode()), 'probe.csv')},
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body['summary']['create'] == 1, body

    product = Product.query.filter_by(name='CSV Import Probe').first()
    assert product is not None
    assert float(product.price) == 9.99

    # FINDING: the template's own documentation says stock_qty=50 should seed
    # starting stock for this new product. It does not — the column default
    # (0) is what actually lands, because stock_qty is never read into the
    # field-application dict. This is silent data loss relative to what the
    # importer's own header comment promises an operator.
    assert product.stock_qty == 0

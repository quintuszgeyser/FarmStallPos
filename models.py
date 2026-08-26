from decimal import Decimal
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Numeric

db = SQLAlchemy()

SESSION_TIMEOUT_MINUTES = 480   # 8-hour shift: idle logout after 8 h
SESSION_LOGOUT_HOURS    = 24    # hard logout after 24 h (end of day)


class User(db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(60), nullable=False, default='teller')
    active        = db.Column(db.Boolean, nullable=False, default=True)

    @property
    def roles(self):
        return [r.strip() for r in self.role.split(',') if r.strip()]

    def has_role(self, *roles):
        return any(r in self.roles for r in roles)


class Product(db.Model):
    __tablename__ = 'products'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), unique=True, nullable=False)
    price         = db.Column(Numeric(10, 2), nullable=True)
    barcode       = db.Column(db.String(32), unique=True, nullable=True)
    stock_qty     = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    product_type  = db.Column(db.String(20), nullable=False, default='simple', server_default='simple')
    unit_type     = db.Column(db.String(10), nullable=True)
    base_unit     = db.Column(db.String(10), nullable=True)
    sold_by_weight      = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    is_for_sale         = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    vat_type            = db.Column(db.String(20), nullable=False, default='standard', server_default="'standard'")
    price_per_unit      = db.Column(Numeric(10, 4), nullable=True)
    low_stock_threshold = db.Column(Numeric(10, 4), nullable=True)
    package_size        = db.Column(Numeric(10, 4), nullable=True)
    package_size_unit   = db.Column(db.String(10), nullable=True)
    package_unit        = db.Column(db.String(30), nullable=True)
    parent_stock_item_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    margin_pct           = db.Column(Numeric(8, 2), nullable=True)
    is_prepared          = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    is_available_online  = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    image_url            = db.Column(db.String(200), nullable=True)
    description          = db.Column(db.Text, nullable=True)
    is_archived          = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    archived_reason      = db.Column(db.String(200), nullable=True)
    product_code         = db.Column(db.Integer, unique=True, nullable=True)
    category_id          = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=True, index=True)
    # Scale sync fields - POS is single source of truth, scale is downstream cache
    sync_to_scale        = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    scale_tare           = db.Column(Numeric(8, 3), nullable=True)        # tare in grams
    scale_shelf_life     = db.Column(db.Integer, nullable=True)           # days
    scale_pack_qty       = db.Column(db.Integer, nullable=True)           # pack quantity
    scale_open_price     = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    scale_msg1           = db.Column(db.String(80), nullable=True)         # extra message text
    scale_msg2           = db.Column(db.String(80), nullable=True)
    # Consignment: supplier owes when item is sold, not when received
    is_consignment            = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    settlement_basis          = db.Column(db.String(20), nullable=False, default='FIXED_COST', server_default="'FIXED_COST'")
    consignment_pct           = db.Column(Numeric(5, 2), nullable=True)   # only for PCT_OF_SALE
    # supplier for simple consignment items (stock_item gets supplier from batch)
    consignment_supplier_id   = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    # fixed cost per base unit for simple consignment items (stock_item gets cost from batch)
    consignment_cost_per_unit = db.Column(Numeric(10, 6), nullable=True)
    scale_last_synced_at = db.Column(db.DateTime(timezone=True), nullable=True)
    scale_last_sync_status = db.Column(db.String(20), nullable=True)     # ok / error / pending
    scale_last_sync_error  = db.Column(db.Text, nullable=True)
    scale_hash           = db.Column(db.String(64), nullable=True)       # SHA-256 of last sent payload
    # Stats normalisation — grams/ml products set this to a "typical portion" so rankings
    # compare fairly against unit products (e.g. 250g = 1 portion of honey)
    stat_unit_size       = db.Column(Numeric(10, 4), nullable=True)
    # Batch-produce workflow: recipe products that are prepared in advance (cakes, jams, etc.)
    # Produce run consumes ingredients and creates a StockBatch (same engine as stock_item).
    is_produced          = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    batch_size           = db.Column(Numeric(10, 2), nullable=False, default=1, server_default='1')
    stock_unit           = db.Column(db.String(30), nullable=True)
    last_overhead_costs  = db.Column(db.Text, nullable=True)   # JSON — pre-fills next produce run overhead
    auto_price           = db.Column(db.Boolean, nullable=False, default=True, server_default='TRUE')
    pending_price          = db.Column(db.Numeric(10, 2), nullable=True)
    pending_price_per_unit = db.Column(db.Numeric(10, 6), nullable=True)
    sub_category_id    = db.Column(db.Integer, db.ForeignKey('sub_categories.id', ondelete='SET NULL'), nullable=True, index=True)
    product_family_id  = db.Column(db.Integer, db.ForeignKey('product_families.id', ondelete='SET NULL'), nullable=True, index=True)
    is_default_variant = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    # Packaging: optional capacity hint (how many units fit in this box). NULL = unlimited.
    packaging_capacity = db.Column(db.Integer, nullable=True)
    inventory_policy   = db.Column(db.String(20), nullable=False, default='ALLOW_NEGATIVE', server_default="'ALLOW_NEGATIVE'")


class Category(db.Model):
    """Central product category. One category per product (Product.category_id).
    name      = display form, as entered (trimmed).
    name_norm = lower(trim(name)), UNIQUE - enforces case/whitespace de-duplication.
    """
    __tablename__ = 'categories'
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(80), nullable=False)
    name_norm    = db.Column(db.String(80), unique=True, nullable=False, index=True)
    created_at   = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    is_packaging = db.Column(db.Boolean, nullable=False, default=False, server_default='false')

    products   = db.relationship('Product', backref='category', lazy='dynamic',
                                 foreign_keys='Product.category_id')


class SubCategory(db.Model):
    """Sub-category beneath a Category. Used for online shop filtering and product organisation."""
    __tablename__ = 'sub_categories'
    id          = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id', ondelete='CASCADE'), nullable=False, index=True)
    name        = db.Column(db.String(100), nullable=False)
    name_norm   = db.Column(db.String(100), nullable=False)  # lower(strip(name)) — unique per category
    sort_order  = db.Column(db.Integer, nullable=False, default=0)
    created_at  = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    products    = db.relationship('Product', backref='sub_category', lazy='dynamic',
                                  foreign_keys='Product.sub_category_id')


class ProductFamily(db.Model):
    """Groups related product variants (e.g. Apron Red, Apron Blue → Lady Coleen Apron)."""
    __tablename__ = 'product_families'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    slug        = db.Column(db.String(220), nullable=True, unique=True)
    created_at  = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime(timezone=True), nullable=True)

    variants    = db.relationship('Product', backref='family', lazy='dynamic',
                                  foreign_keys='Product.product_family_id')


class Attribute(db.Model):
    """A variant dimension: Colour, Size, Weight, Pack Size, etc."""
    __tablename__ = 'attributes'
    id     = db.Column(db.Integer, primary_key=True)
    name   = db.Column(db.String(100), nullable=False, unique=True)
    values = db.relationship('AttributeValue', backref='attribute', lazy='dynamic',
                             cascade='all, delete-orphan')


class AttributeValue(db.Model):
    """A specific value for an attribute: Red, Blue, Small, 500g, etc."""
    __tablename__ = 'attribute_values'
    id           = db.Column(db.Integer, primary_key=True)
    attribute_id = db.Column(db.Integer, db.ForeignKey('attributes.id', ondelete='CASCADE'), nullable=False, index=True)
    value        = db.Column(db.String(100), nullable=False)


class ProductVariantAttribute(db.Model):
    """Maps a product to its variant attribute values (e.g. product 5 → Colour=Red, Size=Large)."""
    __tablename__ = 'product_variant_attributes'
    product_id         = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), primary_key=True)
    attribute_value_id = db.Column(db.Integer, db.ForeignKey('attribute_values.id', ondelete='CASCADE'), primary_key=True)


class DeploySchedule(db.Model):
    """Scheduled deployments from QA → PROD."""
    __tablename__ = 'deploy_schedules'
    id           = db.Column(db.Integer, primary_key=True)
    scheduled_at = db.Column(db.DateTime(timezone=True), nullable=False)
    description  = db.Column(db.String(200), nullable=True)
    action       = db.Column(db.String(20), nullable=False, default='deploy')   # deploy/rollback
    status       = db.Column(db.String(20), nullable=False, default='pending')  # pending/running/done/failed
    created_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at   = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    executed_at  = db.Column(db.DateTime(timezone=True), nullable=True)
    result_log   = db.Column(db.Text, nullable=True)


class ProductImportRun(db.Model):
    """Audit log for CSV bulk product imports."""
    __tablename__ = 'product_import_runs'
    id             = db.Column(db.Integer, primary_key=True)
    imported_at    = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    file_name      = db.Column(db.String(200), nullable=True)
    file_hash      = db.Column(db.String(64), nullable=True)
    mode           = db.Column(db.String(20), nullable=False)  # preview/import/strict
    allow_name_match = db.Column(db.Boolean, nullable=False, default=False)
    duration_ms    = db.Column(db.Integer, nullable=True)
    rows_total     = db.Column(db.Integer, nullable=False, default=0)
    rows_created   = db.Column(db.Integer, nullable=False, default=0)
    rows_updated   = db.Column(db.Integer, nullable=False, default=0)
    rows_unchanged = db.Column(db.Integer, nullable=False, default=0)
    rows_skipped   = db.Column(db.Integer, nullable=False, default=0)
    rows_error     = db.Column(db.Integer, nullable=False, default=0)
    imported_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    error_log      = db.Column(db.Text, nullable=True)


class ProductBulkEditRun(db.Model):
    """Audit log for bulk product edits. Stores before-state for rollback."""
    __tablename__ = 'product_bulk_edit_runs'
    id              = db.Column(db.Integer, primary_key=True)
    created_at      = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    created_by      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    description     = db.Column(db.String(200), nullable=True)
    filter_json     = db.Column(db.Text, nullable=False)
    action_json     = db.Column(db.Text, nullable=False)
    product_count   = db.Column(db.Integer, nullable=False, default=0)
    before_json     = db.Column(db.Text, nullable=True)   # {id: {field: old_val}} for rollback
    rolled_back_at  = db.Column(db.DateTime(timezone=True), nullable=True)
    rolled_back_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class ScalePluLog(db.Model):
    """Audit log for PLU (product_code) changes. Prevents ghost products on scale."""
    __tablename__ = 'scale_plu_log'
    id           = db.Column(db.Integer, primary_key=True)
    product_id   = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    old_plu      = db.Column(db.Integer, nullable=True)
    new_plu      = db.Column(db.Integer, nullable=True)
    changed_at   = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    changed_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    sync_cleared = db.Column(db.Boolean, nullable=False, default=False)  # True once old PLU removed from scale


class ScaleSyncRun(db.Model):
    __tablename__ = 'scale_sync_runs'
    id               = db.Column(db.Integer, primary_key=True)
    started_at       = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    completed_at     = db.Column(db.DateTime(timezone=True), nullable=True)
    run_type         = db.Column(db.String(20), nullable=False)  # full / incremental / preview / read
    status           = db.Column(db.String(20), nullable=False, default='running')  # running/ok/error
    products_total   = db.Column(db.Integer, nullable=False, default=0)
    products_sent    = db.Column(db.Integer, nullable=False, default=0)
    products_failed  = db.Column(db.Integer, nullable=False, default=0)
    orphans_detected = db.Column(db.Integer, nullable=False, default=0)
    orphans_removed  = db.Column(db.Integer, nullable=False, default=0)
    error_message    = db.Column(db.Text, nullable=True)
    triggered_by     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class ScaleSnapshot(db.Model):
    __tablename__ = 'scale_snapshots'
    id           = db.Column(db.Integer, primary_key=True)
    captured_at  = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    run_id       = db.Column(db.Integer, db.ForeignKey('scale_sync_runs.id'), nullable=True)
    plu_count    = db.Column(db.Integer, nullable=False, default=0)
    snapshot_json = db.Column(db.Text, nullable=True)   # JSON list of PLUs on scale


class ScaleKeyboardPreset(db.Model):
    """Keyboard shortcut layout for BC-4000 scale (MsgNo 1024). 170 key slots."""
    __tablename__ = 'scale_keyboard_presets'
    id       = db.Column(db.Integer, primary_key=True)
    key_id   = db.Column(db.Integer, nullable=False, unique=True)  # 1–170
    plu_no   = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)  # None = empty slot
    label    = db.Column(db.String(20), nullable=True)  # display label (optional, informational)

    product  = db.relationship('Product', foreign_keys=[plu_no])


class ScaleAdvertMessage(db.Model):
    """Advertisement messages shown on BC-4000 scale display (MsgNo 1029). 43 slots."""
    __tablename__ = 'scale_advert_messages'
    id         = db.Column(db.Integer, primary_key=True)
    slot       = db.Column(db.Integer, nullable=False, unique=True)  # 1–43
    display_no = db.Column(db.Integer, nullable=False, default=2)    # screen (2=main)
    text       = db.Column(db.String(100), nullable=False, default='')
    enabled    = db.Column(db.Boolean, nullable=False, default=True)


class ProductImage(db.Model):
    __tablename__ = 'product_images'
    id            = db.Column(db.Integer, primary_key=True)
    product_id    = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    filename      = db.Column(db.String(200), nullable=False)
    is_primary    = db.Column(db.Boolean, nullable=False, default=False)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    created_at    = db.Column(db.DateTime(timezone=True), server_default=db.func.now())


class KitchenOrder(db.Model):
    __tablename__ = 'kitchen_orders'
    id           = db.Column(db.Integer, primary_key=True)
    sale_id      = db.Column(db.String(64), nullable=False, index=True)
    product_id   = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    product_name = db.Column(db.String(120), nullable=False)
    qty          = db.Column(Numeric(10, 4), nullable=False)
    ingredients  = db.Column(db.Text, nullable=True)
    status       = db.Column(db.String(20), nullable=False, default='pending')
    sort_order   = db.Column(db.Integer, nullable=False, default=0)
    queued_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    teller_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    notes        = db.Column(db.String(500), nullable=True)


class Supplier(db.Model):
    __tablename__ = 'suppliers'
    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(120), unique=True, nullable=False)
    phone   = db.Column(db.String(50),  nullable=True)
    email   = db.Column(db.String(120), nullable=True)
    website = db.Column(db.String(200), nullable=True)
    notes   = db.Column(db.String(500), nullable=True)
    last_run_costs = db.Column(db.Text, nullable=True)  # JSON — pre-fills next purchase run


class CostCategory(db.Model):
    __tablename__ = 'cost_categories'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(64), nullable=False, unique=True)   # slug e.g. "shipping"
    label      = db.Column(db.String(128), nullable=False)               # display e.g. "Shipping"
    color      = db.Column(db.String(16), nullable=True)
    is_active  = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=True)


class SupplierInvoice(db.Model):
    __tablename__ = 'supplier_invoices'
    id                       = db.Column(db.Integer, primary_key=True)
    supplier_id              = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)
    date                     = db.Column(db.Date, nullable=False)
    invoice_number           = db.Column(db.String(128), nullable=True)   # supplier's ref e.g. INV-001
    subtotal                 = db.Column(db.Numeric(18, 4), nullable=True)  # sum of product lines excl VAT
    additional_costs_json    = db.Column(db.Text, nullable=True)            # [{label,type,amount}] — shipping/overhead only
    additional_costs_total   = db.Column(db.Numeric(18, 4), nullable=True)
    vat_total                = db.Column(db.Numeric(18, 4), nullable=True)  # VAT on invoice — allocated to batches proportionally
    discount_total           = db.Column(db.Numeric(18, 4), nullable=True)  # total discount on invoice — allocated to batches proportionally
    discounts_json           = db.Column(db.Text, nullable=True)              # [{label,amount}] structured discount entries
    supplier_invoice_total   = db.Column(db.Numeric(18, 4), nullable=True)   # total printed on supplier invoice
    reconciliation_difference = db.Column(db.Numeric(18, 4), nullable=True)  # calculated_total - supplier_invoice_total
    vat_treatment            = db.Column(db.Text, nullable=True)            # lines_excl_vat | lines_incl_vat | unknown
    accounting_balanced      = db.Column(db.Boolean, nullable=True)         # lines + overheads + VAT ≈ invoice total
    total                    = db.Column(db.Numeric(18, 4), nullable=True)  # subtotal + additional_costs_total + vat_total - discount_total
    status                   = db.Column(db.String(20), nullable=False, default='posted')  # draft | posted
    source                   = db.Column(db.String(30), nullable=True)      # purchase_run | single_receive | bulk_receive
    notes                    = db.Column(db.Text, nullable=True)
    created_at               = db.Column(db.DateTime, nullable=True)
    created_by               = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    scan_raw_json            = db.Column(db.Text, nullable=True)            # original parser output for learning
    documents                = db.relationship('SupplierDocument', backref='invoice', lazy='dynamic',
                                               foreign_keys='SupplierDocument.invoice_id')


class SupplierInvoiceTemplate(db.Model):
    """Per-supplier document layout, VAT rules, and line classifier rules.
    Learned from confirmed purchase runs. One active template per supplier."""
    __tablename__ = 'supplier_invoice_templates'
    id               = db.Column(db.Integer, primary_key=True)
    supplier_id      = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    template_name    = db.Column(db.Text, nullable=True)
    document_type    = db.Column(db.Text, nullable=False, default='unknown')
    layout_type      = db.Column(db.Text, nullable=False, default='unknown')  # table | text | unknown
    column_hints     = db.Column(db.Text, nullable=False, default='{}')        # JSON
    totals_rules     = db.Column(db.Text, nullable=False, default='{}')        # JSON
    vat_rules        = db.Column(db.Text, nullable=False, default='{}')        # JSON
    line_classifier_rules = db.Column(db.Text, nullable=False, default='{}')  # JSON
    confidence       = db.Column(Numeric(5, 4), nullable=False, default=0)
    active           = db.Column(db.Boolean, nullable=False, default=True)
    last_successful_parse_at = db.Column(db.DateTime, nullable=True)
    last_failed_parse_at     = db.Column(db.DateTime, nullable=True)
    created_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SupplierProductMapping(db.Model):
    """Per-supplier learned mapping: raw invoice description → internal product + pack_multiplier.
    product_id is nullable — shipping, discount, and rounding lines are not products."""
    __tablename__ = 'supplier_product_mappings'
    id                         = db.Column(db.Integer, primary_key=True)
    supplier_id                = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)
    supplier_sku               = db.Column(db.Text, nullable=True)
    raw_description_original   = db.Column(db.Text, nullable=False)
    raw_description_normalized = db.Column(db.Text, nullable=False)
    raw_description_hash       = db.Column(db.Text, nullable=False)
    product_id                 = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    line_type                  = db.Column(db.Text, nullable=False, default='STOCK_ITEM')
    invoice_unit               = db.Column(db.Text, nullable=True)
    stock_unit                 = db.Column(db.Text, nullable=True)
    pack_multiplier            = db.Column(Numeric(12, 4), nullable=False, default=1)
    allocation_method          = db.Column(db.Text, nullable=True)
    correction_count           = db.Column(db.Integer, nullable=False, default=1)
    confidence                 = db.Column(Numeric(5, 4), nullable=False, default=Decimal('0.6000'))
    mapping_state              = db.Column(db.Text, nullable=False, default='SUGGESTED')  # SUGGESTED | CONFIRMED | REJECTED | IGNORED
    document_type              = db.Column(db.Text, nullable=False, default='unknown')
    first_learned_at           = db.Column(db.DateTime, nullable=True)
    default_overhead_allocation = db.Column(db.Text, nullable=True)
    purchase_unit              = db.Column(db.Text, nullable=True)
    contains_qty               = db.Column(db.Integer, nullable=True)
    item_size                  = db.Column(Numeric(10, 4), nullable=True)
    item_size_unit             = db.Column(db.Text, nullable=True)
    raw_description_tokens     = db.Column(db.Text, nullable=True)
    last_used_at               = db.Column(db.DateTime, nullable=True)
    created_at                 = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at                 = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SupplierInvoiceLearningEvent(db.Model):
    """Immutable audit trail of every learning action — never updated or deleted."""
    __tablename__ = 'supplier_invoice_learning_events'
    id                 = db.Column(db.Integer, primary_key=True)
    invoice_id         = db.Column(db.Integer, db.ForeignKey('supplier_invoices.id'), nullable=True, index=True)
    supplier_id        = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)
    mapping_id         = db.Column(db.Integer, db.ForeignKey('supplier_product_mappings.id'), nullable=True)
    raw_description    = db.Column(db.Text, nullable=True)
    matched_product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    action             = db.Column(db.Text, nullable=False)  # created | updated | confirmed | rejected | ignored | state_changed
    old_confidence     = db.Column(Numeric(5, 4), nullable=True)
    new_confidence     = db.Column(Numeric(5, 4), nullable=True)
    old_product_id     = db.Column(db.Integer, nullable=True)
    new_product_id     = db.Column(db.Integer, nullable=True)
    old_state          = db.Column(db.Text, nullable=True)
    new_state          = db.Column(db.Text, nullable=True)
    match_score        = db.Column(Numeric(5, 4), nullable=True)
    created_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SupplierDocument(db.Model):
    __tablename__ = 'supplier_documents'
    id           = db.Column(db.Integer, primary_key=True)
    supplier_id  = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    invoice_id   = db.Column(db.Integer, db.ForeignKey('supplier_invoices.id'), nullable=True, index=True)
    filename     = db.Column(db.String(200), nullable=False)   # stored filename on disk
    original_name = db.Column(db.String(200), nullable=False)  # original upload name
    uploaded_at  = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    uploaded_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class RecipeLine(db.Model):
    __tablename__ = 'recipe_lines'
    id            = db.Column(db.Integer, primary_key=True)
    product_id    = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_base      = db.Column(Numeric(10, 4), nullable=False)


class StockBatch(db.Model):
    __tablename__ = 'stock_batches'
    id                  = db.Column(db.Integer, primary_key=True)
    product_id          = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_purchased_base  = db.Column(Numeric(10, 4), nullable=False)
    qty_remaining_base  = db.Column(Numeric(10, 4), nullable=False)
    cost_per_base_unit  = db.Column(Numeric(10, 6), nullable=False)
    purchased_at        = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    supplier_id         = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    user_id             = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    sort_order          = db.Column(db.Integer, nullable=True)
    import_run_id       = db.Column(db.String(36), nullable=True)  # UUID grouping batches from one CSV import
    produce_ref         = db.Column(db.String(36), nullable=True)   # produce_uuid — links to StockConsumption records for the produce run
    produce_cost        = db.Column(Numeric(10, 4), nullable=True)   # total ingredient cost stamped at produce time
    # VAT-aware costing columns — all stamped at creation time, never derived at query time.
    # Allocation order: base_cost_total (ex-VAT) → +vat_amount → base_cost_incl_vat
    #                   → +overheads → -allocated_discount → final_cost_incl_vat  (= cost_per_base_unit × qty_base)
    # Future: add vat_rate per line when mixed-rate (0%/15%) invoices are needed.
    vat_amount           = db.Column(Numeric(10, 4), nullable=True)  # proportional VAT share
    base_cost_total      = db.Column(Numeric(18, 4), nullable=True)  # product line cost ex-VAT
    base_cost_incl_vat   = db.Column(Numeric(18, 4), nullable=True)  # base_cost_total + vat_amount
    allocated_shipping   = db.Column(Numeric(18, 4), nullable=True)  # shipping overhead share only
    final_cost_incl_vat  = db.Column(Numeric(18, 4), nullable=True)  # base_incl_vat + all overheads - allocated_discount
    allocated_discount   = db.Column(Numeric(18, 4), nullable=True)  # proportional discount share for this batch
    additional_costs     = db.Column(db.Text, nullable=True)         # JSON {label,type,amount,source,source_id}
    cost_adjustment_reason = db.Column(db.Text, nullable=True)     # optional free-text reason for a post-creation edit
    updated_at          = db.Column(db.DateTime, nullable=True)     # stamped on explicit batch edits
    updated_by          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    invoice_id          = db.Column(db.Integer, db.ForeignKey('supplier_invoices.id'), nullable=True, index=True)
    # Consignment ownership — set at receive time, immutable; history is always correct even if product flag changes later
    # Values: NORMAL | CONSIGNMENT | RETURNED
    ownership_type        = db.Column(db.String(20), nullable=False, default='NORMAL', server_default="'NORMAL'")
    consignment_unit_cost = db.Column(Numeric(10, 4), nullable=True)  # settlement contract cost, separate from FIFO cost
    batch_type            = db.Column(db.String(30), nullable=False, default='normal', server_default="'normal'")


class StockConsumption(db.Model):
    __tablename__ = 'stock_consumption'
    id                  = db.Column(db.Integer, primary_key=True)
    sale_id             = db.Column(db.String(64), nullable=False, index=True)
    ingredient_id       = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    batch_id            = db.Column(db.Integer, db.ForeignKey('stock_batches.id'), nullable=False)
    qty_consumed_base   = db.Column(Numeric(10, 4), nullable=False)
    cost_per_base_unit  = db.Column(Numeric(10, 6), nullable=False)
    consumed_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class StockAdjustment(db.Model):
    __tablename__ = 'stock_adjustments'
    id                = db.Column(db.Integer, primary_key=True)
    product_id        = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    adjustment_type   = db.Column(db.String(20), nullable=False)
    qty_change_base   = db.Column(Numeric(10, 4), nullable=False)
    system_qty_before = db.Column(Numeric(10, 4), nullable=False)
    cost_written_off  = db.Column(Numeric(10, 4), nullable=True)
    base_unit         = db.Column(db.String(20), nullable=True)
    reason            = db.Column(db.String(200), nullable=False)
    adjusted_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user_id           = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class Purchase(db.Model):
    __tablename__ = 'purchases'
    id             = db.Column(db.Integer, primary_key=True)
    product_id     = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty_added      = db.Column(db.Integer, nullable=False)
    purchase_price = db.Column(Numeric(10, 2), nullable=False)
    date_time      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class Setting(db.Model):
    __tablename__ = 'settings'
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(50), unique=True, nullable=False)
    # Widened 200->2000 for branding_invoice_footer etc. (see strong_migrate ALTER).
    # Must ship in the same image as the ALTER so SQLAlchemy doesn't truncate to 200.
    value = db.Column(db.String(2000), nullable=False)


class BackupLog(db.Model):
    __tablename__ = 'backup_log'
    id                   = db.Column(db.Integer, primary_key=True)
    started_at           = db.Column(db.DateTime, nullable=False)
    completed_at         = db.Column(db.DateTime)
    status               = db.Column(db.String(20), nullable=False)  # running / ok / failed
    triggered_by         = db.Column(db.String(20))   # manual / schedule / pre-restore / pre-upgrade / verify
    db_name              = db.Column(db.String(100))
    file_name            = db.Column(db.String(250))
    file_size            = db.Column(db.BigInteger)
    sha256               = db.Column(db.String(64))
    provider             = db.Column(db.String(20))
    drive_file_id        = db.Column(db.String(200))
    app_version          = db.Column(db.String(50))
    schema_version       = db.Column(db.Integer)
    error                = db.Column(db.Text)
    restore_status       = db.Column(db.String(20))
    restore_completed_at = db.Column(db.DateTime)
    restore_target_db    = db.Column(db.String(100))


class UserSession(db.Model):
    __tablename__ = 'user_sessions'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    logged_in   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    logged_out  = db.Column(db.DateTime, nullable=True)
    last_active = db.Column(db.DateTime, nullable=True)


class Sale(db.Model):
    __tablename__ = 'sales'
    id           = db.Column(db.Integer, primary_key=True)
    sale_id      = db.Column(db.String(64), index=True, nullable=False)
    date_time    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    product_id   = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    product_name = db.Column(db.String(200), nullable=True)
    qty          = db.Column(Numeric(10, 4), nullable=False)
    unit_price   = db.Column(Numeric(10, 2), nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    customer_id  = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    voided       = db.Column(db.Boolean, nullable=False, default=False)
    voided_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    voided_at    = db.Column(db.DateTime, nullable=True)
    void_reason  = db.Column(db.String(200), nullable=True)
    flagged      = db.Column(db.Boolean, nullable=False, default=False)
    flag_note    = db.Column(db.String(500), nullable=True)
    flag_resolved = db.Column(db.Boolean, nullable=False, default=False)
    sub_log       = db.Column(db.Text, nullable=True)
    discount_json = db.Column(db.Text, nullable=True)
    discount_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    # Tender info (ISSUE-29): what the customer paid with. Nullable so historical rows
    # (pre-migration) stay valid; new sales persist the teller's cash/card choice.
    payment_method    = db.Column(db.String(16), nullable=True)   # 'cash' | 'card' | 'qr' | 'split'
    cash_tendered     = db.Column(Numeric(10, 2), nullable=True)  # change calc / till reconciliation
    card_amount       = db.Column(Numeric(10, 2), nullable=True)  # split payment card portion
    original_sale_id  = db.Column(db.String(36), nullable=True)   # set on return rows; points to the originating sale_id
    cogs              = db.Column(Numeric(10, 4), nullable=True)   # FIFO cost stamped at checkout — immutable


class AuditLog(db.Model):
    """Append-only forensic trail for destructive actions (voids, edits) - ISSUE-31.
    Never UPDATE/DELETE these rows. before_json captures the pre-mutation state so a
    voided/edited sale can always be reconstructed (SARS s29 unalterable records)."""
    __tablename__ = 'audit_log'
    id            = db.Column(db.Integer, primary_key=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    event_type    = db.Column(db.String(40), nullable=False)   # 'sale_void' | 'sale_edit'
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    target_table  = db.Column(db.String(40), nullable=True)
    target_id     = db.Column(db.String(64), nullable=True)    # sale_id or row id
    before_json   = db.Column(db.Text, nullable=True)
    note          = db.Column(db.String(500), nullable=True)


class Special(db.Model):
    __tablename__ = 'specials'
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(120), nullable=False)
    special_price  = db.Column(Numeric(10, 2), nullable=False)
    active         = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    schedule       = db.Column(db.Text, nullable=True)
    discount_type  = db.Column(db.String(20), nullable=False, default='fixed_price', server_default='fixed_price')
    discount_value = db.Column(Numeric(10, 2), nullable=True)


class Invoice(db.Model):
    __tablename__ = 'invoices'
    id               = db.Column(db.Integer, primary_key=True)
    invoice_number   = db.Column(db.String(20), unique=True, nullable=False)
    created_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    due_date         = db.Column(db.String(20), nullable=True)
    customer_name    = db.Column(db.String(120), nullable=True)
    customer_phone   = db.Column(db.String(50), nullable=True)
    customer_email   = db.Column(db.String(120), nullable=True)
    customer_address = db.Column(db.Text, nullable=True)
    notes            = db.Column(db.Text, nullable=True)
    bank_details     = db.Column(db.Text, nullable=True)
    lines_json       = db.Column(db.Text, nullable=False, default='[]')
    sale_id          = db.Column(db.String(64), nullable=True)
    subtotal         = db.Column(Numeric(10, 2), nullable=False, default=0)
    discount_pct     = db.Column(Numeric(5, 2), nullable=True)
    total            = db.Column(Numeric(10, 2), nullable=False, default=0)
    status           = db.Column(db.String(20), nullable=False, default='draft')
    created_by       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    customer_id      = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)


class SpecialLine(db.Model):
    __tablename__ = 'special_lines'
    id         = db.Column(db.Integer, primary_key=True)
    special_id = db.Column(db.Integer, db.ForeignKey('specials.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    qty        = db.Column(db.Integer, nullable=False, default=1)
    group_id   = db.Column(db.Integer, nullable=True)  # lines sharing a group_id are alternatives (OR); different group_ids are all required (AND)


class Customer(db.Model):
    __tablename__ = 'customers'
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(120), nullable=True)
    phone           = db.Column(db.String(50),  nullable=True)
    email           = db.Column(db.String(120), nullable=True)
    notes           = db.Column(db.Text,        nullable=True)
    enrolled_at     = db.Column(db.DateTime,    nullable=False, default=datetime.utcnow)
    enrolled_by     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    last_visit      = db.Column(db.DateTime,    nullable=True)
    visit_count     = db.Column(db.Integer,     nullable=False, default=0)
    active          = db.Column(db.Boolean,     nullable=False, default=True)
    auto_enrolled   = db.Column(db.Boolean,     nullable=False, default=False)
    customer_number = db.Column(db.String(20),  nullable=True, unique=True)
    first_seen      = db.Column(db.DateTime,    nullable=True)
    is_employee          = db.Column(db.Boolean, nullable=False, default=False)
    merged_into          = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    is_online_customer   = db.Column(db.Boolean, nullable=False, default=False)
    is_pos_customer      = db.Column(db.Boolean, nullable=False, default=False)

    plates  = db.relationship('CustomerPlate', backref='customer', lazy='dynamic',
                              foreign_keys='CustomerPlate.customer_id')
    faces   = db.relationship('CustomerFace', backref='customer', lazy='dynamic',
                              foreign_keys='CustomerFace.customer_id')
    gaits   = db.relationship('CustomerGait', backref='customer', lazy='dynamic',
                              foreign_keys='CustomerGait.customer_id')


class CustomerPlate(db.Model):
    __tablename__ = 'customer_plates'
    id           = db.Column(db.Integer, primary_key=True)
    customer_id  = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    plate_number = db.Column(db.String(20), nullable=False, unique=True)
    enrolled_at  = db.Column(db.DateTime,   nullable=False, default=datetime.utcnow)
    active       = db.Column(db.Boolean,    nullable=False, default=True)


class CustomerFace(db.Model):
    __tablename__ = 'customer_faces'
    id           = db.Column(db.Integer, primary_key=True)
    customer_id  = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    embedding    = db.Column(db.LargeBinary, nullable=False)
    photo        = db.Column(db.LargeBinary, nullable=True)
    body_photo   = db.Column(db.LargeBinary, nullable=True)
    enrolled_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    active       = db.Column(db.Boolean,  nullable=False, default=True)
    quality      = db.Column(Numeric(4, 3), nullable=True)
    camera_source = db.Column(db.String(20), nullable=True)
    original_customer_id = db.Column(db.Integer, nullable=True)


class CustomerGait(db.Model):
    __tablename__ = 'customer_gaits'
    id            = db.Column(db.Integer, primary_key=True)
    customer_id   = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    gait_features = db.Column(db.LargeBinary, nullable=False)
    enrolled_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    active        = db.Column(db.Boolean,  nullable=False, default=True)


class CustomerVisit(db.Model):
    __tablename__ = 'customer_visits'
    id               = db.Column(db.Integer, primary_key=True)
    customer_id      = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    detected_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    matched_signals  = db.Column(db.String(50),  nullable=False)
    confidence_scores = db.Column(db.Text,       nullable=True)
    camera_source    = db.Column(db.String(20),  nullable=True)
    acknowledged     = db.Column(db.Boolean,     nullable=False, default=False)


class TillSession(db.Model):
    """End-of-day cash-up record. One row per till close (ISSUE-33).
    Captures opening float, counted cash, and computes over/under vs POS cash sales."""
    __tablename__ = 'till_sessions'
    id              = db.Column(db.Integer, primary_key=True)
    opened_at       = db.Column(db.DateTime, nullable=False)                    # start of trading period
    closed_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    opened_by       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    closed_by       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    opening_float   = db.Column(Numeric(10, 2), nullable=False, default=0)      # cash in drawer at open
    counted_cash    = db.Column(Numeric(10, 2), nullable=False)                 # physical count at close
    pos_cash_sales  = db.Column(Numeric(10, 2), nullable=False)                 # POS cash sales in period
    pos_card_sales  = db.Column(Numeric(10, 2), nullable=False, default=0)
    pos_total_sales = db.Column(Numeric(10, 2), nullable=False)
    expected_cash   = db.Column(Numeric(10, 2), nullable=False)                 # opening_float + pos_cash_sales
    over_under      = db.Column(Numeric(10, 2), nullable=False)                 # counted_cash - expected_cash
    void_total      = db.Column(Numeric(10, 2), nullable=False, default=0)
    cash_refunds    = db.Column(Numeric(10, 2), nullable=True, default=0)       # cash paid out for returns
    notes           = db.Column(db.Text, nullable=True)


# ── Label Printing Subsystem ──────────────────────────────────────────────────

class LabelTemplate(db.Model):
    """
    A reusable drag-and-drop label layout. Elements are stored as JSON.
    Each element: {type, x, y, w, h, font_size, align, bold, color,
                   barcode_format, value}
    Dimensions in mm. category: small_barcode | shelf | sticker | price_tag | custom
    """
    __tablename__ = 'label_templates'
    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(100), nullable=False)
    description      = db.Column(db.String(300), nullable=True)
    width_mm         = db.Column(Numeric(6, 2), nullable=False)
    height_mm        = db.Column(Numeric(6, 2), nullable=False)
    category         = db.Column(db.String(30), nullable=False, default='custom')
    elements_json    = db.Column(db.Text, nullable=False, default='[]')
    background_color = db.Column(db.String(10), nullable=False, default='#ffffff')
    border           = db.Column(db.Boolean, nullable=False, default=False)
    is_archived      = db.Column(db.Boolean, nullable=False, default=False)
    created_by       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at       = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime(timezone=True), nullable=True)


class LabelPrintJob(db.Model):
    """Audit log: every label print event — user, product, template, qty, outcome."""
    __tablename__ = 'label_print_jobs'
    id          = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('label_templates.id'), nullable=True)
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'),        nullable=True)
    qty         = db.Column(db.Integer, nullable=False, default=1)
    printer_id  = db.Column(db.Integer, nullable=True)   # LabelPrinter.id (soft ref)
    status      = db.Column(db.String(20), nullable=False, default='sent')  # sent|failed|browser_print
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    printed_at  = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    notes       = db.Column(db.Text, nullable=True)


class LabelPrinter(db.Model):
    """
    Configured printers per store. One row per physical printer.
    connection: usb | bluetooth | network
    address:    USB vid:pid, BT MAC, or IP:port for network printers.
    """
    __tablename__ = 'label_printers'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(80),  nullable=False)
    model      = db.Column(db.String(60),  nullable=False, default='xprinter_xp365b')
    connection = db.Column(db.String(20),  nullable=False, default='usb')
    address    = db.Column(db.String(120), nullable=True)   # USB vid:pid | BT MAC | IP:port
    is_active  = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class PlateDetection(db.Model):
    __tablename__ = 'plate_detections'
    id            = db.Column(db.Integer, primary_key=True)
    plate_number  = db.Column(db.String(20),  nullable=False)
    confidence    = db.Column(Numeric(3, 2),  nullable=True)
    detected_at   = db.Column(db.DateTime,    nullable=False, default=datetime.utcnow)
    customer_id   = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    matched       = db.Column(db.Boolean,     nullable=False, default=False)
    snapshot_path = db.Column(db.Text,        nullable=True)


# ── Consignment Inventory ─────────────────────────────────────────────────────
# Rule: owe supplier on consumption (sale/write-off), not on receipt.

class ConsignmentSettlement(db.Model):
    """One settlement per supplier per pay run."""
    __tablename__ = 'consignment_settlements'
    id           = db.Column(db.Integer, primary_key=True)
    supplier_id  = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)
    total_amount = db.Column(Numeric(10, 2), nullable=False)
    note         = db.Column(db.Text, nullable=True)
    created_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ConsignmentLiability(db.Model):
    """One row per FIFO batch consumption of a consignment item.
    status: outstanding | settled | voided"""
    __tablename__ = 'consignment_liabilities'
    id                         = db.Column(db.Integer, primary_key=True)
    supplier_id                = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)
    product_id                 = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    batch_id                   = db.Column(db.Integer, db.ForeignKey('stock_batches.id'), nullable=True)   # NULL for simple products
    sale_id                    = db.Column(db.String(64), nullable=True, index=True)  # NULL for write-offs
    qty_consumed               = db.Column(Numeric(10, 4), nullable=False)
    unit_cost                  = db.Column(Numeric(10, 6), nullable=False)   # consignment_unit_cost snapshot
    amount_owed                = db.Column(Numeric(10, 2), nullable=False)
    sale_price_at_time         = db.Column(Numeric(10, 2), nullable=True)   # for PCT_OF_SALE audit trail
    settlement_percent_at_time = db.Column(Numeric(5, 2), nullable=True)    # for PCT_OF_SALE audit trail
    status                     = db.Column(db.String(20), nullable=False, default='outstanding')
    settlement_id              = db.Column(db.Integer, db.ForeignKey('consignment_settlements.id'), nullable=True)
    created_at                 = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    settled_at                 = db.Column(db.DateTime, nullable=True)


class ConsignmentSettlementLine(db.Model):
    """Normalised snapshot line — one row per liability included in a settlement."""
    __tablename__ = 'consignment_settlement_lines'
    id            = db.Column(db.Integer, primary_key=True)
    settlement_id = db.Column(db.Integer, db.ForeignKey('consignment_settlements.id'), nullable=False, index=True)
    liability_id  = db.Column(db.Integer, db.ForeignKey('consignment_liabilities.id'), nullable=False)
    supplier_id   = db.Column(db.Integer, nullable=False)
    product_id    = db.Column(db.Integer, nullable=False)
    batch_id      = db.Column(db.Integer, nullable=True)
    qty           = db.Column(Numeric(10, 4), nullable=False)
    unit_cost     = db.Column(Numeric(10, 6), nullable=False)
    amount        = db.Column(Numeric(10, 2), nullable=False)
    camera_source = db.Column(db.String(20),  nullable=True)


# ── Packaging suggestions ─────────────────────────────────────────────────────

class PackagingUsage(db.Model):
    """Tracks which packaging product was used with which product at checkout.
    Drives smart suggestions sorted by use_count.

    product_id = 0 is the cart-level sentinel (no FK — intentional, 0 is not a real product).
    qty_bucket: 1=1-2 units, 2=3-6, 3=7-12, 4=12+
    """
    __tablename__  = 'packaging_usage'
    id                   = db.Column(db.Integer, primary_key=True)
    product_id           = db.Column(db.Integer, nullable=False, default=0, index=True)
    qty_bucket           = db.Column(db.SmallInteger, nullable=False, default=1)
    packaging_product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    use_count            = db.Column(db.Integer, nullable=False, default=1)
    last_used_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__       = (db.UniqueConstraint('product_id', 'qty_bucket', 'packaging_product_id'),)


class ProductPurchaseOption(db.Model):
    """Multiple supplier package sizes for one product (e.g. 250 g and 350 g)."""
    __tablename__ = 'product_purchase_options'
    id                = db.Column(db.Integer, primary_key=True)
    product_id        = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False, index=True)
    package_size      = db.Column(db.Numeric(10, 4), nullable=False)
    package_size_unit = db.Column(db.String(10), nullable=False, default='g')
    package_unit      = db.Column(db.String(30), nullable=True)
    sort_order        = db.Column(db.Integer, nullable=False, default=0)


class CustomisationRule(db.Model):
    """Flat-price rules for recipe customisations at the teller (swap / extra).
    Rules take priority over FIFO cost-based pricing when a category match is found.
    """
    __tablename__ = 'customisation_rules'
    id            = db.Column(db.Integer, primary_key=True)
    rule_type     = db.Column(db.String(10), nullable=False)     # 'swap' | 'extra'
    from_category = db.Column(db.String(120), nullable=True)     # swap: original ingredient category (null = any)
    to_category   = db.Column(db.String(120), nullable=False)    # swap: replacement category; extra: added ingredient category
    price_adj     = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    label         = db.Column(db.String(200), nullable=True)     # shown to teller e.g. "Oat milk surcharge"
    active        = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    sort_order    = db.Column(db.Integer, nullable=False, default=0, server_default='0')


# ── Employee / Payroll Subsystem ──────────────────────────────────────────────

class Employee(db.Model):
    __tablename__ = 'employees'
    id                   = db.Column(db.Integer, primary_key=True)
    user_id              = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    name                 = db.Column(db.String(120), nullable=False)
    employee_number      = db.Column(db.String(20), nullable=True, unique=True)
    id_number            = db.Column(db.String(13), nullable=True)
    tax_number           = db.Column(db.String(20), nullable=True)
    uif_number           = db.Column(db.String(20), nullable=True)
    bank_name            = db.Column(db.String(80), nullable=True)
    bank_account         = db.Column(db.String(30), nullable=True)
    bank_branch_code     = db.Column(db.String(10), nullable=True)
    phone                = db.Column(db.String(20), nullable=True)
    start_date           = db.Column(db.Date, nullable=True)
    employment_type      = db.Column(db.String(20), nullable=False, default='permanent', server_default="'permanent'")
    hourly_rate          = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    normal_hours_per_day = db.Column(Numeric(4, 2), nullable=False, default=9, server_default='9')
    normal_days_per_week = db.Column(db.Integer, nullable=False, default=5, server_default='5')
    pay_frequency        = db.Column(db.String(20), nullable=False, default='biweekly', server_default="'biweekly'")
    pay_day_of_week      = db.Column(db.Integer, nullable=False, default=5, server_default='5')  # 0=Mon…6=Sun
    leave_days_per_year  = db.Column(Numeric(5, 2), nullable=False, default=21, server_default='21')
    is_active            = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    notes                = db.Column(db.Text, nullable=True)
    pay_type             = db.Column(db.String(20), nullable=False, default='hourly', server_default="'hourly'")
    # hourly: only pay for logged hours; salaried: guaranteed daily pay, absent days deducted
    work_days_json       = db.Column(db.Text, nullable=False, default='0,1,2,3,4,5', server_default="'0,1,2,3,4,5'")
    rotation_start_day   = db.Column(db.Integer, nullable=True)
    rotation_slot        = db.Column(db.Integer, nullable=True)  # position in global rotation order; null = alphabetical
    created_at           = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by           = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    user        = db.relationship('User', foreign_keys=[user_id])
    deductions  = db.relationship('EmployeeDeduction', backref='employee', lazy='dynamic',
                                  foreign_keys='EmployeeDeduction.employee_id')
    attendance  = db.relationship('EmployeeAttendance', backref='employee', lazy='dynamic',
                                  foreign_keys='EmployeeAttendance.employee_id')
    pay_runs    = db.relationship('PayRun', backref='employee', lazy='dynamic',
                                  foreign_keys='PayRun.employee_id')


class EmployeeDeduction(db.Model):
    __tablename__ = 'employee_deductions'
    id             = db.Column(db.Integer, primary_key=True)
    employee_id    = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    label          = db.Column(db.String(80), nullable=False)
    deduction_type = db.Column(db.String(30), nullable=False, default='fixed', server_default="'fixed'")
    # fixed | percentage_of_gross | auto_uif | auto_paye
    amount         = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    is_active      = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    sort_order     = db.Column(db.Integer, nullable=False, default=0, server_default='0')


class PayRule(db.Model):
    """Configurable multipliers per day type — no hardcoded rates in payroll logic."""
    __tablename__ = 'pay_rules'
    id          = db.Column(db.Integer, primary_key=True)
    day_type    = db.Column(db.String(30), nullable=False, unique=True)
    label       = db.Column(db.String(60), nullable=False)
    multiplier  = db.Column(Numeric(4, 2), nullable=False, default=1, server_default='1')
    is_paid     = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    description = db.Column(db.String(200), nullable=True)
    sort_order  = db.Column(db.Integer, nullable=False, default=0, server_default='0')


class EmployeeAttendance(db.Model):
    """Clock-in/out record — the raw audit trail source of truth."""
    __tablename__ = 'employee_attendance'
    id            = db.Column(db.Integer, primary_key=True)
    employee_id   = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    work_date     = db.Column(db.Date, nullable=False, index=True)
    clock_in      = db.Column(db.Time, nullable=True)
    clock_out     = db.Column(db.Time, nullable=True)
    break_minutes = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    hours_worked  = db.Column(Numeric(5, 2), nullable=True)  # computed or manually overridden
    day_type      = db.Column(db.String(30), nullable=False, default='normal', server_default="'normal'")
    # normal | overtime | sunday | public_holiday | vacation | sick | unpaid_leave
    source        = db.Column(db.String(20), nullable=False, default='manual', server_default="'manual'")
    # manual | admin_entry | mobile
    notes         = db.Column(db.String(300), nullable=True)
    approved_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at   = db.Column(db.DateTime, nullable=True)
    created_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    updated_at    = db.Column(db.DateTime, nullable=True)
    __table_args__ = (db.UniqueConstraint('employee_id', 'work_date',
                                          name='uq_employee_attendance_date'),)


class ShiftSchedule(db.Model):
    """Planned shifts — compared against actual attendance for variance reporting."""
    __tablename__ = 'shift_schedules'
    id             = db.Column(db.Integer, primary_key=True)
    employee_id    = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    scheduled_date = db.Column(db.Date, nullable=False)
    expected_start = db.Column(db.Time, nullable=True)
    expected_end   = db.Column(db.Time, nullable=True)
    expected_hours = db.Column(Numeric(5, 2), nullable=True)
    notes          = db.Column(db.String(200), nullable=True)
    created_by     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at     = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('employee_id', 'scheduled_date',
                                          name='uq_shift_schedule_date'),)


class LeaveRequest(db.Model):
    __tablename__ = 'leave_requests'
    id               = db.Column(db.Integer, primary_key=True)
    employee_id      = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    leave_type       = db.Column(db.String(30), nullable=False)
    # annual | sick | family_responsibility | unpaid
    date_from        = db.Column(db.Date, nullable=False)
    date_to          = db.Column(db.Date, nullable=False)
    days_requested   = db.Column(Numeric(5, 2), nullable=False)
    reason           = db.Column(db.Text, nullable=True)
    status           = db.Column(db.String(20), nullable=False, default='requested', server_default="'requested'")
    # requested | approved | rejected
    approved_by      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at      = db.Column(db.DateTime, nullable=True)
    rejection_reason = db.Column(db.String(300), nullable=True)
    document_filename = db.Column(db.String(200), nullable=True)
    created_at       = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class LeaveBalance(db.Model):
    __tablename__ = 'leave_balances'
    id            = db.Column(db.Integer, primary_key=True)
    employee_id   = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    leave_type    = db.Column(db.String(30), nullable=False)
    year          = db.Column(db.Integer, nullable=False)
    allocated_days = db.Column(Numeric(5, 2), nullable=False, default=0, server_default='0')
    used_days      = db.Column(Numeric(5, 2), nullable=False, default=0, server_default='0')
    __table_args__ = (db.UniqueConstraint('employee_id', 'leave_type', 'year',
                                          name='uq_leave_balance'),)


class EmployeeLeaveAdjustment(db.Model):
    """Admin-awarded bonus days or entitlement overrides per employee per leave type."""
    __tablename__ = 'employee_leave_adjustments'
    id              = db.Column(db.Integer, primary_key=True)
    employee_id     = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    leave_type      = db.Column(db.String(30), nullable=False)
    adjustment_days = db.Column(Numeric(5, 2), nullable=False)  # positive=award, negative=deduct
    year            = db.Column(db.Integer, nullable=True)  # NULL=every year; set=one-off for that year
    reason          = db.Column(db.String(200), nullable=True)
    created_by      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class LeavePolicy(db.Model):
    """Configurable leave rules per leave type."""
    __tablename__ = 'leave_policies'
    id                  = db.Column(db.Integer, primary_key=True)
    leave_type          = db.Column(db.String(30), nullable=False, unique=True)
    label               = db.Column(db.String(80), nullable=False)
    accrual_method      = db.Column(db.String(30), nullable=False, default='daily')
    # 'daily'      — annual: accrues daily from start date
    # 'sick_cycle' — 30d per 36-month cycle, prorated first 6 months
    # 'fixed'      — family responsibility: lump sum each year
    # 'none'       — unpaid: no balance tracked
    days_per_year       = db.Column(Numeric(5, 2), nullable=True)
    carry_over_max      = db.Column(Numeric(5, 2), nullable=False, default=0, server_default='0')
    cycle_days          = db.Column(Numeric(5, 2), nullable=True)
    cycle_months        = db.Column(db.Integer, nullable=True)
    first_period_months = db.Column(db.Integer, nullable=True)
    first_period_per_26 = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    requires_proof_after_days = db.Column(db.Integer, nullable=True)
    is_paid             = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    sort_order          = db.Column(db.Integer, nullable=False, default=0, server_default='0')


class EmployeeAdvance(db.Model):
    """Cash advances — auto-deducted from next pay run."""
    __tablename__ = 'employee_advances'
    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    amount      = db.Column(Numeric(10, 2), nullable=False)
    date_given  = db.Column(db.Date, nullable=False)
    reason      = db.Column(db.String(200), nullable=True)
    status      = db.Column(db.String(20), nullable=False, default='outstanding', server_default="'outstanding'")
    # outstanding | deducted | cancelled
    pay_run_id  = db.Column(db.Integer, db.ForeignKey('pay_runs.id'), nullable=True)
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EmployeeLoan(db.Model):
    """Structured loans with installment deductions per pay run."""
    __tablename__ = 'employee_loans'
    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    principal   = db.Column(Numeric(10, 2), nullable=False)
    balance     = db.Column(Numeric(10, 2), nullable=False)
    installment = db.Column(Numeric(10, 2), nullable=False)
    date_given  = db.Column(db.Date, nullable=False)
    reason      = db.Column(db.String(200), nullable=True)
    status      = db.Column(db.String(20), nullable=False, default='active', server_default="'active'")
    # active | settled | written_off
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EmployeeDocument(db.Model):
    """Digital employee file — contracts, ID copies, warnings, leave docs."""
    __tablename__ = 'employee_documents'
    id            = db.Column(db.Integer, primary_key=True)
    employee_id   = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    document_type = db.Column(db.String(40), nullable=False)
    # contract | id_copy | uif_form | bank_letter | disciplinary | warning | leave_doc | other
    label         = db.Column(db.String(200), nullable=False)
    filename      = db.Column(db.String(200), nullable=False)
    original_name = db.Column(db.String(200), nullable=False)
    uploaded_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    uploaded_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class PayRun(db.Model):
    """Locked payroll calculation for one employee for one period."""
    __tablename__ = 'pay_runs'
    id                         = db.Column(db.Integer, primary_key=True)
    reference                  = db.Column(db.String(20), nullable=True, unique=True)
    employee_id                = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False, index=True)
    period_start               = db.Column(db.Date, nullable=False)
    period_end                 = db.Column(db.Date, nullable=False)
    pay_date                   = db.Column(db.Date, nullable=True)
    hourly_rate_snapshot       = db.Column(Numeric(10, 2), nullable=False)
    # Hour breakdown
    normal_hours               = db.Column(Numeric(6, 2), nullable=False, default=0, server_default='0')
    overtime_hours             = db.Column(Numeric(6, 2), nullable=False, default=0, server_default='0')
    sunday_hours               = db.Column(Numeric(6, 2), nullable=False, default=0, server_default='0')
    holiday_hours              = db.Column(Numeric(6, 2), nullable=False, default=0, server_default='0')
    vacation_hours             = db.Column(Numeric(6, 2), nullable=False, default=0, server_default='0')
    sick_hours                 = db.Column(Numeric(6, 2), nullable=False, default=0, server_default='0')
    # Pay breakdown
    normal_pay                 = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    overtime_pay               = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    sunday_pay                 = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    holiday_pay                = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    vacation_pay               = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    gross_pay                  = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    # Snapshots (locked at approval — never re-derived)
    deductions_json            = db.Column(db.Text, nullable=False, default='[]', server_default="'[]'")
    employer_contributions_json = db.Column(db.Text, nullable=False, default='[]', server_default="'[]'")
    advances_json              = db.Column(db.Text, nullable=False, default='[]', server_default="'[]'")
    attendance_json            = db.Column(db.Text, nullable=False, default='[]', server_default="'[]'")
    total_deductions           = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    net_pay                    = db.Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    # Status: draft → approved → paid  (no edits allowed after approved)
    status                     = db.Column(db.String(20), nullable=False, default='draft', server_default="'draft'")
    approved_by                = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at                = db.Column(db.DateTime, nullable=True)
    paid_at                    = db.Column(db.DateTime, nullable=True)
    notes                      = db.Column(db.Text, nullable=True)
    created_by                 = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at                 = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# ── Cost Corrections ──────────────────────────────────────────────────────────

class CostAdjustment(db.Model):
    """Immutable audit record for a retroactive batch cost correction.
    status: applied | reversed
    scope: remaining (future-only) | entire_batch (historical + future)
    """
    __tablename__ = 'cost_adjustments'
    id                    = db.Column(db.Integer, primary_key=True)
    batch_id              = db.Column(db.Integer, db.ForeignKey('stock_batches.id'), nullable=False, index=True)
    product_id            = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    supplier_id           = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    scope                 = db.Column(db.String(30), nullable=False)
    old_cost_per_unit     = db.Column(Numeric(10, 6), nullable=False)
    new_cost_per_unit     = db.Column(Numeric(10, 6), nullable=False)
    old_base_cost_total   = db.Column(Numeric(18, 4), nullable=True)
    new_base_cost_total   = db.Column(Numeric(18, 4), nullable=True)
    reason                = db.Column(db.Text, nullable=False)
    status                = db.Column(db.String(20), nullable=False, default='applied')
    reversed_by_id        = db.Column(db.Integer, db.ForeignKey('cost_adjustments.id'), nullable=True)
    created_by            = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at            = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    idempotency_key       = db.Column(db.String(64), nullable=True, unique=True)
    sales_affected        = db.Column(db.Integer, nullable=True)
    consumptions_affected = db.Column(db.Integer, nullable=True)
    cogs_delta            = db.Column(Numeric(18, 4), nullable=True)
    liability_delta       = db.Column(Numeric(18, 4), nullable=True)


class CostAdjustmentLine(db.Model):
    """One row per affected entity in a CostAdjustment — enables reversal and audit."""
    __tablename__ = 'cost_adjustment_lines'
    id            = db.Column(db.Integer, primary_key=True)
    adjustment_id = db.Column(db.Integer, db.ForeignKey('cost_adjustments.id'), nullable=False, index=True)
    entity_type   = db.Column(db.String(20), nullable=False)  # batch | consumption | sale | liability
    entity_id     = db.Column(db.Integer, nullable=True)
    sale_id_str   = db.Column(db.String(64), nullable=True)
    old_value     = db.Column(Numeric(18, 6), nullable=True)
    new_value     = db.Column(Numeric(18, 6), nullable=True)
    qty           = db.Column(Numeric(10, 4), nullable=True)
    old_total     = db.Column(Numeric(18, 4), nullable=True)
    new_total     = db.Column(Numeric(18, 4), nullable=True)

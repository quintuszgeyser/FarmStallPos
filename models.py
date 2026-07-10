from decimal import Decimal
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Numeric

db = SQLAlchemy()

SESSION_TIMEOUT_MINUTES = 10
SESSION_LOGOUT_HOURS    = 2


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
    price_per_unit      = db.Column(Numeric(10, 4), nullable=True)
    low_stock_threshold = db.Column(Numeric(10, 4), nullable=True)
    package_size        = db.Column(Numeric(10, 4), nullable=True)
    package_size_unit   = db.Column(db.String(10), nullable=True)
    package_unit        = db.Column(db.String(30), nullable=True)
    parent_stock_item_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    margin_pct           = db.Column(Numeric(5, 2), nullable=True)
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
    scale_prohibit       = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    scale_last_synced_at = db.Column(db.DateTime(timezone=True), nullable=True)
    scale_last_sync_status = db.Column(db.String(20), nullable=True)     # ok / error / pending
    scale_last_sync_error  = db.Column(db.Text, nullable=True)
    scale_hash           = db.Column(db.String(64), nullable=True)       # SHA-256 of last sent payload
    # Stats normalisation — grams/ml products set this to a "typical portion" so rankings
    # compare fairly against unit products (e.g. 250g = 1 portion of honey)
    stat_unit_size       = db.Column(Numeric(10, 4), nullable=True)


class Category(db.Model):
    """Central product category. One category per product (Product.category_id).
    name      = display form, as entered (trimmed).
    name_norm = lower(trim(name)), UNIQUE - enforces case/whitespace de-duplication.
    """
    __tablename__ = 'categories'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(80), nullable=False)
    name_norm  = db.Column(db.String(80), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    products   = db.relationship('Product', backref='category', lazy='dynamic',
                                 foreign_keys='Product.category_id')


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


class SupplierDocument(db.Model):
    __tablename__ = 'supplier_documents'
    id           = db.Column(db.Integer, primary_key=True)
    supplier_id  = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
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
    product_id   = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
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
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    special_price = db.Column(Numeric(10, 2), nullable=False)
    active        = db.Column(db.Boolean, nullable=False, default=True, server_default='true')
    schedule      = db.Column(db.Text, nullable=True)


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
    camera_source = db.Column(db.String(20),  nullable=True)

from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class WebCustomer(db.Model):
    __tablename__ = "web_customers"

    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(200), nullable=False)
    email          = db.Column(db.String(200), unique=True, nullable=False)
    phone          = db.Column(db.String(50))
    password_hash  = db.Column(db.Text, nullable=False)
    created_at     = db.Column(db.DateTime(timezone=True), default=utcnow)
    deleted_at     = db.Column(db.DateTime(timezone=True))
    pos_customer_id = db.Column(db.Integer)  # FK to POS customers.id - optional link

    cake_orders  = db.relationship("CakeOrder", back_populates="customer", lazy="dynamic")


class CakeOrder(db.Model):
    __tablename__ = "cake_orders"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending','quoted','confirmed','in_production','completed','cancelled')",
            name="chk_cake_status"
        ),
    )

    id                = db.Column(db.Integer, primary_key=True)
    reference         = db.Column(db.String(20), unique=True, nullable=False)

    # Customer - either account or guest
    web_customer_id   = db.Column(db.Integer, db.ForeignKey("web_customers.id"))
    guest_name        = db.Column(db.String(200))
    guest_email       = db.Column(db.String(200))
    guest_phone       = db.Column(db.String(50))

    status            = db.Column(db.String(30), default="pending", nullable=False)
    date_required     = db.Column(db.Date, nullable=False)
    size              = db.Column(db.String(100), nullable=False)
    flavor            = db.Column(db.String(200), nullable=False)
    serves            = db.Column(db.Integer)
    design_description = db.Column(db.Text)
    image_path        = db.Column(db.Text)

    admin_notes       = db.Column(db.Text)
    quoted_price      = db.Column(db.Numeric(10, 2))
    invoice_id        = db.Column(db.Integer)  # FK to POS invoices.id

    created_at        = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at        = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    customer = db.relationship("WebCustomer", back_populates="cake_orders")
    payment  = db.relationship("Payment", back_populates="cake_order",
                               primaryjoin="and_(Payment.order_type=='cake', foreign(Payment.order_id)==CakeOrder.id)",
                               uselist=False)

    @property
    def customer_name(self):
        return self.customer.name if self.customer else self.guest_name

    @property
    def customer_email(self):
        return self.customer.email if self.customer else self.guest_email

    @property
    def customer_phone(self):
        return self.customer.phone if self.customer else self.guest_phone


class Payment(db.Model):
    __tablename__ = "payments"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending','paid','failed')",
            name="chk_payment_status"
        ),
        db.CheckConstraint(
            "method IN ('eft','payfast','paygate','card')",
            name="chk_payment_method"
        ),
        db.CheckConstraint(
            "order_type IN ('cake','farmshop')",
            name="chk_payment_order_type"
        ),
    )

    id               = db.Column(db.Integer, primary_key=True)
    reference        = db.Column(db.String(100), unique=True)
    order_type       = db.Column(db.String(20), nullable=False)
    order_id         = db.Column(db.Integer, nullable=False)
    amount           = db.Column(db.Numeric(10, 2), nullable=False)
    method           = db.Column(db.String(20), default="eft", nullable=False)
    status           = db.Column(db.String(20), default="pending", nullable=False)
    proof_path       = db.Column(db.Text)
    external_payload = db.Column(db.JSON)  # raw gateway response for PayFast (future)
    paid_at          = db.Column(db.DateTime(timezone=True))
    notes            = db.Column(db.Text)
    created_at       = db.Column(db.DateTime(timezone=True), default=utcnow)

    cake_order = db.relationship(
        "CakeOrder",
        primaryjoin="and_(Payment.order_type=='cake', foreign(Payment.order_id)==CakeOrder.id)",
        uselist=False,
        overlaps="payment"
    )

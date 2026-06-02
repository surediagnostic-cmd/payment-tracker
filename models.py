from datetime import datetime, timezone
from flask_login import UserMixin
from app import db

# Many-to-many: users ↔ branches
user_branches = db.Table('user_branches',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    db.Column('branch_id', db.Integer, db.ForeignKey('branches.id', ondelete='CASCADE'), primary_key=True)
)


class Branch(db.Model):
    __tablename__ = "branches"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    source_account = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    payment_requests = db.relationship("PaymentRequest", backref="branch", lazy=True)


class Category(db.Model):
    __tablename__ = "categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    cost_type = db.Column(db.String(20), nullable=False, default='overhead')  # 'direct_cost' | 'overhead'
    is_active = db.Column(db.Boolean, default=True)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="accountant")
    branch_id = db.Column(db.Integer, nullable=True)  # kept for compatibility
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    branches = db.relationship('Branch', secondary=user_branches, backref='accountants')
    submissions = db.relationship("PaymentRequest", backref="submitter", lazy=True)

    @property
    def is_mds(self):
        return self.role == "mds"

    @property
    def branch(self):
        """Backward-compat helper — returns the first assigned branch or None."""
        return self.branches[0] if self.branches else None


class PaymentRequest(db.Model):
    __tablename__ = "payment_requests"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True, nullable=False)
    date = db.Column(db.Date, nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey("branches.id"), nullable=False)
    beneficiary_name = db.Column(db.String(150), nullable=False)
    beneficiary_account = db.Column(db.String(30), nullable=False)
    beneficiary_bank = db.Column(db.String(100), nullable=False)
    bank_code = db.Column(db.String(10), nullable=True)
    requested_amount = db.Column(db.Numeric(14, 2), nullable=False)
    approved_amount = db.Column(db.Numeric(14, 2), nullable=True)
    mds_comment = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default="pending")
    upload_status = db.Column(db.String(20), default="not_uploaded")
    receipt_filename = db.Column(db.String(255), nullable=True)
    submitted_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_at = db.Column(db.DateTime, nullable=True)
    items = db.relationship('PaymentRequestItem', backref='request', cascade='all, delete-orphan', lazy=True)

    @staticmethod
    def generate_reference(branch_id):
        now = datetime.now(timezone.utc)
        prefix = f"PAY-{now.strftime('%Y%m')}"
        # Use MAX over the numeric suffix so gaps/ghost rows don't collide
        existing = db.session.query(PaymentRequest.reference)\
            .filter(PaymentRequest.reference.like(f"{prefix}-%")).all()
        max_num = 0
        for (ref,) in existing:
            try:
                max_num = max(max_num, int(ref.rsplit("-", 1)[-1]))
            except (ValueError, IndexError):
                pass
        return f"{prefix}-{str(max_num + 1).zfill(3)}"

    @property
    def status_color(self):
        return {"pending": "yellow", "approved": "green", "rejected": "red"}.get(self.status, "gray")

    @property
    def upload_color(self):
        return "green" if self.upload_status == "uploaded" else "gray"


class PaymentRequestItem(db.Model):
    __tablename__ = "payment_request_items"
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('payment_requests.id'), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    rate = db.Column(db.Numeric(14, 2), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    category = db.relationship('Category')


class Budget(db.Model):
    """Planned spending per branch + category + period.

    period_type | year | month | week
    'monthly'   |  Y   |   Y   | null  → one calendar month
    'yearly'    |  Y   | null  | null  → full calendar year
    'weekly'    |  Y   |   Y   |  Y   → week 1-4 of a month
                                         week 1 = days 1-7
                                         week 2 = days 8-14
                                         week 3 = days 15-21
                                         week 4 = days 22-end
    """
    __tablename__ = "budgets"
    id = db.Column(db.Integer, primary_key=True)
    branch_id   = db.Column(db.Integer, db.ForeignKey("branches.id"),  nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False)
    period_type = db.Column(db.String(10), nullable=False)   # monthly | yearly | weekly
    year        = db.Column(db.Integer, nullable=False)
    month       = db.Column(db.Integer, nullable=True)       # 1-12
    week        = db.Column(db.Integer, nullable=True)       # 1-4
    amount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    notes       = db.Column(db.Text, nullable=True)
    created_by  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                             onupdate=lambda: datetime.now(timezone.utc))

    branch   = db.relationship("Branch")
    category = db.relationship("Category")
    creator  = db.relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        db.UniqueConstraint(
            'branch_id', 'category_id', 'period_type', 'year', 'month', 'week',
            name='uq_budget_period'
        ),
    )

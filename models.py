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
    cost_type = db.Column(db.String(20), nullable=False, default='overhead')
    is_active = db.Column(db.Boolean, default=True)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="accountant")
    branch_id = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    branches = db.relationship('Branch', secondary=user_branches, backref='accountants')
    submissions = db.relationship("PaymentRequest", backref="submitter", lazy=True)

    @property
    def is_mds(self):
        return self.role == "mds"

    @property
    def is_lab_staff(self):
        return self.role == "lab_staff"

    @property
    def can_view_inventory(self):
        return self.role in ("mds", "accountant", "lab_staff")

    @property
    def branch(self):
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
    parent_id = db.Column(
        db.Integer,
        db.ForeignKey('payment_request_items.id', ondelete='SET NULL'),
        nullable=True,
    )
    description = db.Column(db.String(255), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    rate = db.Column(db.Numeric(14, 2), nullable=False)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='SET NULL'), nullable=True)
    qty_ordered       = db.Column(db.Numeric(14, 4), nullable=True)
    inventory_item    = db.relationship('InventoryItem', foreign_keys=[inventory_item_id])
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    notes = db.Column(db.String(500), nullable=True)
    category = db.relationship('Category')
    children = db.relationship(
        'PaymentRequestItem',
        primaryjoin='PaymentRequestItem.parent_id == PaymentRequestItem.id',
        foreign_keys='[PaymentRequestItem.parent_id]',
        lazy=True,
    )

    @property
    def display_amount(self):
        try:
            if self.children:
                return sum(float(c.amount) for c in self.children)
        except Exception:
            pass
        return float(self.amount)


class Budget(db.Model):
    __tablename__ = "budgets"
    id = db.Column(db.Integer, primary_key=True)
    branch_id   = db.Column(db.Integer, db.ForeignKey("branches.id"),  nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False)
    period_type = db.Column(db.String(10), nullable=False)
    year        = db.Column(db.Integer, nullable=False)
    month       = db.Column(db.Integer, nullable=True)
    week        = db.Column(db.Integer, nullable=True)
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


# ─── Inventory Management ────────────────────────────────────────────────────

ITEM_CATEGORY_LABELS = {
    'lab_reagent':   'Lab Reagent',
    'lab_consumable':'Lab Consumable',
    'usg':           'USG Consumable',
    'xray':          'X-Ray / Imaging',
    'ecg':           'ECG',
    'general':       'General',
}

CASE_TYPE_LABELS = {
    'lab':  'Lab',
    'usg':  'USG',
    'xray': 'X-Ray',
    'ecg':  'ECG',
}


class InventoryItem(db.Model):
    __tablename__ = "inventory_items"
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(200), nullable=False)
    item_code   = db.Column(db.String(50),  nullable=True)
    category    = db.Column(db.String(50),  nullable=False, default='lab_reagent')
    unit        = db.Column(db.String(50),  nullable=False, default='unit')
    pack_size      = db.Column(db.Integer,     nullable=True)
    purchase_unit  = db.Column(db.String(40),  nullable=True)
    unit_price  = db.Column(db.Numeric(14, 2), nullable=True)
    reorder_level = db.Column(db.Numeric(14, 4), nullable=True)
    is_active   = db.Column(db.Boolean, default=True)
    notes       = db.Column(db.Text, nullable=True)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    stock_levels  = db.relationship('StockLevel',      back_populates='item', cascade='all, delete-orphan', lazy=True)
    transactions  = db.relationship('StockTransaction', back_populates='item', lazy=True)
    test_mappings = db.relationship('TestReagentMap',   back_populates='item', cascade='all, delete-orphan', lazy=True)

    @property
    def category_label(self):
        return ITEM_CATEGORY_LABELS.get(self.category, self.category)

    @property
    def total_stock(self):
        return sum(float(sl.qty_on_hand) for sl in self.stock_levels)

    @property
    def is_low_stock(self):
        if self.reorder_level is None:
            return False
        return self.total_stock < float(self.reorder_level)


class TestBranchPrice(db.Model):
    """Per-branch price override for a test. Falls back to TestCatalogue.price when absent."""
    __tablename__ = "test_branch_prices"
    id        = db.Column(db.Integer, primary_key=True)
    test_id   = db.Column(db.Integer, db.ForeignKey('test_catalogue.id', ondelete='CASCADE'), nullable=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id', ondelete='CASCADE'), nullable=False)
    price     = db.Column(db.Numeric(12, 2), nullable=False)
    updated_at= db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_by= db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)

    test   = db.relationship('TestCatalogue', back_populates='branch_prices')
    branch = db.relationship('Branch')
    editor = db.relationship('User', foreign_keys=[updated_by])

    __table_args__ = (
        db.UniqueConstraint('test_id', 'branch_id', name='uq_test_branch_price'),
    )


class TestCatalogue(db.Model):
    __tablename__ = "test_catalogue"
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(300), nullable=False)
    labsmart_name = db.Column(db.String(300), nullable=False)
    case_type     = db.Column(db.String(20),  nullable=False, default='lab')
    price         = db.Column(db.Numeric(12, 2), nullable=True)
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    reagent_mappings = db.relationship('TestReagentMap',  back_populates='test', cascade='all, delete-orphan', lazy=True)
    package_links    = db.relationship('PackageTest',      back_populates='test', cascade='all, delete-orphan', lazy=True)
    branch_prices    = db.relationship('TestBranchPrice',  back_populates='test', cascade='all, delete-orphan', lazy=True)

    @property
    def case_type_label(self):
        return CASE_TYPE_LABELS.get(self.case_type, self.case_type)

    @property
    def reagent_cost(self):
        total = 0.0
        for m in self.reagent_mappings:
            if m.item and m.item.unit_price:
                total += float(m.qty_per_test) * float(m.item.unit_price)
        return total

    def price_for_branch(self, branch_id):
        """Effective price: branch override if set, else default price."""
        for bp in self.branch_prices:
            if bp.branch_id == branch_id:
                return float(bp.price)
        return float(self.price) if self.price else None

    def margin_for_branch(self, branch_id):
        price = self.price_for_branch(branch_id)
        if price is None:
            return None
        return round(price - self.reagent_cost, 2)

    def margin_pct_for_branch(self, branch_id):
        price = self.price_for_branch(branch_id)
        if not price or price == 0:
            return None
        m = self.margin_for_branch(branch_id)
        if m is None:
            return None
        return round((m / price) * 100, 1)

    @property
    def margin(self):
        if self.price is None:
            return None
        return round(float(self.price) - self.reagent_cost, 2)

    @property
    def margin_pct(self):
        if not self.price or float(self.price) == 0:
            return None
        m = self.margin
        if m is None:
            return None
        return round((m / float(self.price)) * 100, 1)

    @property
    def branch_price_count(self):
        return len(self.branch_prices)


class TestReagentMap(db.Model):
    __tablename__ = "test_reagent_maps"
    id           = db.Column(db.Integer, primary_key=True)
    test_id      = db.Column(db.Integer, db.ForeignKey('test_catalogue.id', ondelete='CASCADE'), nullable=False)
    item_id      = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='CASCADE'), nullable=False)
    qty_per_test = db.Column(db.Numeric(10, 4), nullable=False, default=1.0)

    test = db.relationship('TestCatalogue', back_populates='reagent_mappings')
    item = db.relationship('InventoryItem',  back_populates='test_mappings')

    __table_args__ = (
        db.UniqueConstraint('test_id', 'item_id', name='uq_test_reagent'),
    )


class PackageCatalogue(db.Model):
    __tablename__ = "package_catalogue"
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(300), nullable=False)
    labsmart_name = db.Column(db.String(300), nullable=False)
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    tests = db.relationship('PackageTest', back_populates='package', cascade='all, delete-orphan', lazy=True)


class PackageTest(db.Model):
    __tablename__ = "package_tests"
    id         = db.Column(db.Integer, primary_key=True)
    package_id = db.Column(db.Integer, db.ForeignKey('package_catalogue.id', ondelete='CASCADE'), nullable=False)
    test_id    = db.Column(db.Integer, db.ForeignKey('test_catalogue.id', ondelete='CASCADE'), nullable=False)

    package = db.relationship('PackageCatalogue', back_populates='tests')
    test    = db.relationship('TestCatalogue',    back_populates='package_links')

    __table_args__ = (
        db.UniqueConstraint('package_id', 'test_id', name='uq_package_test'),
    )


class StockLevel(db.Model):
    __tablename__ = "stock_levels"
    id           = db.Column(db.Integer, primary_key=True)
    item_id      = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='CASCADE'), nullable=False)
    branch_id    = db.Column(db.Integer, db.ForeignKey('branches.id', ondelete='SET NULL'), nullable=True)
    qty_on_hand  = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    last_updated = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    item   = db.relationship('InventoryItem', back_populates='stock_levels')
    branch = db.relationship('Branch')


class StockTransaction(db.Model):
    __tablename__ = "stock_transactions"
    id           = db.Column(db.Integer, primary_key=True)
    item_id      = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='CASCADE'), nullable=False)
    branch_id    = db.Column(db.Integer, db.ForeignKey('branches.id', ondelete='SET NULL'), nullable=True)
    txn_type     = db.Column(db.String(20), nullable=False)
    qty          = db.Column(db.Numeric(14, 4), nullable=False)
    unit_cost    = db.Column(db.Numeric(14, 2), nullable=True)
    batch_number = db.Column(db.String(100), nullable=True)
    expiry_date  = db.Column(db.Date, nullable=True)
    reference    = db.Column(db.String(100), nullable=True)
    notes        = db.Column(db.Text, nullable=True)
    created_by   = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    item    = db.relationship('InventoryItem', back_populates='transactions')
    branch  = db.relationship('Branch')
    creator = db.relationship('User', foreign_keys=[created_by])


class LisUpload(db.Model):
    __tablename__ = "lis_uploads"
    id             = db.Column(db.Integer, primary_key=True)
    filename       = db.Column(db.String(255), nullable=False)
    uploaded_by    = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    record_count   = db.Column(db.Integer, default=0)
    matched_count  = db.Column(db.Integer, default=0)
    unmatched_count= db.Column(db.Integer, default=0)
    status         = db.Column(db.String(20), default='pending_review')
    start_date     = db.Column(db.Date, nullable=True)
    end_date       = db.Column(db.Date, nullable=True)
    created_at     = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    applied_at     = db.Column(db.DateTime, nullable=True)

    uploader  = db.relationship('User', foreign_keys=[uploaded_by])
    unmatched = db.relationship('UnmatchedInvestigation', back_populates='upload', cascade='all, delete-orphan', lazy=True)
    rows      = db.relationship('LisUploadRow', back_populates='upload', cascade='all, delete-orphan', lazy=True)


class LisUploadRow(db.Model):
    __tablename__ = "lis_upload_rows"
    id                 = db.Column(db.Integer, primary_key=True)
    upload_id          = db.Column(db.Integer, db.ForeignKey('lis_uploads.id', ondelete='CASCADE'), nullable=False)
    case_id            = db.Column(db.String(50),  nullable=True)
    case_type          = db.Column(db.String(20),  nullable=True)
    case_date          = db.Column(db.Date,        nullable=True)
    investigations_raw = db.Column(db.Text,        nullable=True)
    branch_raw         = db.Column(db.String(200), nullable=True)
    branch_id          = db.Column(db.Integer, db.ForeignKey('branches.id', ondelete='SET NULL'), nullable=True)
    is_cancelled       = db.Column(db.Boolean, default=False)

    upload = db.relationship('LisUpload', back_populates='rows')
    branch = db.relationship('Branch')


class UnmatchedInvestigation(db.Model):
    __tablename__ = "unmatched_investigations"
    id               = db.Column(db.Integer, primary_key=True)
    upload_id        = db.Column(db.Integer, db.ForeignKey('lis_uploads.id', ondelete='CASCADE'), nullable=False)
    raw_name         = db.Column(db.String(500), nullable=False)
    occurrence_count = db.Column(db.Integer, default=1)
    suggested_test_id= db.Column(db.Integer, db.ForeignKey('test_catalogue.id', ondelete='SET NULL'), nullable=True)
    suggested_score  = db.Column(db.Numeric(5, 4), nullable=True)
    resolved_test_id = db.Column(db.Integer, db.ForeignKey('test_catalogue.id', ondelete='SET NULL'), nullable=True)
    resolved_by      = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    resolved_at      = db.Column(db.DateTime, nullable=True)
    action           = db.Column(db.String(20), default='pending')

    upload         = db.relationship('LisUpload',    back_populates='unmatched')
    suggested_test = db.relationship('TestCatalogue', foreign_keys=[suggested_test_id])
    resolved_test  = db.relationship('TestCatalogue', foreign_keys=[resolved_test_id])
    resolver       = db.relationship('User',          foreign_keys=[resolved_by])


class ProjectedIncome(db.Model):
    __tablename__ = "projected_income"
    id         = db.Column(db.Integer, primary_key=True)
    branch_id  = db.Column(db.Integer, db.ForeignKey("branches.id"), nullable=False)
    year       = db.Column(db.Integer, nullable=False)
    month      = db.Column(db.Integer, nullable=True)
    amount     = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    notes      = db.Column(db.String(500), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    branch     = db.relationship("Branch")


# ── Feature #2: In-Transit Stock ──────────────────────────────────────────────

class InTransitStock(db.Model):
    __tablename__ = "in_transit_stock"
    id                      = db.Column(db.Integer, primary_key=True)
    payment_request_id      = db.Column(db.Integer, db.ForeignKey('payment_requests.id', ondelete='CASCADE'), nullable=False)
    payment_request_item_id = db.Column(db.Integer, db.ForeignKey('payment_request_items.id', ondelete='CASCADE'), nullable=False)
    inventory_item_id       = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='CASCADE'), nullable=False)
    branch_id               = db.Column(db.Integer, db.ForeignKey('branches.id', ondelete='SET NULL'), nullable=True)
    qty                     = db.Column(db.Numeric(14, 4), nullable=False)
    status                  = db.Column(db.String(20), default='in_transit')
    confirmed_by            = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    confirmed_at            = db.Column(db.DateTime, nullable=True)
    created_at              = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    payment_request  = db.relationship('PaymentRequest')
    inventory_item   = db.relationship('InventoryItem')
    branch           = db.relationship('Branch')
    confirmer        = db.relationship('User', foreign_keys=[confirmed_by])


class BudgetLineItem(db.Model):
    __tablename__ = "budget_line_items"
    id          = db.Column(db.Integer, primary_key=True)
    branch_id   = db.Column(db.Integer, db.ForeignKey('branches.id', ondelete='CASCADE'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id', ondelete='CASCADE'), nullable=False)
    year        = db.Column(db.Integer, nullable=False)
    month       = db.Column(db.Integer, nullable=False)
    name        = db.Column(db.String(200), nullable=False)
    amount      = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    sort_order  = db.Column(db.Integer, default=0)
    created_by  = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    updated_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    branch   = db.relationship('Branch')
    category = db.relationship('Category')


# ── Feature #3: Revenue Share ─────────────────────────────────────────────────

class RevenueShareRecipient(db.Model):
    __tablename__ = "revenue_share_recipients"
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(200), nullable=False)
    account_name    = db.Column(db.String(200), nullable=True)
    account_number  = db.Column(db.String(30), nullable=True)
    bank_name       = db.Column(db.String(100), nullable=True)
    description     = db.Column(db.String(500), nullable=True)
    is_active       = db.Column(db.Boolean, default=True)
    created_at      = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class RevenueSharePeriod(db.Model):
    __tablename__ = "revenue_share_periods"
    id            = db.Column(db.Integer, primary_key=True)
    label         = db.Column(db.String(100), nullable=False)
    branch_id     = db.Column(db.Integer, db.ForeignKey('branches.id', ondelete='SET NULL'), nullable=True)
    gross_revenue = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    period_start  = db.Column(db.Date, nullable=True)
    period_end    = db.Column(db.Date, nullable=True)
    status        = db.Column(db.String(20), default='draft')
    notes         = db.Column(db.Text, nullable=True)
    created_by    = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    branch        = db.relationship('Branch')
    allocations   = db.relationship('RevenueShareAllocation', back_populates='period', cascade='all, delete-orphan')

    @property
    def total_allocated_pct(self):
        return sum(float(a.percentage or 0) for a in self.allocations)

    @property
    def total_allocated_amount(self):
        return sum(float(a.amount_calculated or 0) for a in self.allocations)


class RevenueShareAllocation(db.Model):
    __tablename__ = "revenue_share_allocations"
    id                 = db.Column(db.Integer, primary_key=True)
    period_id          = db.Column(db.Integer, db.ForeignKey('revenue_share_periods.id', ondelete='CASCADE'), nullable=False)
    recipient_id       = db.Column(db.Integer, db.ForeignKey('revenue_share_recipients.id', ondelete='CASCADE'), nullable=False)
    percentage         = db.Column(db.Numeric(6, 4), nullable=False, default=0)
    amount_calculated  = db.Column(db.Numeric(14, 2), nullable=True)
    payment_request_id = db.Column(db.Integer, db.ForeignKey('payment_requests.id', ondelete='SET NULL'), nullable=True)
    is_paid            = db.Column(db.Boolean, default=False)
    paid_at            = db.Column(db.DateTime, nullable=True)
    notes              = db.Column(db.String(500), nullable=True)

    period             = db.relationship('RevenueSharePeriod', back_populates='allocations')
    recipient          = db.relationship('RevenueShareRecipient')
    payment_request    = db.relationship('PaymentRequest')


class BranchAllocationTemplate(db.Model):
    """Management-approved default % split per branch, used to pre-populate weekly runs."""
    __tablename__ = "branch_allocation_templates"
    id           = db.Column(db.Integer, primary_key=True)
    branch_id    = db.Column(db.Integer, db.ForeignKey('branches.id', ondelete='CASCADE'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('revenue_share_recipients.id', ondelete='CASCADE'), nullable=False)
    percentage   = db.Column(db.Numeric(6, 3), nullable=False, default=0)
    updated_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_by   = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    branch       = db.relationship('Branch')
    recipient    = db.relationship('RevenueShareRecipient')
    __table_args__ = (db.UniqueConstraint('branch_id', 'recipient_id', name='uq_bat_branch_recipient'),)

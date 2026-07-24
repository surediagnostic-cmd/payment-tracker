import os
from flask import Flask, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from sqlalchemy.pool import NullPool

db = SQLAlchemy()
login_manager = LoginManager()
mail = Mail()


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:///payments.db"
    )
    # Render/Supabase sometimes returns postgres:// — SQLAlchemy needs postgresql://
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
        app.config["SQLALCHEMY_DATABASE_URI"] = app.config[
            "SQLALCHEMY_DATABASE_URI"
        ].replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # NullPool + PgBouncer-safe options for Supabase Session Pooler on Railway.
    # Railway is IPv4-only; direct Supabase port 5432 resolves to IPv6 and fails.
    # Session Pooler (pooler.supabase.com) uses IPv4 and works fine on Railway.
    # prepared_statement_cache_size=0 disables named prepared statements which
    # PgBouncer does not support in session-pooling mode.
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": NullPool,
        "connect_args": {
            "sslmode": "require",
            "options": "-c statement_timeout=30000",
        },
        "execution_options": {"prepared_statement_cache_size": 0},
    }

    app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 587))
    app.config["MAIL_USE_TLS"] = True
    app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
    app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
    app.config["MAIL_DEFAULT_SENDER"] = os.environ.get(
        "MAIL_DEFAULT_SENDER", "noreply@surediagnostics.com"
    )
    app.config["MDS_EMAIL"] = os.environ.get("MDS_EMAIL", "")

    db.init_app(app)
    login_manager.init_app(app)
    mail.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"

    from routes.auth import auth_bp
    from routes.requests import requests_bp
    from routes.approvals import approvals_bp
    from routes.reports import reports_bp
    from routes.admin import admin_bp
    from routes.budget import budget_bp
    from routes.inventory import inventory_bp
    from routes.revenue_share import revenue_share_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(requests_bp)
    app.register_blueprint(approvals_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(budget_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(revenue_share_bp)

    @app.route("/")
    def root():
        from flask_login import current_user
        if current_user.is_authenticated:
            return redirect(url_for("requests.dashboard"))
        return redirect(url_for("auth.login"))

    @app.route("/health")
    def health():
        return "ok", 200

    @app.errorhandler(500)
    def internal_error(error):
        import traceback
        tb = traceback.format_exc()
        print(f"[500 ERROR]\n{tb}", flush=True)
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            from flask import render_template as rt
            return rt("500.html", error=str(error), traceback=tb), 500
        except Exception:
            return (
                f"<pre style='padding:20px;background:#0b1e3d;color:#ff9d45;"
                f"font-family:monospace;font-size:13px;min-height:100vh;margin:0;'>"
                f"500 Internal Server Error\n\n{tb}</pre>"
            ), 500

    with app.app_context():
        # Step 1 — migrations (schema changes to existing tables)
        try:
            _run_migrations()
        except Exception as e:
            print(f"[startup] migration warning: {e}", flush=True)

        # Step 2 — always create missing tables, even if migrations failed
        try:
            db.create_all()
        except Exception as e:
            print(f"[startup] db.create_all warning: {e}", flush=True)

        # Step 3 — seed default data if tables are empty
        try:
            _seed_defaults()
        except Exception as e:
            print(f"[startup] seed warning: {e}", flush=True)

    @app.route("/debug-db")
    def debug_db():
        from flask_login import current_user
        # Only allow MDS or unauthenticated access during setup
        try:
            from sqlalchemy import text, inspect as sa_inspect
            insp = sa_inspect(db.engine)
            tables = sorted(insp.get_table_names())
            rows = {}
            for t in tables:
                try:
                    count = db.session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                    cols  = [c['name'] for c in insp.get_columns(t)]
                    rows[t] = {"count": count, "columns": cols}
                except Exception as e:
                    rows[t] = {"error": str(e)}
            import json
            return f"<pre style='font-family:monospace;font-size:13px;padding:20px;'>{json.dumps({'tables': rows}, indent=2)}</pre>", 200
        except Exception as e:
            return f"<pre>debug error: {e}</pre>", 500

    return app


def _run_migrations():
    """Apply incremental schema changes to existing deployments."""
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())

    # 1. Add cost_type to categories
    if 'categories' in tables:
        cols = {c['name'] for c in insp.get_columns('categories')}
        if 'cost_type' not in cols:
            try:
                db.session.execute(text(
                    "ALTER TABLE categories ADD COLUMN cost_type VARCHAR(20) NOT NULL DEFAULT 'overhead'"
                ))
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"[migration] cost_type: {e}")

    # 2. Create user_branches and migrate existing branch_id data
    if 'user_branches' not in tables and 'users' in tables and 'branches' in tables:
        try:
            db.session.execute(text("""
                CREATE TABLE user_branches (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    PRIMARY KEY (user_id, branch_id)
                )
            """))
            db.session.execute(text("""
                INSERT INTO user_branches (user_id, branch_id)
                SELECT id, branch_id FROM users
                WHERE branch_id IS NOT NULL AND role = 'accountant'
            """))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[migration] user_branches: {e}")
    elif 'user_branches' in tables and 'users' in tables:
        # Table exists but may be empty — re-run the data migration safely
        try:
            db.session.execute(text("""
                INSERT INTO user_branches (user_id, branch_id)
                SELECT u.id, u.branch_id FROM users u
                WHERE u.branch_id IS NOT NULL AND u.role = 'accountant'
                  AND NOT EXISTS (
                      SELECT 1 FROM user_branches ub
                      WHERE ub.user_id = u.id AND ub.branch_id = u.branch_id
                  )
            """))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[migration] user_branches backfill: {e}")

    # 3. If payment_requests still has the old single-item columns, rebuild both tables.
    #    The old schema had description/category_id/quantity/rate as NOT NULL columns.
    #    New schema moves those fields to payment_request_items.
    if 'payment_requests' in tables:
        pr_cols = {c['name'] for c in insp.get_columns('payment_requests')}
        if 'description' in pr_cols or 'category_id' in pr_cols:
            try:
                db.session.execute(text("DROP TABLE IF EXISTS payment_request_items"))
                db.session.execute(text("DROP TABLE payment_requests CASCADE"))
                db.session.commit()
                print("[migration] rebuilt payment_requests to multi-item schema")
            except Exception as e:
                db.session.rollback()
                # SQLite fallback: drop columns individually
                try:
                    for col in ['description', 'category_id', 'quantity', 'rate']:
                        if col in pr_cols:
                            db.session.execute(text(
                                f"ALTER TABLE payment_requests DROP COLUMN {col}"
                            ))
                    db.session.commit()
                    print(f"[migration] dropped old columns (SQLite fallback): {e}")
                except Exception as e2:
                    db.session.rollback()
                    print(f"[migration] payment_requests schema update failed: {e2}")

    # 4. Add receipt_filename column if missing
    if 'payment_requests' in tables:
        pr_cols = {c['name'] for c in insp.get_columns('payment_requests')}
        if 'receipt_filename' not in pr_cols:
            try:
                db.session.execute(text(
                    "ALTER TABLE payment_requests ADD COLUMN receipt_filename VARCHAR(255)"
                ))
                db.session.commit()
                print("[migration] added receipt_filename column")
            except Exception as e:
                db.session.rollback()
                print(f"[migration] receipt_filename: {e}")

    # 5. Add notes column to payment_request_items if missing
    if 'payment_request_items' in tables:
        item_cols = {c['name'] for c in insp.get_columns('payment_request_items')}
        if 'notes' not in item_cols:
            try:
                db.session.execute(text(
                    "ALTER TABLE payment_request_items ADD COLUMN notes VARCHAR(500)"
                ))
                db.session.commit()
                print("[migration] added notes column to payment_request_items")
            except Exception as e:
                db.session.rollback()
                print(f"[migration] item notes: {e}")

    # 6b. Add parent_id column to payment_request_items if missing
    if 'payment_request_items' in tables:
        item_cols = {c['name'] for c in insp.get_columns('payment_request_items')}
        if 'parent_id' not in item_cols:
            try:
                db.session.execute(text(
                    "ALTER TABLE payment_request_items "
                    "ADD COLUMN parent_id INTEGER REFERENCES payment_request_items(id) ON DELETE SET NULL"
                ))
                db.session.commit()
                print("[migration] added parent_id column to payment_request_items")
            except Exception as e:
                db.session.rollback()
                print(f"[migration] parent_id: {e}")

    # 6c. Add inventory_item_id and qty_ordered to payment_request_items (Feature #2)
    if 'payment_request_items' in tables:
        item_cols = {c['name'] for c in insp.get_columns('payment_request_items')}
        if 'inventory_item_id' not in item_cols:
            try:
                db.session.execute(text(
                    "ALTER TABLE payment_request_items "
                    "ADD COLUMN inventory_item_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL"
                ))
                db.session.commit()
                print("[migration] added inventory_item_id to payment_request_items")
            except Exception as e:
                db.session.rollback()
                print(f"[migration] inventory_item_id: {e}")
        if 'qty_ordered' not in item_cols:
            try:
                db.session.execute(text(
                    "ALTER TABLE payment_request_items ADD COLUMN qty_ordered NUMERIC(14,4)"
                ))
                db.session.commit()
                print("[migration] added qty_ordered to payment_request_items")
            except Exception as e:
                db.session.rollback()
                print(f"[migration] qty_ordered: {e}")

    # 7. Create budgets table if missing (db.create_all handles new deployments;
    #    this covers existing deployments that already have the other tables)
    if 'budgets' not in tables:
        try:
            db.session.execute(text("""
                CREATE TABLE budgets (
                    id SERIAL PRIMARY KEY,
                    branch_id INTEGER NOT NULL REFERENCES branches(id),
                    category_id INTEGER NOT NULL REFERENCES categories(id),
                    period_type VARCHAR(10) NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER,
                    week INTEGER,
                    amount NUMERIC(14,2) NOT NULL DEFAULT 0,
                    notes TEXT,
                    created_by INTEGER NOT NULL REFERENCES users(id),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    CONSTRAINT uq_budget_period UNIQUE (branch_id, category_id, period_type, year, month, week)
                )
            """))
            db.session.commit()
            print("[migration] created budgets table")
        except Exception as e:
            db.session.rollback()
            print(f"[migration] budgets table: {e}")


    # 7. branch_allocation_templates (Revenue Share templates)
    if 'branch_allocation_templates' not in tables:
        try:
            db.session.execute(text("""
                CREATE TABLE branch_allocation_templates (
                    id           SERIAL PRIMARY KEY,
                    branch_id    INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    recipient_id INTEGER NOT NULL REFERENCES revenue_share_recipients(id) ON DELETE CASCADE,
                    percentage   NUMERIC(6,3) NOT NULL DEFAULT 0,
                    updated_at   TIMESTAMP DEFAULT NOW(),
                    updated_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    CONSTRAINT uq_bat_branch_recipient UNIQUE (branch_id, recipient_id)
                )
            """))
            db.session.commit()
            print("[migration] created branch_allocation_templates table")
        except Exception as e:
            db.session.rollback()
            print(f"[migration] branch_allocation_templates: {e}")


    # 8. purchase_unit column on inventory_items
    if 'inventory_items' in tables:
        inv_cols = {col['name'] for col in insp.get_columns('inventory_items')}
        if 'purchase_unit' not in inv_cols:
            try:
                db.session.execute(text(
                    "ALTER TABLE inventory_items ADD COLUMN purchase_unit VARCHAR(40)"
                ))
                db.session.commit()
                print("[migration] added purchase_unit to inventory_items")
            except Exception as e:
                db.session.rollback()
                print(f"[migration] purchase_unit: {e}")

    # 9. price column on test_catalogue
    if 'test_catalogue' in tables:
        tc_cols = {col['name'] for col in insp.get_columns('test_catalogue')}
        if 'price' not in tc_cols:
            try:
                db.session.execute(text(
                    "ALTER TABLE test_catalogue ADD COLUMN price NUMERIC(12,2)"
                ))
                db.session.commit()
                print("[migration] added price to test_catalogue")
            except Exception as e:
                db.session.rollback()
                print(f"[migration] test_catalogue price: {e}")

    # 10. test_branch_prices table (per-branch price overrides)
    if 'test_branch_prices' not in tables:
        try:
            db.session.execute(text("""
                CREATE TABLE test_branch_prices (
                    id         SERIAL PRIMARY KEY,
                    test_id    INTEGER NOT NULL REFERENCES test_catalogue(id) ON DELETE CASCADE,
                    branch_id  INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    price      NUMERIC(12,2) NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    CONSTRAINT uq_test_branch_price UNIQUE (test_id, branch_id)
                )
            """))
            db.session.commit()
            print("[migration] created test_branch_prices table")
        except Exception as e:
            db.session.rollback()
            print(f"[migration] test_branch_prices: {e}")


def _seed_defaults():
    from models import Branch, Category, User
    import bcrypt

    if Branch.query.count() == 0:
        for name, account in [
            ("Ijofi", "Zenith Ijofi"),
            ("OAUTH", "Zenith OAUTH"),
            ("ILASA", "Zenith ILASA"),
            ("Palm Avenue", "Zenith Palm Avenue"),
            ("Ikeja", "Zenith Ikeja"),
        ]:
            db.session.add(Branch(name=name, source_account=account))
        db.session.commit()

    if Category.query.count() == 0:
        for name in [
            "Lab Supplies / Reagents",
            "Doctor's Payment",
            "Staff Salary / Bonus",
            "Equipment / Maintenance",
            "Electricity / Utilities",
            "Stationery / Office",
            "Cleaning / Sanitation",
            "Imprest / Float",
            "X-Ray / Imaging",
            "Other",
        ]:
            db.session.add(Category(name=name))
        db.session.commit()

    if User.query.count() == 0:
        pw = bcrypt.hashpw(b"Admin@1234", bcrypt.gensalt()).decode()
        db.session.add(
            User(name="MDS Admin", email="admin@surediagnostics.com",
                 password_hash=pw, role="mds")
        )
        db.session.commit()
        print("Default MDS account created: admin@surediagnostics.com / Admin@1234")

    _seed_inventory()


def _seed_inventory():
    """Seed initial reagents/consumables and packages if tables are empty."""
    from models import InventoryItem, PackageCatalogue

    if InventoryItem.query.count() == 0:
        # (name, code, category, unit, pack_size, unit_price, notes)
        ITEMS = [
            ("BIL INDIRECT/DIRECT","s051","lab_reagent","test",60,150.0,"elba, 8550 for 120 mls"),
            ("BILIRUBIN TOT.","s050","lab_reagent","test",None,100.0,None),
            ("PIPETTE","S193","lab_consumable","piece",None,None,None),
            ("3% ACID ALCHO","s076","lab_reagent","ml",None,2.5,None),
            ("ACCU CHECK BATTERY",None,"lab_consumable","piece",None,None,None),
            ("ACCU CHECK STRIP","s190","lab_consumable","strip",None,None,None),
            ("ACETATE PAPER","s017","lab_consumable","piece",None,10.0,None),
            ("ACETONE","s075","lab_reagent","ml",None,7.5,None),
            ("ACTIVATION REAGENT",None,"lab_reagent","ml",None,None,"ELECTROLYTES (NA,K,CL,CA,PH) = 10ML = 7500"),
            ("ACTIVE STRIP","S192","lab_consumable","strip",None,None,None),
            ("AFB STRIP","s064","lab_consumable","strip",None,280.0,None),
            ("ALBUMIN","s052","lab_reagent","test",None,100.0,None),
            ("ALP","s049","lab_reagent","test",None,None,"ELBA 15741 for 120mls; Panel: LIVER FUNCTION TEST"),
            ("ALT",None,"lab_reagent","test",None,None,"ELBA 13316 FOR 200mls; Panel: LIVER FUNCTION TEST"),
            ("AMYLASE","s055","lab_reagent","test",None,100.0,None),
            ("ANTI HUMAN GLO","s024","lab_reagent","test",None,200.0,None),
            ("ANTISERA A","s008","lab_reagent","test",None,200.0,"Panel: BLOOD GROUP"),
            ("ANTISERA B","s009","lab_reagent","test",None,200.0,"Panel: BLOOD GROUP"),
            ("ANTISERA C","s010","lab_reagent","test",None,200.0,"Panel: BLOOD GROUP"),
            ("ANTISERA D","S193","lab_reagent","test",None,1500.0,"Panel: BLOOD GROUP"),
            ("AST",None,"lab_reagent","test",None,None,"ELBA 13316 200MLS; Panel: LIVER FUNCTION TEST"),
            ("BOX FOR LAB","22_","lab_consumable","piece",None,None,None),
            ("BUFFER SOLUTION","s016","lab_reagent","ml",None,5.0,None),
            ("C-CALIBRATION",None,"lab_reagent","ml",None,None,"CALIBRATE TOTAL HCO3 = 10MLS = 7000"),
            ("CALCIUM","s043","lab_reagent","test",None,400.0,None),
            ("CALIBRATION A",None,"lab_reagent","ml",None,None,"ELECTROLYTES = 400ML = 13500"),
            ("CALIBRATION B",None,"lab_reagent","ml",None,None,"ELECTROLYTES = 200ML = 9500"),
            ("CAPILLARY TUBE","s011","lab_consumable","piece",100,7.0,None),
            ("CATHETER","s174","lab_consumable","piece",None,None,None),
            ("CHLORINE","s036","lab_reagent","ml",None,50.0,None),
            ("CHOLESTEROL","s059","lab_reagent","test",None,None,"Panel: FULL LIPID PROFILE"),
            ("COMBI 2","S198","lab_consumable","strip",None,1500.0,None),
            ("COMBI 10","s046","lab_consumable","strip",None,30.0,None),
            ("COTTON WOOL","s002","lab_consumable","piece",100,100.0,None),
            ("COVER SLIP","s027","lab_consumable","piece",100,8.0,None),
            ("CREATININE","124_","lab_reagent","test",None,None,"E&U + CR"),
            ("CRYSTAL VIOLENT","s074","lab_reagent","ml",None,2.5,None),
            ("DEPROTEINIZER",None,"lab_reagent","ml",None,None,"ELECTROLYTES = 10MLS = 7000"),
            ("DISPOSABLE SPECULUM","s173","lab_consumable","piece",None,None,None),
            ("DISTIL WATER","s040","lab_reagent","ml",None,4.0,None),
            ("EDTA BOTTLE","119_","lab_consumable","piece",50,16.0,None),
            ("ENVELOPE","116_","lab_consumable","piece",25,8.0,"Panel: STATIONARY"),
            ("ESR TUBE","s179","lab_consumable","piece",None,None,None),
            ("EVA WATER",None,"lab_consumable","piece",None,None,None),
            ("FACE MASK","s178","lab_consumable","piece",None,None,None),
            ("FIELD STAIN A","s019","lab_reagent","ml",None,10.0,None),
            ("FIELD STAIN B","s020","lab_reagent","ml",None,10.0,None),
            ("FILTER PAPER","122_","lab_consumable","piece",None,None,None),
            ("FLORIDE OXALATE","s091","lab_consumable","piece",None,None,None),
            ("FOETAL SHIELD",None,"lab_consumable","piece",None,None,None),
            ("FSH REAGENT",None,"lab_reagent","test",98,97000.0,"Panel: HORMONES; 98 samples/pack"),
            ("GGT","s061","lab_reagent","test",None,None,"Panel: LIVER FUNCTION TEST"),
            ("GIEMSA STAIN","s022","lab_reagent","ml",None,4.0,None),
            ("GIFT / HAND TOWEL","117_","lab_consumable","piece",None,None,None),
            ("GLOVE","s001","lab_consumable","pair",100,100.0,None),
            ("GLUCOSE","s045","lab_reagent","test",None,None,None),
            ("GRAM STAIN A","s078","lab_reagent","ml",None,2.5,None),
            ("GRAM STAIN B","s079","lab_reagent","ml",None,2.5,None),
            ("GRAM STAIN C","s080","lab_reagent","ml",None,2.5,None),
            ("GRAM STAIN D","s081","lab_reagent","ml",None,2.5,None),
            ("H PYLORI STRIP","s130","lab_consumable","strip",None,650.0,None),
            ("HBA1C STRIP","s195","lab_consumable","strip",None,None,None),
            ("HDL","s063","lab_reagent","test",None,None,"Panel: FULL LIPID PROFILE"),
            ("HEP B STRIP","s131","lab_consumable","strip",50,80.0,None),
            ("HEP C STRIP","s132","lab_consumable","strip",None,80.0,None),
            ("HIV STRIP (DETERMINE)","s133","lab_consumable","strip",None,None,None),
            ("HIV STRIP (STAT-PAK)","s134","lab_consumable","strip",None,None,None),
            ("HIV STRIP (UNIGOLD)","s135","lab_consumable","strip",None,None,None),
            ("IODINE","s082","lab_reagent","ml",None,1.5,None),
            ("IRON","s044","lab_reagent","test",None,None,None),
            ("LABSMART ICT","118_","lab_consumable","piece",None,None,None),
            ("LACTOPHENOL","s083","lab_reagent","ml",None,2.5,None),
            ("LDL",None,"lab_reagent","test",None,None,"Panel: FULL LIPID PROFILE"),
            ("LH REAGENT",None,"lab_reagent","test",None,None,"Panel: HORMONES"),
            ("LIPASE","s060","lab_reagent","test",None,None,None),
            ("LITHIUM HEPARIN","s069","lab_consumable","piece",None,None,None),
            ("LUGOL IODINE","s084","lab_reagent","ml",None,1.5,None),
            ("MAGNESIUM","s047","lab_reagent","test",None,400.0,None),
            ("MALARIA (RAPID)",None,"lab_consumable","strip",None,None,None),
            ("MANSONI TAPE","s029","lab_consumable","piece",None,5.0,None),
            ("METHYLATED SPIRIT","s079","lab_reagent","ml",None,5.0,None),
            ("METHYLENE BLUE","s085","lab_reagent","ml",None,2.5,None),
            ("MICROSCOPE SLIDE","s028","lab_consumable","piece",72,10.0,None),
            ("NITRILE GLOVE","s001a","lab_consumable","pair",100,200.0,None),
            ("PHOSPHATE","s048","lab_reagent","test",None,None,None),
            ("PLAIN BOTTLE","s112","lab_consumable","piece",None,None,None),
            ("POTASSIUM REAGENT","s038","lab_reagent","test",None,None,"Panel: ELECTROLYTES"),
            ("PRL REAGENT",None,"lab_reagent","test",None,None,"Panel: HORMONES"),
            ("PROGESTERONE REAGENT",None,"lab_reagent","test",None,None,"Panel: HORMONES"),
            ("PSA REAGENT",None,"lab_reagent","test",None,None,None),
            ("RAPIDE WIDAL","s148","lab_consumable","strip",None,300.0,None),
            ("REPORT SHEET","s115","lab_consumable","piece",None,None,None),
            ("RINGERS LACTATE","s113","lab_consumable","piece",None,None,None),
            ("SERUM PROGESTERONE",None,"lab_reagent","test",None,None,"Panel: HORMONES"),
            ("SODIUM CHLORIDE","s039","lab_reagent","ml",None,2.0,None),
            ("SODIUM REAGENT","s037","lab_reagent","test",None,None,"Panel: ELECTROLYTES"),
            ("STERILE SWAB","s003","lab_consumable","piece",None,50.0,None),
            ("STOOL CONTAINER","s114","lab_consumable","piece",None,None,None),
            ("SULFOSALICYLIC ACID","s086","lab_reagent","ml",None,2.0,None),
            ("SYRINGE (5ML)","s097","lab_consumable","piece",None,20.0,None),
            ("SYRINGE (2ML)","s096","lab_consumable","piece",None,10.0,None),
            ("TEST TUBE","s102","lab_consumable","piece",None,None,None),
            ("TESTOSTERONE REAGENT",None,"lab_reagent","test",None,None,"Panel: HORMONES"),
            ("THIOGLYCOLLATE BROTH","s104","lab_reagent","ml",None,None,None),
            ("TIGECYCLINE DISC","s105","lab_consumable","piece",None,None,None),
            ("TOTAL PROTEIN","s068","lab_reagent","test",None,100.0,None),
            ("TRIGLYCERIDES","s066","lab_reagent","test",None,None,"Panel: FULL LIPID PROFILE"),
            ("TROPONIN STRIP",None,"lab_consumable","strip",None,None,None),
            ("TSH REAGENT",None,"lab_reagent","test",None,None,"Panel: THYROID"),
            ("T3 REAGENT",None,"lab_reagent","test",None,None,"Panel: THYROID"),
            ("T4 REAGENT",None,"lab_reagent","test",None,None,"Panel: THYROID"),
            ("TYPHIDOT STRIP","s149","lab_consumable","strip",None,600.0,None),
            ("UREA REAGENT","s108","lab_reagent","test",None,None,"Panel: E&U"),
            ("URIC ACID","s053","lab_reagent","test",None,None,None),
            ("URINE CONTAINER","s111","lab_consumable","piece",None,None,None),
            ("VDRL ANTIGEN","s167","lab_reagent","test",None,200.0,None),
            ("WIDAL TEST KIT","s169","lab_reagent","test",None,300.0,None),
            ("WRIGHT STAIN","s023","lab_reagent","ml",None,4.0,None),
            ("XYLENE","s087","lab_reagent","ml",None,None,None),
            ("ZIEHL NEELSEN STAIN A","s088","lab_reagent","ml",None,2.5,None),
            ("ZIEHL NEELSEN STAIN B","s089","lab_reagent","ml",None,2.5,None),
            ("ZINC SULPHATE","s090","lab_reagent","ml",None,2.0,None),
            # USG / Radiology consumables
            ("ULTRASOUND GEL",None,"usg","tube",None,None,None),
            ("X-RAY FILM",None,"xray","piece",None,None,None),
            ("X-RAY DEVELOPER",None,"xray","ml",None,None,None),
            ("X-RAY FIXER",None,"xray","ml",None,None,None),
            ("ECG ELECTRODE",None,"ecg","piece",None,None,None),
            ("ECG PAPER",None,"ecg","roll",None,None,None),
            ("ECG GEL",None,"ecg","tube",None,None,None),
            ("PRINTER PAPER",None,"general","ream",None,None,None),
        ]
        for (name, code, cat, unit, pack, price, notes) in ITEMS:
            db.session.add(InventoryItem(
                name=name, item_code=code, category=cat, unit=unit,
                pack_size=pack, unit_price=price, notes=notes,
            ))
        try:
            db.session.commit()
            print(f"[seed] {len(ITEMS)} inventory items seeded")
        except Exception as e:
            db.session.rollback()
            print(f"[seed] inventory items: {e}")

    if PackageCatalogue.query.count() == 0:
        PACKAGES = [
            "SURE COMPLETE HEALTH PACKAGE",
            "SURE KIDNEY HEALTH PACKAGE",
            "DIABETES SCREENING",
            "SURE VITAL HEALTH PLAN",
            "SURE HEART HEALTH PACKAGE",
            "SURE ESSENTIAL HEALTH CHECK MALE 4OYRS & ABOVE",
            "SURE ESSENTIAL HEALTH CHECK <40YRS (MALE & FEMALE)",
            "SURE BASIC HEALTH CHECK <40YRS (MALE & FEMALE)",
            "SURE ESSENTIAL HEALTH CHECK FEMALE 4OYRS & ABOVE",
            "FOOD HANDLERS SCREENING_BASIC",
            "DOMESTIC HELPER CHECK UP",
            "KIDS HEALTH PLAN",
            "SURE PROSTATE HEALTH PACKAGE",
            "SURE COLON HEALTH PACKAGE",
            "SURE PRE MARITAL MEDICAL SCREENING",
            "ANTENATAL PACKAGE~ STANDARD",
            "ANTENATAL PACKAGE~ BASIC",
            "SURE BREAST HEALTH PACKAGE",
            "SURE ESSENTIAL HEALTH PACKAGE ~ STANDARD",
            "SURE ESSENTIAL HEALTH PACKAGE ~ BASIC",
            "SURE CERVICAL CANCER PREVENTION PACKAGE",
        ]
        for name in PACKAGES:
            db.session.add(PackageCatalogue(name=name, labsmart_name=name))
        try:
            db.session.commit()
            print(f"[seed] {len(PACKAGES)} packages seeded")
        except Exception as e:
            db.session.rollback()
            print(f"[seed] packages: {e}")


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)



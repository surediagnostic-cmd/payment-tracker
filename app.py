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

    app.register_blueprint(auth_bp)
    app.register_blueprint(requests_bp)
    app.register_blueprint(approvals_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(budget_bp)

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


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)

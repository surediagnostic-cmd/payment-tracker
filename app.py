import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail

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

    app.register_blueprint(auth_bp)
    app.register_blueprint(requests_bp)
    app.register_blueprint(approvals_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)

    @app.route("/health")
    def health():
        return "ok", 200

    with app.app_context():
        try:
            _run_migrations()
            db.create_all()
            _seed_defaults()
        except Exception as e:
            print(f"[startup] DB init warning: {e}", flush=True)

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

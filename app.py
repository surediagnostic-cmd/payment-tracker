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

    with app.app_context():
        db.create_all()
        _seed_defaults()

    return app


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


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)

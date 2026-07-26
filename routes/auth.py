import bcrypt
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app import db, login_manager
from models import User

auth_bp = Blueprint("auth", __name__)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def _home():
    """Return the correct home URL for the current user's role."""
    if current_user.role == "lab_staff":
        return url_for("inventory.dashboard")
    return url_for("requests.dashboard")


@auth_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(_home())
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_home())
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").encode()
        user = User.query.filter_by(email=email, is_active=True).first()
        if user and bcrypt.checkpw(password, user.password_hash.encode()):
            login_user(user, remember=True)
            return redirect(_home())
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))

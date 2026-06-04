import bcrypt
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app import db
from models import Branch, Category, User

admin_bp = Blueprint("admin", __name__)


def _mds_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_mds:
            flash("Admin access required.", "error")
            return redirect(url_for("requests.dashboard"))
        return f(*args, **kwargs)
    return decorated


@admin_bp.route("/admin")
@login_required
@_mds_required
def admin():
    branches = Branch.query.order_by(Branch.name).all()
    categories = Category.query.order_by(Category.name).all()
    users = User.query.order_by(User.name).all()
    return render_template("admin.html", branches=branches, categories=categories, users=users)


# --- Branches ---

@admin_bp.route("/admin/branches/add", methods=["POST"])
@login_required
@_mds_required
def add_branch():
    name = request.form.get("name", "").strip()
    account = request.form.get("source_account", "").strip()
    if not name:
        flash("Branch name is required.", "error")
        return redirect(url_for("admin.admin"))
    if Branch.query.filter_by(name=name).first():
        flash(f"Branch '{name}' already exists.", "error")
        return redirect(url_for("admin.admin"))
    db.session.add(Branch(name=name, source_account=account))
    db.session.commit()
    flash(f"Branch '{name}' added.", "success")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/branches/<int:branch_id>/toggle", methods=["POST"])
@login_required
@_mds_required
def toggle_branch(branch_id):
    branch = Branch.query.get_or_404(branch_id)
    branch.is_active = not branch.is_active
    db.session.commit()
    flash(f"Branch '{branch.name}' {'activated' if branch.is_active else 'deactivated'}.", "success")
    return redirect(url_for("admin.admin"))


# --- Categories ---

@admin_bp.route("/admin/categories/add", methods=["POST"])
@login_required
@_mds_required
def add_category():
    name = request.form.get("name", "").strip()
    cost_type = request.form.get("cost_type", "overhead")
    if cost_type not in ("direct_cost", "overhead"):
        cost_type = "overhead"
    if not name:
        flash("Category name is required.", "error")
        return redirect(url_for("admin.admin"))
    if Category.query.filter_by(name=name).first():
        flash(f"Category '{name}' already exists.", "error")
        return redirect(url_for("admin.admin"))
    db.session.add(Category(name=name, cost_type=cost_type))
    db.session.commit()
    flash(f"Category '{name}' added.", "success")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/categories/<int:cat_id>/toggle", methods=["POST"])
@login_required
@_mds_required
def toggle_category(cat_id):
    cat = Category.query.get_or_404(cat_id)
    cat.is_active = not cat.is_active
    db.session.commit()
    flash(f"Category '{cat.name}' {'activated' if cat.is_active else 'deactivated'}.", "success")
    return redirect(url_for("admin.admin"))


# --- Users ---

@admin_bp.route("/admin/users/add", methods=["POST"])
@login_required
@_mds_required
def add_user():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "accountant")
    branch_ids = request.form.getlist("branch_ids[]", type=int)

    if not all([name, email, password]):
        flash("Name, email, and password are required.", "error")
        return redirect(url_for("admin.admin"))
    if User.query.filter_by(email=email).first():
        flash(f"User with email '{email}' already exists.", "error")
        return redirect(url_for("admin.admin"))

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user = User(name=name, email=email, password_hash=pw_hash, role=role)
    if role == "accountant" and branch_ids:
        user.branches = Branch.query.filter(Branch.id.in_(branch_ids)).all()
    db.session.add(user)
    db.session.commit()
    flash(f"User '{name}' created.", "success")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/users/<int:user_id>/edit", methods=["POST"])
@login_required
@_mds_required
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", user.role)
    branch_ids = request.form.getlist("branch_ids[]", type=int)

    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect(url_for("admin.admin"))

    existing = User.query.filter_by(email=email).first()
    if existing and existing.id != user.id:
        flash(f"Email '{email}' is already used by another account.", "error")
        return redirect(url_for("admin.admin"))

    user.name = name
    user.email = email
    user.role = role
    if role == "accountant":
        user.branches = Branch.query.filter(Branch.id.in_(branch_ids)).all() if branch_ids else []
    else:
        user.branches = []
    db.session.commit()
    flash(f"User '{user.name}' updated.", "success")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@_mds_required
def toggle_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin.admin"))
    user.is_active = not user.is_active
    db.session.commit()
    flash(f"User '{user.name}' {'activated' if user.is_active else 'deactivated'}.", "success")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@_mds_required
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    new_pw = request.form.get("new_password", "").strip()
    if len(new_pw) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("admin.admin"))
    user.password_hash = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
    db.session.commit()
    flash(f"Password reset for '{user.name}'.", "success")
    return redirect(url_for("admin.admin"))

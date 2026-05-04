from datetime import datetime, date
from decimal import Decimal
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user
from sqlalchemy import func
from app import db
from models import PaymentRequest, Branch, Category, User
from utils import send_email

requests_bp = Blueprint("requests", __name__)


def _parse_month(month_str):
    """Return (year, month) int tuple from 'YYYY-MM'.
    month=0 means 'all months in that year'.
    Defaults to current month when month_str is empty.
    """
    now = datetime.utcnow()
    if month_str:
        try:
            parts = month_str.split("-")
            yr = int(parts[0])
            mo = int(parts[1]) if len(parts) > 1 else 0
            return yr, mo
        except Exception:
            pass
    return now.year, now.month


@requests_bp.route("/dashboard")
@login_required
def dashboard():
    month_str = request.args.get("month", "")
    selected_branch = request.args.get("branch_id", type=int)
    yr, mo = _parse_month(month_str)
    month_label = date(yr, mo, 1).strftime("%B %Y") if mo else f"All of {yr}"
    month_start = date(yr, mo, 1) if mo else date(yr, 1, 1)

    branches = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    categories = Category.query.filter_by(is_active=True).all()

    def month_filter(q):
        q = q.filter(func.extract("year", PaymentRequest.date) == yr)
        if mo:
            q = q.filter(func.extract("month", PaymentRequest.date) == mo)
        return q

    if current_user.is_mds:
        # ── Pending (all time, not just this month) ──
        pending_q = PaymentRequest.query.filter_by(status="pending").order_by(
            PaymentRequest.created_at.desc()
        )
        if selected_branch:
            pending_q = pending_q.filter_by(branch_id=selected_branch)
        pending = pending_q.all()

        # ── Month KPIs ──
        approved_q = PaymentRequest.query.filter_by(status="approved")
        approved_q = month_filter(approved_q)
        if selected_branch:
            approved_q = approved_q.filter_by(branch_id=selected_branch)
        approved_this_month = approved_q.all()
        total_approved = sum(float(r.approved_amount or 0) for r in approved_this_month)
        total_pending_amt = sum(
            float(r.requested_amount)
            for r in PaymentRequest.query.filter_by(status="pending").all()
        )

        reviewed = PaymentRequest.query.filter(
            PaymentRequest.status.in_(["approved", "rejected"])
        )
        reviewed = month_filter(reviewed)
        if selected_branch:
            reviewed = reviewed.filter_by(branch_id=selected_branch)
        reviewed_count = reviewed.count()
        approved_count = len(approved_this_month)
        approval_rate = round((approved_count / reviewed_count) * 100) if reviewed_count else 0

        # ── Status tracker (selected month) ──
        def status_count(status):
            q = PaymentRequest.query.filter_by(status=status)
            q = month_filter(q)
            if selected_branch:
                q = q.filter_by(branch_id=selected_branch)
            return q.count()

        uploaded_count = month_filter(
            PaymentRequest.query.filter_by(upload_status="uploaded")
        )
        if selected_branch:
            uploaded_count = uploaded_count.filter_by(branch_id=selected_branch)

        status_counts = {
            "pending":  status_count("pending"),
            "approved": status_count("approved"),
            "rejected": status_count("rejected"),
            "uploaded": uploaded_count.count(),
        }

        # ── Branch totals (selected month) ──
        branch_totals_q = db.session.query(
            Branch.name,
            func.sum(PaymentRequest.requested_amount).label("requested"),
            func.sum(PaymentRequest.approved_amount).label("approved"),
            func.count(PaymentRequest.id).label("count"),
        ).join(Branch).filter(PaymentRequest.status.in_(["approved", "pending"]))
        branch_totals_q = month_filter(branch_totals_q)
        if selected_branch:
            branch_totals_q = branch_totals_q.filter(PaymentRequest.branch_id == selected_branch)
        branch_totals = branch_totals_q.group_by(Branch.name).order_by(
            func.sum(PaymentRequest.approved_amount).desc()
        ).all()

        # ── Category breakdown (selected month) ──
        cat_q = db.session.query(
            Category.name,
            func.sum(PaymentRequest.approved_amount).label("total"),
            func.count(PaymentRequest.id).label("count"),
        ).join(Category).filter(PaymentRequest.status == "approved")
        cat_q = month_filter(cat_q)
        if selected_branch:
            cat_q = cat_q.filter(PaymentRequest.branch_id == selected_branch)
        category_data = cat_q.group_by(Category.name).order_by(
            func.sum(PaymentRequest.approved_amount).desc()
        ).all()

        # ── Variance report (selected month) ──
        var_q = PaymentRequest.query.filter(
            PaymentRequest.status == "approved",
            PaymentRequest.approved_amount != PaymentRequest.requested_amount,
        )
        var_q = month_filter(var_q)
        if selected_branch:
            var_q = var_q.filter_by(branch_id=selected_branch)
        variance_data = var_q.order_by(PaymentRequest.date.desc()).limit(30).all()

        # ── Recent payments (selected month) ──
        recent_q = PaymentRequest.query
        recent_q = month_filter(recent_q)
        if selected_branch:
            recent_q = recent_q.filter_by(branch_id=selected_branch)
        recent = recent_q.order_by(PaymentRequest.created_at.desc()).limit(20).all()

        effective_month_str = month_str or (f"{yr:04d}-{mo:02d}" if mo else f"{yr:04d}-0")
        return render_template(
            "dashboard.html",
            # filters
            month_str=effective_month_str,
            month_label=month_label,
            selected_branch=selected_branch,
            branches=branches,
            # KPIs
            pending=pending,
            total_approved=total_approved,
            total_pending_amt=total_pending_amt,
            approval_rate=approval_rate,
            # analytics
            status_counts=status_counts,
            branch_totals=branch_totals,
            category_data=category_data,
            variance_data=variance_data,
            recent=recent,
        )

    else:
        # ── Accountant view ──
        my_q = PaymentRequest.query.filter_by(submitted_by=current_user.id)
        my_q = month_filter(my_q)
        my_requests = my_q.order_by(PaymentRequest.created_at.desc()).all()

        my_pending  = sum(1 for r in my_requests if r.status == "pending")
        my_approved = [r for r in my_requests if r.status == "approved"]
        my_total    = sum(float(r.approved_amount or 0) for r in my_approved)

        # Simple category breakdown for accountant branch
        cat_q = db.session.query(
            Category.name,
            func.sum(PaymentRequest.approved_amount).label("total"),
            func.count(PaymentRequest.id).label("count"),
        ).join(Category).filter(
            PaymentRequest.status == "approved",
            PaymentRequest.submitted_by == current_user.id,
        )
        cat_q = month_filter(cat_q)
        category_data = cat_q.group_by(Category.name).order_by(
            func.sum(PaymentRequest.approved_amount).desc()
        ).all()

        status_counts = {
            "pending":  sum(1 for r in my_requests if r.status == "pending"),
            "approved": sum(1 for r in my_requests if r.status == "approved"),
            "rejected": sum(1 for r in my_requests if r.status == "rejected"),
            "uploaded": sum(1 for r in my_requests if r.upload_status == "uploaded"),
        }

        return render_template(
            "dashboard.html",
            month_str=month_str or (f"{yr:04d}-{mo:02d}" if mo else f"{yr:04d}-0"),
            month_label=month_label,
            selected_branch=None,
            branches=branches,
            my_requests=my_requests,
            my_pending=my_pending,
            my_total=my_total,
            approved_count=len(my_approved),
            category_data=category_data,
            status_counts=status_counts,
        )


@requests_bp.route("/requests/new", methods=["GET", "POST"])
@login_required
def new_request():
    if current_user.is_mds:
        flash("MDS account cannot submit payment requests.", "error")
        return redirect(url_for("requests.dashboard"))

    categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()

    if request.method == "POST":
        try:
            qty = int(request.form["quantity"])
            rate = Decimal(request.form["rate"].replace(",", ""))
            requested_amount = qty * rate
            branch_id = current_user.branch_id

            ref = PaymentRequest.generate_reference(branch_id)
            pr = PaymentRequest(
                reference=ref,
                date=datetime.strptime(request.form["date"], "%Y-%m-%d").date(),
                description=request.form["description"].strip(),
                category_id=int(request.form["category_id"]),
                branch_id=branch_id,
                quantity=qty,
                rate=rate,
                requested_amount=requested_amount,
                beneficiary_name=request.form["beneficiary_name"].strip(),
                beneficiary_account=request.form["beneficiary_account"].strip(),
                beneficiary_bank=request.form["beneficiary_bank"].strip(),
                bank_code=request.form.get("bank_code", "").strip(),
                submitted_by=current_user.id,
            )
            db.session.add(pr)
            db.session.commit()

            mds_email = current_app.config.get("MDS_EMAIL")
            if mds_email:
                send_email(
                    to=mds_email,
                    subject=f"[Sure Finance] New Payment Request — {pr.reference}",
                    body=(
                        f"A new payment request has been submitted.\n\n"
                        f"Reference: {pr.reference}\n"
                        f"Branch: {current_user.branch.name}\n"
                        f"Description: {pr.description}\n"
                        f"Amount: ₦{pr.requested_amount:,.2f}\n"
                        f"Beneficiary: {pr.beneficiary_name} ({pr.beneficiary_bank})\n"
                        f"Submitted by: {current_user.name}\n\n"
                        f"Please log in to review this request."
                    ),
                )

            flash(f"Payment request {ref} submitted successfully.", "success")
            return redirect(url_for("requests.dashboard"))
        except Exception as e:
            db.session.rollback()
            flash(f"Error submitting request: {str(e)}", "error")

    return render_template("new_request.html", categories=categories)


@requests_bp.route("/requests/<int:req_id>")
@login_required
def view_request(req_id):
    pr = PaymentRequest.query.get_or_404(req_id)
    if not current_user.is_mds and pr.submitted_by != current_user.id:
        flash("Access denied.", "error")
        return redirect(url_for("requests.dashboard"))
    return render_template("request_detail.html", pr=pr)


@requests_bp.route("/requests/<int:req_id>/upload", methods=["POST"])
@login_required
def mark_uploaded(req_id):
    pr = PaymentRequest.query.get_or_404(req_id)
    if pr.status != "approved":
        flash("Only approved requests can be marked as uploaded.", "error")
        return redirect(url_for("requests.view_request", req_id=req_id))
    pr.upload_status = "uploaded"
    db.session.commit()
    flash(f"{pr.reference} marked as uploaded.", "success")
    return redirect(url_for("requests.dashboard"))


@requests_bp.route("/requests")
@login_required
def list_requests():
    q = PaymentRequest.query
    if not current_user.is_mds:
        q = q.filter_by(submitted_by=current_user.id)

    branch_id   = request.args.get("branch_id", type=int)
    month       = request.args.get("month", "")
    status      = request.args.get("status", "")
    category_id = request.args.get("category_id", type=int)

    if branch_id:   q = q.filter_by(branch_id=branch_id)
    if status:      q = q.filter_by(status=status)
    if category_id: q = q.filter_by(category_id=category_id)
    if month:
        try:
            yr, mo = month.split("-")
            q = q.filter(
                func.extract("year",  PaymentRequest.date) == int(yr),
                func.extract("month", PaymentRequest.date) == int(mo),
            )
        except ValueError:
            pass

    requests_list = q.order_by(PaymentRequest.created_at.desc()).all()
    branches   = Branch.query.filter_by(is_active=True).all()
    categories = Category.query.filter_by(is_active=True).all()

    return render_template(
        "list_requests.html",
        requests=requests_list,
        branches=branches,
        categories=categories,
        filters={"branch_id": branch_id, "month": month, "status": status, "category_id": category_id},
    )

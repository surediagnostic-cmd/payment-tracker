from datetime import datetime, timezone
from decimal import Decimal
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user
from app import db
from models import PaymentRequest, User
from utils import send_email

approvals_bp = Blueprint("approvals", __name__)


def _mds_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_mds:
            flash("MDS access required.", "error")
            return redirect(url_for("requests.dashboard"))
        return f(*args, **kwargs)
    return decorated


@approvals_bp.route("/requests/<int:req_id>/review", methods=["GET", "POST"])
@login_required
@_mds_required
def review(req_id):
    pr = PaymentRequest.query.get_or_404(req_id)

    if request.method == "POST":
        action = request.form.get("action")
        comment = request.form.get("mds_comment", "").strip()

        if action not in ("approve", "reject"):
            flash("Invalid action.", "error")
            return redirect(url_for("approvals.review", req_id=req_id))

        if action == "approve":
            approved_str = request.form.get("approved_amount", "").replace(",", "")
            try:
                approved_amount = Decimal(approved_str)
            except Exception:
                flash("Invalid approved amount.", "error")
                return redirect(url_for("approvals.review", req_id=req_id))
            pr.approved_amount = approved_amount
            pr.status = "approved"
            subject = f"[Sure Finance] Request {pr.reference} Approved"
            body = (
                f"Your payment request has been approved.\n\n"
                f"Reference: {pr.reference}\n"
                f"Description: {pr.description}\n"
                f"Requested: ₦{pr.requested_amount:,.2f}\n"
                f"Approved Amount: ₦{pr.approved_amount:,.2f}\n"
                f"MDS Comment: {comment or 'N/A'}\n\n"
                f"You can now proceed with the bank upload."
            )
        else:
            pr.status = "rejected"
            subject = f"[Sure Finance] Request {pr.reference} Rejected"
            body = (
                f"Your payment request has been rejected.\n\n"
                f"Reference: {pr.reference}\n"
                f"Description: {pr.description}\n"
                f"Requested Amount: ₦{pr.requested_amount:,.2f}\n"
                f"Reason: {comment or 'No reason provided'}\n\n"
                f"Please contact MDS for clarification."
            )

        pr.mds_comment = comment
        pr.reviewed_at = datetime.now(timezone.utc)
        db.session.commit()

        # Notify submitter
        submitter = User.query.get(pr.submitted_by)
        if submitter and submitter.email:
            send_email(to=submitter.email, subject=subject, body=body)

        flash(
            f"Request {pr.reference} has been {'approved' if action == 'approve' else 'rejected'}.",
            "success" if action == "approve" else "warning",
        )
        return redirect(url_for("requests.dashboard"))

    return render_template("review_request.html", pr=pr)

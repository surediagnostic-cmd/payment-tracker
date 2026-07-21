from datetime import datetime, timezone
from decimal import Decimal
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user
from sqlalchemy.orm import subqueryload, joinedload
from app import db
from models import PaymentRequest, PaymentRequestItem, User, InTransitStock
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
    pr = PaymentRequest.query.options(
        subqueryload(PaymentRequest.items).joinedload(PaymentRequestItem.category),
        subqueryload(PaymentRequest.items).subqueryload(PaymentRequestItem.children),
        joinedload(PaymentRequest.branch),
        joinedload(PaymentRequest.submitter),
    ).filter_by(id=req_id).first_or_404()

    if request.method == "POST":
        try:
            action = request.form.get("action")
            comment = request.form.get("mds_comment", "").strip()

            if action not in ("approve", "reject"):
                flash("Invalid action.", "error")
                return redirect(url_for("approvals.review", req_id=req_id))

            item_lines = "\n".join(
                f"  • {it.description} ({it.category.name}) — ₦{it.amount:,.2f}"
                for it in pr.items
            ) or "(no items)"

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
                    f"Branch: {pr.branch.name}\n"
                    f"Requested: ₦{pr.requested_amount:,.2f}\n"
                    f"Approved Amount: ₦{pr.approved_amount:,.2f}\n"
                    f"MDS Comment: {comment or 'N/A'}\n\n"
                    f"Items:\n{item_lines}\n\n"
                    f"You can now proceed with the bank upload."
                )
            else:
                pr.status = "rejected"
                subject = f"[Sure Finance] Request {pr.reference} Rejected"
                body = (
                    f"Your payment request has been rejected.\n\n"
                    f"Reference: {pr.reference}\n"
                    f"Branch: {pr.branch.name}\n"
                    f"Requested Amount: ₦{pr.requested_amount:,.2f}\n"
                    f"Reason: {comment or 'No reason provided'}\n\n"
                    f"Items:\n{item_lines}\n\n"
                    f"Please contact MDS for clarification."
                )

            pr.mds_comment = comment
            pr.reviewed_at = datetime.now(timezone.utc)
            db.session.commit()

            # ── Create InTransitStock entries for inventory-linked items ──────────
            if action == "approve":
                try:
                    inv_items = PaymentRequestItem.query.filter(
                        PaymentRequestItem.request_id == pr.id,
                        PaymentRequestItem.inventory_item_id.isnot(None),
                    ).all()
                    transit_count = 0
                    for item in inv_items:
                        qty = float(item.qty_ordered or item.quantity or 1)
                        if qty > 0:
                            db.session.add(InTransitStock(
                                payment_request_id=pr.id,
                                payment_request_item_id=item.id,
                                inventory_item_id=item.inventory_item_id,
                                branch_id=pr.branch_id,
                                qty=qty,
                            ))
                            transit_count += 1
                    if transit_count:
                        db.session.commit()
                        current_app.logger.info(
                            f"Created {transit_count} in-transit entries for {pr.reference}"
                        )
                except Exception as transit_err:
                    current_app.logger.warning(
                        f"In-transit creation failed for {pr.reference}: {transit_err}"
                    )

            try:
                submitter = User.query.get(pr.submitted_by)
                if submitter and submitter.email:
                    send_email(to=submitter.email, subject=subject, body=body)
            except Exception as mail_err:
                current_app.logger.warning(f"Post-approval email failed: {mail_err}")

            flash(
                f"Request {pr.reference} has been {'approved' if action == 'approve' else 'rejected'}.",
                "success" if action == "approve" else "warning",
            )
            return redirect(url_for("requests.dashboard"))

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            current_app.logger.error(f"Approval error for req {req_id}: {e}", exc_info=True)
            print(f"[approval error] req {req_id}: {e}", flush=True)
            flash(f"An error occurred: {str(e)}", "error")
            return redirect(url_for("approvals.review", req_id=req_id))

    try:
        return render_template("review_request.html", pr=pr)
    except Exception as tpl_err:
        print(f"[review render error] req {req_id}: {tpl_err}", flush=True)
        flash(f"Could not load review page: {tpl_err}", "error")
        return redirect(url_for("requests.dashboard"))

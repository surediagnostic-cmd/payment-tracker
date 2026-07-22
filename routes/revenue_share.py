"""Revenue Share Module — allocate gross revenue percentages to recipients."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from app import db
from models import (
    Branch, Category, PaymentRequest, PaymentRequestItem,
    RevenueShareRecipient, RevenueSharePeriod, RevenueShareAllocation,
)

revenue_share_bp = Blueprint("revenue_share", __name__, url_prefix="/revenue-share")


def _mds_required(f):
    """MDS only — used for finalise (creates payment requests)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_mds:
            flash("MDS access required.", "error")
            return redirect(url_for("requests.dashboard"))
        return f(*args, **kwargs)
    return decorated


def _finance_required(f):
    """MDS + Accountant — can view/edit revenue share data."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ("mds", "accountant"):
            flash("Access restricted.", "error")
            return redirect(url_for("requests.dashboard"))
        return f(*args, **kwargs)
    return decorated


def _get_or_create_rs_category():
    cat = Category.query.filter(
        func.lower(Category.name) == "revenue share"
    ).first()
    if not cat:
        cat = Category(name="Revenue Share", cost_type="overhead", is_active=True)
        db.session.add(cat)
        db.session.flush()
    return cat


def _total_pct(period_id):
    r = db.session.query(
        func.coalesce(func.sum(RevenueShareAllocation.percentage), 0)
    ).filter_by(period_id=period_id).scalar()
    return float(r or 0)


def _total_amount(period_id):
    r = db.session.query(
        func.coalesce(func.sum(RevenueShareAllocation.amount_calculated), 0)
    ).filter_by(period_id=period_id).scalar()
    return float(r or 0)


# ── Index + analytics ─────────────────────────────────────────────────────────

@revenue_share_bp.route("/")
@login_required
@_finance_required
def index():
    periods  = RevenueSharePeriod.query.order_by(RevenueSharePeriod.created_at.desc()).all()
    branches = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    recipients_all = RevenueShareRecipient.query.filter_by(is_active=True).order_by(
        RevenueShareRecipient.name
    ).all()

    # KPIs
    total_disbursed = float(
        db.session.query(func.coalesce(func.sum(RevenueShareAllocation.amount_calculated), 0))
        .join(RevenueSharePeriod, RevenueShareAllocation.period_id == RevenueSharePeriod.id)
        .filter(RevenueSharePeriod.status == "finalised")
        .scalar() or 0
    )
    total_pending = (
        RevenueShareAllocation.query
        .join(RevenueSharePeriod, RevenueShareAllocation.period_id == RevenueSharePeriod.id)
        .filter(RevenueSharePeriod.status == "finalised", RevenueShareAllocation.is_paid == False)
        .count()
    )

    # Per-recipient totals (for analytics table)
    rec_totals = (
        db.session.query(
            RevenueShareRecipient.name,
            func.coalesce(func.sum(RevenueShareAllocation.amount_calculated), 0).label("total"),
            func.count(RevenueShareAllocation.id).label("count"),
        )
        .join(RevenueShareAllocation, RevenueShareAllocation.recipient_id == RevenueShareRecipient.id)
        .join(RevenueSharePeriod, RevenueSharePeriod.id == RevenueShareAllocation.period_id)
        .filter(RevenueSharePeriod.status == "finalised")
        .group_by(RevenueShareRecipient.id, RevenueShareRecipient.name)
        .order_by(func.sum(RevenueShareAllocation.amount_calculated).desc())
        .all()
    )

    # Last 10 finalised periods for bar chart (oldest→newest)
    chart_periods = (
        RevenueSharePeriod.query
        .filter_by(status="finalised")
        .order_by(RevenueSharePeriod.created_at.desc())
        .limit(10).all()
    )[::-1]

    return render_template(
        "revenue_share/index.html",
        periods=periods, branches=branches,
        recipients_all=recipients_all,
        total_disbursed=total_disbursed,
        total_pending=total_pending,
        rec_totals=rec_totals,
        chart_periods=chart_periods,
    )


# ── New period ────────────────────────────────────────────────────────────────

@revenue_share_bp.route("/periods/new", methods=["GET", "POST"])
@login_required
@_finance_required
def new_period():
    branches   = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    recipients = RevenueShareRecipient.query.filter_by(is_active=True).order_by(
        RevenueShareRecipient.name
    ).all()

    if request.method == "POST":
        try:
            label = request.form.get("label", "").strip()
            if not label:
                flash("Period label is required.", "error")
                return redirect(url_for("revenue_share.new_period"))

            branch_id_raw = request.form.get("branch_id", "").strip()
            gross_raw     = request.form.get("gross_revenue", "0").replace(",", "").strip()
            try:
                gross = Decimal(gross_raw) if gross_raw else Decimal("0")
            except InvalidOperation:
                flash("Invalid gross revenue amount.", "error")
                return redirect(url_for("revenue_share.new_period"))

            start_raw = request.form.get("period_start", "").strip()
            end_raw   = request.form.get("period_end", "").strip()

            period = RevenueSharePeriod(
                label         = label,
                branch_id     = int(branch_id_raw) if branch_id_raw else None,
                gross_revenue = gross,
                period_start  = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else None,
                period_end    = datetime.strptime(end_raw,   "%Y-%m-%d").date() if end_raw   else None,
                notes         = request.form.get("notes", "").strip() or None,
                status        = "draft",
                created_by    = current_user.id,
            )
            db.session.add(period)
            db.session.flush()

            # Allocations from form (recipient percentages)
            for rec in recipients:
                pct_raw = request.form.get(f"pct_{rec.id}", "").replace(",", "").strip()
                if pct_raw:
                    try:
                        pct = Decimal(pct_raw)
                        if pct > 0:
                            amount = (gross * pct / 100).quantize(Decimal("0.01"))
                            db.session.add(RevenueShareAllocation(
                                period_id=period.id, recipient_id=rec.id,
                                percentage=pct, amount_calculated=amount,
                            ))
                    except InvalidOperation:
                        pass

            db.session.commit()
            flash(f"Period '{label}' created.", "success")
            return redirect(url_for("revenue_share.period_detail", period_id=period.id))

        except Exception as e:
            try: db.session.rollback()
            except Exception: pass
            flash(f"Error creating period: {e}", "error")

    return render_template("revenue_share/new_period.html",
                           branches=branches, recipients=recipients)


# ── Period detail ─────────────────────────────────────────────────────────────

@revenue_share_bp.route("/periods/<int:period_id>")
@login_required
@_finance_required
def period_detail(period_id):
    period     = RevenueSharePeriod.query.get_or_404(period_id)
    branches   = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    recipients = RevenueShareRecipient.query.filter_by(is_active=True).order_by(
        RevenueShareRecipient.name
    ).all()
    alloc_map  = {a.recipient_id: a for a in period.allocations}
    return render_template(
        "revenue_share/period_detail.html",
        period=period, branches=branches,
        recipients=recipients, alloc_map=alloc_map,
    )


@revenue_share_bp.route("/periods/<int:period_id>/save-allocation", methods=["POST"])
@login_required
@_finance_required
def save_allocation(period_id):
    """AJAX upsert/remove one allocation."""
    try:
        period = RevenueSharePeriod.query.get_or_404(period_id)
        if period.status == "finalised":
            return jsonify(ok=False, error="Period is finalised."), 400

        recipient_id = int(request.form["recipient_id"])
        pct_raw = request.form.get("percentage", "0").replace(",", "").strip()
        try:
            pct = Decimal(pct_raw) if pct_raw else Decimal("0")
        except InvalidOperation:
            return jsonify(ok=False, error="Invalid percentage"), 400

        gross  = Decimal(str(period.gross_revenue or 0))
        amount = (gross * pct / 100).quantize(Decimal("0.01"))

        alloc = RevenueShareAllocation.query.filter_by(
            period_id=period_id, recipient_id=recipient_id
        ).first()

        if pct <= 0:
            if alloc:
                db.session.delete(alloc)
                db.session.commit()
            return jsonify(ok=True, removed=True,
                           total_pct=_total_pct(period_id),
                           total_amount=_total_amount(period_id))

        if alloc:
            alloc.percentage        = pct
            alloc.amount_calculated = amount
        else:
            alloc = RevenueShareAllocation(
                period_id=period_id, recipient_id=recipient_id,
                percentage=pct, amount_calculated=amount,
            )
            db.session.add(alloc)

        db.session.commit()
        return jsonify(
            ok=True, id=alloc.id,
            pct=float(pct), amount=float(amount),
            total_pct=_total_pct(period_id),
            total_amount=_total_amount(period_id),
        )

    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


@revenue_share_bp.route("/periods/<int:period_id>/update", methods=["POST"])
@login_required
@_finance_required
def update_period(period_id):
    """AJAX update period header fields + recalculate allocations."""
    try:
        period = RevenueSharePeriod.query.get_or_404(period_id)
        if period.status == "finalised":
            return jsonify(ok=False, error="Period is finalised."), 400

        gross_raw = request.form.get("gross_revenue", "").replace(",", "").strip()
        if gross_raw:
            try:
                period.gross_revenue = Decimal(gross_raw)
            except InvalidOperation:
                return jsonify(ok=False, error="Invalid gross revenue"), 400

        for field in ("label", "notes"):
            if field in request.form:
                val = request.form[field].strip()
                setattr(period, field, val or getattr(period, field))
        if "branch_id" in request.form:
            br = request.form["branch_id"].strip()
            period.branch_id = int(br) if br else None

        # Recalculate allocation amounts from new gross
        gross = Decimal(str(period.gross_revenue or 0))
        for alloc in period.allocations:
            alloc.amount_calculated = (
                gross * Decimal(str(alloc.percentage)) / 100
            ).quantize(Decimal("0.01"))

        db.session.commit()
        return jsonify(ok=True, gross=float(period.gross_revenue),
                       total_amount=_total_amount(period_id))

    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


@revenue_share_bp.route("/periods/<int:period_id>/finalise", methods=["POST"])
@login_required
@_mds_required
def finalise_period(period_id):
    """Create one PaymentRequest per allocation, mark period as finalised."""
    try:
        period = RevenueSharePeriod.query.get_or_404(period_id)
        if period.status == "finalised":
            flash("Period already finalised.", "warning")
            return redirect(url_for("revenue_share.period_detail", period_id=period_id))
        if not period.allocations:
            flash("Add at least one recipient allocation before finalising.", "error")
            return redirect(url_for("revenue_share.period_detail", period_id=period_id))

        rs_cat    = _get_or_create_rs_category()
        branch_id = period.branch_id or 1
        created   = 0

        for alloc in period.allocations:
            rec = alloc.recipient
            amt = Decimal(str(alloc.amount_calculated or 0))
            if amt <= 0:
                continue

            ref = PaymentRequest.generate_reference(branch_id)
            pr  = PaymentRequest(
                reference           = ref,
                date                = datetime.now(timezone.utc).date(),
                branch_id           = branch_id,
                beneficiary_name    = rec.name,
                beneficiary_account = rec.account_number or "—",
                beneficiary_bank    = rec.bank_name or "—",
                requested_amount    = amt,
                submitted_by        = current_user.id,
                status              = "pending",
            )
            db.session.add(pr)
            db.session.flush()
            db.session.add(PaymentRequestItem(
                request_id  = pr.id,
                description = f"Revenue Share — {period.label}",
                category_id = rs_cat.id,
                quantity    = 1,
                rate        = amt,
                amount      = amt,
            ))
            alloc.payment_request_id = pr.id
            created += 1

        period.status = "finalised"
        db.session.commit()
        flash(f"Period finalised — {created} payment request(s) created and queued for MDS approval.", "success")
        return redirect(url_for("revenue_share.period_detail", period_id=period_id))

    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        flash(f"Finalise error: {e}", "error")
        return redirect(url_for("revenue_share.period_detail", period_id=period_id))


@revenue_share_bp.route("/periods/<int:period_id>/allocations/<int:alloc_id>/toggle-paid", methods=["POST"])
@login_required
@_finance_required
def toggle_paid(period_id, alloc_id):
    try:
        alloc = RevenueShareAllocation.query.get_or_404(alloc_id)
        alloc.is_paid = not alloc.is_paid
        alloc.paid_at = datetime.now(timezone.utc) if alloc.is_paid else None
        db.session.commit()
        return jsonify(ok=True, is_paid=alloc.is_paid,
                       paid_at=alloc.paid_at.strftime("%d %b %Y") if alloc.paid_at else None)
    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


# ── Recipients ────────────────────────────────────────────────────────────────

@revenue_share_bp.route("/recipients")
@login_required
@_finance_required
def recipients():
    recs = RevenueShareRecipient.query.order_by(
        RevenueShareRecipient.is_active.desc(), RevenueShareRecipient.name
    ).all()
    return render_template("revenue_share/recipients.html", recipients=recs)


@revenue_share_bp.route("/recipients/save", methods=["POST"])
@login_required
@_finance_required
def save_recipient():
    try:
        rec_id = request.form.get("id", "").strip()
        name   = request.form.get("name", "").strip()
        if not name:
            return jsonify(ok=False, error="Name is required"), 400

        fields = dict(
            name           = name,
            account_name   = request.form.get("account_name",   "").strip() or None,
            account_number = request.form.get("account_number", "").strip() or None,
            bank_name      = request.form.get("bank_name",      "").strip() or None,
            description    = request.form.get("description",    "").strip() or None,
            is_active      = request.form.get("is_active", "1") == "1",
        )

        if rec_id:
            rec = RevenueShareRecipient.query.get_or_404(int(rec_id))
            for k, v in fields.items():
                setattr(rec, k, v)
        else:
            rec = RevenueShareRecipient(**fields)
            db.session.add(rec)

        db.session.commit()
        return jsonify(ok=True, id=rec.id, name=rec.name,
                       account_number=rec.account_number or "—",
                       bank_name=rec.bank_name or "—",
                       is_active=rec.is_active)

    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


@revenue_share_bp.route("/recipients/<int:rec_id>/delete", methods=["POST"])
@login_required
@_finance_required
def delete_recipient(rec_id):
    try:
        rec = RevenueShareRecipient.query.get_or_404(rec_id)
        if RevenueShareAllocation.query.filter_by(recipient_id=rec_id).first():
            return jsonify(ok=False,
                           error="Recipient is referenced in existing periods — deactivate instead."), 400
        db.session.delete(rec)
        db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500

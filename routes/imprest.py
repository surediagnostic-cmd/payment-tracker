"""Imprest & Expense management — branch floats funded via payments, drawn down
by expenses, with low-balance alerts, a who-did-what audit trail, and Zoho-format
CSV import/export."""
from datetime import datetime, timezone, date as date_type
from decimal import Decimal
from functools import wraps
import csv, io, re

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, Response)
from flask_login import login_required, current_user
from sqlalchemy import func

from app import db
from models import (
    ImprestAccount, Expense, ImprestActivityLog,
    Category, Branch, User,
    PaymentRequest, PaymentRequestItem,
)

imprest_bp = Blueprint("imprest", __name__, url_prefix="/imprest")


# ── Access control ────────────────────────────────────────────────────────────

def _imprest_view(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.can_use_imprest:
            flash("Access denied.", "error")
            return redirect(url_for("requests.dashboard"))
        return f(*args, **kwargs)
    return decorated


def _imprest_admin(f):
    """Manage the accounts themselves (create, set float) — MDS / accountant."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ("mds", "accountant"):
            flash("Accountant or MDS access required.", "error")
            return redirect(url_for("imprest.dashboard"))
        return f(*args, **kwargs)
    return decorated


# ── Helpers ───────────────────────────────────────────────────────────────────

def _num(v):
    v = (v or "").strip().replace(",", "")
    v = re.sub(r"[^\d.\-]", "", v)
    try:
        return Decimal(v) if v not in ("", "-", ".") else None
    except Exception:
        return None


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _bool(v):
    return (v or "").strip().lower() in ("true", "1", "yes", "y")


def _allowed_branch_ids():
    """None → all branches (mds/accountant); else the user's own branch ids."""
    if current_user.role in ("mds", "accountant"):
        return None
    return [b.id for b in current_user.branches]


def _visible_accounts():
    q = ImprestAccount.query.filter_by(is_active=True)
    allowed = _allowed_branch_ids()
    if allowed is not None:
        q = q.filter(ImprestAccount.branch_id.in_(allowed or [-1]))
    return q.order_by(ImprestAccount.name).all()


def _account_or_403(account_id):
    acc = ImprestAccount.query.get_or_404(account_id)
    allowed = _allowed_branch_ids()
    if allowed is not None and acc.branch_id not in allowed:
        return None
    return acc


def _can_edit_expense():
    # Cashiers can add but not edit/delete; managers/accountant/mds can.
    return current_user.role in ("mds", "accountant", "branch_manager")


def _log(action, account_id=None, expense_id=None, amount=None, detail=None):
    db.session.add(ImprestActivityLog(
        account_id=account_id, expense_id=expense_id,
        user_id=current_user.id if current_user.is_authenticated else None,
        action=action, amount=amount, detail=detail,
    ))


def _get_topup_category():
    cat = Category.query.filter(func.lower(Category.name) == "imprest top-up").first()
    if not cat:
        cat = Category(name="Imprest Top-Up", cost_type="overhead")
        db.session.add(cat)
        db.session.flush()
    return cat


# ── Dashboard ─────────────────────────────────────────────────────────────────

@imprest_bp.route("/")
@login_required
@_imprest_view
def dashboard():
    accounts = _visible_accounts()
    low = [a for a in accounts if a.is_low]
    total_released = sum(a.total_released for a in accounts)
    total_balance  = sum(a.balance for a in accounts)

    now = datetime.now(timezone.utc)
    exp_q = Expense.query.filter(
        func.extract("year", Expense.date) == now.year,
        func.extract("month", Expense.date) == now.month,
    )
    allowed = _allowed_branch_ids()
    if allowed is not None:
        exp_q = exp_q.filter(Expense.account_id.in_([a.id for a in accounts] or [-1]))
    spent_month = sum(float(e.amount or 0) for e in exp_q.all())

    act_q = ImprestActivityLog.query
    if allowed is not None:
        act_q = act_q.filter(ImprestActivityLog.account_id.in_([a.id for a in accounts] or [-1]))
    recent_activity = act_q.order_by(ImprestActivityLog.created_at.desc()).limit(12).all()

    return render_template("imprest/dashboard.html",
        accounts=accounts, low=low,
        total_released=total_released, total_balance=total_balance,
        spent_month=spent_month, recent_activity=recent_activity,
    )


# ── Accounts ──────────────────────────────────────────────────────────────────

@imprest_bp.route("/accounts")
@login_required
@_imprest_view
def accounts():
    return render_template("imprest/accounts.html",
        accounts=_visible_accounts(),
        branches=Branch.query.filter_by(is_active=True).order_by(Branch.name).all(),
        can_admin=(current_user.role in ("mds", "accountant")),
    )


@imprest_bp.route("/accounts/add", methods=["POST"])
@login_required
@_imprest_admin
def add_account():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Account name is required.", "error")
        return redirect(url_for("imprest.accounts"))
    acc = ImprestAccount(
        name=name,
        branch_id=request.form.get("branch_id", type=int) or None,
        account_code=(request.form.get("account_code") or "").strip() or None,
        float_amount=_num(request.form.get("float_amount")) or 0,
        low_threshold_pct=_num(request.form.get("low_threshold_pct")) or 20,
    )
    db.session.add(acc)
    db.session.commit()
    flash(f"Imprest account '{name}' created.", "success")
    return redirect(url_for("imprest.account_detail", account_id=acc.id))


@imprest_bp.route("/accounts/<int:account_id>/edit", methods=["POST"])
@login_required
@_imprest_admin
def edit_account(account_id):
    acc = ImprestAccount.query.get_or_404(account_id)
    acc.name = (request.form.get("name", acc.name) or acc.name).strip()
    acc.branch_id = request.form.get("branch_id", type=int) or None
    acc.account_code = (request.form.get("account_code") or "").strip() or None
    acc.float_amount = _num(request.form.get("float_amount")) or 0
    acc.low_threshold_pct = _num(request.form.get("low_threshold_pct")) or 20
    if request.form.get("is_active") is not None:
        acc.is_active = request.form.get("is_active") == "1"
    _log("account_edited", account_id=acc.id, detail=f"Float ₦{acc.float_amount}, threshold {acc.low_threshold_pct}%")
    db.session.commit()
    flash("Account updated.", "success")
    return redirect(url_for("imprest.account_detail", account_id=account_id))


@imprest_bp.route("/accounts/<int:account_id>")
@login_required
@_imprest_view
def account_detail(account_id):
    acc = _account_or_403(account_id)
    if not acc:
        flash("You don't have access to that account.", "error")
        return redirect(url_for("imprest.dashboard"))
    expenses = (Expense.query.filter_by(account_id=account_id)
                .order_by(Expense.date.desc(), Expense.id.desc()).limit(200).all())
    funding = [pr for pr in acc.funding_payments]
    funding.sort(key=lambda p: p.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    activity = (ImprestActivityLog.query.filter_by(account_id=account_id)
                .order_by(ImprestActivityLog.created_at.desc()).limit(50).all())
    return render_template("imprest/account_detail.html",
        acc=acc, expenses=expenses, funding=funding, activity=activity,
        categories=Category.query.filter_by(is_active=True).order_by(Category.name).all(),
        can_edit=_can_edit_expense(),
        can_topup=(current_user.role in ("mds", "accountant", "branch_manager")),
        can_admin=(current_user.role in ("mds", "accountant")),
        branches=Branch.query.filter_by(is_active=True).order_by(Branch.name).all(),
        today=date_type.today().isoformat(),
    )


@imprest_bp.route("/accounts/<int:account_id>/request-topup", methods=["POST"])
@login_required
@_imprest_view
def request_topup(account_id):
    if current_user.role not in ("mds", "accountant", "branch_manager"):
        flash("Only a branch manager or accountant can request a top-up.", "error")
        return redirect(url_for("imprest.account_detail", account_id=account_id))
    acc = _account_or_403(account_id)
    if not acc:
        flash("You don't have access to that account.", "error")
        return redirect(url_for("imprest.dashboard"))
    amount = _num(request.form.get("amount"))
    if not amount or amount <= 0:
        flash("Enter a valid top-up amount.", "error")
        return redirect(url_for("imprest.account_detail", account_id=account_id))

    branch = acc.branch
    branch_id = acc.branch_id or (branch.id if branch else 1)
    cat = _get_topup_category()
    ref = PaymentRequest.generate_reference(branch_id)
    pr = PaymentRequest(
        reference=ref, date=datetime.now(timezone.utc).date(), branch_id=branch_id,
        beneficiary_name=acc.name,
        beneficiary_account=acc.account_code or "—",
        beneficiary_bank=(branch.source_account if branch else None) or "—",
        requested_amount=amount, submitted_by=current_user.id, status="pending",
        imprest_account_id=acc.id,
    )
    db.session.add(pr)
    db.session.flush()
    db.session.add(PaymentRequestItem(
        request_id=pr.id, description=f"Imprest top-up — {acc.name}",
        category_id=cat.id, quantity=1, rate=amount, amount=amount,
    ))
    db.session.commit()
    flash(f"Top-up request {ref} for ₦{amount:,.0f} raised — awaiting MDS approval.", "success")
    return redirect(url_for("imprest.account_detail", account_id=account_id))


# ── Expenses ──────────────────────────────────────────────────────────────────

def _filtered_expense_query():
    q = Expense.query
    allowed = _allowed_branch_ids()
    if allowed is not None:
        acc_ids = [a.id for a in _visible_accounts()]
        q = q.filter(Expense.account_id.in_(acc_ids or [-1]))
    aid = request.args.get("account_id", type=int)
    if aid:
        q = q.filter(Expense.account_id == aid)
    cid = request.args.get("category_id", type=int)
    if cid:
        q = q.filter(Expense.category_id == cid)
    bid = request.args.get("branch_id", type=int)
    if bid:
        q = q.filter(Expense.branch_id == bid)
    df = _parse_date(request.args.get("date_from", ""))
    dt = _parse_date(request.args.get("date_to", ""))
    if df:
        q = q.filter(Expense.date >= df)
    if dt:
        q = q.filter(Expense.date <= dt)
    return q


@imprest_bp.route("/expenses")
@login_required
@_imprest_view
def expenses():
    rows = _filtered_expense_query().order_by(Expense.date.desc(), Expense.id.desc()).limit(500).all()
    total = sum(float(e.amount or 0) for e in rows)
    return render_template("imprest/expenses.html",
        expenses=rows, total=total,
        accounts=_visible_accounts(),
        categories=Category.query.filter_by(is_active=True).order_by(Category.name).all(),
        branches=Branch.query.filter_by(is_active=True).order_by(Branch.name).all(),
        can_edit=_can_edit_expense(),
        args=request.args,
        today=date_type.today().isoformat(),
    )


def _apply_expense_form(exp, form):
    exp.date        = _parse_date(form.get("date")) or exp.date or datetime.now(timezone.utc).date()
    exp.description = (form.get("description") or "").strip() or None
    exp.category_id = form.get("category_id", type=int) or None
    exp.vendor      = (form.get("vendor") or "").strip() or None
    exp.amount      = _num(form.get("amount")) or 0
    exp.total       = _num(form.get("total")) or exp.amount
    exp.reference   = (form.get("reference") or "").strip() or None
    exp.is_billable     = _bool(form.get("is_billable"))
    exp.is_reimbursable = _bool(form.get("is_reimbursable"))
    exp.vendor      = (form.get("vendor") or "").strip() or None


@imprest_bp.route("/expenses/add", methods=["POST"])
@login_required
@_imprest_view
def add_expense():
    account_id = request.form.get("account_id", type=int)
    acc = _account_or_403(account_id) if account_id else None
    if not acc:
        flash("Choose a valid imprest account you have access to.", "error")
        return redirect(request.referrer or url_for("imprest.expenses"))
    amount = _num(request.form.get("amount"))
    if not amount or amount <= 0:
        flash("Enter a valid expense amount.", "error")
        return redirect(request.referrer or url_for("imprest.account_detail", account_id=account_id))

    exp = Expense(account_id=acc.id, branch_id=acc.branch_id, currency="NGN",
                  created_by=current_user.id)
    _apply_expense_form(exp, request.form)
    db.session.add(exp)
    db.session.flush()
    _log("expense_added", account_id=acc.id, expense_id=exp.id, amount=exp.amount,
         detail=f"{exp.description or 'Expense'} — ₦{exp.amount:,.0f}")
    db.session.commit()
    flash(f"Expense of ₦{amount:,.0f} recorded.", "success")
    return redirect(request.referrer or url_for("imprest.account_detail", account_id=account_id))


@imprest_bp.route("/expenses/<int:expense_id>/edit", methods=["POST"])
@login_required
@_imprest_view
def edit_expense(expense_id):
    exp = Expense.query.get_or_404(expense_id)
    acc = _account_or_403(exp.account_id)
    if not acc or not _can_edit_expense():
        flash("You can't edit this expense.", "error")
        return redirect(url_for("imprest.account_detail", account_id=exp.account_id))
    _apply_expense_form(exp, request.form)
    exp.updated_by = current_user.id
    _log("expense_edited", account_id=exp.account_id, expense_id=exp.id, amount=exp.amount,
         detail=f"Edited — ₦{exp.amount:,.0f}")
    db.session.commit()
    flash("Expense updated.", "success")
    return redirect(request.referrer or url_for("imprest.account_detail", account_id=exp.account_id))


@imprest_bp.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@login_required
@_imprest_view
def delete_expense(expense_id):
    exp = Expense.query.get_or_404(expense_id)
    acc = _account_or_403(exp.account_id)
    if not acc or not _can_edit_expense():
        flash("You can't delete this expense.", "error")
        return redirect(url_for("imprest.account_detail", account_id=exp.account_id))
    account_id = exp.account_id
    _log("expense_deleted", account_id=account_id, amount=exp.amount,
         detail=f"Deleted — {exp.description or 'Expense'} ₦{exp.amount:,.0f}")
    db.session.delete(exp)
    db.session.commit()
    flash("Expense deleted.", "success")
    return redirect(request.referrer or url_for("imprest.account_detail", account_id=account_id))


# ── Zoho-format export / import ───────────────────────────────────────────────

ZOHO_HEADER = [
    "Expense Date", "Expense Description", "Expense Account", "Expense Account Code",
    "Paid Through", "Paid Through Account Code", "Vendor", "Company ID", "Location Name",
    "Project Name", "Entry Number", "Currency Code", "Exchange Rate", "Is Inclusive Tax",
    "Mileage Rate", "Mileage Unit", "Distance", "Start Odometer Reading", "End Odometer Reading",
    "Mileage Type", "Vehicle Name", "Claimant Email", "Tax Name", "Tax Percentage", "Tax Type",
    "Tax Amount", "Expense Amount", "Total", "Reference#", "Is Billable", "Customer Name",
    "Expense Reference ID", "Recurrence Name", "ExpenseReport Name", "Is Reimbursable",
    "LINEITEM.TAG.Branch", "LINEITEM.TAG.Case Type", "LINEITEM.TAG.Clients Categories",
    "CF.Unit", "CF.Sure Ilesha Branches", "CF.Branch",
]


def _expense_to_zoho_row(e):
    d = e.date.strftime("%Y-%m-%d") if e.date else ""
    branch = e.branch.name if e.branch else ""
    return [
        d, e.description or "", (e.category.name if e.category else ""), "",
        (e.account.name if e.account else ""), (e.account.account_code if e.account else "") or "",
        e.vendor or "", "", (e.account.branch.name if e.account and e.account.branch else branch),
        "", e.entry_number or "", e.currency or "NGN", str(e.exchange_rate or 1),
        "true" if e.is_inclusive_tax else "false",
        "0.00", "", "", "", "", "NonMileage", "", e.claimant_email or "",
        e.tax_name or "", str(e.tax_percentage or 0), e.tax_type or "ItemAmount",
        f"{float(e.tax_amount or 0):.6f}", f"{float(e.amount or 0):.6f}", f"{float(e.total or e.amount or 0):.3f}",
        e.reference or "", "true" if e.is_billable else "false", e.customer_name or "",
        e.expense_reference_id or "", e.recurrence_name or "", e.expense_report_name or "",
        "true" if e.is_reimbursable else "false",
        e.tag_case_type or "", e.tag_case_type or "", e.tag_client_category or "",
        e.cf_unit or "", branch, branch,
    ]


@imprest_bp.route("/expenses/export")
@login_required
@_imprest_view
def export_expenses():
    rows = _filtered_expense_query().order_by(Expense.date.asc(), Expense.id.asc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ZOHO_HEADER)
    for e in rows:
        w.writerow(_expense_to_zoho_row(e))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=expenses_export.csv"})


@imprest_bp.route("/expenses/import", methods=["POST"])
@login_required
@_imprest_admin
def import_expenses():
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("imprest.expenses"))
    try:
        raw = f.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(raw))

        cats = {c.name.strip().lower(): c for c in Category.query.all()}
        accts = {a.name.strip().lower(): a for a in ImprestAccount.query.all()}
        branches = {b.name.strip().lower(): b for b in Branch.query.all()}
        existing = {e.expense_reference_id: e for e in Expense.query.filter(
            Expense.expense_reference_id.isnot(None)).all()}

        added = updated = skipped = 0
        for row in reader:
            g = lambda k: (row.get(k) or "").strip()
            amt = _num(g("Expense Amount"))
            acc_name = g("Paid Through")
            if not amt or not acc_name:
                skipped += 1
                continue
            acc = accts.get(acc_name.lower())
            if not acc:
                skipped += 1
                continue
            # category (expense account) — create if missing
            cname = g("Expense Account")
            cat = cats.get(cname.lower()) if cname else None
            if cname and not cat:
                cat = Category(name=cname, cost_type="overhead")
                db.session.add(cat); db.session.flush()
                cats[cname.lower()] = cat
            # branch from CF.Branch / Location Name
            bname = g("CF.Branch") or g("Location Name")
            branch = branches.get(bname.lower()) if bname else None
            bid = branch.id if branch else acc.branch_id

            ref_id = g("Expense Reference ID") or None
            exp = existing.get(ref_id) if ref_id else None
            is_new = exp is None
            if is_new:
                exp = Expense(account_id=acc.id)
                db.session.add(exp)
            exp.account_id = acc.id
            exp.branch_id = bid
            exp.date = _parse_date(g("Expense Date")) or datetime.now(timezone.utc).date()
            exp.description = g("Expense Description") or None
            exp.category_id = cat.id if cat else None
            exp.vendor = g("Vendor") or None
            exp.amount = amt
            exp.total = _num(g("Total")) or amt
            exp.tax_name = g("Tax Name") or None
            exp.tax_percentage = _num(g("Tax Percentage"))
            exp.tax_amount = _num(g("Tax Amount"))
            exp.tax_type = g("Tax Type") or None
            exp.is_inclusive_tax = _bool(g("Is Inclusive Tax"))
            exp.reference = g("Reference#") or None
            exp.entry_number = g("Entry Number") or None
            exp.currency = g("Currency Code") or "NGN"
            exp.exchange_rate = _num(g("Exchange Rate")) or 1
            exp.is_billable = _bool(g("Is Billable"))
            exp.is_reimbursable = _bool(g("Is Reimbursable"))
            exp.customer_name = g("Customer Name") or None
            exp.recurrence_name = g("Recurrence Name") or None
            exp.expense_report_name = g("ExpenseReport Name") or None
            exp.expense_reference_id = ref_id
            exp.claimant_email = g("Claimant Email") or None
            exp.tag_case_type = g("LINEITEM.TAG.Case Type") or None
            exp.tag_client_category = g("LINEITEM.TAG.Clients Categories") or None
            exp.cf_unit = g("CF.Unit") or None
            if is_new:
                exp.created_by = current_user.id
                added += 1
            else:
                exp.updated_by = current_user.id
                updated += 1
            if ref_id:
                existing[ref_id] = exp

        db.session.commit()
        flash(f"Import complete — {added} added, {updated} updated, {skipped} skipped.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Import failed: {e}", "error")
    return redirect(url_for("imprest.expenses"))


# ── Guide ─────────────────────────────────────────────────────────────────────

@imprest_bp.route("/guide")
@login_required
@_imprest_view
def guide():
    return render_template("imprest/guide.html")

import io
import re
from datetime import datetime, timezone, date as date_type
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort
)
from flask_login import login_required, current_user
from app import db
from models import (
    Branch, Category, ImprestAccount,
    DailyReport, DailyRevenueEntry, DailyPaymentMode, DailyExpenseEntry,
)

clinic_report_bp = Blueprint("clinic_report", __name__, url_prefix="/clinic-report")

SERVICES = ["Lab", "Scan", "X-Ray", "ECG", "Other"]
MODES    = ["Cash", "POS", "Transfer"]


def _branch_ids_for_user():
    """Return list of branch IDs the current user may see, or None for MDS (all)."""
    if current_user.is_mds:
        return None
    return [b.id for b in current_user.branches]


def _guard():
    if not current_user.can_use_daily_report:
        abort(403)


# ── List ──────────────────────────────────────────────────────────────────────

@clinic_report_bp.route("/")
@login_required
def list_reports():
    _guard()
    branch_ids = _branch_ids_for_user()

    q = DailyReport.query.order_by(DailyReport.report_date.desc())
    if branch_ids is not None:
        q = q.filter(DailyReport.branch_id.in_(branch_ids))

    # Filter by branch
    branch_filter = request.args.get("branch_id", type=int)
    if branch_filter:
        if branch_ids is None or branch_filter in branch_ids:
            q = q.filter(DailyReport.branch_id == branch_filter)

    # Filter by month
    month_filter = request.args.get("month", "")
    if month_filter:
        try:
            yr, mo = month_filter.split("-")
            q = q.filter(
                db.extract("year",  DailyReport.report_date) == int(yr),
                db.extract("month", DailyReport.report_date) == int(mo),
            )
        except Exception:
            pass

    reports = q.limit(120).all()

    branches = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    if branch_ids is not None:
        branches = [b for b in branches if b.id in branch_ids]

    return render_template(
        "clinic_report/list.html",
        reports=reports,
        branches=branches,
        branch_filter=branch_filter,
        month_filter=month_filter,
    )


# ── New / Edit ────────────────────────────────────────────────────────────────

@clinic_report_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_report():
    _guard()
    branch_ids = _branch_ids_for_user()

    branches = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    if branch_ids is not None:
        branches = [b for b in branches if b.id in branch_ids]

    categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()

    if request.method == "POST":
        try:
            branch_id    = int(request.form["branch_id"])
            report_date  = datetime.strptime(request.form["report_date"], "%Y-%m-%d").date()
            patient_count = request.form.get("patient_count", "").strip()
            patient_count = int(patient_count) if patient_count else None
            notes = request.form.get("notes", "").strip() or None

            # Guard — non-MDS can only submit for their branches
            if branch_ids is not None and branch_id not in branch_ids:
                flash("You do not have access to that branch.", "error")
                return redirect(url_for("clinic_report.new_report"))

            # Duplicate check
            existing = DailyReport.query.filter_by(
                branch_id=branch_id, report_date=report_date
            ).first()
            if existing:
                flash(
                    f"A report for that branch and date already exists. "
                    f"<a href='{url_for('clinic_report.view_report', report_id=existing.id)}' "
                    f"style='color:var(--orange2);text-decoration:underline;'>View it here.</a>",
                    "warning",
                )
                return redirect(url_for("clinic_report.new_report"))

            report = DailyReport(
                branch_id=branch_id,
                report_date=report_date,
                patient_count=patient_count,
                notes=notes,
                status="submitted",
                submitted_by=current_user.id,
                submitted_at=datetime.now(timezone.utc),
            )
            db.session.add(report)
            db.session.flush()  # get report.id

            # Revenue entries
            services = request.form.getlist("service[]")
            rev_amounts = request.form.getlist("rev_amount[]")
            for svc, amt_str in zip(services, rev_amounts):
                svc = svc.strip()
                amt_str = amt_str.strip().replace(",", "")
                if svc and amt_str:
                    try:
                        amt = float(amt_str)
                        if amt > 0:
                            db.session.add(DailyRevenueEntry(
                                report_id=report.id, service=svc, amount=amt
                            ))
                    except ValueError:
                        pass

            # Payment modes
            modes = request.form.getlist("mode[]")
            mode_amounts = request.form.getlist("mode_amount[]")
            for mode, amt_str in zip(modes, mode_amounts):
                mode = mode.strip()
                amt_str = amt_str.strip().replace(",", "")
                if mode and amt_str:
                    try:
                        amt = float(amt_str)
                        if amt > 0:
                            db.session.add(DailyPaymentMode(
                                report_id=report.id, mode=mode, amount=amt
                            ))
                    except ValueError:
                        pass

            # Expense entries
            exp_descs      = request.form.getlist("exp_desc[]")
            exp_amounts    = request.form.getlist("exp_amount[]")
            exp_cats       = request.form.getlist("exp_category_id[]")
            exp_imprests   = request.form.getlist("exp_imprest_id[]")
            exp_notes_list = request.form.getlist("exp_notes[]")

            for i, (desc, amt_str) in enumerate(zip(exp_descs, exp_amounts)):
                desc = desc.strip()
                amt_str = amt_str.strip().replace(",", "")
                if not desc or not amt_str:
                    continue
                try:
                    amt = float(amt_str)
                    if amt <= 0:
                        continue
                except ValueError:
                    continue

                cat_id = None
                if i < len(exp_cats) and exp_cats[i]:
                    try:
                        cat_id = int(exp_cats[i])
                    except ValueError:
                        pass

                imp_id = None
                if i < len(exp_imprests) and exp_imprests[i]:
                    try:
                        imp_id = int(exp_imprests[i])
                    except ValueError:
                        pass

                exp_note = (exp_notes_list[i].strip() if i < len(exp_notes_list) else "") or None

                db.session.add(DailyExpenseEntry(
                    report_id=report.id,
                    description=desc,
                    category_id=cat_id,
                    imprest_account_id=imp_id,
                    amount=amt,
                    notes=exp_note,
                    status="pending",
                ))

            db.session.commit()
            flash("Daily report submitted successfully.", "success")
            return redirect(url_for("clinic_report.view_report", report_id=report.id))

        except Exception as exc:
            db.session.rollback()
            flash(f"Error saving report: {exc}", "error")

    today = date_type.today().strftime("%Y-%m-%d")

    # Imprest accounts scoped to user's branches
    imp_q = ImprestAccount.query.filter_by(is_active=True).order_by(ImprestAccount.name)
    if branch_ids is not None:
        imp_q = imp_q.filter(ImprestAccount.branch_id.in_(branch_ids + [None]))
    imprest_accounts = imp_q.all()

    return render_template(
        "clinic_report/new.html",
        branches=branches,
        categories=categories,
        imprest_accounts=imprest_accounts,
        services=SERVICES,
        modes=MODES,
        today=today,
    )


# ── View / Approve ────────────────────────────────────────────────────────────

@clinic_report_bp.route("/<int:report_id>")
@login_required
def view_report(report_id):
    _guard()
    report = DailyReport.query.get_or_404(report_id)

    branch_ids = _branch_ids_for_user()
    if branch_ids is not None and report.branch_id not in branch_ids:
        abort(403)

    categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()
    imprest_accounts = ImprestAccount.query.filter_by(is_active=True).order_by(ImprestAccount.name).all()

    return render_template(
        "clinic_report/view.html",
        report=report,
        categories=categories,
        imprest_accounts=imprest_accounts,
    )


@clinic_report_bp.route("/<int:report_id>/expense/<int:exp_id>/action", methods=["POST"])
@login_required
def expense_action(report_id, exp_id):
    if not current_user.can_approve_daily_expense:
        abort(403)

    report  = DailyReport.query.get_or_404(report_id)
    expense = DailyExpenseEntry.query.get_or_404(exp_id)
    if expense.report_id != report_id:
        abort(400)

    branch_ids = _branch_ids_for_user()
    if branch_ids is not None and report.branch_id not in branch_ids:
        abort(403)

    action = request.form.get("action")  # "approve" or "reject"

    if action == "approve":
        raw = request.form.get("approved_amount", "").strip().replace(",", "")
        try:
            approved_amt = float(raw) if raw else float(expense.amount)
        except ValueError:
            approved_amt = float(expense.amount)
        expense.status          = "approved"
        expense.approved_amount = approved_amt
        expense.approved_by     = current_user.id
        expense.approved_at     = datetime.now(timezone.utc)
        db.session.commit()
        flash(f"Expense approved: ₦{approved_amt:,.0f}", "success")

    elif action == "reject":
        expense.status      = "rejected"
        expense.approved_by = current_user.id
        expense.approved_at = datetime.now(timezone.utc)
        db.session.commit()
        flash("Expense rejected.", "warning")

    return redirect(url_for("clinic_report.view_report", report_id=report_id))


@clinic_report_bp.route("/<int:report_id>/approve-report", methods=["POST"])
@login_required
def approve_report(report_id):
    if not current_user.can_approve_daily_expense:
        abort(403)

    report = DailyReport.query.get_or_404(report_id)
    branch_ids = _branch_ids_for_user()
    if branch_ids is not None and report.branch_id not in branch_ids:
        abort(403)

    # Auto-approve any still-pending expenses at their claimed amount
    for exp in report.expense_entries:
        if exp.status == "pending":
            exp.status          = "approved"
            exp.approved_amount = exp.amount
            exp.approved_by     = current_user.id
            exp.approved_at     = datetime.now(timezone.utc)

    report.status      = "approved"
    report.approved_by = current_user.id
    report.approved_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Report approved. All pending expenses auto-approved at claimed amounts.", "success")
    return redirect(url_for("clinic_report.view_report", report_id=report_id))


# ── Excel Upload ─────────────────────────────────────────────────────────────

def _parse_ledger_excel(file_bytes):
    """Parse the Branch Daily Ledger Report Excel format.

    Layout:
      Row 1, Col A : 'DAILY EXPENSE & PATIENT REPORT  (BranchName)'
      Row 2        : 'EXPENSES', None, None, 'Total No. of Patient', <n>
      Rows below   : Col A/B = expense items | Col D/E = revenue / payment modes
    Returns dict with keys: branch_hint, patient_count, expenses, revenue, payment_modes
    """
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active

    rows = [tuple(row) for row in ws.iter_rows(values_only=True)]

    result = {
        "branch_hint": None,
        "patient_count": None,
        "expenses": [],       # [(description, amount)]
        "revenue": [],        # [(service, amount)]
        "payment_modes": [],  # [(mode, amount)]
        "errors": [],
    }

    if not rows:
        result["errors"].append("File appears to be empty.")
        return result

    # Row 1 — branch hint
    first_cell = str(rows[0][0] or "")
    m = re.search(r'\(([^)]+)\)', first_cell)
    if m:
        result["branch_hint"] = m.group(1).strip()

    SKIP_LEFT = {"total", "expenses", "item", ""}
    SKIP_RIGHT = {"total", "service", "amount", "mode", "revenue by service",
                  "payment mode breakdown", "payment modes"}

    right_section = None   # "revenue" or "payment_modes"

    for row in rows[1:]:
        # Pad to 5 columns
        r = list(row) + [None] * 5
        a, b, _c, d, e = r[0], r[1], r[2], r[3], r[4]

        a_str = str(a).strip() if a is not None else ""
        d_str = str(d).strip() if d is not None else ""

        # Patient count
        if "patient" in d_str.lower() and isinstance(e, (int, float)):
            result["patient_count"] = int(e)

        # Right-side section detection
        if d_str.upper() == "REVENUE BY SERVICE":
            right_section = "revenue"
            continue
        if d_str.upper() in ("PAYMENT MODE BREAKDOWN", "PAYMENT MODES"):
            right_section = "payment_modes"
            continue

        # Right-side data rows
        if right_section and d_str and d_str.lower() not in SKIP_RIGHT:
            if isinstance(e, (int, float)) and e > 0:
                result[right_section].append((d_str, float(e)))

        # Left-side expense rows
        if a_str and a_str.lower() not in SKIP_LEFT:
            # Skip long footnote strings
            if len(a_str) < 120 and isinstance(b, (int, float)) and b > 0:
                result["expenses"].append((a_str, float(b)))

    if not result["revenue"] and not result["expenses"]:
        result["errors"].append(
            "No revenue or expense data found. Check that the file matches the expected format."
        )

    return result


@clinic_report_bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload_report():
    _guard()
    branch_ids = _branch_ids_for_user()

    branches = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    if branch_ids is not None:
        branches = [b for b in branches if b.id in branch_ids]

    categories = Category.query.filter_by(is_active=True).order_by(Category.name).all()

    imp_q = ImprestAccount.query.filter_by(is_active=True).order_by(ImprestAccount.name)
    if branch_ids is not None:
        imp_q = imp_q.filter(ImprestAccount.branch_id.in_(branch_ids + [None]))
    imprest_accounts = imp_q.all()

    parsed = None
    branch_hint_id = None

    if request.method == "POST":
        action = request.form.get("action", "parse")

        # ── Step 1: parse the file and show preview ───────────────────────────
        if action == "parse":
            f = request.files.get("ledger_file")
            if not f or not f.filename:
                flash("Please select an Excel file to upload.", "error")
                return render_template("clinic_report/upload.html",
                                       branches=branches, categories=categories,
                                       imprest_accounts=imprest_accounts,
                                       parsed=None, branch_hint_id=None,
                                       today=date_type.today().strftime("%Y-%m-%d"))

            filename = f.filename.lower()
            if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
                flash("Only .xlsx / .xls files are supported.", "error")
                return render_template("clinic_report/upload.html",
                                       branches=branches, categories=categories,
                                       imprest_accounts=imprest_accounts,
                                       parsed=None, branch_hint_id=None,
                                       today=date_type.today().strftime("%Y-%m-%d"))

            file_bytes = f.read()
            try:
                parsed = _parse_ledger_excel(file_bytes)
            except Exception as exc:
                flash(f"Could not read file: {exc}", "error")
                return render_template("clinic_report/upload.html",
                                       branches=branches, categories=categories,
                                       imprest_accounts=imprest_accounts,
                                       parsed=None, branch_hint_id=None,
                                       today=date_type.today().strftime("%Y-%m-%d"))

            if parsed["errors"]:
                for err in parsed["errors"]:
                    flash(err, "warning")

            # Try to match branch hint to a known branch
            if parsed["branch_hint"]:
                hint_lower = parsed["branch_hint"].lower()
                for b in branches:
                    if hint_lower in b.name.lower() or b.name.lower() in hint_lower:
                        branch_hint_id = b.id
                        break

            return render_template("clinic_report/upload.html",
                                   branches=branches, categories=categories,
                                   imprest_accounts=imprest_accounts,
                                   parsed=parsed, branch_hint_id=branch_hint_id,
                                   today=date_type.today().strftime("%Y-%m-%d"))

        # ── Step 2: confirm & save ────────────────────────────────────────────
        elif action == "save":
            try:
                branch_id    = int(request.form["branch_id"])
                report_date  = datetime.strptime(request.form["report_date"], "%Y-%m-%d").date()
                pc_raw       = request.form.get("patient_count", "").strip()
                patient_count = int(pc_raw) if pc_raw else None
                notes        = request.form.get("notes", "").strip() or None

                if branch_ids is not None and branch_id not in branch_ids:
                    flash("You do not have access to that branch.", "error")
                    return redirect(url_for("clinic_report.upload_report"))

                existing = DailyReport.query.filter_by(
                    branch_id=branch_id, report_date=report_date
                ).first()
                if existing:
                    flash(
                        f"A report for that branch and date already exists. "
                        f"<a href='{url_for('clinic_report.view_report', report_id=existing.id)}' "
                        f"style='color:var(--orange2);text-decoration:underline;'>View it here.</a>",
                        "warning",
                    )
                    return redirect(url_for("clinic_report.upload_report"))

                report = DailyReport(
                    branch_id=branch_id,
                    report_date=report_date,
                    patient_count=patient_count,
                    notes=notes,
                    status="submitted",
                    submitted_by=current_user.id,
                    submitted_at=datetime.now(timezone.utc),
                )
                db.session.add(report)
                db.session.flush()

                # Revenue
                services = request.form.getlist("service[]")
                rev_amounts = request.form.getlist("rev_amount[]")
                for svc, amt_str in zip(services, rev_amounts):
                    svc = svc.strip()
                    amt_str = amt_str.strip().replace(",", "")
                    if svc and amt_str:
                        try:
                            amt = float(amt_str)
                            if amt > 0:
                                db.session.add(DailyRevenueEntry(
                                    report_id=report.id, service=svc, amount=amt
                                ))
                        except ValueError:
                            pass

                # Payment modes
                modes = request.form.getlist("mode[]")
                mode_amounts = request.form.getlist("mode_amount[]")
                for mode, amt_str in zip(modes, mode_amounts):
                    mode = mode.strip()
                    amt_str = amt_str.strip().replace(",", "")
                    if mode and amt_str:
                        try:
                            amt = float(amt_str)
                            if amt > 0:
                                db.session.add(DailyPaymentMode(
                                    report_id=report.id, mode=mode, amount=amt
                                ))
                        except ValueError:
                            pass

                # Expenses
                exp_descs      = request.form.getlist("exp_desc[]")
                exp_amounts    = request.form.getlist("exp_amount[]")
                exp_cats       = request.form.getlist("exp_category_id[]")
                exp_imprests   = request.form.getlist("exp_imprest_id[]")
                exp_notes_list = request.form.getlist("exp_notes[]")

                for i, (desc, amt_str) in enumerate(zip(exp_descs, exp_amounts)):
                    desc = desc.strip()
                    amt_str = amt_str.strip().replace(",", "")
                    if not desc or not amt_str:
                        continue
                    try:
                        amt = float(amt_str)
                        if amt <= 0:
                            continue
                    except ValueError:
                        continue

                    cat_id = None
                    if i < len(exp_cats) and exp_cats[i]:
                        try:
                            cat_id = int(exp_cats[i])
                        except ValueError:
                            pass

                    imp_id = None
                    if i < len(exp_imprests) and exp_imprests[i]:
                        try:
                            imp_id = int(exp_imprests[i])
                        except ValueError:
                            pass

                    exp_note = (exp_notes_list[i].strip() if i < len(exp_notes_list) else "") or None

                    db.session.add(DailyExpenseEntry(
                        report_id=report.id,
                        description=desc,
                        category_id=cat_id,
                        imprest_account_id=imp_id,
                        amount=amt,
                        notes=exp_note,
                        status="pending",
                    ))

                db.session.commit()
                flash(
                    f"Report imported from Excel: "
                    f"{len(services)} revenue lines, {len(exp_descs)} expenses.",
                    "success",
                )
                return redirect(url_for("clinic_report.view_report", report_id=report.id))

            except Exception as exc:
                db.session.rollback()
                flash(f"Error saving report: {exc}", "error")

    today = date_type.today().strftime("%Y-%m-%d")
    return render_template("clinic_report/upload.html",
                           branches=branches, categories=categories,
                           imprest_accounts=imprest_accounts,
                           parsed=None, branch_hint_id=None,
                           today=today)


# ── Consolidated (MD view) ────────────────────────────────────────────────────

@clinic_report_bp.route("/consolidated")
@login_required
def consolidated():
    _guard()

    month_filter = request.args.get("month", date_type.today().strftime("%Y-%m"))
    try:
        yr, mo = month_filter.split("-")
        yr, mo = int(yr), int(mo)
    except Exception:
        yr, mo = date_type.today().year, date_type.today().month

    branch_ids = _branch_ids_for_user()
    q = DailyReport.query.filter(
        db.extract("year",  DailyReport.report_date) == yr,
        db.extract("month", DailyReport.report_date) == mo,
    )
    if branch_ids is not None:
        q = q.filter(DailyReport.branch_id.in_(branch_ids))

    reports = q.order_by(DailyReport.report_date.desc(), DailyReport.branch_id).all()

    branches = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    if branch_ids is not None:
        branches = [b for b in branches if b.id in branch_ids]

    return render_template(
        "clinic_report/consolidated.html",
        reports=reports,
        branches=branches,
        month_filter=month_filter,
        yr=yr, mo=mo,
    )

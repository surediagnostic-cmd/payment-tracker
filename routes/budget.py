"""Budget planning routes.

Hierarchy (Option B — linked periods):
  Monthly  → primary editable input (stored in DB, period_type='monthly')
  Weekly   → derived: monthly ÷ 4, shown per week with real actuals (read-only)
  Yearly   → derived: sum of all 12 months' budgets + full-year actuals (read-only)

Only monthly rows are stored in the DB.
Changing any monthly figure instantly flows into weekly and yearly views.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import calendar

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func, extract

from app import db
from models import Branch, Category, Budget, PaymentRequest, PaymentRequestItem, ProjectedIncome, BudgetLineItem

budget_bp = Blueprint("budget", __name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _allowed_branch_ids():
    if current_user.is_mds:
        return [b.id for b in Branch.query.filter_by(is_active=True).all()]
    return [b.id for b in current_user.branches]


def _week_day_range(week: int):
    return {1: (1, 7), 2: (8, 14), 3: (15, 21), 4: (22, 31)}.get(week, (1, 31))


def _get_actuals(branch_id, category_id, year, month=None, week=None):
    q = (
        db.session.query(func.coalesce(func.sum(PaymentRequestItem.amount), 0))
        .join(PaymentRequest, PaymentRequestItem.request_id == PaymentRequest.id)
        .filter(
            PaymentRequest.branch_id == branch_id,
            PaymentRequestItem.category_id == category_id,
            PaymentRequest.status == "approved",
            extract("year", PaymentRequest.date) == year,
        )
    )
    if month:
        q = q.filter(extract("month", PaymentRequest.date) == month)
    if week and month:
        s, e = _week_day_range(week)
        q = q.filter(
            extract("day", PaymentRequest.date) >= s,
            extract("day", PaymentRequest.date) <= e,
        )
    return Decimal(str(q.scalar() or 0))


def _monthly_budget(branch_id, category_id, year, month):
    return Budget.query.filter_by(
        branch_id=branch_id, category_id=category_id,
        period_type="monthly", year=year, month=month, week=None
    ).first()


def _yearly_budget_amount(branch_id, category_id, year):
    """Sum of all 12 monthly budgets — derived, never stored."""
    total = db.session.query(
        func.coalesce(func.sum(Budget.amount), 0)
    ).filter_by(
        branch_id=branch_id, category_id=category_id,
        period_type="monthly", year=year
    ).scalar()
    return Decimal(str(total or 0))


def _branch_totals(branches, categories, grid):
    totals = {}
    for br in branches:
        b = sum(grid[br.id][c.id]["budgeted"] for c in categories)
        a = sum(grid[br.id][c.id]["actual"]   for c in categories)
        totals[br.id] = dict(
            budgeted=b, actual=a, remaining=b - a,
            pct=int(a / b * 100) if b > 0 else None,
            over=a > b and b > 0,
        )
    return totals


# ── projected income helpers ─────────────────────────────────────────────────

def _get_income_map(branch_ids, year, month=None):
    """Return {branch_id: ProjectedIncome} for the given period."""
    q = ProjectedIncome.query.filter(
        ProjectedIncome.branch_id.in_(branch_ids),
        ProjectedIncome.year == year,
    )
    if month:
        q = q.filter(ProjectedIncome.month == month)
    else:
        q = q.filter(ProjectedIncome.month.is_(None))
    return {pi.branch_id: pi for pi in q.all()}


# ── grid builders ─────────────────────────────────────────────────────────────

def _bulk_budgets(branch_ids, category_ids, year):
    """Fetch all monthly budgets for the given branches/categories/year in ONE query.
    Returns dict keyed by (branch_id, category_id, month) → Budget object."""
    rows = Budget.query.filter(
        Budget.branch_id.in_(branch_ids),
        Budget.category_id.in_(category_ids),
        Budget.period_type == "monthly",
        Budget.year == year,
        Budget.week.is_(None),
    ).all()
    return {(b.branch_id, b.category_id, b.month): b for b in rows}


def _bulk_actuals(branch_ids, category_ids, year, month=None):
    """Fetch approved spending grouped by (branch, category, month).
    Returns dict keyed by (branch_id, category_id, month_int) → Decimal.

    Uses round() on the extracted month float so PostgreSQL double-precision
    results like 5.9999… never land in the wrong month bucket.
    """
    q = (
        db.session.query(
            PaymentRequest.branch_id,
            PaymentRequestItem.category_id,
            extract("month", PaymentRequest.date).label("mo"),
            func.sum(PaymentRequestItem.amount).label("total"),
        )
        .join(PaymentRequest, PaymentRequestItem.request_id == PaymentRequest.id)
        .filter(
            PaymentRequest.branch_id.in_(branch_ids),
            PaymentRequestItem.category_id.in_(category_ids),
            PaymentRequest.status == "approved",
            extract("year", PaymentRequest.date) == year,
        )
        .group_by(
            PaymentRequest.branch_id,
            PaymentRequestItem.category_id,
            extract("month", PaymentRequest.date),
        )
    )
    if month:
        q = q.filter(extract("month", PaymentRequest.date) == month)
    return {
        (int(r.branch_id), int(r.category_id), int(round(float(r.mo)))): Decimal(str(r.total or 0))
        for r in q.all()
    }


def _build_monthly_grid(branches, categories, year, month):
    """2 bulk queries total regardless of number of branches/categories."""
    bids = [br.id for br in branches]
    cids = [cat.id for cat in categories]
    bmap = _bulk_budgets(bids, cids, year)
    amap = _bulk_actuals(bids, cids, year, month)

    grid = {}
    for br in branches:
        grid[br.id] = {}
        for cat in categories:
            bobj      = bmap.get((br.id, cat.id, month))
            budgeted  = Decimal(str(bobj.amount)) if bobj else Decimal("0")
            actual    = amap.get((br.id, cat.id, month), Decimal("0"))
            remaining = budgeted - actual
            pct = int(actual / budgeted * 100) if budgeted > 0 else None
            grid[br.id][cat.id] = dict(
                budget_id=bobj.id if bobj else None,
                budgeted=budgeted, actual=actual,
                remaining=remaining, pct=pct,
                over=actual > budgeted and budgeted > 0,
                warn=pct is not None and pct >= 80,
                notes=bobj.notes if bobj else "",
            )
    return grid


def _build_yearly_grid(branches, categories, year):
    """4 fast queries — totals via direct DB GROUP BY, months_data from per-month maps."""
    bids = [br.id for br in branches]
    cids = [cat.id for cat in categories]

    # ── Query 1: yearly BUDGET totals per branch+category (DB does the SUM) ──
    budget_total_rows = (
        db.session.query(
            Budget.branch_id,
            Budget.category_id,
            func.sum(Budget.amount).label("total"),
        )
        .filter(
            Budget.branch_id.in_(bids),
            Budget.category_id.in_(cids),
            Budget.period_type == "monthly",
            Budget.year == year,
            Budget.month.isnot(None),
            Budget.week.is_(None),
        )
        .group_by(Budget.branch_id, Budget.category_id)
        .all()
    )
    budget_totals = {
        (int(r.branch_id), int(r.category_id)): Decimal(str(r.total or 0))
        for r in budget_total_rows
    }

    # ── Query 2: yearly ACTUAL totals per branch+category (DB does the SUM) ──
    actual_total_rows = (
        db.session.query(
            PaymentRequest.branch_id,
            PaymentRequestItem.category_id,
            func.sum(PaymentRequestItem.amount).label("total"),
        )
        .join(PaymentRequest, PaymentRequestItem.request_id == PaymentRequest.id)
        .filter(
            PaymentRequest.branch_id.in_(bids),
            PaymentRequestItem.category_id.in_(cids),
            PaymentRequest.status == "approved",
            extract("year", PaymentRequest.date) == year,
        )
        .group_by(PaymentRequest.branch_id, PaymentRequestItem.category_id)
        .all()
    )
    actual_totals = {
        (int(r.branch_id), int(r.category_id)): Decimal(str(r.total or 0))
        for r in actual_total_rows
    }

    # ── Queries 3 & 4: per-month maps for the months_data breakdown sub-row ──
    bmap = _bulk_budgets(bids, cids, year)
    amap = _bulk_actuals(bids, cids, year)   # grouped by month with round()

    grid = {}
    for br in branches:
        grid[br.id] = {}
        for cat in categories:
            budgeted  = budget_totals.get((br.id, cat.id), Decimal("0"))
            actual    = actual_totals.get((br.id, cat.id), Decimal("0"))
            remaining = budgeted - actual
            pct = int(actual / budgeted * 100) if budgeted > 0 else None

            months_data = []
            for m in range(1, 13):
                bobj     = bmap.get((br.id, cat.id, m))
                m_actual = amap.get((br.id, cat.id, m), Decimal("0"))
                months_data.append({
                    "label":    calendar.month_abbr[m],
                    "budgeted": float(bobj.amount) if bobj else 0,
                    "actual":   float(m_actual),
                })

            grid[br.id][cat.id] = dict(
                budgeted=budgeted, actual=actual,
                remaining=remaining, pct=pct,
                over=actual > budgeted and budgeted > 0,
                warn=pct is not None and pct >= 80,
                months_data=months_data,
            )
    return grid


def _build_weekly_grid(branches, categories, year, month, week):
    """2 bulk queries total."""
    bids = [br.id for br in branches]
    cids = [cat.id for cat in categories]
    bmap = _bulk_budgets(bids, cids, year)

    # Fetch weekly actuals in one query with day-range filter
    s, e = _week_day_range(week)
    amap_week = {}
    rows = (
        db.session.query(
            PaymentRequest.branch_id,
            PaymentRequestItem.category_id,
            func.sum(PaymentRequestItem.amount).label("total"),
        )
        .join(PaymentRequest, PaymentRequestItem.request_id == PaymentRequest.id)
        .filter(
            PaymentRequest.branch_id.in_(bids),
            PaymentRequestItem.category_id.in_(cids),
            PaymentRequest.status == "approved",
            extract("year",  PaymentRequest.date) == year,
            extract("month", PaymentRequest.date) == month,
            extract("day",   PaymentRequest.date) >= s,
            extract("day",   PaymentRequest.date) <= e,
        )
        .group_by(PaymentRequest.branch_id, PaymentRequestItem.category_id)
        .all()
    )
    for r in rows:
        amap_week[(int(r.branch_id), int(r.category_id))] = Decimal(str(r.total or 0))

    grid = {}
    for br in branches:
        grid[br.id] = {}
        for cat in categories:
            bobj        = bmap.get((br.id, cat.id, month))
            monthly_amt = Decimal(str(bobj.amount)) if bobj else Decimal("0")
            budgeted    = (monthly_amt / 4).quantize(Decimal("0.01"))
            actual      = amap_week.get((br.id, cat.id), Decimal("0"))
            remaining   = budgeted - actual
            pct = int(actual / budgeted * 100) if budgeted > 0 else None
            grid[br.id][cat.id] = dict(
                budgeted=budgeted, actual=actual,
                remaining=remaining, pct=pct,
                over=actual > budgeted and budgeted > 0,
                warn=pct is not None and pct >= 80,
                monthly_amt=monthly_amt,
            )
    return grid


# ── main page ─────────────────────────────────────────────────────────────────

@budget_bp.route("/budget")
@login_required
def budget_home():
    try:
        now    = datetime.now(timezone.utc)
        period = request.args.get("period", "monthly")
        year   = int(request.args.get("year",  now.year))
        month  = int(request.args.get("month", now.month))
        week   = int(request.args.get("week",  1))

        if period not in ("monthly", "yearly", "weekly"):
            period = "monthly"

        allowed_ids = _allowed_branch_ids()
        branches    = Branch.query.filter(
            Branch.id.in_(allowed_ids), Branch.is_active == True
        ).order_by(Branch.name).all()
        categories  = Category.query.filter_by(is_active=True).order_by(Category.name).all()

        if period == "yearly":
            grid = _build_yearly_grid(branches, categories, year)
            month_label = None
        elif period == "weekly":
            grid = _build_weekly_grid(branches, categories, year, month, week)
            month_label = calendar.month_name[month]
        else:
            grid = _build_monthly_grid(branches, categories, year, month)
            month_label = calendar.month_name[month]

        branch_totals  = _branch_totals(branches, categories, grid)
        total_budgeted = sum(v["budgeted"] for v in branch_totals.values())
        total_actual   = sum(v["actual"]   for v in branch_totals.values())

        # Projected income
        bids = [br.id for br in branches]
        if period == "yearly":
            income_map = _get_income_map(bids, year, month=None)
        else:
            income_map = _get_income_map(bids, year, month=month)
        total_projected_income = sum(
            Decimal(str(pi.amount)) for pi in income_map.values()
        )

        # Budget line items (sub-items under each category, monthly only)
        cids = [cat.id for cat in categories]
        if period == "monthly":
            li_rows = BudgetLineItem.query.filter(
                BudgetLineItem.branch_id.in_(bids),
                BudgetLineItem.category_id.in_(cids),
                BudgetLineItem.year == year,
                BudgetLineItem.month == month,
            ).order_by(BudgetLineItem.sort_order, BudgetLineItem.id).all()
            line_items_map = {}
            for li in li_rows:
                line_items_map.setdefault((li.branch_id, li.category_id), []).append(li)
        else:
            line_items_map = {}

        return render_template(
            "budget.html",
            branches=branches, categories=categories,
            grid=grid, branch_totals=branch_totals,
            total_budgeted=total_budgeted, total_actual=total_actual,
            income_map=income_map,
            total_projected_income=total_projected_income,
            line_items_map=line_items_map,
            period=period, year=year, month=month, week=week,
            month_label=month_label,
            year_range=range(now.year - 1, now.year + 3),
            months=list(enumerate(calendar.month_name))[1:],
        )
    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        print(f"[budget error] {e}", flush=True)
        import traceback; traceback.print_exc()
        flash(f"Budget page error: {str(e)}", "error")
        return redirect(url_for("requests.dashboard"))


# ── save monthly budget cell (AJAX) ──────────────────────────────────────────

@budget_bp.route("/budget/save", methods=["POST"])
@login_required
def save_budget():
    """Only monthly budgets are stored. Returns updated monthly stats
    AND the recalculated yearly totals so the UI can update live."""
    try:
        branch_id   = int(request.form["branch_id"])
        category_id = int(request.form["category_id"])
        year        = int(request.form["year"])
        month       = int(request.form["month"])
        notes       = request.form.get("notes", "").strip()

        raw = request.form.get("amount", "0").replace(",", "").strip()
        try:
            amount = Decimal(raw) if raw else Decimal("0")
        except InvalidOperation:
            return jsonify(ok=False, error="Invalid amount"), 400

        if branch_id not in _allowed_branch_ids():
            return jsonify(ok=False, error="Not authorised for this branch"), 403

        existing = Budget.query.filter_by(
            branch_id=branch_id, category_id=category_id,
            period_type="monthly", year=year, month=month, week=None
        ).first()

        if existing:
            existing.amount     = amount
            existing.notes      = notes
            existing.updated_at = datetime.now(timezone.utc)
        else:
            db.session.add(Budget(
                branch_id=branch_id, category_id=category_id,
                period_type="monthly", year=year, month=month, week=None,
                amount=amount, notes=notes, created_by=current_user.id
            ))

        db.session.commit()

        # Monthly cell stats
        actual    = _get_actuals(branch_id, category_id, year, month)
        remaining = amount - actual
        pct       = int(actual / amount * 100) if amount > 0 else None

        # Yearly rollup — recalculated from all 12 months after this save
        y_bud  = _yearly_budget_amount(branch_id, category_id, year)
        y_act  = _get_actuals(branch_id, category_id, year)
        y_pct  = int(y_act / y_bud * 100) if y_bud > 0 else None

        # Weekly derived (month ÷ 4)
        w_bud = float((amount / 4).quantize(Decimal("0.01")))

        return jsonify(
            ok=True,
            # monthly
            budgeted=float(amount), actual=float(actual),
            remaining=float(remaining), pct=pct,
            over=actual > amount and amount > 0,
            warn=pct is not None and pct >= 80,
            # yearly rollup (so yearly view can update without reload)
            yearly_budgeted=float(y_bud),
            yearly_actual=float(y_act),
            yearly_remaining=float(y_bud - y_act),
            yearly_pct=y_pct,
            yearly_over=y_act > y_bud and y_bud > 0,
            # weekly derived
            weekly_budgeted=w_bud,
        )

    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


# ── copy last month ───────────────────────────────────────────────────────────

@budget_bp.route("/budget/copy", methods=["POST"])
@login_required
def copy_last_period():
    year  = int(request.form.get("year",  datetime.now(timezone.utc).year))
    month = int(request.form.get("month", datetime.now(timezone.utc).month))

    prev_year  = year if month > 1 else year - 1
    prev_month = month - 1 if month > 1 else 12

    allowed = _allowed_branch_ids()
    sources = Budget.query.filter_by(
        period_type="monthly", year=prev_year, month=prev_month, week=None
    ).filter(Budget.branch_id.in_(allowed)).all()

    copied = 0
    for s in sources:
        if not Budget.query.filter_by(
            branch_id=s.branch_id, category_id=s.category_id,
            period_type="monthly", year=year, month=month, week=None
        ).first():
            db.session.add(Budget(
                branch_id=s.branch_id, category_id=s.category_id,
                period_type="monthly", year=year, month=month, week=None,
                amount=s.amount, notes=s.notes, created_by=current_user.id
            ))
            copied += 1

    db.session.commit()
    flash(f"Copied {copied} entries from {calendar.month_name[prev_month]} {prev_year}.", "success")
    return redirect(url_for("budget.budget_home") + f"?period=monthly&year={year}&month={month}")


# ── dashboard alerts ──────────────────────────────────────────────────────────

@budget_bp.route("/budget/alerts")
@login_required
def budget_alerts():
    now     = datetime.now(timezone.utc)
    allowed = _allowed_branch_ids()
    budgets = Budget.query.filter_by(
        period_type="monthly", year=now.year, month=now.month, week=None
    ).filter(Budget.branch_id.in_(allowed)).all()

    alerts = []
    for b in budgets:
        if b.amount <= 0:
            continue
        actual = _get_actuals(b.branch_id, b.category_id, now.year, now.month)
        pct    = int(actual / b.amount * 100)
        if pct >= 80:
            alerts.append(dict(
                branch=b.branch.name, category=b.category.name,
                pct=pct, over=actual > b.amount,
                budgeted=float(b.amount), actual=float(actual),
            ))

    alerts.sort(key=lambda x: -x["pct"])
    return jsonify(alerts)


# ── save projected income cell (AJAX) ────────────────────────────────────────

@budget_bp.route("/budget/save_income", methods=["POST"])
@login_required
def save_income():
    try:
        branch_id = int(request.form["branch_id"])
        year      = int(request.form["year"])
        month_raw = request.form.get("month", "")
        month     = int(month_raw) if month_raw else None
        notes     = request.form.get("notes", "").strip()

        raw = request.form.get("amount", "0").replace(",", "").strip()
        try:
            amount = Decimal(raw) if raw else Decimal("0")
        except InvalidOperation:
            return jsonify(ok=False, error="Invalid amount"), 400

        if branch_id not in _allowed_branch_ids():
            return jsonify(ok=False, error="Not authorised for this branch"), 403

        existing = ProjectedIncome.query.filter_by(
            branch_id=branch_id, year=year, month=month
        ).first()

        if existing:
            existing.amount     = amount
            existing.notes      = notes
            existing.updated_at = datetime.now(timezone.utc)
        else:
            db.session.add(ProjectedIncome(
                branch_id=branch_id, year=year, month=month,
                amount=amount, notes=notes, created_by=current_user.id
            ))

        db.session.commit()
        return jsonify(ok=True, amount=float(amount))

    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


# ── budget line items (sub-items) ─────────────────────────────────────────────

@budget_bp.route("/budget/line-item/save", methods=["POST"])
@login_required
def save_line_item():
    try:
        branch_id   = int(request.form["branch_id"])
        category_id = int(request.form["category_id"])
        year        = int(request.form["year"])
        month       = int(request.form["month"])
        name        = request.form.get("name", "").strip()
        item_id_raw = request.form.get("id", "").strip()

        if not name:
            return jsonify(ok=False, error="Name is required"), 400
        if branch_id not in _allowed_branch_ids():
            return jsonify(ok=False, error="Not authorised for this branch"), 403

        raw = request.form.get("amount", "0").replace(",", "").strip()
        try:
            amount = Decimal(raw) if raw else Decimal("0")
        except InvalidOperation:
            return jsonify(ok=False, error="Invalid amount"), 400

        if item_id_raw:
            li = BudgetLineItem.query.get_or_404(int(item_id_raw))
            if li.branch_id != branch_id:
                return jsonify(ok=False, error="Access denied"), 403
            li.name       = name
            li.amount     = amount
            li.updated_at = datetime.now(timezone.utc)
        else:
            li = BudgetLineItem(
                branch_id=branch_id, category_id=category_id,
                year=year, month=month,
                name=name, amount=amount,
                created_by=current_user.id,
            )
            db.session.add(li)

        db.session.commit()
        return jsonify(ok=True, id=li.id, name=li.name, amount=float(li.amount))

    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


@budget_bp.route("/budget/line-item/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_line_item(item_id):
    try:
        li = BudgetLineItem.query.get_or_404(item_id)
        if li.branch_id not in _allowed_branch_ids():
            return jsonify(ok=False, error="Access denied"), 403
        db.session.delete(li)
        db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500

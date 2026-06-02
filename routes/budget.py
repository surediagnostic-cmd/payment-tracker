"""Budget planning routes.

Accountants can set/edit budgets for their own branches.
MDS can set/edit budgets for any branch and view company-wide comparisons.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import calendar

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func, extract

from app import db
from models import Branch, Category, Budget, PaymentRequest, PaymentRequestItem

budget_bp = Blueprint("budget", __name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _week_day_range(week: int):
    """Return (start_day, end_day) for week 1-4 within a month.
    Week 1 = days 1-7, Week 2 = days 8-14, Week 3 = days 15-21, Week 4 = days 22-31.
    """
    starts = {1: 1, 2: 8, 3: 15, 4: 22}
    ends   = {1: 7, 2: 14, 3: 21, 4: 31}
    return starts.get(week, 1), ends.get(week, 31)


def _get_actuals(branch_id, category_id, period_type, year, month=None, week=None):
    """Sum approved payment amounts for the given scope."""
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
    if period_type in ("monthly", "weekly") and month:
        q = q.filter(extract("month", PaymentRequest.date) == month)
    if period_type == "weekly" and week:
        start_day, end_day = _week_day_range(week)
        q = q.filter(
            extract("day", PaymentRequest.date) >= start_day,
            extract("day", PaymentRequest.date) <= end_day,
        )
    return Decimal(str(q.scalar() or 0))


def _allowed_branch_ids():
    """Return branch IDs this user may budget for."""
    if current_user.is_mds:
        return [b.id for b in Branch.query.filter_by(is_active=True).all()]
    return [b.id for b in current_user.branches]


def _build_grid(branches, categories, period_type, year, month=None, week=None):
    """Return a nested dict: grid[branch_id][category_id] = {budget, actual, pct, over}."""
    # Fetch all budgets for this scope in one query
    bq = Budget.query.filter_by(period_type=period_type, year=year)
    if month is not None:
        bq = bq.filter_by(month=month)
    else:
        bq = bq.filter(Budget.month.is_(None))
    if week is not None:
        bq = bq.filter_by(week=week)
    else:
        bq = bq.filter(Budget.week.is_(None))

    budgets = {(b.branch_id, b.category_id): b for b in bq.all()}

    grid = {}
    for br in branches:
        grid[br.id] = {}
        for cat in categories:
            bobj = budgets.get((br.id, cat.id))
            budgeted = Decimal(str(bobj.amount)) if bobj else Decimal("0")
            actual   = _get_actuals(br.id, cat.id, period_type, year, month, week)
            remaining = budgeted - actual
            pct = int(actual / budgeted * 100) if budgeted > 0 else None
            grid[br.id][cat.id] = {
                "budget_id": bobj.id if bobj else None,
                "budgeted":  budgeted,
                "actual":    actual,
                "remaining": remaining,
                "pct":       pct,
                "over":      actual > budgeted and budgeted > 0,
                "warn":      pct is not None and pct >= 80,
                "notes":     bobj.notes if bobj else "",
            }
    return grid


# ── main budget page ──────────────────────────────────────────────────────────

@budget_bp.route("/budget")
@login_required
def budget_home():
    now    = datetime.now(timezone.utc)
    period = request.args.get("period", "monthly")
    year   = int(request.args.get("year",  now.year))
    month  = int(request.args.get("month", now.month)) if period in ("monthly", "weekly") else None
    week   = int(request.args.get("week",  1))          if period == "weekly" else None

    allowed_ids = _allowed_branch_ids()
    branches    = Branch.query.filter(Branch.id.in_(allowed_ids), Branch.is_active == True).order_by(Branch.name).all()
    categories  = Category.query.filter_by(is_active=True).order_by(Category.name).all()

    grid = _build_grid(branches, categories, period, year, month, week)

    # Summary totals per branch
    branch_totals = {}
    for br in branches:
        b_total = sum(grid[br.id][c.id]["budgeted"] for c in categories)
        a_total = sum(grid[br.id][c.id]["actual"]   for c in categories)
        branch_totals[br.id] = {
            "budgeted":  b_total,
            "actual":    a_total,
            "remaining": b_total - a_total,
            "pct":       int(a_total / b_total * 100) if b_total > 0 else None,
            "over":      a_total > b_total and b_total > 0,
        }

    # Company-wide totals
    total_budgeted = sum(v["budgeted"] for v in branch_totals.values())
    total_actual   = sum(v["actual"]   for v in branch_totals.values())

    month_name = calendar.month_name[month] if month else ""
    weeks_in_month = 4  # always 4 planning weeks

    return render_template(
        "budget.html",
        branches=branches,
        categories=categories,
        grid=grid,
        branch_totals=branch_totals,
        total_budgeted=total_budgeted,
        total_actual=total_actual,
        period=period,
        year=year,
        month=month,
        week=week,
        month_name=month_name,
        weeks_in_month=weeks_in_month,
        year_range=range(now.year - 1, now.year + 3),
        months=list(enumerate(calendar.month_name))[1:],  # [(1,'January'),...]
    )


# ── save a single budget cell ─────────────────────────────────────────────────

@budget_bp.route("/budget/save", methods=["POST"])
@login_required
def save_budget():
    """AJAX endpoint — saves one budget cell and returns updated totals."""
    try:
        branch_id   = int(request.form["branch_id"])
        category_id = int(request.form["category_id"])
        period_type = request.form["period_type"]
        year        = int(request.form["year"])
        month       = int(request.form["month"])  if request.form.get("month")  else None
        week        = int(request.form["week"])   if request.form.get("week")   else None
        notes       = request.form.get("notes", "").strip()

        raw = request.form.get("amount", "0").replace(",", "").strip()
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return jsonify(ok=False, error="Invalid amount"), 400

        # Permission check
        allowed = _allowed_branch_ids()
        if branch_id not in allowed:
            return jsonify(ok=False, error="Not authorised for this branch"), 403

        # Upsert
        existing = Budget.query.filter_by(
            branch_id=branch_id, category_id=category_id,
            period_type=period_type, year=year, month=month, week=week
        ).first()

        if existing:
            existing.amount     = amount
            existing.notes      = notes
            existing.updated_at = datetime.now(timezone.utc)
        else:
            existing = Budget(
                branch_id=branch_id, category_id=category_id,
                period_type=period_type, year=year, month=month, week=week,
                amount=amount, notes=notes, created_by=current_user.id
            )
            db.session.add(existing)

        db.session.commit()

        actual = _get_actuals(branch_id, category_id, period_type, year, month, week)
        pct    = int(actual / amount * 100) if amount > 0 else None

        return jsonify(
            ok=True,
            budgeted=float(amount),
            actual=float(actual),
            remaining=float(amount - actual),
            pct=pct,
            over=actual > amount and amount > 0,
            warn=pct is not None and pct >= 80,
        )

    except Exception as e:
        try: db.session.rollback()
        except Exception: pass
        return jsonify(ok=False, error=str(e)), 500


# ── copy last period's budgets ────────────────────────────────────────────────

@budget_bp.route("/budget/copy", methods=["POST"])
@login_required
def copy_last_period():
    """Copy all budget entries from the previous period into the current one."""
    period_type = request.form.get("period_type", "monthly")
    year        = int(request.form.get("year",  datetime.now(timezone.utc).year))
    month       = int(request.form.get("month", datetime.now(timezone.utc).month)) \
                  if period_type in ("monthly", "weekly") else None
    week        = int(request.form.get("week",  1)) if period_type == "weekly" else None

    # Determine previous period
    if period_type == "yearly":
        prev_year, prev_month, prev_week = year - 1, None, None
    elif period_type == "monthly":
        if month == 1:
            prev_year, prev_month, prev_week = year - 1, 12, None
        else:
            prev_year, prev_month, prev_week = year, month - 1, None
    else:  # weekly
        if week == 1:
            if month == 1:
                prev_year, prev_month, prev_week = year - 1, 12, 4
            else:
                prev_year, prev_month, prev_week = year, month - 1, 4
        else:
            prev_year, prev_month, prev_week = year, month, week - 1

    allowed = _allowed_branch_ids()
    src = Budget.query.filter_by(
        period_type=period_type, year=prev_year, month=prev_month, week=prev_week
    ).filter(Budget.branch_id.in_(allowed)).all()

    copied = 0
    for s in src:
        exists = Budget.query.filter_by(
            branch_id=s.branch_id, category_id=s.category_id,
            period_type=period_type, year=year, month=month, week=week
        ).first()
        if not exists:
            db.session.add(Budget(
                branch_id=s.branch_id, category_id=s.category_id,
                period_type=period_type, year=year, month=month, week=week,
                amount=s.amount, notes=s.notes, created_by=current_user.id
            ))
            copied += 1

    db.session.commit()
    flash(f"Copied {copied} budget entries from the previous period.", "success")

    params = f"period={period_type}&year={year}"
    if month: params += f"&month={month}"
    if week:  params += f"&week={week}"
    return redirect(url_for("budget.budget_home") + "?" + params)


# ── JSON API for dashboard alerts ─────────────────────────────────────────────

@budget_bp.route("/budget/alerts")
@login_required
def budget_alerts():
    """Return branches/categories that are ≥ 80% of budget this month."""
    now    = datetime.now(timezone.utc)
    year   = now.year
    month  = now.month
    allowed_ids = _allowed_branch_ids()

    budgets = Budget.query.filter_by(
        period_type="monthly", year=year, month=month
    ).filter(Budget.branch_id.in_(allowed_ids)).all()

    alerts = []
    for b in budgets:
        if b.amount <= 0:
            continue
        actual = _get_actuals(b.branch_id, b.category_id, "monthly", year, month)
        pct    = int(actual / b.amount * 100)
        if pct >= 80:
            alerts.append({
                "branch":   b.branch.name,
                "category": b.category.name,
                "pct":      pct,
                "over":     actual > b.amount,
                "budgeted": float(b.amount),
                "actual":   float(actual),
            })

    alerts.sort(key=lambda x: -x["pct"])
    return jsonify(alerts)

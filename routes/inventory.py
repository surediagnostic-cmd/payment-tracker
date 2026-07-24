"""Inventory Management — reagents, consumables, LIS upload & consumption tracking."""
from datetime import datetime, timezone, date as date_type
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import csv, io
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, abort, jsonify, Response)
from flask_login import login_required, current_user
from sqlalchemy import func

from app import db
from models import (
    Branch, User,
    InventoryItem, TestCatalogue, TestReagentMap, TestBranchPrice,
    TestVolumeLog, PriceAuditLog,
    PackageCatalogue, PackageTest,
    StockLevel, StockTransaction,
    LisUpload, LisUploadRow, UnmatchedInvestigation,
    ITEM_CATEGORY_LABELS, CASE_TYPE_LABELS,
)

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")

ITEM_CATEGORIES = list(ITEM_CATEGORY_LABELS.items())
CASE_TYPES      = list(CASE_TYPE_LABELS.items())

AUTO_MATCH_THRESHOLD = 0.82   # score >= this → auto-match
SUGGEST_THRESHOLD    = 0.50   # score >= this → show as suggestion


# ── Access decorators ─────────────────────────────────────────────────────────

def _inventory_access(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.can_view_inventory:
            flash("Access denied.", "error")
            return redirect(url_for("requests.dashboard"))
        return f(*args, **kwargs)
    return decorated


def _stock_manager(f):
    """Accountant + lab_staff + mds can receive/adjust stock."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ("accountant", "lab_staff", "mds"):
            flash("Access denied.", "error")
            return redirect(url_for("inventory.dashboard"))
        return f(*args, **kwargs)
    return decorated


def _upload_access(f):
    """Only accountant/mds can upload LIS CSV."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ("accountant", "mds"):
            flash("Accountant access required to upload LIS data.", "error")
            return redirect(url_for("inventory.dashboard"))
        return f(*args, **kwargs)
    return decorated


def _mds_only(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_mds:
            flash("MDS access required.", "error")
            return redirect(url_for("inventory.dashboard"))
        return f(*args, **kwargs)
    return decorated


# ── Audit helpers ────────────────────────────────────────────────────────────

def _log_price(entity_type, entity_id, old_price, new_price, branch_id=None, notes=None):
    """Record a price/cost change in price_audit_log."""
    try:
        entry = PriceAuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            branch_id=branch_id,
            old_price=float(old_price) if old_price is not None else None,
            new_price=float(new_price) if new_price is not None else None,
            changed_by=current_user.id if current_user.is_authenticated else None,
            notes=notes,
        )
        db.session.add(entry)
    except Exception as e:
        print(f"[audit] price log failed: {e}")


# ── Stock helpers ─────────────────────────────────────────────────────────────

def _get_or_create_sl(item_id, branch_id):
    sl = StockLevel.query.filter_by(item_id=item_id, branch_id=branch_id).first()
    if not sl:
        sl = StockLevel(item_id=item_id, branch_id=branch_id, qty_on_hand=0)
        db.session.add(sl)
    return sl


def _apply_txn(item_id, branch_id, txn_type, qty, user_id,
               unit_cost=None, batch=None, expiry=None, reference=None, notes=None):
    txn = StockTransaction(
        item_id=item_id, branch_id=branch_id, txn_type=txn_type,
        qty=qty, unit_cost=unit_cost, batch_number=batch,
        expiry_date=expiry, reference=reference, notes=notes,
        created_by=user_id,
    )
    db.session.add(txn)
    sl = _get_or_create_sl(item_id, branch_id)
    sl.qty_on_hand = float(sl.qty_on_hand) + float(qty)
    sl.last_updated = datetime.now(timezone.utc)


# ── Fuzzy / branch matching ───────────────────────────────────────────────────

def _fuzzy_score(a, b):
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


def _best_test_match(name, all_tests):
    """Return (TestCatalogue|None, score) for the best fuzzy match."""
    best, score = None, 0.0
    for t in all_tests:
        s = _fuzzy_score(name, t.labsmart_name)
        if s > score:
            score, best = s, t
    return best, score


def _best_package_match(name, all_pkgs):
    best, score = None, 0.0
    for p in all_pkgs:
        s = _fuzzy_score(name, p.labsmart_name)
        if s > score:
            score, best = s, p
    return best, score


def _match_branch(raw, branches):
    if not raw:
        return None
    raw_lower = raw.strip().lower()
    for b in branches:
        if b.name.lower() in raw_lower:
            return b.id
    return None


def _parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ── Dashboard ─────────────────────────────────────────────────────────────────

@inventory_bp.route("/")
@login_required
@_inventory_access
def dashboard():
    total_items    = InventoryItem.query.filter_by(is_active=True).count()
    pending_uploads = LisUpload.query.filter_by(status='pending_review').count()

    low_stock = (
        db.session.query(InventoryItem, func.sum(StockLevel.qty_on_hand).label('total'))
        .outerjoin(StockLevel, StockLevel.item_id == InventoryItem.id)
        .filter(InventoryItem.is_active == True, InventoryItem.reorder_level != None)
        .group_by(InventoryItem.id)
        .having(func.coalesce(func.sum(StockLevel.qty_on_hand), 0) < InventoryItem.reorder_level)
        .all()
    )

    recent_txns = (
        StockTransaction.query
        .order_by(StockTransaction.created_at.desc())
        .limit(12).all()
    )

    recent_uploads = LisUpload.query.order_by(LisUpload.created_at.desc()).limit(5).all()

    from models import InTransitStock
    in_transit_count = InTransitStock.query.filter_by(status="in_transit").count()

    # Low-margin tests with recent volume (last 90 days)
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    low_margin_tests = []
    try:
        vol_test_ids = {
            row[0] for row in
            db.session.query(TestVolumeLog.test_id)
            .filter(TestVolumeLog.created_at >= cutoff).distinct().all()
        }
        if vol_test_ids:
            for t in TestCatalogue.query.filter(
                TestCatalogue.id.in_(vol_test_ids), TestCatalogue.is_active == True
            ).all():
                mpct = t.margin_pct
                if mpct is not None and mpct < 30:
                    low_margin_tests.append((t, mpct))
            low_margin_tests.sort(key=lambda x: x[1])
    except Exception:
        db.session.rollback()

    return render_template("inventory/dashboard.html",
        total_items=total_items,
        pending_uploads=pending_uploads,
        low_stock=low_stock,
        recent_txns=recent_txns,
        recent_uploads=recent_uploads,
        in_transit_count=in_transit_count,
        low_margin_tests=low_margin_tests,
    )


# ── Items catalogue ───────────────────────────────────────────────────────────

@inventory_bp.route("/items")
@login_required
@_inventory_access
def items():
    cat = request.args.get("cat", "")
    q   = request.args.get("q", "").strip()
    query = InventoryItem.query
    if cat:
        query = query.filter_by(category=cat)
    if q:
        query = query.filter(InventoryItem.name.ilike(f"%{q}%"))
    items_list = query.order_by(InventoryItem.name).all()
    return render_template("inventory/items.html",
        items=items_list, cat_filter=cat, q=q,
        ITEM_CATEGORIES=ITEM_CATEGORIES,
    )


@inventory_bp.route("/items/add", methods=["POST"])
@login_required
@_inventory_access
def add_item():
    name   = request.form.get("name", "").strip()
    code   = request.form.get("item_code", "").strip() or None
    cat    = request.form.get("category", "lab_reagent")
    unit   = request.form.get("unit", "unit").strip() or "unit"
    pack          = request.form.get("pack_size", "").strip()
    purchase_unit = request.form.get("purchase_unit", "").strip() or None
    price  = request.form.get("unit_price", "").strip()
    reorder= request.form.get("reorder_level", "").strip()
    notes  = request.form.get("notes", "").strip() or None

    if not name:
        flash("Item name is required.", "error")
        return redirect(url_for("inventory.items"))

    item = InventoryItem(
        name=name, item_code=code, category=cat, unit=unit,
        pack_size=int(pack) if pack else None,
        purchase_unit=purchase_unit,
        unit_price=float(price) if price else None,
        reorder_level=float(reorder) if reorder else None,
        notes=notes,
    )
    db.session.add(item)
    db.session.commit()
    flash(f"Item '{name}' added.", "success")
    return redirect(url_for("inventory.item_detail", item_id=item.id))


@inventory_bp.route("/items/<int:item_id>")
@login_required
@_inventory_access
def item_detail(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    stock_levels = StockLevel.query.filter_by(item_id=item_id).all()
    txns = (StockTransaction.query
            .filter_by(item_id=item_id)
            .order_by(StockTransaction.created_at.desc())
            .limit(30).all())
    branches = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    all_tests = TestCatalogue.query.filter_by(is_active=True).order_by(TestCatalogue.name).all()
    try:
        cost_audit = (PriceAuditLog.query
                      .filter_by(entity_type="item_cost", entity_id=item_id)
                      .order_by(PriceAuditLog.changed_at.desc()).limit(20).all())
    except Exception:
        db.session.rollback()
        cost_audit = []
    return render_template("inventory/item_detail.html",
        item=item, stock_levels=stock_levels, txns=txns,
        branches=branches, all_tests=all_tests,
        cost_audit=cost_audit,
        ITEM_CATEGORIES=ITEM_CATEGORIES,
    )


@inventory_bp.route("/items/<int:item_id>/edit", methods=["POST"])
@login_required
@_inventory_access
def edit_item(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    item.name     = request.form.get("name", item.name).strip()
    item.item_code= request.form.get("item_code", "").strip() or None
    item.category = request.form.get("category", item.category)
    item.unit     = request.form.get("unit", item.unit).strip() or item.unit
    pack          = request.form.get("pack_size", "").strip()
    purchase_unit = request.form.get("purchase_unit", "").strip() or None
    price_raw     = request.form.get("unit_price", "").strip()
    reorder       = request.form.get("reorder_level", "").strip()
    new_price     = float(price_raw) if price_raw else None
    old_price     = float(item.unit_price) if item.unit_price is not None else None
    if old_price != new_price:
        _log_price("item_cost", item_id, old_price, new_price)
    item.pack_size     = int(pack) if pack else None
    item.purchase_unit = purchase_unit
    item.unit_price    = new_price
    item.reorder_level = float(reorder) if reorder else None
    item.notes         = request.form.get("notes", "").strip() or None
    db.session.commit()
    flash(f"Item '{item.name}' updated.", "success")
    return redirect(url_for("inventory.item_detail", item_id=item_id))


@inventory_bp.route("/items/<int:item_id>/toggle", methods=["POST"])
@login_required
@_mds_only
def toggle_item(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    item.is_active = not item.is_active
    db.session.commit()
    flash(f"Item '{item.name}' {'activated' if item.is_active else 'deactivated'}.", "success")
    return redirect(url_for("inventory.item_detail", item_id=item_id))


# ── Test catalogue ────────────────────────────────────────────────────────────

@inventory_bp.route("/tests")
@login_required
@_inventory_access
def tests():
    type_filter = request.args.get("type", "")
    q           = request.args.get("q", "").strip()
    query = TestCatalogue.query
    if type_filter:
        query = query.filter_by(case_type=type_filter)
    if q:
        query = query.filter(
            TestCatalogue.name.ilike(f"%{q}%") |
            TestCatalogue.labsmart_name.ilike(f"%{q}%")
        )
    tests_list = query.order_by(TestCatalogue.name).all()
    return render_template("inventory/tests.html",
        tests=tests_list, type_filter=type_filter, q=q,
        CASE_TYPES=CASE_TYPES,
    )


@inventory_bp.route("/tests/add", methods=["POST"])
@login_required
@_inventory_access
def add_test():
    name      = request.form.get("name", "").strip()
    lis_name  = request.form.get("labsmart_name", "").strip()
    case_type = request.form.get("case_type", "lab")
    price_raw = request.form.get("price", "").strip().replace(",", "")
    if not name or not lis_name:
        flash("Test name and LIS name are required.", "error")
        return redirect(url_for("inventory.tests"))
    try:
        price = Decimal(price_raw) if price_raw else None
    except Exception:
        price = None
    t = TestCatalogue(name=name, labsmart_name=lis_name, case_type=case_type, price=price)
    db.session.add(t)
    db.session.commit()
    flash(f"Test '{name}' added.", "success")
    return redirect(url_for("inventory.test_detail", test_id=t.id))


@inventory_bp.route("/tests/<int:test_id>")
@login_required
@_inventory_access
def test_detail(test_id):
    test      = TestCatalogue.query.get_or_404(test_id)
    all_items = InventoryItem.query.filter_by(is_active=True).order_by(InventoryItem.name).all()
    branches  = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    # build a lookup {branch_id: TestBranchPrice} for the template
    bp_map      = {bp.branch_id: bp for bp in test.branch_prices}
    try:
        price_audit = (PriceAuditLog.query
                       .filter(PriceAuditLog.entity_type.in_(["test_default", "test_branch"]),
                               PriceAuditLog.entity_id == test_id)
                       .order_by(PriceAuditLog.changed_at.desc()).limit(30).all())
    except Exception:
        db.session.rollback()
        price_audit = []
    # Branch name lookup for audit log display
    branch_map  = {b.id: b.name for b in branches}
    return render_template("inventory/test_detail.html",
        test=test, all_items=all_items, CASE_TYPES=CASE_TYPES,
        branches=branches, bp_map=bp_map,
        price_audit=price_audit, branch_map=branch_map,
    )


@inventory_bp.route("/tests/<int:test_id>/edit", methods=["POST"])
@login_required
@_inventory_access
def edit_test(test_id):
    test = TestCatalogue.query.get_or_404(test_id)
    test.name          = request.form.get("name", test.name).strip()
    test.labsmart_name = request.form.get("labsmart_name", test.labsmart_name).strip()
    test.case_type     = request.form.get("case_type", test.case_type)
    price_raw = request.form.get("price", "").strip().replace(",", "")
    old_price = float(test.price) if test.price is not None else None
    try:
        new_price = Decimal(price_raw) if price_raw else None
    except Exception:
        new_price = test.price
    if (float(new_price) if new_price is not None else None) != old_price:
        _log_price("test_default", test_id, old_price,
                   float(new_price) if new_price is not None else None)
    test.price = new_price
    db.session.commit()
    flash(f"Test '{test.name}' updated.", "success")
    return redirect(url_for("inventory.test_detail", test_id=test_id))


@inventory_bp.route("/tests/template")
@login_required
@_inventory_access
def download_tests_template():
    import csv, io
    from models import TestCatalogue as TC
    branches = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    si = io.StringIO()
    w  = csv.writer(si)
    branch_headers = [f"Price ({b.name})" for b in branches]
    w.writerow(["Test Name", "LIS Name", "Case Type", "Sub-Category", "Default Price (N)", "Active"] + branch_headers)
    bp_index = {}  # {(test_id, branch_id): price}
    from models import TestBranchPrice as TBP
    for bp in TBP.query.all():
        bp_index[(bp.test_id, bp.branch_id)] = float(bp.price)
    for t in TC.query.order_by(TC.name).all():
        branch_prices = [bp_index.get((t.id, b.id), "") for b in branches]
        w.writerow([
            t.name, t.labsmart_name, t.case_type,
            t.sub_category or "",
            float(t.price) if t.price else "",
            "Yes" if t.is_active else "No",
        ] + branch_prices)
    output = si.getvalue()
    from flask import make_response
    resp = make_response(output)
    resp.headers["Content-Disposition"] = "attachment; filename=test_catalogue_template.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp


@inventory_bp.route("/tests/upload", methods=["POST"])
@login_required
@_inventory_access
def upload_tests_csv():
    import csv, io
    from models import TestCatalogue as TC, TestBranchPrice as TBP
    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for("inventory.tests"))

    VALID_TYPES = {v for v, _ in CASE_TYPES}
    # Build branch lookup: "ijofi" → Branch object (match Price (BranchName) headers)
    all_branches = {b.name.lower(): b for b in Branch.query.all()}

    try:
        raw    = file.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(raw))
        headers = [h.strip() for h in (reader.fieldnames or [])]
        # Identify branch price columns: "Price (BranchName)"
        branch_cols = {}  # header → Branch
        for h in headers:
            if h.lower().startswith("price (") and h.endswith(")"):
                bname = h[7:-1].strip().lower()
                b = all_branches.get(bname)
                if b:
                    branch_cols[h] = b

        added = updated = skipped = 0
        for row in reader:
            name     = (row.get("Test Name") or "").strip()
            lis_name = (row.get("LIS Name")  or "").strip()
            if not name and not lis_name:
                skipped += 1
                continue
            case_type    = (row.get("Case Type") or "lab").strip().lower()
            if case_type not in VALID_TYPES:
                case_type = "lab"
            sub_category = (row.get("Sub-Category") or "").strip() or None
            price_raw    = (row.get("Default Price (N)") or row.get("Price (N)") or "").replace(",", "").strip()
            active_raw = (row.get("Active") or "yes").strip().lower()
            is_active  = active_raw not in ("no", "false", "0")
            try:
                price = Decimal(price_raw) if price_raw else None
            except Exception:
                price = None

            # Match by LIS name first, then display name
            existing = None
            if lis_name:
                existing = TC.query.filter(TC.labsmart_name.ilike(lis_name)).first()
            if not existing and name:
                existing = TC.query.filter(TC.name.ilike(name)).first()

            if existing:
                if name:          existing.name          = name
                if lis_name:      existing.labsmart_name = lis_name
                existing.case_type    = case_type
                existing.sub_category = sub_category
                existing.price        = price
                existing.is_active    = is_active
                updated += 1
            else:
                if not name or not lis_name:
                    skipped += 1
                    continue
                existing = TC(
                    name=name, labsmart_name=lis_name,
                    case_type=case_type, sub_category=sub_category,
                    price=price, is_active=is_active,
                )
                db.session.add(existing)
                db.session.flush()
                added += 1

            # Handle branch prices
            for col_header, branch in branch_cols.items():
                bp_raw = (row.get(col_header) or "").replace(",", "").strip()
                if not bp_raw:
                    continue
                try:
                    bp_val = Decimal(bp_raw)
                except Exception:
                    continue
                tbp = TBP.query.filter_by(test_id=existing.id, branch_id=branch.id).first()
                if tbp:
                    tbp.price = bp_val
                    tbp.updated_by = current_user.id
                else:
                    db.session.add(TBP(
                        test_id=existing.id, branch_id=branch.id,
                        price=bp_val, updated_by=current_user.id,
                    ))

        db.session.commit()
        flash(f"CSV imported — {added} added, {updated} updated, {skipped} skipped.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Import failed: {e}", "error")
    return redirect(url_for("inventory.tests"))


@inventory_bp.route("/tests/<int:test_id>/branch-price", methods=["POST"])
@login_required
@_inventory_access
def set_branch_price(test_id):
    test      = TestCatalogue.query.get_or_404(test_id)
    branch_id = request.form.get("branch_id", type=int)
    price_raw = request.form.get("price", "").strip().replace(",", "")
    clear     = request.form.get("clear") == "1"

    if not branch_id:
        flash("Branch required.", "error")
        return redirect(url_for("inventory.test_detail", test_id=test_id))

    existing = TestBranchPrice.query.filter_by(test_id=test_id, branch_id=branch_id).first()

    if clear or not price_raw:
        if existing:
            db.session.delete(existing)
            db.session.commit()
            flash("Branch price cleared — will use default price.", "success")
        return redirect(url_for("inventory.test_detail", test_id=test_id))

    try:
        price = Decimal(price_raw)
    except Exception:
        flash("Invalid price value.", "error")
        return redirect(url_for("inventory.test_detail", test_id=test_id))

    old_price = float(existing.price) if existing else None
    _log_price("test_branch", test_id, old_price, float(price), branch_id=branch_id)
    if existing:
        existing.price      = price
        existing.updated_by = current_user.id
        from datetime import datetime, timezone
        existing.updated_at = datetime.now(timezone.utc)
    else:
        db.session.add(TestBranchPrice(
            test_id=test_id, branch_id=branch_id,
            price=price, updated_by=current_user.id,
        ))
    db.session.commit()
    branch = Branch.query.get(branch_id)
    flash(f"Price set for {branch.name}.", "success")
    return redirect(url_for("inventory.test_detail", test_id=test_id))


@inventory_bp.route("/tests/<int:test_id>/mappings/add", methods=["POST"])
@login_required
@_inventory_access
def add_test_mapping(test_id):
    test    = TestCatalogue.query.get_or_404(test_id)
    item_id = request.form.get("item_id", type=int)
    qty     = request.form.get("qty_per_test", "1").strip()
    if not item_id:
        flash("Select an inventory item.", "error")
        return redirect(url_for("inventory.test_detail", test_id=test_id))
    existing = TestReagentMap.query.filter_by(test_id=test_id, item_id=item_id).first()
    if existing:
        existing.qty_per_test = float(qty) if qty else 1.0
    else:
        db.session.add(TestReagentMap(test_id=test_id, item_id=item_id,
                                      qty_per_test=float(qty) if qty else 1.0))
    db.session.commit()
    flash("Mapping saved.", "success")
    return redirect(url_for("inventory.test_detail", test_id=test_id))


@inventory_bp.route("/tests/<int:test_id>/mappings/<int:map_id>/delete", methods=["POST"])
@login_required
@_inventory_access
def delete_test_mapping(test_id, map_id):
    m = TestReagentMap.query.get_or_404(map_id)
    db.session.delete(m)
    db.session.commit()
    flash("Mapping removed.", "success")
    return redirect(url_for("inventory.test_detail", test_id=test_id))


# ── Packages catalogue ────────────────────────────────────────────────────────

@inventory_bp.route("/packages")
@login_required
@_inventory_access
def packages():
    pkgs = PackageCatalogue.query.order_by(PackageCatalogue.name).all()
    return render_template("inventory/packages.html", packages=pkgs)


@inventory_bp.route("/packages/add", methods=["POST"])
@login_required
@_inventory_access
def add_package():
    name     = request.form.get("name", "").strip()
    lis_name = request.form.get("labsmart_name", "").strip()
    price_s  = request.form.get("price", "").strip()
    if not name or not lis_name:
        flash("Package name and LIS name are required.", "error")
        return redirect(url_for("inventory.packages"))
    price = float(price_s) if price_s else None
    p = PackageCatalogue(name=name, labsmart_name=lis_name, price=price)
    db.session.add(p)
    db.session.flush()
    if price is not None:
        _log_price("package", p.id, None, price)
    db.session.commit()
    flash(f"Package '{name}' added.", "success")
    return redirect(url_for("inventory.package_detail", pkg_id=p.id))


@inventory_bp.route("/packages/<int:pkg_id>")
@login_required
@_inventory_access
def package_detail(pkg_id):
    pkg         = PackageCatalogue.query.get_or_404(pkg_id)
    all_tests   = TestCatalogue.query.filter_by(is_active=True).order_by(TestCatalogue.name).all()
    linked_ids  = {pt.test_id for pt in pkg.tests}
    try:
        price_audit = (PriceAuditLog.query
                       .filter_by(entity_type="package", entity_id=pkg_id)
                       .order_by(PriceAuditLog.changed_at.desc()).limit(20).all())
    except Exception:
        db.session.rollback()
        price_audit = []
    return render_template("inventory/package_detail.html",
        package=pkg, all_tests=all_tests, linked_ids=linked_ids,
        price_audit=price_audit,
    )


@inventory_bp.route("/packages/<int:pkg_id>/edit", methods=["POST"])
@login_required
@_inventory_access
def edit_package(pkg_id):
    pkg = PackageCatalogue.query.get_or_404(pkg_id)
    pkg.name          = request.form.get("name", pkg.name).strip()
    pkg.labsmart_name = request.form.get("labsmart_name", pkg.labsmart_name).strip()
    price_s   = request.form.get("price", "").strip()
    new_price = float(price_s) if price_s else None
    old_price = float(pkg.price) if pkg.price is not None else None
    if new_price != old_price:
        _log_price("package", pkg_id, old_price, new_price)
    pkg.price = new_price
    db.session.commit()
    flash("Package updated.", "success")
    return redirect(url_for("inventory.package_detail", pkg_id=pkg_id))


@inventory_bp.route("/packages/<int:pkg_id>/tests", methods=["POST"])
@login_required
@_inventory_access
def update_package_tests(pkg_id):
    pkg = PackageCatalogue.query.get_or_404(pkg_id)
    test_ids = set(request.form.getlist("test_ids[]", type=int))
    existing_ids = {pt.test_id for pt in pkg.tests}
    # add new
    for tid in test_ids - existing_ids:
        db.session.add(PackageTest(package_id=pkg_id, test_id=tid))
    # remove unselected
    for pt in pkg.tests:
        if pt.test_id not in test_ids:
            db.session.delete(pt)
    db.session.commit()
    flash("Package tests updated.", "success")
    return redirect(url_for("inventory.package_detail", pkg_id=pkg_id))


# ── Receive stock ─────────────────────────────────────────────────────────────

@inventory_bp.route("/receive", methods=["GET", "POST"])
@login_required
@_stock_manager
def receive_stock():
    branches  = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    all_items = InventoryItem.query.filter_by(is_active=True).order_by(InventoryItem.name).all()

    if request.method == "POST":
        item_id  = request.form.get("item_id", type=int)
        branch_id= request.form.get("branch_id", type=int) or None
        qty      = request.form.get("qty", "").strip()
        unit_cost= request.form.get("unit_cost", "").strip()
        batch    = request.form.get("batch_number", "").strip() or None
        expiry_s = request.form.get("expiry_date", "").strip()
        notes    = request.form.get("notes", "").strip() or None

        if not item_id or not qty:
            flash("Item and quantity are required.", "error")
            return redirect(url_for("inventory.receive_stock"))

        expiry    = _parse_date(expiry_s)
        recv_item = InventoryItem.query.get(item_id)
        use_packs = request.form.get("use_packs") == "1"
        qty_f     = float(qty)
        packs_note = ""
        if use_packs and recv_item and recv_item.pack_size:
            packs_note = f"{qty_f} {recv_item.purchase_unit or 'pack'}(s) × {recv_item.pack_size} = "
            qty_f = qty_f * recv_item.pack_size
        _apply_txn(
            item_id=item_id, branch_id=branch_id,
            txn_type="receive", qty=qty_f,
            user_id=current_user.id,
            unit_cost=float(unit_cost) if unit_cost else None,
            batch=batch, expiry=expiry, notes=notes,
        )
        db.session.commit()
        flash(f"Received {packs_note}{qty_f:g} {recv_item.unit if recv_item else ''}(s) of '{recv_item.name if recv_item else ''}'.", "success")
        return redirect(url_for("inventory.dashboard"))

    return render_template("inventory/receive.html",
        branches=branches, all_items=all_items,
    )


# ── Adjust / write-off ────────────────────────────────────────────────────────

@inventory_bp.route("/adjust", methods=["GET", "POST"])
@login_required
@_stock_manager
def adjust_stock():
    branches  = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    all_items = InventoryItem.query.filter_by(is_active=True).order_by(InventoryItem.name).all()

    if request.method == "POST":
        item_id  = request.form.get("item_id", type=int)
        branch_id= request.form.get("branch_id", type=int) or None
        txn_type = request.form.get("txn_type", "adjust")
        qty_raw  = request.form.get("qty", "").strip()
        direction= request.form.get("direction", "out")  # in | out
        notes    = request.form.get("notes", "").strip() or None

        if not item_id or not qty_raw:
            flash("Item and quantity are required.", "error")
            return redirect(url_for("inventory.adjust_stock"))

        qty = float(qty_raw)
        if direction == "out":
            qty = -abs(qty)
        else:
            qty = abs(qty)

        _apply_txn(item_id=item_id, branch_id=branch_id, txn_type=txn_type,
                   qty=qty, user_id=current_user.id, notes=notes)
        db.session.commit()
        item = InventoryItem.query.get(item_id)
        flash(f"Adjustment recorded for '{item.name}'.", "success")
        return redirect(url_for("inventory.dashboard"))

    return render_template("inventory/adjust.html",
        branches=branches, all_items=all_items,
    )


# ── LIS Uploads ───────────────────────────────────────────────────────────────

@inventory_bp.route("/uploads")
@login_required
@_upload_access
def uploads():
    uploads_list = LisUpload.query.order_by(LisUpload.created_at.desc()).limit(50).all()
    return render_template("inventory/uploads.html", uploads=uploads_list)


@inventory_bp.route("/uploads/new", methods=["POST"])
@login_required
@_upload_access
def upload_lis():
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("Please select a CSV file.", "error")
        return redirect(url_for("inventory.uploads"))

    try:
        content = f.read().decode("utf-8-sig", errors="replace")
    except Exception:
        flash("Could not read file. Ensure it is a UTF-8 CSV.", "error")
        return redirect(url_for("inventory.uploads"))

    reader = csv.DictReader(io.StringIO(content))
    fieldnames = [fn.strip() for fn in (reader.fieldnames or [])]

    required = {"Investigations", "Case Type", "Date", "Collection centre", "Canceled"}
    missing  = required - set(fieldnames)
    if missing:
        flash(f"CSV missing expected columns: {', '.join(missing)}", "error")
        return redirect(url_for("inventory.uploads"))

    branches  = Branch.query.all()
    all_tests = TestCatalogue.query.filter_by(is_active=True).all()
    all_pkgs  = PackageCatalogue.query.filter_by(is_active=True).all()

    upload = LisUpload(filename=f.filename, uploaded_by=current_user.id)
    db.session.add(upload)
    db.session.flush()  # get upload.id

    row_count  = 0
    dates      = []
    # unique investigation names that need resolution: {raw_name: count}
    inv_counts = {}

    for raw_row in reader:
        # normalise header names (strip whitespace)
        row = {k.strip(): (v or "").strip() for k, v in raw_row.items()}
        investigations = row.get("Investigations", "")
        if not investigations:
            continue

        cancelled = row.get("Canceled", "false").lower() in ("true", "1", "yes")
        raw_date  = row.get("Date", "")
        branch_raw= row.get("Collection centre", "")
        case_type = row.get("Case Type", "Lab").strip()

        parsed_date = _parse_date(raw_date)
        if parsed_date:
            dates.append(parsed_date)

        branch_id = _match_branch(branch_raw, branches)

        lr = LisUploadRow(
            upload_id=upload.id,
            case_id=row.get("Case Id", ""),
            case_type=case_type,
            case_date=parsed_date,
            investigations_raw=investigations,
            branch_raw=branch_raw,
            branch_id=branch_id,
            is_cancelled=cancelled,
        )
        db.session.add(lr)
        row_count += 1

        if not cancelled:
            for inv in investigations.split(","):
                inv = inv.strip()
                if inv:
                    inv_counts[inv] = inv_counts.get(inv, 0) + 1

    # ── Fuzzy-match each unique investigation name ────────────────────────────
    # Build quick-lookup dicts
    test_exact  = {t.labsmart_name.strip().lower(): t for t in all_tests}
    pkg_exact   = {p.labsmart_name.strip().lower(): p for p in all_pkgs}

    matched_names   = set()
    unmatched_names = {}  # raw_name: (count, suggested_test, score)

    for raw_name, count in inv_counts.items():
        key = raw_name.strip().lower()
        if key in test_exact or key in pkg_exact:
            matched_names.add(raw_name)
            continue

        # fuzzy match against tests
        best_test, t_score = _best_test_match(raw_name, all_tests)
        # fuzzy match against packages
        best_pkg,  p_score = _best_package_match(raw_name, all_pkgs)

        if t_score >= AUTO_MATCH_THRESHOLD:
            matched_names.add(raw_name)
            continue
        if p_score >= AUTO_MATCH_THRESHOLD:
            matched_names.add(raw_name)
            continue

        # needs human review
        suggested = best_test if t_score >= p_score else None
        score     = t_score   if t_score >= p_score else p_score
        unmatched_names[raw_name] = (count, suggested, score)

    for raw_name, (count, suggested, score) in unmatched_names.items():
        ui = UnmatchedInvestigation(
            upload_id=upload.id,
            raw_name=raw_name,
            occurrence_count=count,
            suggested_test_id=suggested.id if (suggested and score >= SUGGEST_THRESHOLD) else None,
            suggested_score=round(score, 4) if score >= SUGGEST_THRESHOLD else None,
        )
        db.session.add(ui)

    upload.record_count    = row_count
    upload.matched_count   = len(matched_names)
    upload.unmatched_count = len(unmatched_names)
    upload.start_date      = min(dates) if dates else None
    upload.end_date        = max(dates) if dates else None

    db.session.commit()

    if upload.unmatched_count == 0:
        flash(f"Upload complete — {row_count} cases, all investigations matched. Ready to apply.", "success")
    else:
        flash(
            f"Upload complete — {row_count} cases, {upload.unmatched_count} unmatched investigation(s) need review.",
            "warning",
        )
    return redirect(url_for("inventory.upload_review", upload_id=upload.id))


@inventory_bp.route("/uploads/<int:upload_id>")
@login_required
@_upload_access
def upload_review(upload_id):
    upload    = LisUpload.query.get_or_404(upload_id)
    all_tests = TestCatalogue.query.filter_by(is_active=True).order_by(TestCatalogue.name).all()
    pending   = [u for u in upload.unmatched if u.action == "pending"]
    return render_template("inventory/upload_review.html",
        upload=upload, all_tests=all_tests, pending=pending,
    )


@inventory_bp.route("/uploads/<int:upload_id>/resolve", methods=["POST"])
@login_required
@_upload_access
def resolve_unmatched(upload_id):
    upload = LisUpload.query.get_or_404(upload_id)
    if upload.status == "applied":
        flash("This upload has already been applied.", "error")
        return redirect(url_for("inventory.upload_review", upload_id=upload_id))

    for ui in upload.unmatched:
        action   = request.form.get(f"action_{ui.id}", "pending")
        test_id  = request.form.get(f"test_id_{ui.id}", type=int)
        if action == "mapped" and test_id:
            ui.action           = "mapped"
            ui.resolved_test_id = test_id
            ui.resolved_by      = current_user.id
            ui.resolved_at      = datetime.now(timezone.utc)
        elif action == "skipped":
            ui.action = "skipped"
            ui.resolved_by = current_user.id
            ui.resolved_at = datetime.now(timezone.utc)

    db.session.commit()
    flash("Resolutions saved.", "success")
    return redirect(url_for("inventory.upload_review", upload_id=upload_id))


@inventory_bp.route("/uploads/<int:upload_id>/apply", methods=["POST"])
@login_required
@_upload_access
def apply_upload(upload_id):
    upload = LisUpload.query.get_or_404(upload_id)
    if upload.status == "applied":
        flash("Already applied.", "error")
        return redirect(url_for("inventory.upload_review", upload_id=upload_id))

    pending = [u for u in upload.unmatched if u.action == "pending"]
    if pending:
        flash(f"{len(pending)} unmatched investigation(s) still need resolution before applying.", "warning")
        return redirect(url_for("inventory.upload_review", upload_id=upload_id))

    all_tests  = TestCatalogue.query.filter_by(is_active=True).all()
    all_pkgs   = PackageCatalogue.query.filter_by(is_active=True).all()

    test_exact = {t.labsmart_name.strip().lower(): t for t in all_tests}
    pkg_exact  = {p.labsmart_name.strip().lower(): p for p in all_pkgs}

    # Resolution map from user-reviewed unmatched items
    resolution_map = {}
    for ui in upload.unmatched:
        if ui.action == "mapped" and ui.resolved_test_id:
            resolution_map[ui.raw_name.strip().lower()] = ui.resolved_test_id

    # Accumulate consumption: {(item_id, branch_id): total_qty}
    # Accumulate test volumes: {(test_id, branch_id): run_count}
    consumption  = {}
    test_volumes = {}

    for row in upload.rows:
        if row.is_cancelled or not row.investigations_raw:
            continue
        branch_id = row.branch_id  # may be None (central)

        for inv in row.investigations_raw.split(","):
            inv = inv.strip()
            if not inv:
                continue
            key = inv.lower()

            # 1. Exact test match
            test = test_exact.get(key)
            if test:
                _accumulate(consumption, test, branch_id)
                vk = (test.id, branch_id)
                test_volumes[vk] = test_volumes.get(vk, 0) + 1
                continue

            # 2. Exact package match → expand to constituent tests
            pkg = pkg_exact.get(key)
            if pkg:
                for pt in pkg.tests:
                    _accumulate(consumption, pt.test, branch_id)
                    vk = (pt.test_id, branch_id)
                    test_volumes[vk] = test_volumes.get(vk, 0) + 1
                continue

            # 3. Fuzzy auto-match (re-check threshold)
            best_t, t_score = _best_test_match(inv, all_tests)
            best_p, p_score = _best_package_match(inv, all_pkgs)
            if t_score >= AUTO_MATCH_THRESHOLD:
                _accumulate(consumption, best_t, branch_id)
                vk = (best_t.id, branch_id)
                test_volumes[vk] = test_volumes.get(vk, 0) + 1
                continue
            if p_score >= AUTO_MATCH_THRESHOLD:
                for pt in best_p.tests:
                    _accumulate(consumption, pt.test, branch_id)
                    vk = (pt.test_id, branch_id)
                    test_volumes[vk] = test_volumes.get(vk, 0) + 1
                continue

            # 4. Resolved by user
            resolved_test_id = resolution_map.get(key)
            if resolved_test_id:
                t = TestCatalogue.query.get(resolved_test_id)
                if t:
                    _accumulate(consumption, t, branch_id)
                    vk = (t.id, branch_id)
                    test_volumes[vk] = test_volumes.get(vk, 0) + 1

    # Write reagent consumption transactions
    ref = f"LIS-{upload.id}"
    for (item_id, branch_id), qty in consumption.items():
        _apply_txn(item_id=item_id, branch_id=branch_id, txn_type="consume",
                   qty=-abs(qty), user_id=current_user.id, reference=ref)

    # Write test volume logs
    upload_date = upload.created_at.date() if upload.created_at else None
    for (test_id, branch_id), count in test_volumes.items():
        db.session.add(TestVolumeLog(
            test_id=test_id, branch_id=branch_id, upload_id=upload_id,
            volume=count, period_start=upload_date, period_end=upload_date,
        ))

    upload.status     = "applied"
    upload.applied_at = datetime.now(timezone.utc)
    db.session.commit()

    flash(f"Upload applied — {len(consumption)} stock depletion(s) recorded.", "success")
    return redirect(url_for("inventory.uploads"))


def _accumulate(consumption, test, branch_id):
    """Add reagent consumption for one test to the accumulator dict."""
    for m in test.reagent_mappings:
        k = (m.item_id, branch_id)
        consumption[k] = consumption.get(k, 0) + float(m.qty_per_test)


# ── Reports ───────────────────────────────────────────────────────────────────

@inventory_bp.route("/reports")
@login_required
@_inventory_access
def reports():
    from sqlalchemy import and_
    date_from_s = request.args.get("date_from", "")
    date_to_s   = request.args.get("date_to", "")
    branch_id   = request.args.get("branch_id", type=int)
    branches    = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()

    date_from = _parse_date(date_from_s)
    date_to   = _parse_date(date_to_s)

    q = (
        db.session.query(
            InventoryItem.id,
            InventoryItem.name,
            InventoryItem.unit,
            InventoryItem.unit_price,
            func.sum(StockTransaction.qty).label("net_qty"),
            func.sum(func.abs(StockTransaction.qty)).filter(StockTransaction.qty < 0).label("consumed"),
            func.sum(StockTransaction.qty).filter(StockTransaction.qty > 0).label("received"),
        )
        .join(StockTransaction, StockTransaction.item_id == InventoryItem.id)
        .filter(StockTransaction.txn_type.in_(["receive", "consume", "adjust", "writeoff"]))
    )

    if date_from:
        q = q.filter(StockTransaction.created_at >= datetime(date_from.year, date_from.month, date_from.day))
    if date_to:
        dt_end = datetime(date_to.year, date_to.month, date_to.day, 23, 59, 59)
        q = q.filter(StockTransaction.created_at <= dt_end)
    if branch_id:
        q = q.filter(StockTransaction.branch_id == branch_id)

    rows = q.group_by(InventoryItem.id, InventoryItem.name, InventoryItem.unit, InventoryItem.unit_price)\
            .order_by(func.sum(func.abs(StockTransaction.qty)).filter(StockTransaction.qty < 0).desc().nullslast())\
            .all()

    return render_template("inventory/reports.html",
        rows=rows, branches=branches,
        branch_id=branch_id,
        date_from=date_from_s, date_to=date_to_s,
    )


# ── CSV import / export ───────────────────────────────────────────────────────

def _csv_response(rows, filename):
    """Build a CSV Flask response from a list-of-lists."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@inventory_bp.route("/items/download")
@login_required
@_inventory_access
def download_items_csv():
    """Export all active inventory items as a CSV (also usable as an upload template)."""
    items = InventoryItem.query.order_by(InventoryItem.name).all()
    rows = [["Name", "Item Code", "Category", "Unit", "Pack Size", "Unit Price (N)", "Reorder Level", "Notes"]]
    for it in items:
        rows.append([
            it.name, it.item_code or "", it.category,
            it.unit, it.pack_size or "", it.unit_price or "", it.reorder_level or "",
            it.notes or "",
        ])
    return _csv_response(rows, "inventory_items.csv")


@inventory_bp.route("/items/upload", methods=["POST"])
@login_required
@_inventory_access
def upload_items_csv():
    """Bulk-import items from CSV. Updates existing (by item_code or exact name), creates new."""
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("Please select a CSV file.", "error")
        return redirect(url_for("inventory.items"))

    try:
        content = f.read().decode("utf-8-sig", errors="replace")
    except Exception:
        flash("Could not read file.", "error")
        return redirect(url_for("inventory.items"))

    reader = csv.DictReader(io.StringIO(content))
    # normalise header names (strip whitespace, lowercase)
    def norm(h): return h.strip().lower().replace(" ", "_").replace("(n)", "").replace("(₦)", "").rstrip("_")

    added = updated = skipped = 0
    for raw_row in reader:
        row = {norm(k): (v or "").strip() for k, v in raw_row.items()}
        name = row.get("name", "").strip()
        if not name:
            skipped += 1
            continue

        code = row.get("item_code", "") or None
        cat  = row.get("category", "lab_reagent").strip() or "lab_reagent"
        unit = row.get("unit", "unit").strip() or "unit"
        pack_s   = row.get("pack_size", "") or ""
        price_s  = row.get("unit_price", "") or ""
        reorder_s= row.get("reorder_level", "") or ""
        notes    = row.get("notes", "") or None

        def to_num(s):
            import re
            s = re.sub(r'[^\d.]', '', str(s))
            try: return float(s) if s else None
            except: return None

        pack  = int(to_num(pack_s))  if pack_s  and to_num(pack_s)  else None
        price = to_num(price_s)
        reorder = to_num(reorder_s)

        # Look for existing by code first, then by exact name
        existing = None
        if code:
            existing = InventoryItem.query.filter_by(item_code=code).first()
        if not existing:
            existing = InventoryItem.query.filter(
                func.lower(InventoryItem.name) == name.lower()
            ).first()

        if existing:
            existing.name          = name
            existing.item_code     = code
            existing.category      = cat
            existing.unit          = unit
            existing.pack_size     = pack
            existing.unit_price    = price
            existing.reorder_level = reorder
            existing.notes         = notes
            updated += 1
        else:
            db.session.add(InventoryItem(
                name=name, item_code=code, category=cat, unit=unit,
                pack_size=pack, unit_price=price, reorder_level=reorder, notes=notes,
            ))
            added += 1

    try:
        db.session.commit()
        flash(f"Items import complete — {added} added, {updated} updated, {skipped} skipped.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Import failed: {e}", "error")
    return redirect(url_for("inventory.items"))


@inventory_bp.route("/receive/template")
@login_required
@_stock_manager
def receive_template():
    """Download a blank CSV template for bulk stock receipt."""
    branches  = Branch.query.filter_by(is_active=True).order_by(Branch.name).all()
    all_items = InventoryItem.query.filter_by(is_active=True).order_by(InventoryItem.name).all()
    rows = [
        ["# Bulk Receive Stock Template — fill in rows below, delete these comment rows before uploading"],
        [f"# Branches available: {', '.join(b.name for b in branches)} (or leave blank for Central)"],
        ["Item Name", "Item Code", "Branch", "Qty", "Unit Cost (N)", "Batch Number", "Expiry Date (YYYY-MM-DD)", "Notes"],
    ]
    # Pre-fill item names as hints (no qty/cost — user fills those)
    for it in all_items[:5]:
        rows.append([it.name, it.item_code or "", "", "", it.unit_price or "", "", "", ""])
    rows.append(["... add more rows as needed", "", "", "", "", "", "", ""])
    return _csv_response(rows, "receive_stock_template.csv")


@inventory_bp.route("/receive/upload", methods=["POST"])
@login_required
@_stock_manager
def bulk_receive():
    """Bulk-receive stock from a filled CSV template."""
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("Please select a CSV file.", "error")
        return redirect(url_for("inventory.receive_stock"))

    try:
        content = f.read().decode("utf-8-sig", errors="replace")
    except Exception:
        flash("Could not read file.", "error")
        return redirect(url_for("inventory.receive_stock"))

    branches  = {b.name.lower(): b.id for b in Branch.query.all()}

    def norm(h): return h.strip().lower().replace(" ", "_").replace("(n)", "").replace("(₦)", "").rstrip("_")

    received_count = 0
    errors = []

    reader = csv.DictReader(io.StringIO(content))
    for i, raw_row in enumerate(reader, start=2):
        row = {norm(k): (v or "").strip() for k, v in raw_row.items()}

        # Skip comment/empty rows
        name     = row.get("item_name", "").strip()
        if not name or name.startswith("#") or name.startswith("..."):
            continue

        code  = row.get("item_code", "") or None
        qty_s = row.get("qty", "").strip()
        if not qty_s:
            errors.append(f"Row {i}: qty missing for '{name}'")
            continue

        import re
        def to_num(s):
            s = re.sub(r'[^\d.]', '', str(s))
            try: return float(s) if s else None
            except: return None

        qty = to_num(qty_s)
        if not qty or qty <= 0:
            errors.append(f"Row {i}: invalid qty for '{name}'")
            continue

        # Find item
        item = None
        if code:
            item = InventoryItem.query.filter_by(item_code=code).first()
        if not item:
            item = InventoryItem.query.filter(
                func.lower(InventoryItem.name) == name.lower()
            ).first()
        if not item:
            errors.append(f"Row {i}: item not found — '{name}'")
            continue

        branch_raw = row.get("branch", "").strip().lower()
        branch_id  = branches.get(branch_raw) if branch_raw else None

        unit_cost = to_num(row.get("unit_cost", ""))
        batch     = row.get("batch_number", "") or None
        expiry    = _parse_date(row.get("expiry_date", ""))
        notes     = row.get("notes", "") or None

        _apply_txn(item_id=item.id, branch_id=branch_id, txn_type="receive",
                   qty=qty, user_id=current_user.id,
                   unit_cost=unit_cost, batch=batch, expiry=expiry, notes=notes)
        received_count += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f"Bulk receive failed: {e}", "error")
        return redirect(url_for("inventory.receive_stock"))

    msg = f"Bulk receive complete — {received_count} line(s) recorded."
    if errors:
        msg += f" {len(errors)} row(s) skipped: {'; '.join(errors[:3])}{'…' if len(errors) > 3 else ''}"
        flash(msg, "warning")
    else:
        flash(msg, "success")
    return redirect(url_for("inventory.dashboard"))


# ── Inventory Guide ───────────────────────────────────────────────────────────

@inventory_bp.route("/guide")
@login_required
@_inventory_access
def guide():
    return render_template("inventory/guide.html")


# ── In-Transit Stock ──────────────────────────────────────────────────────────

@inventory_bp.route("/in-transit")
@login_required
@_inventory_access
def in_transit_list():
    from models import InTransitStock
    q = InTransitStock.query.order_by(InTransitStock.created_at.desc())
    if not current_user.is_mds:
        user_branch_ids = [b.id for b in current_user.branches]
        q = q.filter(InTransitStock.branch_id.in_(user_branch_ids))
    items = q.all()
    return render_template("inventory/in_transit.html", items=items)


@inventory_bp.route("/in-transit/<int:transit_id>/confirm", methods=["POST"])
@login_required
@_inventory_access
def confirm_receipt(transit_id):
    from models import InTransitStock
    transit = InTransitStock.query.get_or_404(transit_id)
    if transit.status != "in_transit":
        flash("Already processed.", "warning")
        return redirect(url_for("inventory.in_transit_list"))

    if not current_user.is_mds:
        user_branch_ids = [b.id for b in current_user.branches]
        if transit.branch_id not in user_branch_ids:
            flash("Access denied.", "error")
            return redirect(url_for("inventory.in_transit_list"))

    ref_label = transit.payment_request.reference if transit.payment_request else f"PR#{transit.payment_request_id}"
    inv_item  = InventoryItem.query.get(transit.inventory_item_id)
    recv_qty  = float(transit.qty)
    if inv_item and inv_item.pack_size and recv_qty > 0:
        recv_qty = recv_qty * inv_item.pack_size
    _apply_txn(
        item_id=transit.inventory_item_id,
        branch_id=transit.branch_id,
        txn_type="receive",
        qty=recv_qty,
        user_id=current_user.id,
        reference=ref_label,
        notes=f"Confirmed receipt from purchase request {ref_label}",
    )

    transit.status = "received"
    transit.confirmed_by = current_user.id
    transit.confirmed_at = datetime.now(timezone.utc)
    db.session.commit()

    flash(f"Receipt confirmed — {transit.inventory_item.name} stock updated.", "success")
    return redirect(url_for("inventory.in_transit_list"))


@inventory_bp.route("/in-transit/<int:transit_id>/cancel", methods=["POST"])
@login_required
def cancel_transit(transit_id):
    from models import InTransitStock
    if not current_user.is_mds:
        flash("MDS access required.", "error")
        return redirect(url_for("inventory.in_transit_list"))
    transit = InTransitStock.query.get_or_404(transit_id)
    if transit.status != "in_transit":
        flash("Already processed.", "warning")
        return redirect(url_for("inventory.in_transit_list"))
    transit.status = "cancelled"
    db.session.commit()
    flash("Transit entry cancelled.", "warning")
    return redirect(url_for("inventory.in_transit_list"))



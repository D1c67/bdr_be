"""Recipe data for assistant routines (docs/ASSISTANT_PHASE3_PLAN.md 3.2.2).

Membership and readiness are the due digest's own (due_digest.day_bounds,
due_today_projects, readiness_rows) and the missing vendor categories come
from due_reminders.missing_quote_categories, so a routine and the 6:03 AM
digest never disagree about which bids are due or what they lack. Overdue
bids are not included (the digest's rule).

Sync throughout: `sb` is the service-role client. This module is outside
app/assistant/, so it may use the client directly; it reads no assistant_*
table. Callers: the recipe route (through the shim, as the owner's
principal) and the Preview route.
"""

from __future__ import annotations

import concurrent.futures
import html
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.assistant import recipes
from app.core.roles import ACTUAL_BID_VIEWER_ROLES
from app.services import due_digest
from app.services.datetime_format import _parse_ts
from app.services.due_reminders import _page_all, _pg_ts, missing_quote_categories
from app.services.email_branding import (
    _BORDER,
    _FONT,
    _MUTED,
    _NAVY,
    _button,
    render_branded_html,
)

logger = logging.getLogger(__name__)

PT = due_digest.DIGEST_TZ

# The projects columns due_digest.due_today_projects selects, so a watched
# project has the same shape as a due one.
_PROJECT_COLUMNS = "id, name, number, internal_bid_at, current_stage"
# Rows the email lists; the rest are counted ("And N more.").
EMAIL_MAX_ROWS = 50


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=PT).astimezone(timezone.utc)


def _day_label(day: date) -> str:
    return f"{day.strftime('%A, %B')} {day.day}"


def window_for(recipe: str, at_utc: datetime, horizon_days: int | None, *, period: str | None = None,
               week: str | None = None) -> tuple[datetime, datetime, str]:
    """(start, end, label). due_today: due_digest.day_bounds of the Pacific
    day. due_this_week: start of today (Pacific) to the end of the coming
    Sunday (on a Sunday, today). missing_quotes and labor_not_entered:
    start of today to + horizon_days days (HORIZON_DEFAULT when None). Both
    bounds are aware UTC and the end is exclusive; every day boundary is a
    Pacific midnight, so a DST change inside the window moves nothing.
    estimator_vs_bid: the Pacific calendar month of `period` (default last
    month), labelled "September 2026". quote_readiness: Monday 00:00 to
    Saturday 00:00 Pacific of `week` (default next week), labelled
    "Monday, October 26 to Friday, October 30"."""
    recipes.get_recipe(recipe)
    if at_utc.tzinfo is None:
        at_utc = at_utc.replace(tzinfo=timezone.utc)
    today = at_utc.astimezone(PT).date()
    if recipe == "estimator_vs_bid":
        first, following = recipes.resolve_period(period, today)
        return _day_start(first), _day_start(following), recipes.period_label(first)
    if recipe == "quote_readiness":
        monday = recipes.resolve_week(week, today)
        friday = monday + timedelta(days=4)
        return _day_start(monday), _day_start(monday + timedelta(days=5)), f"{_day_label(monday)} to {_day_label(friday)}"
    if recipe == "due_today":
        start, end = due_digest.day_bounds(at_utc.astimezone(PT))
        return start, end, _day_label(today)
    if recipe == "due_this_week":
        days = (6 - today.weekday()) % 7 + 1
    else:
        days = horizon_days if horizon_days is not None else recipes.HORIZON_DEFAULT
    last = today + timedelta(days=days - 1)
    label = _day_label(today) if last == today else f"{_day_label(today)} to {_day_label(last)}"
    return _day_start(today), _day_start(last + timedelta(days=1)), label


def _project_row(sb, project_id: str) -> dict | None:
    rows = (
        sb.table("projects")
        .select(f"{_PROJECT_COLUMNS}, abandoned_at, failed_to_bid_at")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _state_of(row: dict | None) -> str:
    if row is None:
        return "missing"
    if (row.get("current_stage") in due_digest.EXCLUDED_STAGES
            or row.get("abandoned_at") or row.get("failed_to_bid_at")):
        return "closed"
    return "open"


def watch_state(sb, project_id: str) -> str:
    """"open", "closed" (current_stage in due_digest.EXCLUDED_STAGES, or
    abandoned_at or failed_to_bid_at set) or "missing" (deleted or merged)."""
    return _state_of(_project_row(sb, project_id))


def rows_for(sb, recipe: str, *, at_utc: datetime, horizon_days: int | None = None,
             project_id: str | None = None, period: str | None = None, week: str | None = None,
             role: Any = None) -> dict:
    """{recipe, window: {start, end, label}, rows: [{project, checks,
    severity, missing_categories}], watch: {project_id, state} | None}.
    missing_quotes keeps rows whose checks.has_quotes is false (with
    missing_categories, None when the project has no RFQ yet);
    labor_not_entered keeps rows whose checks.has_labor is false. A watch
    reads the one project while it is still open, whatever its due date;
    a closed or missing watch has no rows. Raises recipes.RecipeError for
    an unknown recipe or a bad parameter.

    The phase 4 recipes (docs/ASSISTANT_PHASE4_PLAN.md 3.10.2) take `period`
    (estimator_vs_bid) or `week` (quote_readiness) and the caller's `role`:
    estimator_vs_bid raises RecipeError(code="recipe_unavailable") for a
    role outside ACTUAL_BID_VIEWER_ROLES (and for no role); quote_readiness
    adds the actual bid date and the "Bid date" column for those roles only.
    Their rows add `extra` (see `estimator_vs_bid` and `quote_readiness`)
    and their payload adds `totals` / `extra` (estimator_vs_bid) or nothing
    more (quote_readiness)."""
    params = recipes.validate_params(recipe, horizon_days, project_id, period=period, week=week)
    if recipe == "estimator_vs_bid":
        return estimator_vs_bid(sb, params.get("period"), at_utc, role)
    if recipe == "quote_readiness":
        return quote_readiness(sb, params.get("week"), at_utc, role)
    start, end, label = window_for(recipe, at_utc, params.get("horizon_days"))
    watch = None
    if "project_id" in params:
        pid = params["project_id"]
        row = _project_row(sb, pid)
        state = _state_of(row)
        watch = {"project_id": pid, "state": state}
        projects = (
            [{k: row.get(k) for k in ("id", "name", "number", "internal_bid_at", "current_stage")}]
            if state == "open" else []
        )
    else:
        projects = due_digest.due_today_projects(sb, start, end)

    rows = [{**r, "missing_categories": None} for r in due_digest.readiness_rows(sb, projects)]
    if recipe == "missing_quotes":
        rows = [r for r in rows if not r["checks"]["has_quotes"]]
        if rows:
            categories = missing_quote_categories(sb, [r["project"]["id"] for r in rows])
            for r in rows:
                r["missing_categories"] = categories.get(r["project"]["id"])
    elif recipe == "labor_not_entered":
        rows = [r for r in rows if not r["checks"]["has_labor"]]
    return {
        "recipe": recipe,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "label": label},
        "rows": rows,
        "watch": watch,
    }


# ── phase 4 recipes (docs/ASSISTANT_PHASE4_PLAN.md 3.10.2) ─────────────────────

ESTIMATE_POOL = 6                    # threads reading estimator workbooks
ESTIMATE_BUDGET_SECONDS = 40.0       # total wait before rows say not_loaded_yet
NOT_LOADED_MESSAGE = "Ask again in a minute; the workbook is still being read."
NO_OUR_PRICE_MESSAGE = "We have no recorded price for this bid."
RETURNED_WITHOUT_ESTIMATE_MESSAGE = (
    "The estimator returned the work on {date} but sent no estimate file, so there is no estimator price.")
# Categories that never count toward the key-quote test (the owner's rule,
# matched on the lowercased material_categories.name).
KEY_QUOTE_EXCLUDED = ("general material", "demolition", "lighting", "low voltage")

_CENT = Decimal("0.01")
_PROJECT_COLS_P4 = "id, name, number, internal_bid_at, actual_bid_at, current_stage, abandoned_at, failed_to_bid_at"
_SENT_COLS = ("project_id, gc_id, status, sent_at, material_amount, gear_amount, underground_amount, "
              "low_voltage_amount, labor_amount")


def _ts(value) -> datetime | None:
    try:
        parsed = _parse_ts(value) if value else None
    except (ValueError, TypeError, AttributeError):
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _amount(value: Decimal | None) -> str | None:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP)) if value is not None else None


def _in_paged(sb, table: str, columns: str, column: str, ids: list, *, eq: tuple[str, str] | None = None,
              chunk: int = 100) -> list[dict]:
    """Every row of `table` whose `column` is in `ids`: ids in chunks of 100
    (URL length) and each chunk drained past PostgREST's response cap."""
    out: list[dict] = []
    ids = list(ids)
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]

        def build(lo, hi, part=part):
            query = sb.table(table).select(columns).in_(column, part)
            if eq:
                query = query.eq(*eq)
            return query.order("id").range(lo, hi)

        out.extend(_page_all(build))
    return out


def _projects_between(sb, column: str, start: datetime, end: datetime) -> list[dict]:
    """Bids (not pm_only, not cp_only, not an RFP test session) whose `column`
    falls in [start, end)."""
    return _page_all(
        lambda lo, hi: sb.table("projects")
        .select(_PROJECT_COLS_P4)
        .neq("current_stage", "pm_only")
        .neq("current_stage", "cp_only")
        .is_("test_session_id", "null")
        .gte(column, _pg_ts(start))
        .lt(column, _pg_ts(end))
        .order("id")
        .range(lo, hi)
    )


def _project_out(project: dict, viewer: bool) -> dict:
    out = {k: project.get(k) for k in ("id", "name", "number", "internal_bid_at", "current_stage")}
    if viewer:
        out["actual_bid_at"] = project.get("actual_bid_at")
    return out


def _load_estimates(sb, project_ids: list[str]) -> dict[str, dict | None]:
    """auto_send_estimate.load_estimate per project in a pool of
    ESTIMATE_POOL threads, waiting ESTIMATE_BUDGET_SECONDS in all. A project
    whose workbook is not read in time maps to None (the thread keeps
    running and fills the auto_send_estimates cache, so the next ask is
    fast)."""
    from app.services import auto_send_estimate

    results: dict[str, dict | None] = {pid: None for pid in project_ids}
    if not project_ids:
        return results
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=ESTIMATE_POOL)
    futures = {pool.submit(auto_send_estimate.load_estimate, pid, sb=sb): pid for pid in project_ids}
    done, _pending = concurrent.futures.wait(futures, timeout=ESTIMATE_BUDGET_SECONDS)
    for future in done:
        pid = futures[future]
        try:
            results[pid] = future.result()
        except Exception:  # noqa: BLE001 - load_estimate never raises; belt and braces
            logger.exception("assistant recipe: estimate load failed for project %s", pid)
            results[pid] = {"ok": False, "code": "lookup_failed", "message": None}
    pool.shutdown(wait=False, cancel_futures=True)
    return results


def _estimate_view(result: dict | None) -> tuple[Decimal | None, str | None, str | None]:
    """(estimator base, code, message) from one load_estimate result."""
    if result is None:
        return None, "not_loaded_yet", NOT_LOADED_MESSAGE
    if result.get("ok"):
        base = _dec(result.get("base"))
        if base is None or base <= 0:
            return None, "unpriced_or_zero", "The estimate is not priced."
        return base, None, None
    return None, str(result.get("code") or "unreadable"), result.get("message")


def _estimator_returned(sb, project_ids: list[str]) -> dict[str, datetime]:
    """When the estimator handed the work back, per project: the first
    estimator_submissions.submitted_at, else the earliest
    estimator_assignments.returned_at (the bid_invitations report's rule)."""
    submitted: dict[str, datetime] = {}
    returned: dict[str, datetime] = {}
    if not project_ids:
        return {}
    for table, column, into in (("estimator_submissions", "submitted_at", submitted),
                                ("estimator_assignments", "returned_at", returned)):
        for r in _in_paged(sb, table, f"id, project_id, {column}", "project_id", project_ids):
            at = _ts(r.get(column))
            if at and (r["project_id"] not in into or at < into[r["project_id"]]):
                into[r["project_id"]] = at
    return {pid: submitted.get(pid) or returned[pid]
            for pid in project_ids if submitted.get(pid) or returned.get(pid)}


def _evb_row(project: dict, sends: list[dict], result: dict | None,
             returned_at: datetime | None = None) -> dict:
    from app.services.outcome import our_amount_of

    base, code, message = _estimate_view(result)
    if code == "estimate_not_received" and returned_at is not None:
        # The estimator handed the work back but no sealed estimate file
        # (project_files category 'estimate', estimator_deliverable, with a
        # submission round) came with it, so there is still no BASE BID
        # PRICE to compare; say so instead of "not submitted yet".
        code = "returned_without_estimate"
        message = RETURNED_WITHOUT_ESTIMATE_MESSAGE.format(date=recipes.short_date(returned_at))
    amounts = [a for a in (our_amount_of(s) for s in sends) if a is not None]
    our = min(amounts) if amounts else None
    sent_times = [t for t in (_ts(s.get("sent_at")) for s in sends) if t]
    first_sent = min(sent_times) if sent_times else None
    if our is None and code is None:
        code, message = "no_our_price", NO_OUR_PRICE_MESSAGE
    verdict = difference = pct = None
    if base is not None and our is not None:
        difference = (our - base).quantize(_CENT, rounding=ROUND_HALF_UP)
        pct = (difference / base * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        verdict = "We bid higher" if difference > 0 else ("We bid lower" if difference < 0 else "Same price")
    result = result or {}
    uploaded = _ts(result.get("uploaded_at"))
    alternates = any("alternate" in str(w).lower() for w in (result.get("warnings") or []))
    return {
        "project": _project_out(project, True),
        "checks": {},
        "severity": None,
        "missing_categories": None,
        "extra": {
            "estimator_base": _amount(base),
            "estimator_name": result.get("estimator_name"),
            "round": result.get("round"),
            "our_min": _amount(our),
            "gc_count": len({s.get("gc_id") for s in sends}),
            "difference": _amount(difference),
            "difference_pct": str(pct) if pct is not None else None,
            "verdict": verdict,
            "code": code,
            "message": message,
            "revised_after_send": bool(uploaded and first_sent and uploaded > first_sent),
            "estimator_returned_at": _iso(returned_at),
            "first_sent_at": _iso(first_sent),
            "estimator_uploaded_at": _iso(uploaded),
            "alternates_note": ("The estimate mentions alternates; they are not in the base bid price."
                                if alternates else None),
        },
    }


def _evb_totals(rows: list[dict]) -> dict:
    both = [r["extra"] for r in rows if r["extra"]["verdict"]]
    est_all = [_dec(r["extra"]["estimator_base"]) for r in rows]
    our_all = [_dec(r["extra"]["our_min"]) for r in rows]
    est_both = sum((_dec(e["estimator_base"]) for e in both), Decimal(0))
    our_both = sum((_dec(e["our_min"]) for e in both), Decimal(0))
    diff = our_both - est_both
    pct = (diff / est_both * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if est_both else None
    return {
        "like_for_like": {
            "count": len(both),
            "estimator_total": _amount(est_both),
            "our_total": _amount(our_both),
            "difference": _amount(diff),
            "difference_pct": str(pct) if pct is not None else None,
        },
        "all_rows": {
            "count": len(rows),
            "estimator_total": _amount(sum((v for v in est_all if v is not None), Decimal(0))),
            "our_total": _amount(sum((v for v in our_all if v is not None), Decimal(0))),
            "note": "All rows, not like-for-like: a bid with only one of the two prices is counted on that side.",
        },
    }


def estimator_vs_bid(sb, period: str | None, at_utc: datetime, role: Any) -> dict:
    """The owner's monthly comparison of the external estimator's BASE BID
    PRICE with the lowest price we sent a GC. Members: bids whose
    actual_bid_at is in the Pacific month, not pm_only or cp_only, not an RFP
    test session. A bid sent in the month but due the next one (internal
    bid date in the next month) goes to payload `extra.sent_early`, outside
    the rows and the totals. Raises RecipeError(code="recipe_unavailable")
    for a role outside ACTUAL_BID_VIEWER_ROLES.

    Payload: {recipe, window {start, end, label}, rows, watch None,
    totals {like_for_like, all_rows}, extra {sent_early: [row]}}. Amounts are
    strings with cents. A row's `extra` has: estimator_base, estimator_name,
    round, our_min, gc_count, difference, difference_pct, verdict ("We bid
    higher" | "We bid lower" | "Same price" | None), code (not_loaded_yet,
    no_our_price, returned_without_estimate or an auto_send_estimate code),
    message, revised_after_send, estimator_returned_at (set only on a
    returned_without_estimate row), first_sent_at, estimator_uploaded_at,
    alternates_note.

    The estimator's price is only ever the sealed estimate workbook's BASE BID
    PRICE (the owner's rule). When there is no sealed estimate file but the
    estimator did hand the work back (a submission round or an assignment
    returned_at), the row says when instead of "not submitted yet"."""
    if role not in ACTUAL_BID_VIEWER_ROLES:
        raise recipes.RecipeError("Estimator vs our price is only available to roles that may see bid amounts.",
                                  code="recipe_unavailable")
    start, end, label = window_for("estimator_vs_bid", at_utc, None, period=period)
    next_first = end.astimezone(PT).date()
    after_next = (next_first.replace(day=28) + timedelta(days=4)).replace(day=1)
    next_end = _day_start(after_next)

    projects = _projects_between(sb, "actual_bid_at", start, end)
    early_ids = set()
    for p in projects:
        due = _ts(p.get("internal_bid_at"))
        if due is not None and end <= due < next_end:
            early_ids.add(p["id"])
    members = sorted((p for p in projects if p["id"] not in early_ids),
                     key=lambda p: (str(p.get("actual_bid_at")), str(p.get("number"))))
    early = sorted((p for p in projects if p["id"] in early_ids),
                   key=lambda p: (str(p.get("actual_bid_at")), str(p.get("number"))))
    ordered = members + early

    sends_by: dict[str, list[dict]] = {}
    if ordered:
        for s in _in_paged(sb, "proposal_sends", _SENT_COLS, "project_id", [p["id"] for p in ordered],
                           eq=("status", "sent")):
            sends_by.setdefault(s["project_id"], []).append(s)
    estimates = _load_estimates(sb, [p["id"] for p in ordered])
    unreceived = [pid for pid, res in estimates.items() if res and res.get("code") == "estimate_not_received"]
    returned = _estimator_returned(sb, unreceived)

    def row(p: dict) -> dict:
        return _evb_row(p, sends_by.get(p["id"], []), estimates.get(p["id"]), returned.get(p["id"]))

    rows = [row(p) for p in members]
    sent_early = [row(p) for p in early]
    return {
        "recipe": "estimator_vs_bid",
        "window": {"start": start.isoformat(), "end": end.isoformat(), "label": label},
        "rows": rows,
        "watch": None,
        "totals": _evb_totals(rows),
        "extra": {"sent_early": sent_early},
    }


def _week_phrase(monday: date, today: date) -> str:
    this_monday = today - timedelta(days=today.weekday())
    if monday == this_monday:
        return "this week"
    if monday == this_monday + timedelta(days=7):
        return "next week"
    return f"the week of {monday.month}/{monday.day}/{monday.year}"


def _qr_member_sort(project: dict, viewer: bool) -> tuple:
    anchor = (_ts(project.get("actual_bid_at")) if viewer else None) or _ts(project.get("internal_bid_at"))
    return (anchor or datetime.max.replace(tzinfo=timezone.utc), (project.get("name") or "").lower())


def quote_readiness(sb, week: str | None, at_utc: datetime, role: Any) -> dict:
    """Key-quote coverage for the bids of one bid week (Monday to Friday
    Pacific). Members: internal_bid_at in the week, or (viewer roles only)
    actual_bid_at in the week; not pm_only, cp_only, an RFP test session,
    declined, abandoned or failed to bid. Required categories: those with at
    least one rfq_sends row status 'sent', minus KEY_QUOTE_EXCLUDED. Covered:
    a non-late quote on the rfq (linked to a send or not) or a send with
    quote_received_at. Zero required renders "n/a", never a pass.

    Payload: {recipe, window {start, end, label, phrase}, rows, watch None}.
    A row's `extra`: required, covered, key_quotes ("3/4" | "n/a"), complete,
    internal_due, bid_date (viewer roles only; the key is absent otherwise),
    estimator_returned_at, estimator_returned_text, categories [{category,
    vendor_emails, first_sent_at, quotes_received, covered, counts_toward_key}]
    (every category with a sent RFQ, excluded ones flagged). The row's
    `missing_categories` lists the required categories still uncovered."""
    viewer = role in ACTUAL_BID_VIEWER_ROLES
    start, end, label = window_for("quote_readiness", at_utc, None, week=week)
    monday = start.astimezone(PT).date()
    today = at_utc.astimezone(PT).date()

    found: dict[str, dict] = {}
    for column in (("internal_bid_at", "actual_bid_at") if viewer else ("internal_bid_at",)):
        for p in _projects_between(sb, column, start, end):
            found.setdefault(p["id"], p)
    projects = sorted(
        (p for p in found.values()
         if p.get("current_stage") != "declined" and not p.get("abandoned_at") and not p.get("failed_to_bid_at")),
        key=lambda p: _qr_member_sort(p, viewer),
    )
    pids = [p["id"] for p in projects]

    rfqs_by: dict[str, list[dict]] = {}
    sends_by_rfq: dict[str, list[dict]] = {}
    quotes_by_rfq: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    if pids:
        rfqs = _in_paged(sb, "rfqs", "id, project_id, material_category_id", "project_id", pids)
        for r in rfqs:
            rfqs_by.setdefault(r["project_id"], []).append(r)
        rfq_ids = [r["id"] for r in rfqs]
        if rfq_ids:
            for c in _in_paged(sb, "material_categories", "id, name", "id",
                               sorted({r["material_category_id"] for r in rfqs if r.get("material_category_id")})):
                names[str(c["id"])] = str(c.get("name") or "")
            for s in _in_paged(sb, "rfq_sends", "id, rfq_id, status, sent_at, quote_received_at", "rfq_id",
                               rfq_ids, eq=("status", "sent")):
                sends_by_rfq.setdefault(s["rfq_id"], []).append(s)
            for q in _in_paged(sb, "quotes", "id, rfq_id, rfq_send_id, received_after_submission", "rfq_id",
                               rfq_ids):
                # Late quotes (0138) are on record only: not bid-time coverage.
                if not q.get("received_after_submission"):
                    quotes_by_rfq.setdefault(q["rfq_id"], []).append(q)
    returned = _estimator_returned(sb, pids)

    rows = []
    for p in projects:
        categories = []
        for rfq in rfqs_by.get(p["id"], []):
            sends = sends_by_rfq.get(rfq["id"], [])
            if not sends:
                continue
            name = names.get(str(rfq.get("material_category_id")), "") or "Unnamed category"
            quotes = quotes_by_rfq.get(rfq["id"], [])
            sent_times = [t for t in (_ts(s.get("sent_at")) for s in sends) if t]
            categories.append({
                "category": name,
                "vendor_emails": len(sends),
                "first_sent_at": _iso(min(sent_times)) if sent_times else None,
                "quotes_received": len(quotes),
                "covered": bool(quotes) or any(s.get("quote_received_at") for s in sends),
                "counts_toward_key": name.strip().lower() not in KEY_QUOTE_EXCLUDED,
            })
        categories.sort(key=lambda c: c["category"].lower())
        required = [c for c in categories if c["counts_toward_key"]]
        covered = [c for c in required if c["covered"]]
        back = returned.get(p["id"])
        extra = {
            "required": len(required),
            "covered": len(covered),
            "key_quotes": f"{len(covered)}/{len(required)}" if required else "n/a",
            "complete": bool(required) and len(covered) == len(required),
            "internal_due": recipes.due_text(p.get("internal_bid_at")),
            "estimator_returned_at": _iso(back),
            "estimator_returned_text": (
                f"Estimator returned the estimate to us on {recipes.short_date(back)}"
                if back else "Estimator has not returned the estimate to us yet"),
            "categories": categories,
        }
        if viewer:
            extra["bid_date"] = recipes.due_text(p.get("actual_bid_at"))
        rows.append({
            "project": _project_out(p, viewer),
            "checks": {},
            "severity": None,
            "missing_categories": [c["category"] for c in required if not c["covered"]],
            "extra": extra,
        })
    return {
        "recipe": "quote_readiness",
        "window": {"start": start.isoformat(), "end": end.isoformat(), "label": label,
                   "phrase": _week_phrase(monday, today)},
        "rows": rows,
        "watch": None,
    }


def digest_sent(sb, user_id: str, local_date: date) -> bool:
    """A due_digest_log row exists for (local_date, user_id): the precedence
    stub against the 6:03 AM digest (3.4.7)."""
    rows = (
        sb.table("due_digest_log")
        .select("id")
        .eq("digest_date", local_date.isoformat())
        .eq("user_id", str(user_id))
        .limit(1)
        .execute()
    ).data or []
    return bool(rows)


def render_email(title: str, payload: dict, *, recipient_name: str, base_url: str) -> str:
    """A recipe-generic branded body (email_branding.render_branded_html and
    the logo helpers due_digest uses): one table (Project, Due, Missing),
    project names HTML-escaped, a button to /assistant?tab=routines. The
    count line is the chat message's own sentence."""
    recipe = str((payload or {}).get("recipe") or "")
    content, _ = recipes.message_for(recipe, payload)
    rows = [r for r in (payload or {}).get("rows") or [] if isinstance(r, dict)]
    base = base_url.rstrip("/")
    first = (recipient_name or "").strip().split(" ")[0]
    greeting = f"Hi {html.escape(first)}," if first else "Hi there,"
    window = ((payload or {}).get("window") or {}).get("label") or ""

    th = (
        f"padding:9px 10px;{_FONT}font-size:11px;letter-spacing:1px;"
        f"color:{_MUTED};text-transform:uppercase;border-bottom:2px solid {_NAVY};"
    )
    cell = f"padding:10px;border-bottom:1px solid {_BORDER};{_FONT}font-size:14px;"
    body_rows = []
    if recipe == "quote_readiness":
        # Generic table over the recipe's own columns; project names are
        # HTML-escaped and link to the project.
        columns, cells = recipes.table_for(recipe, rows[:EMAIL_MAX_ROWS])
        for row, values in zip(rows, cells):
            project = row.get("project") or {}
            label = html.escape(values[0])
            if project.get("id"):
                url = html.escape(f"{base}/projects/{project['id']}", quote=True)
                label = (f'<a href="{url}" target="_blank" '
                         f'style="color:{_NAVY};font-weight:bold;text-decoration:underline;">{label}</a>')
            body_rows.append(
                "<tr>" + f'<td style="{cell}">{label}</td>'
                + "".join(f'<td style="{cell}">{html.escape(v)}</td>' for v in values[1:]) + "</tr>"
            )
        table = ""
        if body_rows:
            table = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'style="border-collapse:collapse;">'
                "<tr>" + "".join(f'<th align="left" style="{th}">{html.escape(c)}</th>' for c in columns) + "</tr>"
                + "".join(body_rows) + "</table>"
            )
    elif recipe == "estimator_vs_bid":
        # Counts and a link only: amounts never go into an email (3.10.1).
        table = ""
    else:
        body_rows = []
        for row in rows[:EMAIL_MAX_ROWS]:
            project = row.get("project") or {}
            label = html.escape(recipes.project_label(project))
            if project.get("id"):
                url = html.escape(f"{base}/projects/{project['id']}", quote=True)
                label = (f'<a href="{url}" target="_blank" '
                         f'style="color:{_NAVY};font-weight:bold;text-decoration:underline;">{label}</a>')
            body_rows.append(
                f"<tr>"
                f'<td style="{cell}">{label}</td>'
                f'<td style="{cell}">{html.escape(recipes.due_text(project.get("internal_bid_at")))}</td>'
                f'<td style="{cell}">{html.escape(recipes.missing_text(recipe, row))}</td>'
                f"</tr>"
            )
        table = ""
        if body_rows:
            table = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'style="border-collapse:collapse;">'
                f'<tr><th align="left" style="{th}">Project</th>'
                f'<th align="left" style="{th}">Due (Pacific)</th>'
                f'<th align="left" style="{th}">Missing</th></tr>'
                + "".join(body_rows)
                + "</table>"
            )
    more = ""
    if table and len(rows) > EMAIL_MAX_ROWS:
        more = (f'<p style="margin:10px 0 0;font-size:12px;color:{_MUTED};">'
                f"And {len(rows) - EMAIL_MAX_ROWS} more.</p>")
    body = (
        f'<p style="margin:0 0 14px;color:{_MUTED};">{greeting}</p>'
        f'<div style="font-size:19px;font-weight:bold;color:{_NAVY};margin:0 0 4px;">'
        f"{html.escape(title)}</div>"
        + (f'<p style="margin:0 0 6px;color:{_MUTED};">{html.escape(window)}</p>' if window else "")
        + f'<p style="margin:0 0 16px;">{html.escape(content)}</p>'
        + table
        + more
        + f'<p style="margin:16px 0 16px;font-size:12px;color:{_MUTED};">'
        "You set up this routine with the BDR assistant. Pause or end it on the Routines tab.</p>"
        + _button("Open Routines", f"{base}/assistant?tab=routines")
    )
    return render_branded_html(body, subtitle="ASSISTANT ROUTINE")

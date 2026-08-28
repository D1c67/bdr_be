"""Estimating Engineer (Labor) analytics - the "Master Analytics" dashboard.

One row per project that has PASSED its internal bid date (the deadline the
labor-focused engineer works against), tracing the engineer's timeline through
the bid:

  * quotes_complete_at   - when every RFQ category had at least one quote in
                           (max over the per-category FIRST quote times). The
                           General Material category is priced off the estimate
                           extraction, so its "first quote" is the earlier of
                           its quotes row and general_material_estimates row.
  * first_opened_at      - first time a labor-focused engineer opened the
                           project page (project_open_events, kind 'project';
                           the table only exists from migration 0117 on, so
                           earlier opens were never recorded).
  * labor_first_saved_at - first labor-panel save, read from the audit log's
                           'labor.review' entries (every PUT /labor audits).
  * markup_completed_at  - when the Markup lane completed, i.e. markup for
                           every present pricing section AND labor was saved
                           and the lane advanced (project_category_state
                           .completed_at; a reopened lane re-stamps it).
  * sent_out_at          - first proposal transmission (proposal_sends), with
                           the 'submitted' stage event as a fallback.
  * sent_status          - on_time / late vs the internal bid date, plus
                           after_actual when the send happened after the
                           confidential actual bid date. The label is shown to
                           every internal role (it names no date), but the
                           "how long after the actual date" delta is only
                           included for ACTUAL_BID_VIEWER_ROLES - everyone
                           else gets deltas against the internal date alone,
                           mirroring bid_invitations' redaction stance.

Declined, abandoned and PM/CP-only projects never went to (or dropped out of)
bid, so they are excluded from the cohort.
"""

import re
from collections import defaultdict
from datetime import datetime, timezone

from app.core.roles import ACTUAL_BID_VIEWER_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services.analytics_metrics import _iso, _parse
from app.services.bid_invitations import _in

_PROJECT_COLS = "id, name, number, internal_bid_at, actual_bid_at, current_stage"


def _sanitize_search(q: str) -> str:
    """Strip characters that would break the PostgREST or_() filter syntax or
    act as ilike wildcards (% _ and the * alias); plain substring search only,
    capped so a huge term can't inflate the request. Same idiom as emails.py."""
    return re.sub(r"[,()%_*\\]", " ", q[:200]).strip()


def report(search: str | None, limit: int, offset: int, role: str) -> dict:
    sb = get_supabase()
    now = datetime.now(timezone.utc)

    query = (
        sb.table("projects")
        .select(_PROJECT_COLS, count="exact")
        .lt("internal_bid_at", now.isoformat())
        .neq("current_stage", "pm_only")
        .neq("current_stage", "cp_only")
        .neq("current_stage", "declined")
        .is_("abandoned_at", "null")
    )
    if search:
        term = _sanitize_search(search)
        if term:
            query = query.or_(f"number.ilike.%{term}%,name.ilike.%{term}%")
    resp = (
        query.order("internal_bid_at", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    projects = resp.data or []
    total = resp.count if resp.count is not None else len(projects)

    return {
        "meta": {
            "total": total,
            "limit": limit,
            "offset": offset,
            "actual_deltas_visible": role in ACTUAL_BID_VIEWER_ROLES,
        },
        "rows": build_rows(sb, projects, role),
    }


def _keep_min(store: dict, key: str, at: datetime | None) -> None:
    if at is not None and (key not in store or at < store[key]):
        store[key] = at


def build_rows(sb, projects: list[dict], role: str) -> list[dict]:
    """Assemble the timeline row for each project (page-sized list)."""
    pids = [p["id"] for p in projects]
    if not pids:
        return []
    show_actual = role in ACTUAL_BID_VIEWER_ROLES

    # The project's categories are its RFQ rows: one per material category.
    rfqs = _in(
        lambda: sb.table("rfqs").select("id, project_id, material_category_id"),
        "project_id",
        pids,
    )
    cats = sb.table("material_categories").select("id, name, is_general").execute().data or []
    cat_name = {c["id"]: c["name"] for c in cats}
    general_ids = {c["id"] for c in cats if c.get("is_general")}
    project_rfqs: dict[str, list[dict]] = defaultdict(list)
    for r in rfqs:
        project_rfqs[r["project_id"]].append(r)

    # First quote per RFQ. All origins count: emailed vendor quotes, hand-typed
    # candidates, and General Material's estimate-derived row all mean "this
    # category has a figure".
    first_quote: dict[str, datetime] = {}
    rfq_ids = [r["id"] for r in rfqs]
    if rfq_ids:
        for q in _in(
            lambda: sb.table("quotes").select("rfq_id, received_at"), "rfq_id", rfq_ids
        ):
            _keep_min(first_quote, q["rfq_id"], _parse(q.get("received_at")))

    gm_created: dict[str, datetime] = {}
    for g in _in(
        lambda: sb.table("general_material_estimates").select("project_id, created_at"),
        "project_id",
        pids,
    ):
        _keep_min(gm_created, g["project_id"], _parse(g.get("created_at")))

    # First labor save: PUT /projects/{id}/labor audits 'labor.review' on every
    # save, so the earliest entry is the first time the labor panel was saved.
    labor_first: dict[str, datetime] = {}
    for a in _in(
        lambda: sb.table("audit_log")
        .select("entity_id, created_at")
        .eq("action", "labor.review")
        .eq("entity", "project"),
        "entity_id",
        pids,
    ):
        _keep_min(labor_first, a["entity_id"], _parse(a.get("created_at")))

    # Markup lane completion. Single-task lanes emit no stage event when they
    # complete; project_category_state.completed_at is the record (re-stamped
    # if the lane reopens and completes again).
    markup_done: dict[str, datetime] = {}
    for s in _in(
        lambda: sb.table("project_category_state")
        .select("project_id, completed_at")
        .eq("category", "markup")
        .eq("status", "complete"),
        "project_id",
        pids,
    ):
        at = _parse(s.get("completed_at"))
        if at is not None:
            markup_done[s["project_id"]] = at

    # Sent out: first proposal transmission wins; the 'submitted' stage event
    # (pressed after the sends) is a fallback for rows without send records.
    sent_out: dict[str, datetime] = {}
    for s in _in(
        lambda: sb.table("proposal_sends").select("project_id, sent_at").eq("status", "sent"),
        "project_id",
        pids,
    ):
        _keep_min(sent_out, s["project_id"], _parse(s.get("sent_at")))
    for e in _in(
        lambda: sb.table("stage_events")
        .select("project_id, entered_at")
        .eq("to_stage", "submitted"),
        "project_id",
        pids,
    ):
        _keep_min(sent_out, e["project_id"], _parse(e.get("entered_at")))

    # Page/details opens by users whose role is estimating_engineer_labor.
    opens = _in(
        lambda: sb.table("project_open_events").select("project_id, user_id, kind, opened_at"),
        "project_id",
        pids,
    )
    labor_users: set[str] = set()
    uids = list({o["user_id"] for o in opens})
    if uids:
        labor_users = {
            pr["id"]
            for pr in _in(
                lambda: sb.table("profiles")
                .select("id")
                .eq("role", Role.ESTIMATING_ENGINEER_LABOR),
                "id",
                uids,
            )
        }
    first_open: dict[str, datetime] = {}
    open_count: dict[str, int] = defaultdict(int)
    details_count: dict[str, int] = defaultdict(int)
    for o in opens:
        if o["user_id"] not in labor_users:
            continue
        at = _parse(o.get("opened_at"))
        if at is None:
            continue
        if o["kind"] == "project":
            open_count[o["project_id"]] += 1
            _keep_min(first_open, o["project_id"], at)
        elif o["kind"] == "details":
            details_count[o["project_id"]] += 1

    rows = []
    for p in projects:
        pid = p["id"]
        quoted: list[tuple[datetime, str]] = []
        missing: list[str] = []
        for r in project_rfqs.get(pid, []):
            name = cat_name.get(r["material_category_id"]) or "Unknown category"
            at = first_quote.get(r["id"])
            if r["material_category_id"] in general_ids:
                gm = gm_created.get(pid)
                if gm is not None and (at is None or gm < at):
                    at = gm
            if at is None:
                missing.append(name)
            else:
                quoted.append((at, name))
        last_at, last_cat = max(quoted, key=lambda t: t[0]) if quoted else (None, None)
        quotes_complete_at = last_at if quoted and not missing else None

        internal = _parse(p.get("internal_bid_at"))
        actual = _parse(p.get("actual_bid_at"))
        sent = sent_out.get(pid)
        if sent is None:
            status = "not_sent"
            sent_delta = None
            after_actual = None
        else:
            sent_delta = int((sent - internal).total_seconds()) if internal else None
            if actual is not None and sent > actual:
                status = "after_actual"
            elif internal is not None and sent > internal:
                status = "late"
            else:
                status = "on_time"
            after_actual = (
                int((sent - actual).total_seconds()) if (show_actual and actual) else None
            )

        rows.append(
            {
                "project_id": pid,
                "number": p.get("number"),
                "name": p.get("name"),
                "internal_bid_at": _iso(internal),
                "quotes_complete_at": _iso(quotes_complete_at),
                "last_quoted_category": last_cat,
                "missing_quote_categories": sorted(missing),
                "category_count": len(project_rfqs.get(pid, [])),
                "first_opened_at": _iso(first_open.get(pid)),
                "project_open_count": open_count.get(pid, 0),
                "labor_first_saved_at": _iso(labor_first.get(pid)),
                "markup_completed_at": _iso(markup_done.get(pid)),
                "sent_out_at": _iso(sent),
                "sent_status": status,
                "sent_delta_seconds": sent_delta,
                "sent_after_actual_seconds": after_actual,
                "details_opened": details_count.get(pid, 0) > 0,
                "details_open_count": details_count.get(pid, 0),
            }
        )
    return rows

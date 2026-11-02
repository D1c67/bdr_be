"""Cross-project PM reads (docs/ASSISTANT_PHASE4_PLAN.md 3.13, deviation D19).

Two bodies, one per route in `app/routers/pm.py`:

- `open_rfis`  -> `GET /pm/rfis/open`
- `billing_summary` -> `GET /pm/billing/summary`

Both cover the PM projects that are still live (`pm_stage` set and
`pm_completed_at` null) and read in a few batched queries, never one query
per project. Pure of FastAPI: the router owns auth and the client handle.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.services import pm_financials as fin

PT = ZoneInfo("America/Los_Angeles")

# PostgREST caps one response (1000 rows by default), so every unbounded read
# pages with .range() over a stable order.
_PAGE = 1000
_MAX_PAGES = 50
# Ids per `in` filter, so a long list never blows the request URL.
_CHUNK = 100

SUBJECT_MAX = 200


def _paged(build: Callable[[], Any]) -> list[dict]:
    """All rows of a query. `build()` returns a fresh ordered query each page."""
    out: list[dict] = []
    for page in range(_MAX_PAGES):
        rows = build().range(page * _PAGE, page * _PAGE + _PAGE - 1).execute().data or []
        out.extend(rows)
        if len(rows) < _PAGE:
            break
    return out


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[i : i + _CHUNK] for i in range(0, len(ids), _CHUNK)]


def _live_pm_projects(sb: Any) -> list[dict]:
    """PM projects not yet completed, by project number."""
    rows = _paged(
        lambda: sb.table("projects")
        .select("id, number, name")
        .not_.is_("pm_stage", "null")
        .is_("pm_completed_at", "null")
        .order("id")
    )
    rows.sort(key=lambda p: (str(p.get("number") or ""), str(p.get("name") or "")))
    return rows


def _project_ref(p: dict) -> dict:
    return {"id": p["id"], "number": p.get("number"), "name": p.get("name")}


def _as_date(value: Any) -> date | None:
    """A date or timestamp column value as a Pacific calendar date."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        if len(text) <= 10:
            try:
                return date.fromisoformat(text)
            except ValueError:
                return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(PT)
    return dt.date()


def _rfi_due_key(rfi: dict) -> tuple:
    """Within a project: dated RFIs first, soonest due first."""
    due = rfi["due"]
    return (due is None, due or "", str(rfi["number"] or ""))


def _rfi_view(r: dict, today: date) -> dict:
    number = r.get("rfi_number")
    if number is None:
        number = r.get("external_number")
    due_date = _as_date(r.get("due_at") or r.get("external_due_date"))
    started = (
        _as_date(r.get("sent_at")) or _as_date(r.get("initiated_at")) or _as_date(r.get("created_at"))
    )
    source = r.get("source") or "internal"
    return {
        "id": r["id"],
        "number": number,
        "subject": str(r.get("subject") or "")[:SUBJECT_MAX],
        "status": r.get("status"),
        "ball_in_court": r.get("ball_in_court"),
        "due": due_date.isoformat() if due_date else None,
        "sent_at": r.get("sent_at"),
        "age_days": max(0, (today - started).days) if started else None,
        "overdue": bool(due_date and due_date < today),
        "source": source,
        "read_only": source == "procore",
    }


def open_rfis(sb: Any, *, limit: int = 200, today: date | None = None) -> dict:
    """Body of `GET /pm/rfis/open`: open RFIs (status `open`) across live PM
    projects, grouped by project, `{projects: [{project {id, number, name},
    rfis: [{id, number (rfi_number or external_number), subject (200),
    status, ball_in_court, due (due_at or external_due_date), sent_at,
    age_days, overdue, source, read_only}]}], truncated}`, at most `limit`
    RFIs. The question, answer and attachments are never returned here; the
    per-project RFI route carries them."""
    limit = max(1, min(int(limit), 200))
    today = today or datetime.now(PT).date()
    projects = _live_pm_projects(sb)
    by_project: dict[str, list[dict]] = {}
    for chunk in _chunks([p["id"] for p in projects]):
        rows = _paged(
            lambda chunk=chunk: sb.table("rfis")
            .select(
                "id, project_id, rfi_number, external_number, subject, status, "
                "ball_in_court, due_at, external_due_date, sent_at, initiated_at, "
                "created_at, source"
            )
            .in_("project_id", chunk)
            .eq("status", "open")
            .order("id")
        )
        for r in rows:
            by_project.setdefault(r["project_id"], []).append(r)

    out: list[dict] = []
    shown = 0
    truncated = False
    for p in projects:
        raw = by_project.get(p["id"]) or []
        if not raw:
            continue
        room = limit - shown
        if room <= 0:
            truncated = True
            break
        items = sorted((_rfi_view(r, today) for r in raw), key=_rfi_due_key)
        if len(items) > room:
            items = items[:room]
            truncated = True
        out.append({"project": _project_ref(p), "rfis": items})
        shown += len(items)
    return {"projects": out, "truncated": truncated}


def billing_summary(sb: Any) -> dict:
    """Body of `GET /pm/billing/summary`: per live PM project the same figures
    as `GET /pm/projects/{id}/financials` (`fin.financials_summary`), read in
    one batched pass, `{projects: [{project, original_contract_value,
    approved_change_total, current_contract_value, billed_to_date,
    retainage_held, balance_to_finish, open_change_order_count,
    pay_app_count}]}`, money as strings (`fin.money`). Contract-derived
    figures are null until the project has an original contract value."""
    projects = _live_pm_projects(sb)
    ids = [p["id"] for p in projects]

    original: dict[str, Any] = {}
    cos: dict[str, list[dict]] = {}
    apps: dict[str, list[dict]] = {}
    for chunk in _chunks(ids):
        for r in _paged(
            lambda chunk=chunk: sb.table("pm_details")
            .select("project_id, original_contract_value")
            .in_("project_id", chunk)
            .order("project_id")
        ):
            original[r["project_id"]] = r.get("original_contract_value")
        for r in _paged(
            lambda chunk=chunk: sb.table("change_orders")
            .select("id, project_id, amount, status")
            .in_("project_id", chunk)
            .order("id")
        ):
            cos.setdefault(r["project_id"], []).append(r)
        for r in _paged(
            lambda chunk=chunk: sb.table("pay_applications")
            .select("id, project_id, app_number, status, retainage_percent")
            .in_("project_id", chunk)
            .order("id")
        ):
            apps.setdefault(r["project_id"], []).append(r)

    lines_by_app: dict[str, list[dict]] = {}
    app_ids = [a["id"] for rows in apps.values() for a in rows]
    for chunk in _chunks(app_ids):
        for r in _paged(
            lambda chunk=chunk: sb.table("pay_app_lines")
            .select("id, pay_app_id, previous_completed, this_period, stored_materials")
            .in_("pay_app_id", chunk)
            .order("id")
        ):
            lines_by_app.setdefault(r["pay_app_id"], []).append(r)

    out: list[dict] = []
    for p in projects:
        pid = p["id"]
        p_cos = cos.get(pid, [])
        p_apps = apps.get(pid, [])
        totals = fin.app_series_totals(p_apps, lines_by_app)
        billed = retainage = Decimal(0)
        live = [a for a in p_apps if a.get("status") != "rejected"]
        latest = max(live, key=lambda a: a["app_number"], default=None)
        if latest is not None:
            billed = fin.dec(totals[latest["id"]]["total_completed_and_stored"])
            retainage = fin.dec(totals[latest["id"]]["retainage_held"])
        summary = fin.financials_summary(
            original.get(pid),
            fin.total_of([c for c in p_cos if c.get("status") == "approved"], "amount"),
            Decimal(0),  # sov_total: not part of this summary
            billed,
            retainage,
            sum(1 for c in p_cos if c.get("status") in ("draft", "submitted")),
            len(p_apps),
        )
        summary.pop("sov_total", None)
        out.append({"project": _project_ref(p), **summary})
    return {"projects": out}

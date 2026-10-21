"""Per-user file access tracking (0161 `file_access_events`).

Answers "did user X open file Y in project Z, and how many times". Every route
that hands a user a file (a signed view/download URL, a ZIP export, or a
cached re-open the frontend reports through POST .../opened) writes one row
here. `action` is the kind of access: `view` (opened in the viewer),
`download` (an attachment download), `export` (one row per file in a ZIP,
grouped by `export_id`) or `prefetch` (the frontend's hover warm-up; kept, but
hidden from the reports unless asked for).

Writes NEVER raise: a logging failure must not block a file open. A database
without 0161 (missing table) is swallowed quietly; anything else is logged.

The read side (`access_report`, `file_access`) aggregates in Python over a
capped, paged fetch, the same approach as analytics_metrics.activity_log.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

TABLE = "file_access_events"

ACTIONS = ("view", "download", "export", "prefetch")
SOURCES = (
    "project_file",
    "bid_split_file",
    "bid_split_segment",
    "ai_estimating_document",
    "ai_estimating_deliverable",
    "submittal_file",
    "email_attachment",
)

# Most rows the report scans for one request; past it `truncated` is set and
# the caller should narrow the range. PostgREST caps a single response (1000
# rows by default), so the scan pages through in _PAGE-sized slices.
SCAN_CAP = 20000
_PAGE = 1000
FILE_EVENTS_LIMIT = 50

_EVENT_COLUMNS = (
    "id, created_at, actor_id, actor_role, project_id, source, file_id, "
    "filename, category, action, export_id, detail"
)


def _missing_table(exc: Exception) -> bool:
    """A database without 0161: PostgREST's PGRST205 (table not in the schema
    cache) or Postgres's 42P01 (undefined table)."""
    code = getattr(exc, "code", None)
    if code in ("PGRST205", "42P01"):
        return True
    text = str(exc)
    return "PGRST205" in text or "42P01" in text


def _role_value(user: Any) -> str | None:
    role = getattr(user, "role", None)
    if role is None:
        return None
    return getattr(role, "value", None) or str(role)


def _row(
    user: Any,
    *,
    action: str,
    source: str,
    file_id: str | None,
    project_id: str | None,
    filename: str | None = None,
    category: str | None = None,
    export_id: str | None = None,
    detail: dict | None = None,
) -> dict:
    extra = dict(detail or {})
    # A dev viewing the portal as an estimator: the row is the estimator's
    # (that is what they would have done), with the real account noted.
    impersonated_by = getattr(user, "impersonated_by", None)
    if impersonated_by:
        extra["impersonated_by"] = impersonated_by
    return {
        "actor_id": getattr(user, "id", None),
        "actor_role": _role_value(user),
        "project_id": str(project_id) if project_id else None,
        "source": source,
        "file_id": str(file_id) if file_id else None,
        "filename": filename,
        "category": str(category) if category else None,
        "action": action,
        "export_id": str(export_id) if export_id else None,
        "detail": extra or None,
    }


def _insert(rows: list[dict]) -> None:
    if not rows:
        return
    try:
        get_supabase().table(TABLE).insert(rows).execute()
    except Exception as exc:  # noqa: BLE001 - tracking must never block a file open
        if _missing_table(exc):
            logger.debug("file_access_events missing (0161 not applied); skipped")
            return
        logger.exception("file access record failed (%d row(s))", len(rows))


def record(
    user: Any,
    *,
    action: str,
    source: str,
    file_id: str | None,
    project_id: str | None,
    filename: str | None = None,
    category: str | None = None,
    export_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Record one file access. Never raises."""
    try:
        row = _row(
            user,
            action=action,
            source=source,
            file_id=file_id,
            project_id=project_id,
            filename=filename,
            category=category,
            export_id=export_id,
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        logger.exception("file access record failed while building the row")
        return
    _insert([row])


def record_many(user: Any, rows: Iterable[dict]) -> None:
    """Record several accesses in one insert (an export). Each item takes the
    keyword arguments of `record`. Never raises."""
    try:
        built = [_row(user, **r) for r in rows]
    except Exception:  # noqa: BLE001
        logger.exception("file access record_many failed while building rows")
        return
    _insert(built)


# ── Read side ────────────────────────────────────────────────────────────────


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _fetch_events(
    sb,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    project_id: str | None = None,
    file_id: str | None = None,
    actor_id: str | None = None,
    source: str | None = None,
    action: str | None = None,
    include_prefetch: bool = False,
    cap: int = SCAN_CAP,
) -> tuple[list[dict], bool]:
    """Matching rows newest first, at most `cap`; the bool is True when the
    cap was hit. A database without 0161 reads as empty."""
    rows: list[dict] = []
    start = 0
    try:
        while len(rows) < cap:
            q = sb.table(TABLE).select(_EVENT_COLUMNS)
            if date_from is not None:
                q = q.gte("created_at", date_from.isoformat())
            if date_to is not None:
                q = q.lte("created_at", date_to.isoformat())
            if project_id:
                q = q.eq("project_id", project_id)
            if file_id:
                q = q.eq("file_id", file_id)
            if actor_id:
                q = q.eq("actor_id", actor_id)
            if source:
                q = q.eq("source", source)
            if action:
                q = q.eq("action", action)
            elif not include_prefetch:
                q = q.neq("action", "prefetch")
            end = min(start + _PAGE, cap) - 1
            page = (
                q.order("created_at", desc=True)
                .order("id", desc=True)
                .range(start, end)
                .execute()
            ).data or []
            rows.extend(page)
            if len(page) < end - start + 1:
                break
            start = end + 1
    except Exception as exc:  # noqa: BLE001
        if _missing_table(exc):
            return [], False
        raise
    # A stale or misbehaving page can never hide prefetches from the report.
    if not action and not include_prefetch:
        rows = [r for r in rows if r.get("action") != "prefetch"]
    return rows[:cap], len(rows) >= cap


def _profiles(sb, ids: set[str]) -> dict[str, dict]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    out: dict[str, dict] = {}
    ordered = sorted(ids)
    for i in range(0, len(ordered), 200):
        for p in (
            sb.table("profiles")
            .select("id, full_name, email, role")
            .in_("id", ordered[i : i + 200])
            .execute()
        ).data or []:
            out[str(p["id"])] = p
    return out


def _projects(sb, ids: set[str]) -> dict[str, dict]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    out: dict[str, dict] = {}
    ordered = sorted(ids)
    for i in range(0, len(ordered), 200):
        for p in (
            sb.table("projects")
            .select("id, number, name")
            .in_("id", ordered[i : i + 200])
            .execute()
        ).data or []:
            out[str(p["id"])] = p
    return out


def _event_out(r: dict, actors: dict[str, dict], projects: dict[str, dict]) -> dict:
    aid = r.get("actor_id")
    pid = r.get("project_id")
    prof = actors.get(str(aid)) if aid else None
    proj = projects.get(str(pid)) if pid else None
    return {
        "id": r.get("id"),
        "created_at": r.get("created_at"),
        "actor_id": aid,
        "actor_name": (prof or {}).get("full_name"),
        # The role recorded at access time; the profile's current role only
        # when the row carries none.
        "actor_role": r.get("actor_role") or (prof or {}).get("role"),
        "project_id": pid,
        "project_number": (proj or {}).get("number"),
        "project_name": (proj or {}).get("name"),
        "source": r.get("source"),
        "file_id": r.get("file_id"),
        "filename": r.get("filename"),
        "category": r.get("category"),
        "action": r.get("action"),
        "export_id": r.get("export_id"),
        "detail": r.get("detail"),
    }


_COUNT_FIELD = {
    "view": "views",
    "download": "downloads",
    "export": "exports",
    "prefetch": "prefetches",
}


def _bump(group: dict, r: dict) -> None:
    field = _COUNT_FIELD.get(r.get("action") or "")
    if field:
        group[field] += 1
    at = r.get("created_at")
    at_dt = _parse(at)
    if at_dt is not None:
        if group["_first"] is None or at_dt < group["_first"]:
            group["_first"], group["first_at"] = at_dt, at
        if group["_last"] is None or at_dt > group["_last"]:
            group["_last"], group["last_at"] = at_dt, at


def _new_counts() -> dict:
    return {
        "views": 0,
        "downloads": 0,
        "exports": 0,
        "prefetches": 0,
        "first_at": None,
        "last_at": None,
        "_first": None,
        "_last": None,
    }


def _finish(group: dict) -> dict:
    group["total"] = group["views"] + group["downloads"] + group["exports"]
    group.pop("_first", None)
    group.pop("_last", None)
    return group


def summarize(rows: list[dict]) -> list[dict]:
    """Group rows by (project, source, file, actor); a row with no file id (a
    deliverable, a backfilled export) groups by its filename instead. Rows
    arrive newest first, so the first row of a group supplies its filename and
    category snapshot. Sorted last_at desc. Names are resolved by the caller."""
    groups: dict[tuple, dict] = {}
    for r in rows:
        fid = r.get("file_id")
        key = (
            r.get("project_id"),
            r.get("source"),
            fid,
            None if fid else r.get("filename"),
            r.get("actor_id"),
        )
        g = groups.get(key)
        if g is None:
            g = {
                "project_id": r.get("project_id"),
                "source": r.get("source"),
                "file_id": fid,
                "filename": r.get("filename"),
                "category": r.get("category"),
                "actor_id": r.get("actor_id"),
                **_new_counts(),
            }
            groups[key] = g
        _bump(g, r)
    out = [_finish(g) for g in groups.values()]
    out.sort(key=lambda g: _parse(g.get("last_at")) or _EPOCH, reverse=True)
    return out


def access_report(
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    project_id: str | None = None,
    file_id: str | None = None,
    actor_id: str | None = None,
    source: str | None = None,
    action: str | None = None,
    include_prefetch: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """GET /analytics/file-access: a per (project, file, user) summary plus a
    paginated newest-first event feed over the same filtered rows."""
    sb = get_supabase()
    rows, truncated = _fetch_events(
        sb,
        date_from=date_from,
        date_to=date_to,
        project_id=project_id,
        file_id=file_id,
        actor_id=actor_id,
        source=source,
        action=action,
        include_prefetch=include_prefetch,
    )
    summary = summarize(rows)
    page = rows[offset : offset + limit]

    actor_ids = {str(g["actor_id"]) for g in summary if g.get("actor_id")}
    project_ids = {str(g["project_id"]) for g in summary if g.get("project_id")}
    actors = _profiles(sb, actor_ids)
    projects = _projects(sb, project_ids)

    for g in summary:
        prof = actors.get(str(g["actor_id"])) if g.get("actor_id") else None
        proj = projects.get(str(g["project_id"])) if g.get("project_id") else None
        g["actor_name"] = (prof or {}).get("full_name")
        g["actor_email"] = (prof or {}).get("email")
        g["project_number"] = (proj or {}).get("number")
        g["project_name"] = (proj or {}).get("name")

    return {
        "summary": summary,
        "events": [_event_out(r, actors, projects) for r in page],
        "events_total": len(rows),
        "truncated": truncated,
    }


def file_access(project_id: str, file_id: str, filename: str | None = None) -> dict:
    """GET /projects/{id}/files/{id}/access: who opened one project file, how
    often, and the latest FILE_EVENTS_LIMIT events. Prefetches excluded. The
    file row may be gone (history outlives deletes); the newest event's
    filename snapshot then names it."""
    sb = get_supabase()
    rows, _ = _fetch_events(
        sb, project_id=project_id, file_id=file_id, source="project_file"
    )
    by_actor: dict[Any, dict] = {}
    for r in rows:
        aid = r.get("actor_id")
        g = by_actor.get(aid)
        if g is None:
            g = {"actor_id": aid, "actor_role": r.get("actor_role"), **_new_counts()}
            by_actor[aid] = g
        _bump(g, r)
    users = [_finish(g) for g in by_actor.values()]
    for g in users:
        g.pop("prefetches", None)

    actors = _profiles(sb, {str(a) for a in by_actor if a})
    projects = _projects(sb, {project_id})
    for g in users:
        prof = actors.get(str(g["actor_id"])) if g.get("actor_id") else None
        g["actor_name"] = (prof or {}).get("full_name")
        g["actor_role"] = g.get("actor_role") or (prof or {}).get("role")
    users.sort(key=lambda g: _parse(g.get("last_at")) or _EPOCH, reverse=True)

    if not filename:
        filename = next((r.get("filename") for r in rows if r.get("filename")), None)
    return {
        "file_id": file_id,
        "filename": filename,
        "users": users,
        "events": [_event_out(r, actors, projects) for r in rows[:FILE_EVENTS_LIMIT]],
    }

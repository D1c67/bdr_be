"""RFP Ingestion test bench: the /rfp-testing API (docs/RFP_TESTING.md
section 8).

Reads the session table, the event ledger and the tagged rows of a session
and shapes them for the /rfp-testing page; the session actions (activate,
end, auto-create, cleanup) live in app/services/rfp_test.py and are mapped
onto HTTP statuses here.

Access, three locks deep (doc section 2):

- The env flag. `require_rfp_testing` is a ROUTER-level dependency, so
  while RFP_TESTING_ENABLED is false every route 404s with the bare "Not
  Found" body before a token is ever read (the /rfp-emails contract).
- The caller: a dev account (`profiles.is_dev`) whose role is `it_admin`;
  anyone else is 403 `rfp_testing_forbidden`. `require_dev_it_admin` checks
  both on the authenticated user in one place so the two refusals carry the
  same code (the page renders one "not available" block).
- The rate limit: the generous catch-all budget (`default_rate_limit_per_min`,
  240 per minute) on every route. The state route is polled every 3 seconds
  by one person, so the 120 per minute dev-page buckets are too tight for
  it and nothing here is expensive enough to need its own.

Event-loop rule: every handler is a plain `def`; the Supabase SDK is sync
and FastAPI runs these in its threadpool. Cleanup (section 9) runs inside
its handler for the same reason.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.ratelimit import rate_limit
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import rfp_harvest, rfp_test, workflow

logger = logging.getLogger(__name__)


# ── Gates ────────────────────────────────────────────────────────────────


def require_rfp_testing() -> None:
    """Router-level dependency: 404 while the switch is off (or the master
    RFP switch is off), before auth runs."""
    settings = get_settings()
    if not (settings.rfp_testing_enabled and settings.rfp_ingest_enabled):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


async def require_dev_it_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """A dev account in the it_admin role; everyone else 403
    rfp_testing_forbidden (the code is the detail; the page shows it)."""
    if not user.is_dev or user.role != Role.IT_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            ErrorCode.RFP_TESTING_FORBIDDEN.value,
            headers={"X-Error-Code": ErrorCode.RFP_TESTING_FORBIDDEN.value},
        )
    return user


rfp_testing_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

router = APIRouter(
    prefix="/rfp-testing",
    tags=["rfp-testing"],
    dependencies=[Depends(require_rfp_testing)],
)

_SESSION_NOT_FOUND = "Test session not found"
_PROJECT_NOT_FOUND = "Project not found"
_EVENTS_MAX = 500
_ROWS_MAX = 500
_CHIP_EVENTS_MAX = 2000
_IN_CHUNK = 200

_EMAIL_SELECT = (
    "id, status, flag_reason, decided_at_step, from_address, from_name, subject, received_at, "
    "forward_meta, invitation_method, llm_answer, llm_confidence, extracted_project_name, "
    "extracted_gc_name, extracted_bid_due_at, extracted_bid_due_has_time, match_project_id, "
    "matched_at, harvest_id, created_project_id, last_error, attempts, next_attempt_at, "
    "updated_at, body_text, test_session_id"
)
_FILED_SELECT = (
    "id, status, from_address, subject, message_at, project_id, matched_by, match_confidence, "
    "pipeline_round, error, updated_at, suggested_project_id, suggested_confidence"
)
_PROJECT_SELECT = (
    "id, number, name, current_stage, current_owner_role, internal_bid_at, actual_bid_at, "
    "created_at, created_by, abandoned_at, test_session_id"
)


class SessionCreateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    auto_create: bool = False


class SessionPatchIn(BaseModel):
    auto_create: bool


# ── Helpers ──────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_uuid(value: str, label: str) -> None:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label} not found")


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _fetch_in(sb, table: str, select: str, column: str, values: list) -> list[dict]:
    wanted = [v for v in dict.fromkeys(values) if v]
    rows: list[dict] = []
    for chunk in _chunks(wanted):
        rows += (sb.table(table).select(select).in_(column, chunk).execute()).data or []
    return rows


def _count(sb, table: str, **filters) -> int:
    """An exact count of the rows matching the equality filters (ids only
    travel; a session's rows are hundreds at most)."""
    query = sb.table(table).select("id", count="exact")
    for col, val in filters.items():
        query = query.eq(col, val)
    resp = query.execute()
    count = getattr(resp, "count", None)
    return int(count) if count is not None else len(resp.data or [])


def _profile_names(sb, ids: list) -> dict[str, str | None]:
    rows = _fetch_in(sb, "profiles", "id, full_name", "id", ids)
    return {r["id"]: r.get("full_name") for r in rows}


def _session_out(row: dict, names: dict[str, str | None]) -> dict:
    started_by = row.get("started_by")
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "name": row.get("name"),
        "started_by": {"id": started_by, "name": names.get(started_by)} if started_by else None,
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "mailbox": row.get("mailbox"),
        "sender": row.get("sender"),
        "redirect_to": row.get("redirect_to"),
        "auto_create": bool(row.get("auto_create")),
        "cleanup_finished_at": row.get("cleanup_finished_at"),
        "cleanup_report": row.get("cleanup_report"),
    }


def _event_out(ev: dict) -> dict:
    return {
        "id": ev.get("id"),
        "at": ev.get("at"),
        "source": ev.get("source"),
        "kind": ev.get("kind"),
        "level": ev.get("level"),
        "title": ev.get("title"),
        "rfp_email_id": ev.get("rfp_email_id"),
        "ingested_email_id": ev.get("ingested_email_id"),
        "harvest_id": ev.get("harvest_id"),
        "project_id": ev.get("project_id"),
        "detail": ev.get("detail") or {},
    }


def _load_session(sb, session_id: str) -> dict:
    _require_uuid(session_id, "Test session")
    row = rfp_test.get_session(sb, session_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _SESSION_NOT_FOUND)
    return row


def _map_error(exc: rfp_test.RfpTestError) -> HTTPException:
    return HTTPException(exc.status, str(exc), headers={"X-Error-Code": exc.code})


def _project_refs(sb, ids: list) -> dict[str, dict]:
    rows = _fetch_in(sb, "projects", "id, number, name", "id", ids)
    return {r["id"]: {"number": r.get("number"), "name": r.get("name")} for r in rows}


# ── State (section 8) ────────────────────────────────────────────────────


def _gather(*thunks):
    """Run independent reads concurrently (each a zero-argument callable)
    and return their results in call order. The page polls these routes
    every few seconds and each Supabase round trip costs a few hundred
    milliseconds from the office, so a dozen sequential reads is the
    difference between a live page and a spinner. The sync client already
    serves uvicorn's thread pool concurrently, so this is nothing new for
    it. An exception in any thunk propagates like a sequential call."""
    if len(thunks) <= 1:
        return [t() for t in thunks]
    with ThreadPoolExecutor(max_workers=min(len(thunks), 8)) as pool:
        return list(pool.map(lambda t: t(), thunks))


@router.get("/state", dependencies=[Depends(rfp_testing_rate_limit)])
def get_state(
    session_id: str | None = Query(default=None),
    after: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_dev_it_admin),
):
    """Everything the page polls every 3 seconds: the config, the active
    and the selected session, the history, what is paused, the two
    heartbeats, the counts and the events after the cursor."""
    settings = get_settings()
    sb = get_supabase()
    sessions = rfp_test.list_sessions(sb)
    names = _profile_names(sb, [s.get("started_by") for s in sessions])
    active = next((s for s in sessions if s.get("status") == rfp_test.STATUS_ACTIVE), None)
    selected = None
    if session_id:
        _require_uuid(session_id, "Test session")
        selected = next((s for s in sessions if s.get("id") == session_id), None)
        if selected is None:
            selected = rfp_test.get_session(sb, session_id)
            if selected is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, _SESSION_NOT_FOUND)
            names.update(_profile_names(sb, [selected.get("started_by")]))
    if selected is None:
        selected = active or (sessions[0] if sessions else None)

    counts = {"emails": 0, "ignored": 0, "projects": 0, "filed": 0, "mail_out": 0, "events": 0}
    events: list[dict] = []
    if selected is not None:
        sid = selected["id"]
        emails_n, ignored_n, projects_n, filed_n, mail_out_n, events_n, event_rows = _gather(
            lambda: _count(sb, "rfp_emails", test_session_id=sid),
            lambda: _count(sb, rfp_test.TABLE_EVENTS, session_id=sid, kind="ignored"),
            lambda: _count(sb, "projects", test_session_id=sid),
            lambda: _count(sb, "ingested_emails", test_session_id=sid),
            lambda: _count(sb, rfp_test.TABLE_EVENTS, session_id=sid, source=rfp_test.SOURCE_MAIL_OUT),
            lambda: _count(sb, rfp_test.TABLE_EVENTS, session_id=sid),
            lambda: rfp_test.events_for(sb, sid, after=after, limit=_EVENTS_MAX),
        )
        counts = {
            "emails": emails_n, "ignored": ignored_n, "projects": projects_n,
            "filed": filed_n, "mail_out": mail_out_n, "events": events_n,
        }
        events = [_event_out(e) for e in event_rows]

    heartbeat_row = active or selected or {}
    return {
        "enabled": True,
        "config": {
            "mailbox": settings.rfp_testing_mailbox,
            "sender": settings.rfp_testing_sender,
            "redirect_to": settings.rfp_testing_redirect_to,
            "poll_seconds": settings.rfp_testing_poll_seconds,
            "auto_create_env": bool(settings.rfp_create_auto_enabled),
        },
        "active": _session_out(active, names) if active else None,
        "selected": _session_out(selected, names) if selected else None,
        "sessions": [_session_out(s, names) for s in sessions],
        "paused": {
            "rfp_mailboxes": list(settings.rfp_email_ingestion_inboxes),
            "ngem": bool(settings.rfp_ingest_enabled and settings.rfp_ngem_enabled),
            "filer_mailbox": (settings.email_ingest_mailbox or "").strip().lower() or None,
            "note": "harvests already queued keep running",
        },
        "heartbeat": {
            "intake_last_tick_at": heartbeat_row.get("intake_last_tick_at"),
            "filer_last_tick_at": heartbeat_row.get("filer_last_tick_at"),
            "now": _now_iso(),
        },
        "counts": counts,
        "events": events,
    }


# ── Sessions (section 3) ─────────────────────────────────────────────────


@router.post("/sessions", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(rfp_testing_rate_limit)])
def create_session(body: SessionCreateIn, user: CurrentUser = Depends(require_dev_it_admin)):
    sb = get_supabase()
    try:
        session = rfp_test.activate(
            sb, actor_id=user.id, name=body.name, auto_create=body.auto_create
        )
    except rfp_test.RfpTestError as exc:
        raise _map_error(exc)
    return _session_out(session, _profile_names(sb, [user.id]))


@router.post("/sessions/{session_id}/end", dependencies=[Depends(rfp_testing_rate_limit)])
def end_session(session_id: str, user: CurrentUser = Depends(require_dev_it_admin)):
    sb = get_supabase()
    _load_session(sb, session_id)
    session = rfp_test.end_session(sb, session_id, actor_id=user.id)
    return _session_out(session, _profile_names(sb, [session.get("started_by")]))


@router.patch("/sessions/{session_id}", dependencies=[Depends(rfp_testing_rate_limit)])
def patch_session(
    session_id: str, body: SessionPatchIn, user: CurrentUser = Depends(require_dev_it_admin)
):
    sb = get_supabase()
    _load_session(sb, session_id)
    try:
        session = rfp_test.set_auto_create(sb, session_id, body.auto_create)
    except rfp_test.RfpTestError as exc:
        raise _map_error(exc)
    rfp_test.record(
        sb, session_id=session_id, source=rfp_test.SOURCE_SESSION, kind="auto_create",
        title=f"Automatic creation {'on' if body.auto_create else 'off'}",
        detail={"auto_create": body.auto_create, "by": user.id},
    )
    return _session_out(session, _profile_names(sb, [session.get("started_by")]))


@router.post("/sessions/{session_id}/cleanup", dependencies=[Depends(rfp_testing_rate_limit)])
def cleanup_session(session_id: str, user: CurrentUser = Depends(require_dev_it_admin)):
    """Ended sessions only (409 rfp_testing_session_active). Sync on
    purpose: the deletes run in FastAPI's threadpool with this handler."""
    sb = get_supabase()
    _load_session(sb, session_id)
    try:
        report = rfp_test.cleanup(sb, session_id, actor_id=user.id)
    except rfp_test.RfpTestError as exc:
        raise _map_error(exc)
    return {"report": report}


# ── Lists (section 8) ────────────────────────────────────────────────────


def _match_decision(row: dict) -> str | None:
    status_ = row.get("status")
    if status_ in ("merged", "duplicate", "review_match"):
        return status_
    if row.get("matched_at"):
        return "new"
    return None


def _platform_reference(row: dict) -> str | None:
    try:
        ref = rfp_harvest.reference_for(row)
    except Exception:  # noqa: BLE001 - a courtesy field
        return None
    return getattr(ref, "external_key", None) if ref is not None else None


@router.get("/sessions/{session_id}/emails", dependencies=[Depends(rfp_testing_rate_limit)])
def list_session_emails(session_id: str, user: CurrentUser = Depends(require_dev_it_admin)):
    sb = get_supabase()
    _load_session(sb, session_id)
    rows = (
        sb.table("rfp_emails")
        .select(_EMAIL_SELECT)
        .eq("test_session_id", session_id)
        .order("received_at", desc=True)
        .limit(_ROWS_MAX)
        .execute()
    ).data or []
    harvests = {
        h["id"]: h
        for h in _fetch_in(sb, "rfp_harvests", "id, status, split_status, split_job_id", "id",
                           [r.get("harvest_id") for r in rows])
    }
    projects = _project_refs(
        sb, [r.get("created_project_id") for r in rows] + [r.get("match_project_id") for r in rows]
    )
    # The session's pipeline events, grouped by email, for the step chips.
    by_email: dict[str, list[dict]] = {}
    for ev in rfp_test.events_for(sb, session_id, limit=_CHIP_EVENTS_MAX):
        if ev.get("rfp_email_id") and ev.get("source") in (
            rfp_test.SOURCE_INTAKE, rfp_test.SOURCE_HARVEST, rfp_test.SOURCE_SPLIT, rfp_test.SOURCE_CREATE
        ):
            by_email.setdefault(ev["rfp_email_id"], []).append(ev)
    out = []
    for row in rows:
        created = projects.get(row.get("created_project_id")) or {}
        matched = projects.get(row.get("match_project_id")) or {}
        harvest = harvests.get(row.get("harvest_id")) or {}
        out.append(
            {
                "id": row["id"],
                "status": row.get("status"),
                "flag_reason": row.get("flag_reason"),
                "decided_at_step": row.get("decided_at_step"),
                "from_address": row.get("from_address"),
                "from_name": row.get("from_name"),
                "subject": row.get("subject"),
                "received_at": row.get("received_at"),
                "forward_meta": row.get("forward_meta"),
                "method": row.get("invitation_method"),
                "llm_answer": row.get("llm_answer"),
                "llm_confidence": row.get("llm_confidence"),
                "extracted": {
                    "project_name": row.get("extracted_project_name"),
                    "gc_name": row.get("extracted_gc_name"),
                    "bid_due": row.get("extracted_bid_due_at"),
                    "platform_reference": _platform_reference(row),
                },
                "match": {
                    "decision": _match_decision(row),
                    "project_id": row.get("match_project_id"),
                    "project_number": matched.get("number"),
                },
                "harvest_id": row.get("harvest_id"),
                "harvest_status": harvest.get("status"),
                "split_status": harvest.get("split_status"),
                "split_job_id": harvest.get("split_job_id"),
                "created_project_id": row.get("created_project_id"),
                "created_project_number": created.get("number"),
                "steps": rfp_test.step_chips(
                    {**row, "split_status": harvest.get("split_status")}, by_email.get(row["id"], [])
                ),
                "last_error": row.get("last_error"),
                "attempts": row.get("attempts"),
                "next_attempt_at": row.get("next_attempt_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return {"rows": out}


@router.get("/sessions/{session_id}/ignored", dependencies=[Depends(rfp_testing_rate_limit)])
def list_session_ignored(session_id: str, user: CurrentUser = Depends(require_dev_it_admin)):
    sb = get_supabase()
    _load_session(sb, session_id)
    rows = []
    for ev in rfp_test.events_for(sb, session_id, limit=_ROWS_MAX, newest_first=True):
        if ev.get("kind") != "ignored":
            continue
        detail = ev.get("detail") or {}
        rows.append(
            {
                "at": ev.get("at"),
                "source": ev.get("source"),
                "from": detail.get("from"),
                "subject": detail.get("subject"),
                "received_at": detail.get("received_at"),
                "reason": detail.get("reason"),
            }
        )
    return {"rows": rows}


def _filed_reasoning(row: dict) -> str | None:
    matched_by, round_ = row.get("matched_by"), row.get("pipeline_round")
    if matched_by == "conversation":
        return "R1: the conversation map already knew this thread"
    if matched_by == "subject":
        return "R2: the project number in the subject"
    if matched_by == "llm":
        conf = row.get("match_confidence")
        return f"R3: the model matched the subject (confidence {conf})"
    if matched_by == "manual":
        return "Assigned by hand"
    if row.get("suggested_project_id"):
        return f"R3: below the threshold (confidence {row.get('suggested_confidence')}); suggested only"
    if round_ == "r3":
        return "R3: no confident match; Unknown"
    return None


@router.get("/sessions/{session_id}/filed", dependencies=[Depends(rfp_testing_rate_limit)])
def list_session_filed(session_id: str, user: CurrentUser = Depends(require_dev_it_admin)):
    sb = get_supabase()
    _load_session(sb, session_id)
    rows = (
        sb.table("ingested_emails")
        .select(_FILED_SELECT)
        .eq("test_session_id", session_id)
        .order("message_at", desc=True)
        .limit(_ROWS_MAX)
        .execute()
    ).data or []
    projects = _project_refs(sb, [r.get("project_id") for r in rows])
    return {
        "rows": [
            {
                "id": r["id"],
                "status": r.get("status"),
                "from_address": r.get("from_address"),
                "subject": r.get("subject"),
                "received_at": r.get("message_at"),
                "project_id": r.get("project_id"),
                "project_number": (projects.get(r.get("project_id")) or {}).get("number"),
                "match_method": r.get("matched_by"),
                "match_confidence": r.get("match_confidence"),
                "match_reasoning": _filed_reasoning(r),
                "last_error": r.get("error"),
                "updated_at": r.get("updated_at"),
            }
            for r in rows
        ]
    }


@router.get("/sessions/{session_id}/mail-out", dependencies=[Depends(rfp_testing_rate_limit)])
def list_session_mail_out(session_id: str, user: CurrentUser = Depends(require_dev_it_admin)):
    sb = get_supabase()
    _load_session(sb, session_id)
    events = rfp_test.events_for(
        sb, session_id, limit=_ROWS_MAX, source=rfp_test.SOURCE_MAIL_OUT, newest_first=True
    )
    return {"rows": [_event_out(e) for e in events]}


def _project_flags(rec: dict | None) -> list[str]:
    if not rec:
        return []
    flags = []
    if rec.get("sender_was_unauthorized"):
        flags.append("unauthorized_sender")
    if rec.get("bid_time_unknown"):
        flags.append("bid_time_unknown")
    if rec.get("gc_plan") in ("likely", "none"):
        flags.append(f"gc_{rec['gc_plan']}")
    if rec.get("files_status") in ("failed", "none"):
        flags.append(f"files_{rec['files_status']}")
    if rec.get("last_error"):
        flags.append("error")
    return flags


@router.get("/sessions/{session_id}/projects", dependencies=[Depends(rfp_testing_rate_limit)])
def list_session_projects(session_id: str, user: CurrentUser = Depends(require_dev_it_admin)):
    sb = get_supabase()
    _load_session(sb, session_id)
    projects = (
        sb.table("projects")
        .select("id, number, name, current_stage, created_at")
        .eq("test_session_id", session_id)
        .order("created_at", desc=True)
        .limit(_ROWS_MAX)
        .execute()
    ).data or []
    ids = [p["id"] for p in projects]
    links, files, filed, record_rows, mail_events = _gather(
        lambda: _fetch_in(sb, "project_gcs", "project_id, gc_id", "project_id", ids),
        lambda: _fetch_in(sb, "project_files", "id, project_id", "project_id", ids),
        lambda: _fetch_in(sb, "ingested_emails", "id, project_id", "project_id", ids),
        lambda: _fetch_in(
            sb, "rfp_created_projects",
            "project_id, sender_was_unauthorized, bid_time_unknown, gc_plan, files_status, last_error",
            "project_id", ids,
        ),
        lambda: rfp_test.events_for(
            sb, session_id, limit=_CHIP_EVENTS_MAX, source=rfp_test.SOURCE_MAIL_OUT,
        ),
    )
    gc_names = {
        g["id"]: g.get("name")
        for g in _fetch_in(sb, "general_contractors", "id, name", "id", [link.get("gc_id") for link in links])
    }
    records = {r["project_id"]: r for r in record_rows}
    mail_out: dict[str, int] = {}
    for ev in mail_events:
        if ev.get("project_id"):
            mail_out[ev["project_id"]] = mail_out.get(ev["project_id"], 0) + 1
    rows = []
    for p in projects:
        pid = p["id"]
        rows.append(
            {
                "id": pid,
                "number": p.get("number"),
                "name": p.get("name"),
                "current_stage": p.get("current_stage"),
                "created_at": p.get("created_at"),
                "gc_names": [
                    gc_names.get(link["gc_id"]) for link in links
                    if link.get("project_id") == pid and gc_names.get(link.get("gc_id"))
                ],
                "files": sum(1 for f in files if f.get("project_id") == pid),
                "filed_emails": sum(1 for e in filed if e.get("project_id") == pid),
                "mail_out": mail_out.get(pid, 0),
                "flags": _project_flags(records.get(pid)),
            }
        )
    return {"rows": rows}


# ── Project detail (section 8.3) ─────────────────────────────────────────


def _notification_rows(sb, project_id: str) -> list[dict]:
    """`{at, type, recipients, subject}` from the notification log's event
    view (bell rows and emails collapsed the way the log shows them)."""
    from app.services import notification_log

    try:
        entries = notification_log.build(project_id).get("entries") or []
    except Exception:  # noqa: BLE001 - the log is a courtesy on this page
        logger.exception("rfp testing: notification log failed for %s", project_id)
        return []
    out = []
    for entry in entries:
        recipients = []
        for r in entry.get("recipients") or []:
            label = r.get("name") or r.get("email") or r.get("address") or ""
            if r.get("role"):
                label = f"{label} ({r['role']})"
            recipients.append(label)
        out.append(
            {
                "at": entry.get("at"),
                "type": entry.get("type"),
                "title": entry.get("title"),
                "channels": entry.get("channels"),
                "recipients": recipients,
                "subject": entry.get("subject") or entry.get("message"),
            }
        )
    return out


@router.get("/projects/{project_id}", dependencies=[Depends(rfp_testing_rate_limit)])
def get_test_project(project_id: str, user: CurrentUser = Depends(require_dev_it_admin)):
    """Everything the page shows for one of a session's projects: the row,
    the lane state, the GCs and contacts, the files, the filed emails, the
    stage events, the notifications, the outbound mail, the RFP record and
    the source email. Only projects a session created (tagged) answer."""
    _require_uuid(project_id, "Project")
    sb = get_supabase()
    rows = sb.table("projects").select(_PROJECT_SELECT).eq("id", project_id).limit(1).execute().data or []
    if not rows or not rows[0].get("test_session_id"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _PROJECT_NOT_FOUND)
    project = rows[0]
    session_id = project["test_session_id"]

    # Everything that only needs the project id goes out at once; the two
    # GC lookups that need the link rows follow in a second, smaller wave.
    (
        links, sends, files, filed, stage_events, record_rows, category_state,
        notifications, mail_events,
    ) = _gather(
        lambda: (
            sb.table("project_gcs").select("id, gc_id, needs_by").eq("project_id", project_id).execute()
        ).data or [],
        lambda: (
            sb.table("proposal_sends").select("gc_id, sent_at, status").eq("project_id", project_id).execute()
        ).data or [],
        lambda: (
            sb.table("project_files")
            .select("id, filename, category, size_bytes, created_at, rfp_harvest_id, uploaded_by")
            .eq("project_id", project_id)
            .order("created_at", desc=False)
            .execute()
        ).data or [],
        lambda: (
            sb.table("ingested_emails")
            .select("id, from_address, subject, message_at, matched_by")
            .eq("project_id", project_id)
            .order("message_at", desc=True)
            .limit(_ROWS_MAX)
            .execute()
        ).data or [],
        lambda: (
            sb.table("stage_events")
            .select("entered_at, actor_id, from_stage, to_stage, category, note")
            .eq("project_id", project_id)
            .order("entered_at", desc=True)
            .limit(_ROWS_MAX)
            .execute()
        ).data or [],
        lambda: (
            sb.table("rfp_created_projects")
            .select("rfp_email_id, sender_was_unauthorized, bid_time_unknown, gc_plan, likely_gc_name, "
                    "files_status, files_promoted, files_skipped, cleared_at, last_error, automatic")
            .eq("project_id", project_id)
            .limit(1)
            .execute()
        ).data or [],
        lambda: workflow.load_category_state(project_id),
        lambda: _notification_rows(sb, project_id),
        lambda: rfp_test.events_for(
            sb, session_id, limit=_ROWS_MAX, source=rfp_test.SOURCE_MAIL_OUT,
            project_id=project_id, newest_first=True,
        ),
    )
    gc_ids = [link.get("gc_id") for link in links]
    rec = record_rows[0] if record_rows else None
    gc_rows, contacts, actor_names, src_rows = _gather(
        lambda: _fetch_in(sb, "general_contractors", "id, name", "id", gc_ids),
        lambda: _fetch_in(sb, "gc_contacts", "id, gc_id, name, email", "gc_id", gc_ids),
        lambda: _profile_names(
            sb, [e.get("actor_id") for e in stage_events] + [project.get("created_by")]
        ),
        lambda: (
            sb.table("rfp_emails")
            .select("id, subject, from_address, invitation_method")
            .eq("id", rec["rfp_email_id"])
            .limit(1)
            .execute()
        ).data or [] if rec and rec.get("rfp_email_id") else [],
    )
    gc_names = {g["id"]: g.get("name") for g in gc_rows}
    sent_at = {s["gc_id"]: s.get("sent_at") for s in sends if s.get("sent_at")}
    gcs = [
        {
            "gc_id": link.get("gc_id"),
            "name": gc_names.get(link.get("gc_id")),
            "contacts": [
                {"name": c.get("name"), "email": c.get("email")}
                for c in contacts if c.get("gc_id") == link.get("gc_id")
            ],
            "needs_by": link.get("needs_by"),
            "sent_at": sent_at.get(link.get("gc_id")),
        }
        for link in links
    ]

    source_email = None
    if src_rows:
        source_email = {
            "id": src_rows[0]["id"], "subject": src_rows[0].get("subject"),
            "from_address": src_rows[0].get("from_address"), "method": src_rows[0].get("invitation_method"),
        }

    return {
        "project": {
            **{k: project.get(k) for k in (
                "id", "number", "name", "current_stage", "current_owner_role", "internal_bid_at",
                "actual_bid_at", "created_at", "created_by", "abandoned_at", "test_session_id",
            )},
            "created_by_name": actor_names.get(project.get("created_by")),
        },
        "category_state": category_state,
        "gcs": gcs,
        "files": [
            {
                "id": f["id"],
                "name": f.get("filename"),
                "category": f.get("category"),
                "size": f.get("size_bytes"),
                "created_at": f.get("created_at"),
                "source": "rfp_harvest" if f.get("rfp_harvest_id") else ("upload" if f.get("uploaded_by") else "system"),
            }
            for f in files
        ],
        "filed_emails": [
            {
                "id": e["id"], "from_address": e.get("from_address"), "subject": e.get("subject"),
                "received_at": e.get("message_at"), "match_method": e.get("matched_by"),
            }
            for e in filed
        ],
        "stage_events": [
            {
                "at": e.get("entered_at"),
                "actor": actor_names.get(e.get("actor_id")) or ("System" if not e.get("actor_id") else None),
                "from": e.get("from_stage"),
                "to": e.get("to_stage"),
                "category": e.get("category"),
                "note": e.get("note"),
            }
            for e in stage_events
        ],
        "notifications": notifications,
        "mail_out": [_event_out(e) for e in mail_events],
        "rfp_created": {
            "flags": _project_flags(rec),
            "files_status": rec.get("files_status") if rec else None,
            "files_promoted": rec.get("files_promoted") if rec else None,
            "files_skipped": rec.get("files_skipped") if rec else None,
            "cleared_at": rec.get("cleared_at") if rec else None,
            "automatic": rec.get("automatic") if rec else None,
            "last_error": rec.get("last_error") if rec else None,
        } if rec else None,
        "source_email": source_email,
    }

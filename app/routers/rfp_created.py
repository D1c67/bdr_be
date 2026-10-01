"""The "Created from RFP Ingestion" page: /rfp-created (docs/RFP_CREATE.md
sections 7 and 8).

Every bidding project the RFP creation step made (services/rfp_create),
newest first, with the flags a person has to act on: missing files, no GC
(or a likely one to confirm), intake details still empty, a sender that
was unauthorized and allowed by hand, documents the sandbox would not let
through, a failed promotion. Clear hides a row, Restore brings it back,
"Retry documents" re-enqueues the promotion job.

Access, three locks deep, the same contract as /rfp-emails:

- The env switch. `require_rfp_created` is a ROUTER-level dependency:
  while neither the email intake nor the NGEM slice is served every route
  404s with the bare "Not Found" body before a token is read.
- Roles: the Estimating Admin, the Executive and the IT Admin on every
  route (all three may see the actual bid date, which the list carries),
  so nothing here needs per-role redaction. The engineers, the read-only
  accountant and the external estimator never reach it.
- Rate limits: the catch-all budget on every route.

Reads are one query per table over the page (the created records, the
projects, the GC links, the GCs, the profiles, the two source tables),
never a query per row. The list and the counts narrow by a creation window
(`created_from` inclusive, `created_to` exclusive; the page computes its
Pacific-time presets and sends the instants) and a search box `q`, matched
server side through the `rfp_created_search` view (migration 0135) so
paging stays correct. Every mutation is audited here (`rfp_created.*`),
inside the conditional update that performs it.

Event-loop rule: every handler is a plain `def` (the Supabase SDK is sync).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_role
from app.core.error_codes import RateLimitScope
from app.core.ratelimit import ai_rate_limit, rate_limit
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services.notifications import audit

logger = logging.getLogger(__name__)


# ── Feature gate ─────────────────────────────────────────────────────────


def rfp_created_enabled() -> bool:
    """Whether this deployment serves the page: the email intake
    (`rfp_email_ingestion_enabled`) OR a portal slice (`rfp_portal_any_enabled`,
    contract D4; `rfp_ngem_active` kept for a settings object that predates
    the BuildingConnected slice), read through `getattr` so an absent flag
    fails CLOSED, never open."""
    settings = get_settings()
    return (
        bool(getattr(settings, "rfp_email_ingestion_enabled", False))
        or bool(getattr(settings, "rfp_ngem_active", False))
        or bool(getattr(settings, "rfp_portal_any_enabled", False))
    )


def require_rfp_created() -> None:
    """Router-level dependency: while both switches are off every route
    404s with the bare "Not Found" body before auth runs."""
    if not rfp_created_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


PAGE_ROLES = (Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN)
require_page = require_role(*PAGE_ROLES)

rfp_created_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

router = APIRouter(
    prefix="/rfp-created",
    tags=["rfp-created"],
    dependencies=[Depends(require_rfp_created)],
)

TABLE = "rfp_created_projects"
# Migration 0135: one row per record with `project_id, created_at,
# cleared_at` and a lowercased `search_text` (number, name, linked GC
# names, email subject or portal title, sender, method). Service role only.
SEARCH_VIEW = "rfp_created_search"
_Q_MAX = 100
AUDIT_ENTITY = "rfp_created_project"
_NOT_FOUND = "Created project record not found"
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 200
_IN_CHUNK = 200

Cleared = Literal["false", "true", "all"]

# The filter params shared by the list and the counts. Annotated (not a
# `Query(...)` default) so a direct call in the tests sees a real None.
CreatedFrom = Annotated[str | None, Query(max_length=64)]
CreatedTo = Annotated[str | None, Query(max_length=64)]
SearchText = Annotated[str | None, Query(max_length=_Q_MAX)]

# The project columns the list carries: the reference, the two dates, the
# stage and the intake fields the missing-list rule reads.
_PROJECT_SELECT = (
    "id, number, name, current_stage, actual_bid_at, internal_bid_at, "
    "due_from_estimator_at, due_from_vendors_at, project_type, owner_type, labor_needed, "
    "bid_method, competitor_known, gc_known, subs_needed, est_value_band, scope_fit, "
    # The BuildingConnected flags (0136): the files flag and the open GC question.
    "gc_confirm_pending, files_needed_source, files_needed_url, files_needed_set_at, "
    "files_needed_cleared_at"
)
_EMAIL_SELECT = "id, subject, received_at, invitation_method"
_INVITATION_SELECT = "id, title, first_seen_at, portal, external_url"
# The portal whose rows never carry a harvest (docs/RFP_BUILDINGCONNECTED.md
# 3.8): the files flag replaces "Missing files" for them.
METHOD_BC = "buildingconnected"


# ── Service seams ────────────────────────────────────────────────────────


def _create():
    """app/services/rfp_create, imported on first use."""
    from app.services import rfp_create

    return rfp_create


def _files():
    """app/services/rfp_create_files, imported on first use."""
    from app.services import rfp_create_files

    return rfp_create_files


def _intake():
    """app/services/project_intake, imported on first use."""
    from app.services import project_intake

    return project_intake


# ── Helpers ──────────────────────────────────────────────────────────────


def _uuid_or(value: str | None, message: str, status_code: int) -> str:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code, message) from None
    return str(value)


def _uuid_or_404(value: str | None, message: str) -> str:
    return _uuid_or(value, message, status.HTTP_404_NOT_FOUND)


def _uuid_or_400(value: str | None, message: str) -> str:
    return _uuid_or(value, message, status.HTTP_400_BAD_REQUEST)


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _by_id(sb, table: str, ids, select: str) -> dict[str, dict]:
    """`{id: row}` for the ids given, chunked; no query when nothing is wanted."""
    clean = sorted({str(i) for i in ids if i})
    out: dict[str, dict] = {}
    for chunk in _chunks(clean):
        rows = sb.table(table).select(select).in_("id", chunk).execute().data or []
        for row in rows:
            if row.get("id"):
                out[str(row["id"])] = row
    return out


def _person(profiles: dict[str, dict], user_id) -> dict | None:
    if not user_id:
        return None
    profile = profiles.get(str(user_id)) or {}
    return {"id": str(user_id), "full_name": profile.get("full_name")}


def _parse_instant(value: str | None, name: str) -> datetime | None:
    """An ISO timestamp query param (naive = UTC); blank = None, junk = 400."""
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_before(value: str | None) -> str | None:
    parsed = _parse_instant(value, "before")
    return parsed.isoformat() if parsed else None


def _created_window(created_from: str | None, created_to: str | None) -> tuple[str | None, str | None]:
    """The creation window `[created_from, created_to)` as UTC ISO strings
    (either side may be open). 400 on junk, and when `created_from` is not
    strictly before `created_to`."""
    lo = _parse_instant(created_from, "created_from")
    hi = _parse_instant(created_to, "created_to")
    if lo and hi and lo >= hi:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "created_from must be before created_to")

    def utc(d: datetime | None) -> str | None:
        return d.astimezone(timezone.utc).isoformat() if d else None

    return utc(lo), utc(hi)


def _search_pattern(q: str | None) -> str | None:
    """The search box as an ILIKE substring pattern over the view's
    `search_text`, or None when blank. Control characters are dropped, the
    LIKE wildcards and the escape are backslash-escaped (the input is
    literal text), and `*` becomes the one-character wildcard `_`: PostgREST
    reads `*` in a like pattern as `%` and has no escape for it, so a
    literal asterisk can only be matched loosely. The value travels as one
    filter param (`search_text=ilike.<pattern>`), never inside an `or=(...)`
    group, so commas and parentheses need no quoting."""
    text = "".join(ch for ch in (q or "") if ch >= " " and ch != "\x7f").strip().lower()
    if not text:
        return None
    text = text[:_Q_MAX].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{text.replace('*', '_')}%"


def _scoped(query, cleared: str, lo: str | None, hi: str | None, pattern: str | None):
    """The cleared filter, the creation window and the search on a query over
    the table or the view (both carry `created_at` and `cleared_at`)."""
    if cleared == "false":
        query = query.is_("cleared_at", "null")
    elif cleared == "true":
        query = query.not_.is_("cleared_at", "null")
    if lo:
        query = query.gte("created_at", lo)
    if hi:
        query = query.lt("created_at", hi)
    if pattern:
        query = query.ilike("search_text", pattern)
    return query


def _record_or_404(sb, project_id: str) -> dict:
    rows = sb.table(TABLE).select("*").eq("project_id", project_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return rows[0]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_summary(sb, job_ids) -> dict[str, dict]:
    """`{job_id: {files_total, files_done, segments}}` for the split jobs on
    the page (docs/RFP_SPLIT.md 4): one query per table."""
    clean = sorted({str(j) for j in job_ids if j})
    out: dict[str, dict] = {j: {"files_total": 0, "files_done": 0, "segments": 0} for j in clean}
    if not clean:
        return out
    files: list[dict] = []
    for chunk in _chunks(clean):
        files.extend(
            sb.table("bid_split_files").select("id, job_id, status").in_("job_id", chunk).execute().data or []
        )
    job_of = {}
    for f in files:
        job = out.setdefault(str(f.get("job_id")), {"files_total": 0, "files_done": 0, "segments": 0})
        job["files_total"] += 1
        if f.get("status") == "done":
            job["files_done"] += 1
        job_of[str(f["id"])] = str(f.get("job_id"))
    for chunk in _chunks(sorted(job_of)):
        segs = sb.table("bid_split_segments").select("file_id").in_("file_id", chunk).execute().data or []
        for seg in segs:
            job = out.get(job_of.get(str(seg.get("file_id")), ""))
            if job is not None:
                job["segments"] += 1
    return out


def _item(
    rec: dict, project: dict | None, gc_names: dict[str, str], links: dict[str, list[str]],
    profiles: dict[str, dict], emails: dict[str, dict], invitations: dict[str, dict],
    splits: dict[str, dict] | None = None, issues: dict[str, dict | None] | None = None,
) -> dict:
    """One page row (section 8's shape) from the loaded tables. `issues` is
    `rfp_split.issues_for_records` over the page (docs/RFP_SPLIT.md 10)."""
    create = _create()
    project = project or {}
    project_id = str(rec["project_id"])
    gc_ids = links.get(project_id, [])
    likely_id = rec.get("likely_gc_id")
    likely = None
    provisional = None
    named_gc = None
    if likely_id or rec.get("likely_gc_name"):
        named_gc = {
            "id": likely_id,
            "name": gc_names.get(str(likely_id or ""), rec.get("likely_gc_name")) or rec.get("likely_gc_name"),
        }
    if rec.get("gc_plan") == create.GC_LIKELY:
        likely = named_gc
    elif rec.get("gc_plan") == getattr(create, "GC_PROVISIONAL", "provisional"):
        # The BuildingConnected provisional GC (D15): attached, awaiting "same GC?".
        provisional = named_gc
    if rec.get("source_kind") == create.SOURCE_PORTAL:
        inv = invitations.get(str(rec.get("portal_invitation_id") or "")) or {}
        source = {
            "id": rec.get("portal_invitation_id"),
            "title": inv.get("title"),
            "first_seen_at": inv.get("first_seen_at"),
            "invitation_method": rec.get("invitation_method") or inv.get("portal"),
        }
    else:
        email = emails.get(str(rec.get("rfp_email_id") or "")) or {}
        source = {
            "id": rec.get("rfp_email_id"),
            "subject": email.get("subject"),
            "received_at": email.get("received_at"),
            "invitation_method": rec.get("invitation_method") or email.get("invitation_method"),
        }
    files_status = rec.get("files_status") or "none"
    promoted = int(rec.get("files_promoted") or 0)
    skipped = rec.get("files_skipped") or []
    missing = (
        _intake().missing_intake_fields(project, bid_time_unknown=bool(rec.get("bid_time_unknown")))
        if project
        else []
    )
    issue = (issues or {}).get(project_id)
    split_status = rec.get("split_status")
    split_job_id = (issue or {}).get("job_id") or rec.get("split_job_id")
    split = (splits or {}).get(str(split_job_id or ""), {}) if split_job_id else {}
    # BuildingConnected rows (D19): no harvest ever exists, so the promotion
    # status can never say anything; the files flag on the project is the
    # thing to act on, and "Retry documents" has nothing to retry.
    is_bc = source.get("invitation_method") == METHOD_BC
    files_needed = bool(project.get("files_needed_set_at")) and not project.get("files_needed_cleared_at")
    missing_files = (files_status == "none" or (files_status == "complete" and promoted == 0)) and not is_bc
    return {
        "project": {
            "id": project_id,
            "number": project.get("number"),
            "name": project.get("name"),
            "current_stage": project.get("current_stage"),
            "actual_bid_at": project.get("actual_bid_at"),
            "internal_bid_at": project.get("internal_bid_at"),
            "gcs": [{"id": gid, "name": gc_names.get(gid)} for gid in gc_ids],
        },
        "created": {
            "created_at": rec.get("created_at"),
            "created_by": _person(profiles, rec.get("created_by")),
            "automatic": bool(rec.get("automatic")),
            "source_kind": rec.get("source_kind"),
            "source": source,
            "harvest_id": rec.get("harvest_id"),
            "gc_plan": rec.get("gc_plan"),
        },
        "flags": {
            "missing_files": missing_files,
            "no_gc": not gc_ids,
            "likely_gc": likely,
            # BuildingConnected (docs/RFP_BUILDINGCONNECTED.md 3.4 and 3.8):
            # the files flag with its deep link, the open "same GC?" question
            # and the GC attached provisionally. Read off the PROJECT, so a
            # dismissal or a confirmation clears them without touching the record.
            "files_needed": files_needed,
            "files_needed_url": project.get("files_needed_url") if files_needed else None,
            "confirm_gc": bool(project.get("gc_confirm_pending")),
            "provisional_gc": provisional,
            "intake_incomplete": missing,
            "sender_was_unauthorized": bool(rec.get("sender_was_unauthorized")),
            "sender_display": rec.get("sender_display"),
            "sender_allowed_by": _person(profiles, rec.get("sender_allowed_by")),
            "documents_skipped": len(skipped) if isinstance(skipped, list) else 0,
            "files_status": files_status,
            "files_promoted": promoted,
            "files_error": rec.get("files_error"),
            "files_failed": files_status == "failed",
            # A best-effort step that failed after the project existed (the
            # Go/No-Go advance, the mates, the bells; RFP_CREATE.md 4.5).
            "last_error": rec.get("last_error"),
            # The Bid File Splitter step (docs/RFP_SPLIT.md 4): its outcome,
            # the job for the "Open in splitter" link, and the counts behind
            # "N documents from M files". Null status = created before the step.
            "split_status": split_status,
            "split_job_id": split_job_id,
            "split_files_total": int(split.get("files_total") or 0),
            "split_files_done": int(split.get("files_done") or 0),
            "split_segments": int(split.get("segments") or 0),
            # The splitter flag (docs/RFP_SPLIT.md 10), read live from the
            # harvest and the job: `split_failed` covers a staging give-up
            # and a job whose every file failed, `split_partial` a split
            # where some files failed (those were promoted whole), and
            # `split_issue` carries the reason, the failed files, a running
            # manual run and the "split outside the app" resolution.
            "split_failed": (
                (issue or {}).get("state") == "failed" if issues is not None else split_status == "failed"
            ),
            "split_partial": (issue or {}).get("state") == "partial",
            "split_issue": issue,
        },
        "cleared_at": rec.get("cleared_at"),
        "cleared_by": _person(profiles, rec.get("cleared_by")),
    }


def _split():
    """app/services/rfp_split, imported on first use."""
    from app.services import rfp_split

    return rfp_split


def _split_issues(sb, records: list[dict], profiles: dict[str, dict]) -> dict[str, dict | None]:
    """The splitter flag per record (docs/RFP_SPLIT.md 10), plus
    `package_sent` (the hand-off lock the manual run respects) for the
    flagged rows only: one lock read per flagged project, never per row."""
    from app.routers.files import handoff_locked

    names = {pid: (p or {}).get("full_name") for pid, p in profiles.items()}
    issues = _split().issues_for_records(sb, records, names)
    for pid, issue in issues.items():
        if issue is not None:
            try:
                issue["package_sent"] = bool(handoff_locked(pid))
            except Exception:  # noqa: BLE001 - the run itself checks again
                logger.exception("rfp created: hand-off lock read failed for %s", pid)
                issue["package_sent"] = False
    return issues


def issue_for_project(sb, rec: dict) -> dict | None:
    """One record's splitter flag with the resolver's name resolved (the
    project detail route and the three split actions answer with it)."""
    people = _by_id(sb, "profiles", {rec.get("split_resolved_by")}, "id, full_name")
    return _split_issues(sb, [rec], people).get(str(rec["project_id"]))


# ── Routes ───────────────────────────────────────────────────────────────
# Literal paths first (the codebase's literal-before-param ordering rule).


@router.get("/counts", dependencies=[Depends(rfp_created_rate_limit)])
def rfp_created_counts(
    _: CurrentUser = Depends(require_page),
    created_from: CreatedFrom = None,
    created_to: CreatedTo = None,
    q: SearchText = None,
) -> dict:
    """`{active, cleared}`: two exact-count HEAD reads (no rows transferred,
    so PostgREST's max-rows cap never undercounts). With no params this is
    the sidebar badge, read off the table exactly as before; the page passes
    its filter (`created_from`, `created_to`, `q`, the list's meanings and
    400s) for the "N projects" line, and a search reads the view."""
    lo, hi = _created_window(created_from, created_to)
    pattern = _search_pattern(q)
    sb = get_supabase()
    source = SEARCH_VIEW if pattern else TABLE

    def count(cleared: str) -> int:
        query = sb.table(source).select("project_id", count="exact", head=True)
        res = _scoped(query, cleared, lo, hi, pattern).execute()
        return int(res.count if res.count is not None else len(res.data or []))

    return {"active": count("false"), "cleared": count("true")}


@router.get("", dependencies=[Depends(rfp_created_rate_limit)])
def list_rfp_created(
    cleared: Cleared = "false",
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    before: str | None = Query(default=None, max_length=64),
    before_id: str | None = Query(default=None, max_length=64),
    _: CurrentUser = Depends(require_page),
    created_from: CreatedFrom = None,
    created_to: CreatedTo = None,
    q: SearchText = None,
) -> list[dict]:
    """The page, newest first (`created_at desc, project_id desc`), paged
    on the last row's `(created_at, project_id)` pair: `before` is its
    `created_at`, `before_id` its `project_id`, and the next page is every
    row strictly before that pair in the same order, so two rows created in
    the same instant never repeat or vanish between pages. `before` alone
    pages on the timestamp only (an older client). `cleared=false`
    (default) lists the rows a person has not cleared, `true` the cleared
    ones, `all` both. `created_from` (inclusive) and `created_to`
    (exclusive) bound `created_at` (ISO timestamps, 400 on junk or an empty
    window). `q` (trimmed, blank ignored) is a case-insensitive substring
    search over the project number and name, the linked GC names, the
    email subject or portal title, the sender and the method: the page's
    ids come from the `rfp_created_search` view under the same filters,
    order and paging, then the records are read by id in that order. One
    query per table over the page; the flags are computed here and never
    stored (the sender marker excepted)."""
    lo, hi = _created_window(created_from, created_to)
    pattern = _search_pattern(q)
    before_iso = _parse_before(before)
    sb = get_supabase()
    query = sb.table(SEARCH_VIEW).select("project_id") if pattern else sb.table(TABLE).select("*")
    query = _scoped(query, cleared, lo, hi, pattern)
    if before_iso and before_id:
        pid = _uuid_or_400(before_id, "before_id must be a project id")
        query = query.or_(
            f"created_at.lt.{before_iso},and(created_at.eq.{before_iso},project_id.lt.{pid})"
        )
    elif before_iso:
        query = query.lt("created_at", before_iso)
    records = (
        query.order("created_at", desc=True).order("project_id", desc=True).limit(limit).execute()
    ).data or []
    if pattern and records:
        order = [str(r["project_id"]) for r in records if r.get("project_id")]
        by_project: dict[str, dict] = {}
        for chunk in _chunks(order):
            for row in sb.table(TABLE).select("*").in_("project_id", chunk).execute().data or []:
                by_project[str(row["project_id"])] = row
        # A record deleted between the two reads simply drops off the page.
        records = [by_project[key] for key in order if key in by_project]
    if not records:
        return []

    project_ids = [str(r["project_id"]) for r in records if r.get("project_id")]
    projects = _by_id(sb, "projects", project_ids, _PROJECT_SELECT)
    links: dict[str, list[str]] = {}
    for chunk in _chunks(project_ids):
        rows = (
            sb.table("project_gcs").select("project_id, gc_id").in_("project_id", chunk).execute()
        ).data or []
        for row in rows:
            if row.get("project_id") and row.get("gc_id"):
                links.setdefault(str(row["project_id"]), []).append(str(row["gc_id"]))
    gc_ids = {gid for ids in links.values() for gid in ids}
    gc_ids |= {str(r["likely_gc_id"]) for r in records if r.get("likely_gc_id")}
    gcs = _by_id(sb, "general_contractors", gc_ids, "id, name")
    gc_names = {gid: gc.get("name") for gid, gc in gcs.items()}
    people = {r.get("created_by") for r in records} | {r.get("cleared_by") for r in records}
    people |= {r.get("sender_allowed_by") for r in records}
    people |= {r.get("split_resolved_by") for r in records}
    profiles = _by_id(sb, "profiles", people, "id, full_name")
    emails = _by_id(sb, "rfp_emails", {r.get("rfp_email_id") for r in records}, _EMAIL_SELECT)
    invitations = _by_id(
        sb, "rfp_portal_invitations", {r.get("portal_invitation_id") for r in records},
        _INVITATION_SELECT,
    )
    for ids in links.values():
        ids.sort(key=lambda gid: ((gc_names.get(gid) or "").lower(), gid))
    issues = _split_issues(sb, records, profiles)
    splits = _split_summary(
        sb, [(issues.get(str(r.get("project_id"))) or {}).get("job_id") or r.get("split_job_id") for r in records]
    )
    return [
        _item(rec, projects.get(str(rec.get("project_id"))), gc_names, links, profiles, emails,
              invitations, splits, issues)
        for rec in records
    ]


@router.post("/{project_id}/clear", dependencies=[Depends(rfp_created_rate_limit)])
def clear_rfp_created(project_id: str, user: CurrentUser = Depends(require_page)) -> dict:
    """Hide the row from the page (the project itself is untouched). 404
    when the project was not created by this slice, 409 when it is already
    cleared. Audited `rfp_created.clear`."""
    _uuid_or_404(project_id, _NOT_FOUND)
    sb = get_supabase()
    _record_or_404(sb, project_id)
    now = _now_iso()
    rows = (
        sb.table(TABLE)
        .update({"cleared_at": now, "cleared_by": user.id, "restored_at": None})
        .eq("project_id", project_id)
        .is_("cleared_at", "null")
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_409_CONFLICT, "This row is already cleared.")
    audit(user.id, "rfp_created.clear", AUDIT_ENTITY, project_id, None)
    return {"project_id": project_id, "cleared_at": now, "cleared_by": user.id}


@router.post("/{project_id}/restore", dependencies=[Depends(rfp_created_rate_limit)])
def restore_rfp_created(project_id: str, user: CurrentUser = Depends(require_page)) -> dict:
    """Bring a cleared row back. 404 when the project was not created by
    this slice, 409 when it is not cleared. Audited `rfp_created.restore`."""
    _uuid_or_404(project_id, _NOT_FOUND)
    sb = get_supabase()
    _record_or_404(sb, project_id)
    now = _now_iso()
    rows = (
        sb.table(TABLE)
        .update({"cleared_at": None, "cleared_by": None, "restored_at": now})
        .eq("project_id", project_id)
        .not_.is_("cleared_at", "null")
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_409_CONFLICT, "This row is not cleared.")
    audit(user.id, "rfp_created.restore", AUDIT_ENTITY, project_id, None)
    return {"project_id": project_id, "cleared_at": None, "restored_at": now}


@router.post(
    "/{project_id}/retry-files",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rfp_created_rate_limit)],
)
def retry_rfp_created_files(project_id: str, user: CurrentUser = Depends(require_page)) -> dict:
    """Re-enqueue the document promotion (docs/RFP_CREATE.md 5) when
    `files_status` is `failed` or `none` (or `running` under a claim older
    than the queue lease: the worker that held it is gone) and the harvest
    has verified entries: 202 `{job}`. The record goes `pending` FIRST,
    fenced on the status just read, and only then is the job enqueued; an
    enqueue that fails puts the prior status back. 409 while a promotion is
    pending or running, or when there is nothing to promote. 409 as well
    while the Bid File Splitter is running on the project (a manual run
    staging or its job processing: `rfp_split.split_running`): the
    promotion and the splitter's re-file reconcile the same sandbox ids and
    must not run together (docs/RFP_SPLIT.md 10.5). Audited
    `rfp_created.retry_files`."""
    _uuid_or_404(project_id, _NOT_FOUND)
    sb = get_supabase()
    rec = _record_or_404(sb, project_id)
    files = _files()
    prior = str(rec.get("files_status") or files.STATUS_NONE)
    if not files.retryable(rec, get_settings()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The documents are already being promoted (or were promoted); nothing to retry.",
        )
    split = _split()
    if split.split_running(sb, rec):
        raise HTTPException(status.HTTP_409_CONFLICT, split.MSG_RETRY_WHILE_SPLITTING)
    harvest = None
    if rec.get("harvest_id"):
        rows = (
            sb.table("rfp_harvests").select("id, files").eq("id", rec["harvest_id"]).limit(1).execute()
        ).data or []
        harvest = rows[0] if rows else None
    entries = [e for e in ((harvest or {}).get("files") or []) if isinstance(e, dict)]
    if not any(e.get("sandbox_file_id") and e.get("status") in files.ENTRY_WITH_FILE for e in entries):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The harvest has no verified documents to promote."
        )
    from app.services import llm_queue

    if not files.mark_pending(sb, project_id, prior):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Someone else retried the documents for this project first."
        )
    try:
        job = files.enqueue(project_id, created_by=user.id)
    except llm_queue.JobAlreadyActive as exc:
        files.unmark_pending(sb, project_id, prior)
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A promotion for this project is already queued or running."
        ) from exc
    except Exception:
        files.unmark_pending(sb, project_id, prior)
        raise
    audit(user.id, "rfp_created.retry_files", AUDIT_ENTITY, project_id,
          {"from_status": prior, "job_id": job.get("id")})
    return {"job": {"id": job.get("id"), "status": job.get("status")}}


# ── The splitter flag's actions (docs/RFP_SPLIT.md 10) ───────────────────
#
# Same roles as every route here (require_page: Estimating Admin, Executive,
# IT Admin). "Run the splitter" rides the AI budget (`ai_rate_limit`, the
# splitter's own run routes use it); the resolution toggles ride the page's
# catch-all budget.


@router.post(
    "/{project_id}/split/run",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(ai_rate_limit)],
)
def run_rfp_created_split(
    project_id: str, background: BackgroundTasks, user: CurrentUser = Depends(require_page),
) -> dict:
    """Run the Bid File Splitter on a project whose split failed or partly
    failed: the failed files are queued again on the job linked to the
    project, or (no job: staging gave up) a fresh job is staged from the
    project's harvest in the background, linked to the project. Each file
    that finishes re-files the project's documents (a whole file becomes the
    source set in place, its segments are added). 202 `{mode, job_id,
    files, split}` with the flag now `running`. 409 while the splitter or
    the queue is off, the project is marked split outside the app, its
    hand-off package has sent (the corrections sentence), a run is already
    going, there is nothing to re-run, or the model is away (its sentence).
    Audited `rfp_created.split_run`."""
    _uuid_or_404(project_id, _NOT_FOUND)
    sb = get_supabase()
    rec = _record_or_404(sb, project_id)
    split = _split()
    try:
        plan = split.start_manual_run(sb, rec, actor_id=user.id)
    except split.RunRefused as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    if plan["mode"] == "stage":
        background.add_task(split.stage_for_project, project_id, user.id)
    audit(user.id, "rfp_created.split_run", AUDIT_ENTITY, project_id,
          {"mode": plan["mode"], "job_id": plan.get("job_id"), "files": plan.get("files")})
    return {**plan, "split": issue_for_project(sb, _record_or_404(sb, project_id))}


@router.post("/{project_id}/split/outside", dependencies=[Depends(rfp_created_rate_limit)])
def mark_rfp_created_split_outside(project_id: str, user: CurrentUser = Depends(require_page)) -> dict:
    """"I'll split it outside the app": records who and when; the project
    page then shows a muted note with an Undo instead of the banner. The
    person uploads their split files through the normal upload flow. 409
    when there is no open splitter flag (nothing failed, or a run is going)
    or it is already marked. Audited `rfp_created.split_outside`."""
    _uuid_or_404(project_id, _NOT_FOUND)
    sb = get_supabase()
    rec = _record_or_404(sb, project_id)
    issue = issue_for_project(sb, rec)
    if issue is None or issue.get("state") == _split().ISSUE_RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no splitter failure to resolve on this project.")
    now = _now_iso()
    rows = (
        sb.table(TABLE)
        .update({"split_resolution": _split().RESOLUTION_OUTSIDE, "split_resolved_by": user.id,
                 "split_resolved_at": now})
        .eq("project_id", project_id)
        .is_("split_resolution", "null")
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_409_CONFLICT, "This project is already marked as split outside the app.")
    # An interrupted run can leave a job reading "processing" forever; the
    # person is done with the splitter here, so settle it now.
    _split().clear_interrupted(sb, rows[0])
    audit(user.id, "rfp_created.split_outside", AUDIT_ENTITY, project_id,
          {"state": issue.get("state"), "job_id": issue.get("job_id")})
    return {"split": issue_for_project(sb, rows[0])}


@router.delete("/{project_id}/split/outside", dependencies=[Depends(rfp_created_rate_limit)])
def undo_rfp_created_split_outside(project_id: str, user: CurrentUser = Depends(require_page)) -> dict:
    """Undo "split outside the app": the banner and its two actions come
    back. 409 when it is not marked. Audited
    `rfp_created.split_outside_undo`."""
    _uuid_or_404(project_id, _NOT_FOUND)
    sb = get_supabase()
    _record_or_404(sb, project_id)
    rows = (
        sb.table(TABLE)
        .update({"split_resolution": None, "split_resolved_by": None, "split_resolved_at": None})
        .eq("project_id", project_id)
        .eq("split_resolution", _split().RESOLUTION_OUTSIDE)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_409_CONFLICT, "This project is not marked as split outside the app.")
    audit(user.id, "rfp_created.split_outside_undo", AUDIT_ENTITY, project_id, None)
    return {"split": issue_for_project(sb, rows[0])}

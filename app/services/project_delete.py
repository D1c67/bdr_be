"""Delete a project, recoverably (docs/PROJECT_DELETE.md).

RFP ingestion creates bidding projects on its own and some of them should
not exist. A person with an admin role deletes one; the database keeps a full
record (migration 0141, table `deleted_projects`) so an IT Admin can put it
back exactly as it was.

The work happens in two database functions, each one transaction:

- `archive_and_delete_project` re-checks the reason, the note and the typed
  name under a row lock, refuses a project enrolled in Project Management or
  Certified Payroll, walks the foreign-key graph from `pg_constraint`,
  snapshots every row the delete removes and every outside column it sets to
  null, marks the RFP source so it never creates the project again, and
  deletes.
- `restore_deleted_project` re-inserts the snapshot in foreign-key order,
  relinks the SET NULL columns that are still null and lifts the RFP block.

Errors come back as `project_delete:<code>` (with the details in the error's
`details`); `ProjectDeleteError` carries the readable sentence and the HTTP
status the routers answer with.

This module also owns the read side the RFP pipeline needs:
`archive_ref` (what the email and invitation screens show about a deleted
project) and `tombstone_for_email` (the create-time check that keeps an
email the matcher would have linked to a deleted project from creating it
again, docs/PROJECT_DELETE.md 5).

Event-loop rule: everything here is sync (the Supabase SDK is sync); the
routers call it from plain `def` handlers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from postgrest.exceptions import APIError

from app.core.config import Settings
from app.services.notifications import audit

logger = logging.getLogger(__name__)

TABLE = "deleted_projects"

REASON_RFP = "rfp_should_not_exist"
REASON_DUPLICATE = "duplicate"
REASON_MISTAKE = "created_by_mistake"
REASON_OTHER = "other"
REASONS = (REASON_RFP, REASON_DUPLICATE, REASON_MISTAKE, REASON_OTHER)

NOTE_MAX_CHARS = 2000
CONFIRM_MAX_CHARS = 500

# The flag a source row carries when the create step stopped it because a
# project made from it (or from a copy, a shared harvest, or the bid the
# matcher would have linked it to) was deleted.
FLAG_PROJECT_DELETED = "project_deleted"

AUDIT_DELETE = "project.delete"
AUDIT_RESTORE = "project.restore"
AUDIT_RESTORE_FAILED = "project.restore_failed"

BLOCK_PM = "pm_enrolled"
BLOCK_CP = "cp_enrolled"

# The modal's summary buckets: {key: table}. Everything else is "other".
COUNT_TABLES = {
    "rfqs": "rfqs",
    "vendor_emails": "rfq_sends",
    "quotes": "quotes",
    "files": "project_files",
    "gcs": "project_gcs",
}

LIST_SELECT = (
    "id, project_id, project_number, project_name, project_stage, gc_names, reason, note, "
    "from_rfp, rfp_source_kind, rfp_source_id, rfp_source_label, deleted_by, deleted_at, "
    "row_counts, row_total, restored_at, restored_by, restore_error"
)
REF_SELECT = (
    "id, project_id, project_number, project_name, reason, note, deleted_by, deleted_at, "
    "restored_at"
)
# The tombstone check's read: light columns only (never the snapshot).
_TOMBSTONE_SELECT = "id, project_id, project_row, gc_ids, gc_links, test_session_id"
_TOMBSTONE_CAP = 200
_IN_CHUNK = 200

_MSG_PM = (
    "This project is in Project Management. It holds billing and field records, so it "
    "cannot be deleted."
)
_MSG_CP = (
    "This project is enrolled in Certified Payroll. It holds payroll compliance records, "
    "so it cannot be deleted."
)

# code -> (sentence, HTTP status). `{detail}` is the database's detail text.
MESSAGES: dict[str, tuple[str, int]] = {
    "not_found": ("Project not found.", 404),
    "archive_not_found": ("Deleted project record not found.", 404),
    "name_mismatch": (
        "The name you typed does not match the project. Type it exactly as shown.", 422,
    ),
    "bad_reason": ("Pick a reason for deleting this project.", 422),
    "note_required": ("Add a note: it is required when the reason is Other.", 422),
    "note_too_long": (f"The note is too long ({NOTE_MAX_CHARS} characters at most).", 422),
    "not_from_rfp": (
        "This project was not created by RFP ingestion, so that reason does not apply. "
        "Pick another one.", 422,
    ),
    BLOCK_PM: (_MSG_PM, 409),
    BLOCK_CP: (_MSG_CP, 409),
    "referenced": (
        "Other records still point at this project, so it cannot be deleted ({detail}).", 409,
    ),
    "already_restored": ("This project was already restored.", 409),
    "project_exists": ("This project exists again, so there is nothing to restore.", 409),
    "number_taken": (
        "The project number is now used by another project ({detail}), so this one cannot "
        "be restored.", 409,
    ),
    "table_missing": (
        "This project cannot be restored: a table it had rows in no longer exists ({detail}).",
        409,
    ),
    "missing_reference": (
        "This project cannot be restored: a record it depends on was removed since ({detail}).",
        409,
    ),
    "restore_conflict": (
        "This project cannot be restored: its records conflict with ones created since "
        "({detail}).", 409,
    ),
}
_GENERIC = ("The project could not be deleted or restored ({code}).", 409)


class ProjectDeleteError(Exception):
    """A user-facing failure: `.args[0]` is the sentence, `status_code` the
    HTTP status, `code` the machine code (also the X-Error-Code header)."""

    def __init__(self, message: str, status_code: int = 409, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def confirm_text(project: dict) -> tuple[str, bool]:
    """What the person must type to delete: the trimmed name, or the trimmed
    number when the project has no name. Returns (text, uses_number). The
    database applies the same rule (btrim, exact, case-sensitive)."""
    name = (project.get("name") or "").strip()
    if name:
        return name, False
    return (project.get("number") or "").strip(), True


def block_for(project: dict) -> dict | None:
    """`{code, message}` when the project cannot be deleted (enrolled in PM
    or Certified Payroll), else None."""
    if project.get("pm_stage") is not None:
        return {"code": BLOCK_PM, "message": _MSG_PM}
    if project.get("cp_enrolled_at") is not None:
        return {"code": BLOCK_CP, "message": _MSG_CP}
    return None


def reasons_for(from_rfp: bool) -> list[str]:
    """The reasons offered: `rfp_should_not_exist` only for an RFP-created project."""
    return list(REASONS) if from_rfp else [r for r in REASONS if r != REASON_RFP]


def summarize_counts(counts: dict | None) -> tuple[dict[str, int], int]:
    """({rfqs, vendor_emails, quotes, files, gcs, other}, total) from the
    per-table counts the database returns. `other` is every row outside the
    named buckets, the project row itself excluded."""
    counts = {str(k): int(v or 0) for k, v in (counts or {}).items()}
    total = sum(counts.values())
    out = {key: counts.get(table, 0) for key, table in COUNT_TABLES.items()}
    named = sum(out.values()) + counts.get("projects", 0)
    out["other"] = max(total - named, 0)
    return out, total


def validate_request(reason: str | None, note: str | None, confirm_name: str | None,
                     *, from_rfp: bool | None = None) -> str | None:
    """The request checks the database repeats: a known reason, a note for
    `other` (and never over the cap), a confirmation typed at all, and the
    RFP reason only on an RFP-created project (when `from_rfp` is known).
    Returns the cleaned note; raises ProjectDeleteError (422)."""
    if reason not in REASONS:
        raise _error("bad_reason")
    cleaned = (note or "").strip() or None
    if reason == REASON_OTHER and not cleaned:
        raise _error("note_required")
    if cleaned and len(cleaned) > NOTE_MAX_CHARS:
        raise _error("note_too_long")
    if not (confirm_name or "").strip():
        raise _error("name_mismatch")
    if from_rfp is False and reason == REASON_RFP:
        raise _error("not_from_rfp")
    return cleaned


def _error(code: str, detail: str | None = None) -> ProjectDeleteError:
    sentence, status_code = MESSAGES.get(code, _GENERIC)
    detail_text = (detail or "").strip() or "no detail"
    return ProjectDeleteError(
        sentence.format(detail=detail_text, code=code), status_code, code
    )


def error_code_of(exc: Exception) -> str | None:
    """The `project_delete:<code>` a database function raised, if any."""
    text = getattr(exc, "message", None) or str(exc)
    marker = "project_delete:"
    if not isinstance(text, str) or marker not in text:
        return None
    return text.split(marker, 1)[1].split()[0].strip("'\",.}:;")


def _db_error(exc: Exception) -> ProjectDeleteError:
    """Map an APIError from one of the functions; anything else re-raises."""
    code = error_code_of(exc)
    if code is None:
        raise exc
    detail = getattr(exc, "details", None)
    return _error(code, detail if isinstance(detail, str) else None)


def _person(profiles: dict[str, dict], user_id) -> dict | None:
    if not user_id:
        return None
    return {"id": str(user_id), "full_name": (profiles.get(str(user_id)) or {}).get("full_name")}


def _profiles(sb, ids) -> dict[str, dict]:
    clean = sorted({str(i) for i in ids if i})
    out: dict[str, dict] = {}
    for chunk in _chunks(clean):
        for row in sb.table("profiles").select("id, full_name").in_("id", chunk).execute().data or []:
            out[str(row["id"])] = row
    return out


# ── Reads ────────────────────────────────────────────────────────────────────


_PROJECT_SELECT = "id, number, name, pm_stage, cp_enrolled_at"


def _project(sb, project_id: str) -> dict | None:
    rows = (
        sb.table("projects").select(_PROJECT_SELECT).eq("id", project_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def from_rfp(sb, project_id: str) -> bool:
    rows = (
        sb.table("rfp_created_projects").select("project_id").eq("project_id", project_id)
        .limit(1).execute()
    ).data or []
    return bool(rows)


def delete_info(sb, project_id: str) -> dict:
    """What the Delete modal needs (docs/PROJECT_DELETE.md 6): the text to
    type, whether the project came from RFP ingestion, why it cannot be
    deleted (if so), and what a delete would remove. 404 as
    ProjectDeleteError when the project does not exist."""
    project = _project(sb, project_id)
    if project is None:
        raise _error("not_found")
    text, uses_number = confirm_text(project)
    rfp = from_rfp(sb, project_id)
    blocked = block_for(project)
    counts: dict[str, int] = {key: 0 for key in (*COUNT_TABLES, "other")}
    total = 0
    if blocked is None:
        try:
            preview = sb.rpc("project_delete_preview", {"p_project_id": project_id}).execute().data
        except APIError as exc:
            raise _db_error(exc) from exc
        counts, total = summarize_counts((preview or {}).get("counts"))
        blockers = (preview or {}).get("blockers") or []
        if blockers:
            detail = ", ".join(f"{b.get('table')} ({b.get('rows')})" for b in blockers)
            err = _error("referenced", detail)
            blocked = {"code": "referenced", "message": str(err)}
    return {
        "project_id": project["id"],
        "number": project.get("number"),
        "name": project.get("name"),
        "confirm_text": text,
        "confirm_uses_number": uses_number,
        "from_rfp": rfp,
        "blocked": blocked,
        "counts": counts,
        "total_rows": total,
        "reasons": reasons_for(rfp),
    }


# ── Delete ───────────────────────────────────────────────────────────────────


def delete_project(
    sb, project_id: str, *, actor_id: str, reason: str | None, note: str | None,
    confirm_name: str | None,
) -> dict:
    """Archive and delete, one transaction in the database. Raises
    ProjectDeleteError (404, 409 or 422 with a sentence). Audited
    `project.delete` with the reason, the note and the archive id. Nothing
    is emailed and no bell is rung: the delete is silent by design."""
    project = _project(sb, project_id)
    if project is None:
        raise _error("not_found")
    blocked = block_for(project)
    if blocked is not None:
        raise ProjectDeleteError(blocked["message"], 409, blocked["code"])
    cleaned = validate_request(reason, note, confirm_name)
    try:
        result = sb.rpc(
            "archive_and_delete_project",
            {
                "p_project_id": project_id,
                "p_actor": actor_id,
                "p_reason": reason,
                "p_note": cleaned,
                "p_confirm_name": (confirm_name or "")[:CONFIRM_MAX_CHARS],
            },
        ).execute().data
    except APIError as exc:
        raise _db_error(exc) from exc
    result = result or {}
    try:
        audit(actor_id, AUDIT_DELETE, "project", project_id, {
            "archive_id": result.get("archive_id"),
            "reason": reason,
            "note": cleaned,
            "number": result.get("number"),
            "name": result.get("name"),
            "from_rfp": bool(result.get("from_rfp")),
            "row_total": result.get("row_total"),
        })
    except Exception:  # noqa: BLE001 - the delete stands; the archive row is the record
        logger.exception("project delete: audit failed for %s", project_id)
    return {
        "archive_id": result.get("archive_id"),
        "project_id": result.get("project_id") or project_id,
        "number": result.get("number"),
        "name": result.get("name"),
        "from_rfp": bool(result.get("from_rfp")),
        "reason": result.get("reason") or reason,
        "deleted_at": result.get("deleted_at"),
    }


# ── The Deleted Projects page ────────────────────────────────────────────────


def _item(row: dict, profiles: dict[str, dict]) -> dict:
    counts, _ = summarize_counts(row.get("row_counts"))
    source = None
    if row.get("from_rfp") and row.get("rfp_source_id"):
        source = {
            "kind": row.get("rfp_source_kind"),
            "id": row.get("rfp_source_id"),
            "label": row.get("rfp_source_label"),
        }
    return {
        "id": row["id"],
        "project_id": row.get("project_id"),
        "project_number": row.get("project_number"),
        "project_name": row.get("project_name"),
        "gc_names": list(row.get("gc_names") or []),
        "stage": row.get("project_stage"),
        "reason": row.get("reason"),
        "note": row.get("note"),
        "from_rfp": bool(row.get("from_rfp")),
        "rfp_source": source,
        "deleted_at": row.get("deleted_at"),
        "deleted_by": _person(profiles, row.get("deleted_by")),
        "row_total": int(row.get("row_total") or 0),
        "counts": counts,
        "restored_at": row.get("restored_at"),
        "restored_by": _person(profiles, row.get("restored_by")),
        "restore_error": row.get("restore_error"),
    }


def list_deleted(
    sb, *, limit: int, before: str | None = None, before_id: str | None = None,
    reason: str | None = None, restored: str = "false",
) -> list[dict]:
    """Newest first (`deleted_at desc, id desc`), paged on the last row's
    pair like /rfp-created. `restored`: `false` (default) the ones still
    deleted, `true` the restored ones, `all` both. `reason` narrows to one
    code. One profiles read for the page."""
    query = sb.table(TABLE).select(LIST_SELECT)
    if restored == "false":
        query = query.is_("restored_at", "null")
    elif restored == "true":
        query = query.not_.is_("restored_at", "null")
    if reason:
        query = query.eq("reason", reason)
    if before and before_id:
        query = query.or_(f"deleted_at.lt.{before},and(deleted_at.eq.{before},id.lt.{before_id})")
    elif before:
        query = query.lt("deleted_at", before)
    rows = (
        query.order("deleted_at", desc=True).order("id", desc=True).limit(limit).execute()
    ).data or []
    people = {r.get("deleted_by") for r in rows} | {r.get("restored_by") for r in rows}
    profiles = _profiles(sb, people)
    return [_item(r, profiles) for r in rows]


def summary(sb) -> dict:
    """Exact counts for the page header and the "how often was RFP ingestion
    wrong" question: total, still deleted, restored, and per reason (every
    deletion ever, restored or not)."""

    def count(apply) -> int:
        res = apply(sb.table(TABLE).select("id", count="exact", head=True)).execute()
        return int(res.count if res.count is not None else len(res.data or []))

    total = count(lambda q: q)
    not_restored = count(lambda q: q.is_("restored_at", "null"))
    by_reason = {code: count(lambda q, c=code: q.eq("reason", c)) for code in REASONS}
    return {
        "total": total,
        "not_restored": not_restored,
        "restored": max(total - not_restored, 0),
        "by_reason": by_reason,
    }


# ── Restore ──────────────────────────────────────────────────────────────────


def restore(sb, archive_id: str, *, actor_id: str) -> dict:
    """Put the project back, one transaction in the database. A failure is
    recorded on the archive (`restore_error`, best effort) and audited
    `project.restore_failed`, then raised as ProjectDeleteError; a success
    is audited `project.restore` with the counts."""
    try:
        result = sb.rpc(
            "restore_deleted_project", {"p_archive_id": archive_id, "p_actor": actor_id}
        ).execute().data
    except APIError as exc:
        err = _db_error(exc)
        if err.code not in ("archive_not_found", "already_restored"):
            try:
                sb.table(TABLE).update({"restore_error": str(err)[:1000]}).eq("id", archive_id).execute()
            except Exception:  # noqa: BLE001 - the error still reaches the person
                logger.exception("project restore: could not record the failure on %s", archive_id)
            try:
                audit(actor_id, AUDIT_RESTORE_FAILED, "deleted_project", archive_id,
                      {"code": err.code, "error": str(err)[:500]})
            except Exception:  # noqa: BLE001
                logger.exception("project restore: audit failed for %s", archive_id)
        raise err from exc
    result = result or {}
    try:
        audit(actor_id, AUDIT_RESTORE, "project", result.get("project_id"), {
            "archive_id": archive_id,
            "number": result.get("number"),
            "rows_restored": result.get("rows_restored"),
            "links_relinked": result.get("links_relinked"),
            "links_skipped": result.get("links_skipped"),
            "references_cleared": result.get("references_cleared") or [],
        })
    except Exception:  # noqa: BLE001
        logger.exception("project restore: audit failed for %s", archive_id)
    if result.get("project_id"):
        # Jobs queued or running at delete time come back with the snapshot;
        # their worker is gone, so fail them instead of letting them rerun.
        try:
            from app.services import llm_queue

            llm_queue.release_restored_project_jobs(str(result["project_id"]))
        except Exception:  # noqa: BLE001 - the restore itself already succeeded
            logger.exception("project restore: could not release jobs for %s", archive_id)
    profiles = _profiles(sb, [actor_id])
    return {
        "archive_id": result.get("archive_id") or archive_id,
        "project_id": result.get("project_id"),
        "number": result.get("number"),
        "name": result.get("name"),
        "restored_at": result.get("restored_at"),
        "restored_by": _person(profiles, actor_id),
        "rows_restored": int(result.get("rows_restored") or 0),
        "links_relinked": int(result.get("links_relinked") or 0),
        "links_skipped": int(result.get("links_skipped") or 0),
    }


# ── What the RFP screens show ────────────────────────────────────────────────


def archive_refs(sb, archive_ids) -> dict[str, dict]:
    """`{archive_id: ref}` for the ids given (see archive_ref), one read per
    table; an unknown id is simply absent."""
    clean = sorted({str(i) for i in archive_ids if i})
    rows: list[dict] = []
    for chunk in _chunks(clean):
        rows.extend(sb.table(TABLE).select(REF_SELECT).in_("id", chunk).execute().data or [])
    profiles = _profiles(sb, {r.get("deleted_by") for r in rows})
    return {
        str(r["id"]): {
            "archive_id": r["id"],
            "project_id": r.get("project_id"),
            "number": r.get("project_number"),
            "name": r.get("project_name"),
            "deleted_at": r.get("deleted_at"),
            "deleted_by": _person(profiles, r.get("deleted_by")),
            "reason": r.get("reason"),
            "note": r.get("note"),
            "restored_at": r.get("restored_at"),
        }
        for r in rows
    }


def archive_ref(sb, archive_id: str | None) -> dict | None:
    """`{archive_id, project_id, number, name, deleted_at, deleted_by
    {id, full_name}, reason, note, restored_at}` for a source row's
    `create_blocked_archive_id`, or None. Never raises: the screens it feeds
    must render without it."""
    if not archive_id:
        return None
    try:
        return archive_refs(sb, [archive_id]).get(str(archive_id))
    except Exception:  # noqa: BLE001
        logger.exception("project delete: archive lookup failed for %s", archive_id)
        return None


def blocked_sentence(ref: dict | None) -> str:
    """The refusal a blocked source gets (the Create project button's 409)."""
    if not ref:
        return (
            "A project made from this invitation was deleted, so it will not be created again. "
            "An IT Admin can restore it from Deleted Projects."
        )
    label = " ".join(p for p in (ref.get("number"), ref.get("name")) if p) or "The project"
    return (
        f"{label} was made from this invitation and then deleted, so it will not be created "
        "again. An IT Admin can restore it from Deleted Projects."
    )


# ── The create-time tombstone check (docs/PROJECT_DELETE.md 5.3) ─────────────


def tombstone_for_email(sb, row: dict, settings: Settings) -> str | None:
    """The archive id of a deleted, not restored project this email row is
    confidently about, or None. The matcher never sees a deleted project, so
    a later invitation for the same bid (a different sender, a reminder with
    no shared harvest) would otherwise walk straight to `create` and make the
    project again.

    Candidates are archives whose bid date is inside the matcher's candidate
    window (RFP_MATCH_CANDIDATE_WINDOW_DAYS), in the row's test-bench scope,
    and not in the row's `excluded_project_ids`. Each is scored with the
    matcher's own deterministic scorer (`rfp_match.score_candidate`) and must
    pass the deterministic half of its confidence rule (the total, the name
    floor, no discriminator conflict, a date score of 1.0 when the email has
    a date). The LLM half is replaced by agreement a person can see: the
    email has a bid date (and it matched exactly), or the email's resolved
    GC is one of the deleted project's GCs. A row a reviewer marked "Not a
    match" is never inferred onto a deleted project."""
    from app.services import rfp_match

    if not rfp_match.has_project_name(row.get("extracted_project_name")):
        return None
    if row.get("match_review_decision") == "no_match":
        # A person looked at this email's candidates and said it is not an
        # existing project: an inference never overrides that.
        return None
    lo = _now() - timedelta(days=int(settings.rfp_match_candidate_window_days))
    try:
        archives = (
            sb.table(TABLE)
            .select(_TOMBSTONE_SELECT)
            .is_("restored_at", "null")
            .gte("project_bid_at", lo.isoformat())
            .order("deleted_at", desc=True)
            .limit(_TOMBSTONE_CAP)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001 - never block creation on a failed read of the archive
        logger.exception("project delete: tombstone read failed for %s", row.get("id"))
        return None
    excluded = {str(x) for x in (row.get("excluded_project_ids") or [])}
    session = str(row.get("test_session_id") or "") or None
    has_date = row.get("extracted_bid_due_at") is not None
    gc_id = str(row.get("resolved_gc_id") or "") or None
    for archive in archives:
        if str(archive.get("project_id")) in excluded:
            continue
        if (str(archive.get("test_session_id") or "") or None) != session:
            continue
        project: dict[str, Any] = dict(archive.get("project_row") or {})
        project["id"] = archive.get("project_id")
        project["project_gcs"] = archive.get("gc_links") or []
        breakdown = rfp_match.score_candidate(row, project, settings)
        entry = {"breakdown": breakdown, "verdict": rfp_match.VERDICT_SAME, "confidence": 1.0}
        if not rfp_match.is_confident(entry, has_date=has_date, settings=settings):
            continue
        same_gc = bool(gc_id) and gc_id in {str(g) for g in (archive.get("gc_ids") or [])}
        if has_date or same_gc:
            return str(archive["id"])
    return None

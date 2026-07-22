"""PM field operations — milestones, daily logs, RFIs, and manpower entries
(migration 0060). Reads are any PM-read role (accountant included, the external
estimator never); writes are PM-write roles. Every endpoint runs through
require_pm_project first, and every row lookup is scoped to the project, so an
id from another project is indistinguishable from a missing one.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.ratelimit import outbound_email_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    RFI_QUESTION_MAX_CHARS,
    DailyLogIn,
    DailyLogUpdate,
    ManpowerIn,
    ManpowerUpdate,
    MilestoneIn,
    MilestoneUpdate,
    RFIClose,
    RFIIn,
    RFIMarkSentIn,
    RFISendIn,
    RFIUpdate,
)
from app.services import pm_folders, rfi_email, rfi_pdf, storage
from app.services.notifications import audit
from app.services.office_preview import ConversionError
from app.services.pm import require_pm_project
from app.services.sanitize import has_text_content, sanitize_rich_text

router = APIRouter(prefix="/pm/projects/{project_id}", tags=["pm-field"])


def _row_or_404(table: str, row_id: str, project_id: str, label: str) -> dict:
    rows = (
        get_supabase()
        .table(table)
        .select("*")
        .eq("id", row_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{label} not found")
    return rows[0]


def _patch_of(body) -> dict:
    # exclude_unset (not exclude_none) so an explicit null clears a field.
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    return patch


def _reject_cleared(patch: dict, *fields: str) -> None:
    """NOT NULL columns: an explicit null would surface as a raw DB error."""
    cleared = sorted(f for f in fields if f in patch and patch[f] is None)
    if cleared:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{', '.join(cleared)} cannot be cleared"
        )


def _today() -> str:
    # G3 operates in Las Vegas: an RFI answered at 6pm local must not be
    # stamped with tomorrow's (UTC) date. The FE stamps local dates the same way.
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _now_iso() -> str:
    # Wall-clock instant of a send (timestamptz). LA-anchored for parity with the
    # date stamps above; Postgres normalizes to UTC either way.
    return datetime.now(ZoneInfo("America/Los_Angeles")).isoformat()


# ── Milestones ───────────────────────────────────────────────────────────────


@router.get("/milestones")
def list_milestones(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    rows = (
        get_supabase()
        .table("pm_milestones")
        .select("*")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    # sort_order, then planned_date with undated milestones last.
    rows.sort(
        key=lambda r: (
            r.get("sort_order") or 0,
            r.get("planned_date") is None,
            r.get("planned_date") or "",
        )
    )
    return rows


@router.post("/milestones", status_code=status.HTTP_201_CREATED)
def create_milestone(
    project_id: str,
    body: MilestoneIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    payload.update({"project_id": project_id, "created_by": user.id})
    created = get_supabase().table("pm_milestones").insert(payload).execute().data[0]
    audit(user.id, "milestone.create", "project", project_id,
          {"milestone_id": created.get("id"), "name": body.name})
    return created


@router.patch("/milestones/{milestone_id}")
def update_milestone(
    project_id: str,
    milestone_id: str,
    body: MilestoneUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    # Existence is guarded by the scoped UPDATE's empty result (404 below).
    patch = _patch_of(body)
    _reject_cleared(patch, "name", "sort_order")
    updated = (
        get_supabase()
        .table("pm_milestones")
        .update(patch)
        .eq("id", milestone_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Milestone not found")
    audit(user.id, "milestone.update", "project", project_id,
          {"milestone_id": milestone_id, "fields": sorted(patch)})
    return updated[0]


@router.delete("/milestones/{milestone_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_milestone(
    project_id: str,
    milestone_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    row = _row_or_404("pm_milestones", milestone_id, project_id, "Milestone")
    get_supabase().table("pm_milestones").delete().eq("id", milestone_id).eq(
        "project_id", project_id
    ).execute()
    audit(user.id, "milestone.delete", "project", project_id,
          {"milestone_id": milestone_id, "name": row.get("name")})


# ── Daily logs ───────────────────────────────────────────────────────────────


@router.get("/daily-logs")
def list_daily_logs(
    project_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    _: CurrentUser = Depends(require_pm_read),
):
    require_pm_project(project_id)
    query = get_supabase().table("daily_logs").select("*").eq("project_id", project_id)
    if date_from is not None:
        query = query.gte("log_date", date_from.isoformat())
    if date_to is not None:
        query = query.lte("log_date", date_to.isoformat())
    return (
        query.order("log_date", desc=True).order("created_at", desc=True).execute()
    ).data or []


@router.post("/daily-logs", status_code=status.HTTP_201_CREATED)
def create_daily_log(
    project_id: str,
    body: DailyLogIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    payload.update({"project_id": project_id, "created_by": user.id})
    created = get_supabase().table("daily_logs").insert(payload).execute().data[0]
    audit(user.id, "dailylog.create", "project", project_id,
          {"daily_log_id": created.get("id"), "log_date": payload["log_date"]})
    return created


@router.patch("/daily-logs/{log_id}")
def update_daily_log(
    project_id: str,
    log_id: str,
    body: DailyLogUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    # Existence is guarded by the scoped UPDATE's empty result (404 below).
    patch = _patch_of(body)
    _reject_cleared(patch, "log_date", "work_performed")
    updated = (
        get_supabase()
        .table("daily_logs")
        .update(patch)
        .eq("id", log_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Daily log not found")
    audit(user.id, "dailylog.update", "project", project_id,
          {"daily_log_id": log_id, "fields": sorted(patch)})
    return updated[0]


@router.delete("/daily-logs/{log_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_daily_log(
    project_id: str,
    log_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    row = _row_or_404("daily_logs", log_id, project_id, "Daily log")
    # Linked manpower_entries survive (FK is ON DELETE SET NULL).
    get_supabase().table("daily_logs").delete().eq("id", log_id).eq(
        "project_id", project_id
    ).execute()
    audit(user.id, "dailylog.delete", "project", project_id,
          {"daily_log_id": log_id, "log_date": row.get("log_date")})


# ── RFIs ─────────────────────────────────────────────────────────────────────

_RFI_NUMBER_CONFLICT = "RFI numbering conflicted with a concurrent save — please retry"

# "the client didn't send attachment_keys at all", which None can't say: an
# explicit null is a request to detach everything.
_ABSENT = object()


def _is_rfi_number_conflict(exc: Exception) -> bool:
    msg = str(exc)
    return "rfis_project_id_rfi_number_key" in msg or (
        "23505" in msg and "rfi_number" in msg
    )


def _next_rfi_number(project_id: str) -> int:
    rows = (
        get_supabase()
        .table("rfis")
        .select("rfi_number")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    return max((r["rfi_number"] for r in rows if r.get("rfi_number") is not None), default=0) + 1


def _clean_question(raw: str) -> str:
    """Sanitize the rich-text question on WRITE — see services/sanitize.py; the
    frontend renders this straight into the DOM.

    Both checks run on the *sanitized* value: `<p></p>` passes a min_length on
    the raw HTML while rendering to nothing, and a Word paste's stripped markup
    shouldn't count against a limit the author has no way to see.
    """
    clean = sanitize_rich_text(raw)
    if not has_text_content(clean):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Question cannot be empty")
    if len(clean) > RFI_QUESTION_MAX_CHARS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Question must be {RFI_QUESTION_MAX_CHARS} characters or fewer",
        )
    return clean


def _validate_assignee(gc_id: str | None, contact_id: str | None) -> None:
    """A contact must belong to the assigned company. The FK pair cannot express
    that (see 0068), so it is enforced here — otherwise an RFI could name
    company A while addressing a contact who works for B.

    Callers pass the RESULTING pair, not the patch: changing only the company on
    an RFI that already has a contact has to be re-checked.
    """
    if not contact_id:
        return  # no contact → nothing to reconcile; a bare company is fine
    rows = (
        get_supabase()
        .table("gc_contacts")
        .select("id, gc_id")
        .eq("id", contact_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That contact was not found")
    contact_gc = rows[0].get("gc_id")
    if not contact_gc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That contact is not linked to a company"
        )
    if contact_gc != gc_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That contact belongs to a different company"
        )


def _hub_docs(project_id: str) -> dict[str, dict]:
    """The project's Documents-hub listing keyed by handle. Attachments are a
    soft reference into this listing (0068), so it is both the validator and the
    resolver."""
    return {d["key"]: d for d in pm_folders.list_project_documents(project_id)}


def _validate_attachment_keys(keys: list[str], hub: dict[str, dict]) -> None:
    """Unknown or not-visible keys are rejected, never dropped: silently losing
    an attachment the user picked is worse than a 400. Because the hub listing
    is already project-scoped and already excludes unsent estimator drafts, this
    is what stops an RFI from attaching another project's file or a draft the
    team hasn't received.
    """
    unknown = [k for k in keys if k not in hub]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{len(unknown)} attachment(s) are not in this project's documents",
        )


def _sync_attachments(
    rfi_id: str, keys: list[str], actor_id: str, kind: str = "question"
) -> tuple[list[str], list[str]]:
    """Replace one *kind* of an RFI's attachment set with `keys`, as a diff.

    Scoped to `kind` so editing the request's exhibits never disturbs the answer
    documents captured at close, and vice versa. Rows that survive the edit are
    left alone: re-writing them would churn created_by/created_at (losing who
    attached what, and when) for no reason. Deletes by id — precise, and it means
    the other kind is untouched even if the same document is attached as both.
    Returns (added, removed) for the audit trail.
    """
    existing = (
        get_supabase()
        .table("rfi_attachments")
        .select("id, doc_key, kind")
        .eq("rfi_id", rfi_id)
        .execute()
    ).data or []
    # A row predating 0070 has no kind → it is a request attachment.
    same_kind = [r for r in existing if (r.get("kind") or "question") == kind]
    have = {r["doc_key"] for r in same_kind}
    added = [k for k in keys if k not in have]  # keys are already de-duped
    removed = sorted(have - set(keys))
    if added:
        get_supabase().table("rfi_attachments").insert(
            [
                {"rfi_id": rfi_id, "doc_key": k, "created_by": actor_id, "kind": kind}
                for k in added
            ]
        ).execute()
    if removed:
        remove_ids = [r["id"] for r in same_kind if r["doc_key"] in set(removed)]
        get_supabase().table("rfi_attachments").delete().eq("rfi_id", rfi_id).in_(
            "id", remove_ids
        ).execute()
    return added, removed


def _attachment_view(row: dict, doc: dict) -> dict:
    # Field-by-field on purpose: hub rows carry `storage_path`, which must never
    # reach a client (see pm_folders.list_project_documents).
    return {
        "id": row.get("id"),
        "key": row["doc_key"],
        "filename": doc.get("filename"),
        "folder": doc.get("folder"),
        "source": doc.get("source"),
        "size_bytes": doc.get("size_bytes"),
    }


def _names(table: str, ids: set[str], col: str) -> dict[str, str | None]:
    if not ids:
        return {}
    rows = (
        get_supabase().table(table).select(f"id, {col}").in_("id", list(ids)).execute()
    ).data or []
    return {r["id"]: r.get(col) for r in rows}


def _latest_sends(rfi_ids: list[str]) -> dict[str, dict]:
    """The most recent send per RFI (for the detail view's delivery line). One
    bulk, sent_at-descending query; the first row seen per rfi_id is the latest.
    The full history stays in rfi_sends — this only surfaces the last event."""
    if not rfi_ids:
        return {}
    rows = (
        get_supabase()
        .table("rfi_sends")
        .select("rfi_id, method, message, recipients, sent_at, sent_by")
        .in_("rfi_id", rfi_ids)
        .order("sent_at", desc=True)
        .execute()
    ).data or []
    sender_names = _names(
        "profiles", {r["sent_by"] for r in rows if r.get("sent_by")}, "full_name"
    )
    latest: dict[str, dict] = {}
    for r in rows:
        rid = r["rfi_id"]
        if rid in latest:
            continue  # rows are newest-first, so the first per rfi wins
        latest[rid] = {
            "method": r.get("method"),
            "message": r.get("message"),
            "recipients": r.get("recipients") or [],
            "sent_at": r.get("sent_at"),
            "sent_by_name": sender_names.get(r.get("sent_by")),
        }
    return latest


def _enrich_rfis(
    project_id: str, rows: list[dict], hub: dict[str, dict] | None = None
) -> list[dict]:
    """Attach the display-side of each RFI: resolved attachments (split into the
    request's exhibits and the answer documents) and the company / contact /
    author names behind the stored ids.

    Bulk by construction — one query per dimension for the whole page, never one
    per row. `hub` lets a writer hand over the listing it already fetched for
    validation instead of paying for it twice.
    """
    if not rows:
        return rows
    rfi_ids = [r["id"] for r in rows if r.get("id")]
    att_rows = (
        (
            get_supabase()
            .table("rfi_attachments")
            .select("id, rfi_id, doc_key, kind")
            .in_("rfi_id", rfi_ids)
            .execute()
        ).data
        or []
        if rfi_ids
        else []
    )
    if att_rows and hub is None:
        hub = _hub_docs(project_id)

    # 'answer' vs 'question' (0070); a row without kind predates it → request.
    by_rfi: dict[str, list[dict]] = {}
    by_rfi_answer: dict[str, list[dict]] = {}
    for a in att_rows:
        doc = (hub or {}).get(a["doc_key"])
        if doc is None:
            continue  # document deleted from the hub → drops out (0068, by design)
        bucket = by_rfi_answer if a.get("kind") == "answer" else by_rfi
        bucket.setdefault(a["rfi_id"], []).append(_attachment_view(a, doc))
    for grouped in (by_rfi, by_rfi_answer):
        for items in grouped.values():
            items.sort(
                key=lambda i: (
                    pm_folders.folder_rank(i["folder"] or ""),
                    (i["filename"] or "").lower(),
                )
            )

    gc_names = _names(
        "general_contractors", {r["assigned_gc_id"] for r in rows if r.get("assigned_gc_id")}, "name"
    )
    contact_names = _names(
        "gc_contacts",
        {r["assigned_contact_id"] for r in rows if r.get("assigned_contact_id")},
        "name",
    )
    creator_names = _names(
        "profiles", {r["created_by"] for r in rows if r.get("created_by")}, "full_name"
    )
    sender_names = _names(
        "profiles", {r["last_sent_by"] for r in rows if r.get("last_sent_by")}, "full_name"
    )
    last_sends = _latest_sends(rfi_ids)

    for r in rows:
        r["attachments"] = by_rfi.get(r.get("id"), [])
        r["answer_attachments"] = by_rfi_answer.get(r.get("id"), [])
        r["assigned_gc_name"] = gc_names.get(r.get("assigned_gc_id"))
        r["assigned_contact_name"] = contact_names.get(r.get("assigned_contact_id"))
        r["created_by_name"] = creator_names.get(r.get("created_by"))
        r["last_sent_by_name"] = sender_names.get(r.get("last_sent_by"))
        r["last_send"] = last_sends.get(r.get("id"))
    return rows


@router.get("/rfis")
def list_rfis(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    rows = (
        get_supabase()
        .table("rfis")
        .select("*")
        .eq("project_id", project_id)
        .order("rfi_number")
        .execute()
    ).data or []
    return _enrich_rfis(project_id, rows)


@router.post("/rfis", status_code=status.HTTP_201_CREATED)
def create_rfi(
    project_id: str,
    body: RFIIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    # Not a column on `rfis` — attachments live in their own table.
    keys: list[str] = payload.pop("attachment_keys", None) or []
    payload["question"] = _clean_question(payload["question"])
    # Everything is validated BEFORE the insert: a bad key must not leave an
    # orphaned RFI behind, nor burn an RFI number.
    _validate_assignee(payload.get("assigned_gc_id"), payload.get("assigned_contact_id"))
    hub = _hub_docs(project_id) if keys else None
    if keys:
        _validate_attachment_keys(keys, hub)
    payload.update({"project_id": project_id, "created_by": user.id})
    # max+1 can race a concurrent create into the (project_id, rfi_number)
    # unique — one recompute absorbs it, a second collision surfaces as a 409.
    created = None
    last_exc: Exception | None = None
    for _ in range(2):
        payload["rfi_number"] = _next_rfi_number(project_id)
        try:
            created = get_supabase().table("rfis").insert(payload).execute().data[0]
            break
        except Exception as exc:  # noqa: BLE001 — unique violation → recompute
            if not _is_rfi_number_conflict(exc):
                raise
            last_exc = exc
    if created is None:
        raise HTTPException(status.HTTP_409_CONFLICT, _RFI_NUMBER_CONFLICT) from last_exc
    if keys:
        get_supabase().table("rfi_attachments").insert(
            [{"rfi_id": created["id"], "doc_key": k, "created_by": user.id} for k in keys]
        ).execute()
    audit(user.id, "rfi.create", "project", project_id,
          {"rfi_id": created.get("id"), "rfi_number": payload["rfi_number"],
           "subject": body.subject, "attached": keys})
    return _enrich_rfis(project_id, [created], hub)[0]


@router.patch("/rfis/{rfi_id}")
def update_rfi(
    project_id: str,
    rfi_id: str,
    body: RFIUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    current = _row_or_404("rfis", rfi_id, project_id, "RFI")
    patch = _patch_of(body)
    _reject_cleared(
        patch, "subject", "question", "status", "priority",
        "drawing_numbers", "applicable_references",
    )
    # Closing is a gated action (POST .../close): it must record the responder,
    # the answer, and a response document, none of which this generic edit can
    # require. Reopening (closed → open/answered) stays a plain status edit.
    if patch.get("status") == "closed":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Close an RFI through the Close action so the responder, answer, and a "
            "response document are recorded",
        )
    # A closed RFI carries the close-gate invariant (closed ⇒ responder + answer
    # recorded). A plain edit that leaves it closed must not null those fields
    # back out — that would produce a closed RFI with no recorded answer. If the
    # same patch reopens it (status → open/answered), the invariant lifts and
    # clearing is allowed.
    if current.get("status") == "closed" and patch.get("status", "closed") == "closed":
        _reject_cleared(patch, "answer", "answered_at", "answered_by")
    # Absent = leave attachments alone; present (even null) = replace the set.
    # _patch_of uses exclude_unset, which is what makes the two distinguishable.
    raw_keys = patch.pop("attachment_keys", _ABSENT)
    replace_attachments = raw_keys is not _ABSENT
    keys: list[str] = list(raw_keys or []) if replace_attachments else []

    if "question" in patch:
        patch["question"] = _clean_question(patch["question"])
    # Validate the RESULTING pair: changing only the company still has to agree
    # with the contact already on the row.
    if "assigned_gc_id" in patch or "assigned_contact_id" in patch:
        _validate_assignee(
            patch.get("assigned_gc_id", current.get("assigned_gc_id")),
            patch.get("assigned_contact_id", current.get("assigned_contact_id")),
        )
    hub = _hub_docs(project_id) if keys else None
    if keys:
        _validate_attachment_keys(keys, hub)

    # Recording an answer on an open RFI marks it answered without a second
    # status edit; an explicit status in the same patch wins.
    if patch.get("answer") and "status" not in patch and current.get("status") == "open":
        patch["status"] = "answered"
        if not patch.get("answered_at"):
            patch["answered_at"] = _today()

    row = current
    if patch:  # an attachments-only edit touches no `rfis` column
        updated = (
            get_supabase()
            .table("rfis")
            .update(patch)
            .eq("id", rfi_id)
            .eq("project_id", project_id)
            .execute()
        ).data
        if not updated:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "RFI not found")
        row = updated[0]

    added: list[str] = []
    removed: list[str] = []
    if replace_attachments:
        added, removed = _sync_attachments(rfi_id, keys, user.id)

    detail = {"rfi_id": rfi_id, "rfi_number": current.get("rfi_number"),
              "fields": sorted(patch)}
    if added:
        detail["attached"] = added
    if removed:
        detail["detached"] = removed
    audit(user.id, "rfi.update", "project", project_id, detail)
    return _enrich_rfis(project_id, [row], hub)[0]


@router.post("/rfis/{rfi_id}/close")
def close_rfi(
    project_id: str,
    rfi_id: str,
    body: RFIClose,
    user: CurrentUser = Depends(require_pm_write),
):
    """Formally close an RFI: record who answered, the answer, and the response
    document(s). The only path to status='closed' (update_rfi refuses it), so the
    terminal state always carries a responder and at least one answer document.
    """
    require_pm_project(project_id)
    current = _row_or_404("rfis", rfi_id, project_id, "RFI")
    if current.get("status") == "closed":
        raise HTTPException(status.HTTP_409_CONFLICT, "This RFI is already closed")

    keys = body.attachment_keys
    hub = _hub_docs(project_id)
    _validate_attachment_keys(keys, hub)  # answer docs, scoped to this project's hub

    patch = {
        "status": "closed",
        "answer": body.answer,
        "answered_by": body.answered_by,
        "answered_at": body.answered_at.isoformat() if body.answered_at else _today(),
    }
    updated = (
        get_supabase()
        .table("rfis")
        .update(patch)
        .eq("id", rfi_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFI not found")

    added, _ = _sync_attachments(rfi_id, keys, user.id, kind="answer")
    detail = {"rfi_id": rfi_id, "rfi_number": current.get("rfi_number")}
    if added:
        detail["answer_attached"] = added
    audit(user.id, "rfi.close", "project", project_id, detail)
    return _enrich_rfis(project_id, [updated[0]], hub)[0]


def _project_row(project_id: str) -> dict:
    rows = (
        get_supabase()
        .table("projects")
        .select("id, name, number")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return rows[0]


def _resolve_recipients(gc_id: str, contact_ids: list[str]) -> list[dict]:
    """Turn the requested contact ids into [{contact_id, name, email}], proving
    each one belongs to the RFI's assigned company and carries an email. A contact
    from another company, a missing id, or a blank email is a clean 400 — never a
    silent drop, and never an email addressed to the wrong company's people."""
    rows = (
        get_supabase()
        .table("gc_contacts")
        .select("id, gc_id, name, email")
        .in_("id", contact_ids)
        .execute()
    ).data or []
    by_id = {r["id"]: r for r in rows}
    recipients: list[dict] = []
    for cid in contact_ids:
        c = by_id.get(cid)
        if not c:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A selected contact was not found")
        if c.get("gc_id") != gc_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "A selected contact belongs to a different company"
            )
        email = (c.get("email") or "").strip()
        if not email:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{c.get('name') or 'A selected contact'} has no email address",
            )
        recipients.append({"contact_id": cid, "name": c.get("name"), "email": email})
    return recipients


@router.post("/rfis/{rfi_id}/send", dependencies=[Depends(outbound_email_rate_limit)])
def send_rfi(
    project_id: str,
    rfi_id: str,
    body: RFISendIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Send the RFI via the app: render it as a PDF (identical to the "View RFI"
    form), email that PDF to the chosen GC contacts, archive the same PDF in the
    project's Documents, and record the send. Nothing is recorded and the RFI is
    not marked sent unless the email actually goes out.
    """
    require_pm_project(project_id)
    current = _row_or_404("rfis", rfi_id, project_id, "RFI")

    gc_id = current.get("assigned_gc_id")
    if not gc_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Assign a company (GC) to this RFI before sending so recipients can be chosen",
        )
    recipients = _resolve_recipients(gc_id, body.contact_ids)
    to_emails = list(dict.fromkeys(r["email"] for r in recipients))

    if not rfi_email.graph_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Email sending is not configured on this environment",
        )

    proj = _project_row(project_id)
    hub = _hub_docs(project_id)
    enriched = _enrich_rfis(project_id, [dict(current)], hub)[0]
    # The RFI's request attachments travel as short-TTL links, not attached bytes.
    links: list[tuple[str, str]] = []
    for att in enriched.get("attachments", []):
        doc = hub.get(att["key"])
        if doc:
            links.append(
                (att.get("filename") or att["key"], rfi_email.signed_link(doc["storage_path"]))
            )

    # Render first (fail-closed): a converter outage must abort before we email or
    # record anything, never send a broken/empty attachment.
    try:
        pdf_bytes = rfi_pdf.render_pdf(proj, enriched)
    except ConversionError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not generate the RFI PDF — please retry in a moment",
        ) from exc
    pdf_name = rfi_pdf.pdf_filename(enriched)

    try:
        rfi_email.send_rfi_email(
            to=to_emails,
            project=proj,
            rfi=enriched,
            message=body.message,
            pdf_name=pdf_name,
            pdf_bytes=pdf_bytes,
            attachment_links=links,
            sent_by=user.id,
        )
    except Exception as exc:  # noqa: BLE001 — graph_email already logged the failure
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Could not send the RFI email — please retry"
        ) from exc

    # Archive the exact PDF that was sent, as the record of what went out.
    pdf_path = storage.build_object_path(project_id, "pm/rfi", pdf_name)
    storage.upload_file(pdf_path, pdf_bytes, "application/pdf")
    pdf_doc = (
        get_supabase()
        .table("pm_documents")
        .insert(
            {
                "project_id": project_id,
                "category": "rfi",
                "storage_path": pdf_path,
                "filename": pdf_name,
                "mime_type": "application/pdf",
                "size_bytes": len(pdf_bytes),
                "note": f"Sent copy of RFI {str(current.get('rfi_number') or '').zfill(3)}",
                "uploaded_by": user.id,
            }
        )
        .execute()
    ).data[0]

    get_supabase().table("rfi_sends").insert(
        {
            "rfi_id": rfi_id,
            "method": "app",
            "message": body.message,
            "recipients": recipients,
            "pdf_doc_id": pdf_doc["id"],
            "sent_by": user.id,
        }
    ).execute()

    patch = {
        "send_status": "sent_app",
        "sent_via": None,
        "last_sent_at": _now_iso(),
        "last_sent_by": user.id,
    }
    if not current.get("sent_at"):
        patch["sent_at"] = _today()  # fill the form's "date sent" only if still blank
    updated = (
        get_supabase()
        .table("rfis")
        .update(patch)
        .eq("id", rfi_id)
        .eq("project_id", project_id)
        .execute()
    ).data[0]

    audit(user.id, "rfi.send", "project", project_id,
          {"rfi_id": rfi_id, "rfi_number": current.get("rfi_number"),
           "method": "app", "recipients": to_emails})
    return _enrich_rfis(project_id, [updated], hub)[0]


@router.post("/rfis/{rfi_id}/mark-sent")
def mark_rfi_sent(
    project_id: str,
    rfi_id: str,
    body: RFIMarkSentIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Record that the RFI was already sent outside BDR (Procore/Autodesk). No
    email is sent; the log's send status simply reflects the external delivery."""
    require_pm_project(project_id)
    current = _row_or_404("rfis", rfi_id, project_id, "RFI")

    get_supabase().table("rfi_sends").insert(
        {
            "rfi_id": rfi_id,
            "method": body.platform,
            "message": None,
            "recipients": [],
            "pdf_doc_id": None,
            "sent_by": user.id,
        }
    ).execute()

    patch = {
        "send_status": "sent_external",
        "sent_via": body.platform,
        "last_sent_at": _now_iso(),
        "last_sent_by": user.id,
    }
    if not current.get("sent_at"):
        patch["sent_at"] = _today()
    updated = (
        get_supabase()
        .table("rfis")
        .update(patch)
        .eq("id", rfi_id)
        .eq("project_id", project_id)
        .execute()
    ).data[0]

    audit(user.id, "rfi.mark_sent", "project", project_id,
          {"rfi_id": rfi_id, "rfi_number": current.get("rfi_number"), "platform": body.platform})
    return _enrich_rfis(project_id, [updated])[0]


@router.delete("/rfis/{rfi_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rfi(
    project_id: str,
    rfi_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    row = _row_or_404("rfis", rfi_id, project_id, "RFI")
    # Numbers are NOT re-sequenced: a deleted RFI leaves a gap, keeping every
    # number ever referenced in correspondence unambiguous.
    get_supabase().table("rfis").delete().eq("id", rfi_id).eq(
        "project_id", project_id
    ).execute()
    audit(user.id, "rfi.delete", "project", project_id,
          {"rfi_id": rfi_id, "rfi_number": row.get("rfi_number")})


# ── Manpower ─────────────────────────────────────────────────────────────────


def _validate_daily_log(daily_log_id: str, project_id: str) -> None:
    rows = (
        get_supabase()
        .table("daily_logs")
        .select("id, project_id")
        .eq("id", daily_log_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Daily log not found")
    if rows[0].get("project_id") != project_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That daily log belongs to a different project"
        )


@router.get("/manpower")
def list_manpower(
    project_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    _: CurrentUser = Depends(require_pm_read),
):
    require_pm_project(project_id)
    query = (
        get_supabase().table("manpower_entries").select("*").eq("project_id", project_id)
    )
    if date_from is not None:
        query = query.gte("work_date", date_from.isoformat())
    if date_to is not None:
        query = query.lte("work_date", date_to.isoformat())
    return (query.order("work_date", desc=True).execute()).data or []


@router.post("/manpower", status_code=status.HTTP_201_CREATED)
def create_manpower(
    project_id: str,
    body: ManpowerIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    payload = body.model_dump(mode="json")
    if payload.get("daily_log_id"):
        _validate_daily_log(payload["daily_log_id"], project_id)
    payload.update({"project_id": project_id, "created_by": user.id})
    created = get_supabase().table("manpower_entries").insert(payload).execute().data[0]
    audit(user.id, "manpower.create", "project", project_id,
          {"manpower_id": created.get("id"), "work_date": payload["work_date"],
           "classification": body.classification, "workers": body.workers})
    return created


@router.patch("/manpower/{entry_id}")
def update_manpower(
    project_id: str,
    entry_id: str,
    body: ManpowerUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    # Existence is guarded by the scoped UPDATE's empty result (404 below).
    patch = _patch_of(body)
    _reject_cleared(patch, "work_date", "classification", "workers")
    if patch.get("daily_log_id"):  # explicit null just unlinks — always allowed
        _validate_daily_log(patch["daily_log_id"], project_id)
    updated = (
        get_supabase()
        .table("manpower_entries")
        .update(patch)
        .eq("id", entry_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Manpower entry not found")
    audit(user.id, "manpower.update", "project", project_id,
          {"manpower_id": entry_id, "fields": sorted(patch)})
    return updated[0]


@router.delete("/manpower/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_manpower(
    project_id: str,
    entry_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    row = _row_or_404("manpower_entries", entry_id, project_id, "Manpower entry")
    get_supabase().table("manpower_entries").delete().eq("id", entry_id).eq(
        "project_id", project_id
    ).execute()
    audit(user.id, "manpower.delete", "project", project_id,
          {"manpower_id": entry_id, "work_date": row.get("work_date")})

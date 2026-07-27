"""Send-batch lifecycle + the role-shaped read models for the Plans & Specs Log.

A *send batch* is one outbound "team → estimator" delivery of files. Before this
module the send was not a record at all: `estimator_assignments.sent_to_estimator_at`
(0036) stored only *when* the initial package went out, and
`project_files.sent_to_estimators_at` (0048) only *when* a revision/additional
file was emailed — never *who* received *which* files. The three tables added in
0076 (`file_send_batches`, `file_send_recipients`, `file_send_batch_files`) close
that gap, and this module is the only writer/reader of them.

Write ordering — claim-before-send (do NOT reorder)
---------------------------------------------------
`claim_batch()` writes the batch row AND its recipient rows AND its batch-file
links in one call, BEFORE the email is composed. This is what makes the flow
safe:

  * The unique partial index `file_send_batches_one_initial_idx` turns two racing
    initial sends into a 23505 → 409 *here*, before either email is composed, so
    a double-click cannot double-send the initial package.
  * The estimator's entire file surface (the portal is the log after this
    feature) depends on the recipient/file child rows, so they must exist before
    delivery — never in a best-effort post-send patch that could leave a "sent"
    batch with zero recipients and an estimator staring at an empty portal.

Only two things are left as best-effort post-send updates, because both are
genuinely safe to lose:
  * `attach_email_log()` — the mail is already delivered; a lost link is cosmetic.
  * `stamp_sent()` — `build_log`/`_estimator_visible` filter an estimator's file
    list on the stamp, so a file whose stamp was lost renders as *absent* rather
    than as a dead link that 403s on download.

On the first email failure the caller calls `abandon_batch()` (children cascade),
rolls back any assignment it inserted, and re-raises — leaving a clean retry with
`package_sent_at` still null and "Upload plans and specs" still visible.

sync SDK
--------
The Supabase SDK is synchronous, so every function here is a plain `def`. They
are called from plain-`def` route handlers that FastAPI runs in its threadpool
(never from an `async def`), per `bdr-event-loop-blocking`.
"""

import logging
from typing import Any

from fastapi import HTTPException, status

from app.core.deps import CurrentUser
from app.core.file_categories import (
    INITIAL_CATEGORIES,
    PACKAGE_CATEGORIES,
    SENT_GATED_CATEGORIES,
)
from app.core.roles import Role
from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# The three live send kinds. `reconstructed` is a boolean flag on the row, NOT a
# fourth kind (0076) — a backfilled initial send still sorts/labels as initial.
KINDS = ("initial", "revision", "reassign")

# project_files projection the log/handoff readers need. Includes the fields
# `_estimator_visible` gates on (`sent_to_estimators_at`, `uploaded_by`) so the
# estimator projection can be computed without a second read.
_FILE_FIELDS = (
    "id, category, doc_type, filename, size_bytes, note, "
    "addendum_number, addendum_issued_on, sent_to_estimators_at, uploaded_by"
)

_INITIAL_CLAIM_RACE_MESSAGE = (
    "The initial package was just sent — refresh to see the result"
)


def _is_unique_violation(exc: Exception) -> bool:
    s = str(exc)
    return "23505" in s or "duplicate key" in s


def _recipient_batch_ids(sb, estimator_id: str) -> set[str]:
    """The set of batch ids that carry a recipient row for this estimator — the
    scope for every estimator-facing read (log, stats, handoff)."""
    rows = (
        sb.table("file_send_recipients")
        .select("batch_id")
        .eq("estimator_id", estimator_id)
        .execute()
    ).data or []
    return {r["batch_id"] for r in rows}


def _counts_from_summary(summary: dict | None) -> dict[str, int]:
    """Category → count from the at-send-time snapshot, dropping the non-count
    extras (e.g. the `addendum_numbers` list). `bool` is an `int` subclass in
    Python, so it is excluded explicitly."""
    out: dict[str, int] = {}
    for k, v in (summary or {}).items():
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out[k] = v
    return out


# ── Predicates / stats ─────────────────────────────────────────────────────


def has_initial_send(project_id: str) -> bool:
    """EXISTS a batch with kind='initial'. This — not batch_count > 0 — is the
    "Upload plans and specs" predicate: requirement 2 gates that one-shot button
    on the INITIAL send, and the unique partial index guarantees at most one."""
    rows = (
        get_supabase()
        .table("file_send_batches")
        .select("id")
        .eq("project_id", project_id)
        .eq("kind", "initial")
        .limit(1)
        .execute()
    ).data or []
    return bool(rows)


def batch_stats(project_id: str, estimator_id: str | None = None) -> dict:
    """{batch_count, first_sent_at, last_sent_at, package_sent_at}.

    package_sent_at = sent_at of the initial batch (None when there is none).
    When `estimator_id` is given every field is scoped to the batches that
    estimator actually received — a project-wide count would leak to a
    late-added estimator that earlier sends (and therefore other recipients)
    exist.
    """
    sb = get_supabase()
    batches = (
        sb.table("file_send_batches")
        .select("id, kind, sent_at")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if estimator_id is not None:
        allowed = _recipient_batch_ids(sb, estimator_id)
        batches = [b for b in batches if b["id"] in allowed]
    sent_ats = sorted(b["sent_at"] for b in batches if b.get("sent_at"))
    package_sent_at = next(
        (b["sent_at"] for b in batches if b.get("kind") == "initial"), None
    )
    return {
        "batch_count": len(batches),
        "first_sent_at": sent_ats[0] if sent_ats else None,
        "last_sent_at": sent_ats[-1] if sent_ats else None,
        "package_sent_at": package_sent_at,
    }


# ── Batch lifecycle ────────────────────────────────────────────────────────


def claim_batch(
    *,
    project_id: str,
    kind: str,
    sent_by: str | None,
    message: str | None,
    recipients: list[dict],
    file_ids: list[str],
    summary: dict,
    section_notes: dict[str, str] | None = None,
) -> dict:
    """Insert the batch row AND its recipient rows AND its file rows in one call,
    BEFORE the email goes out. Returns the batch row (`{"id": ..., ...}`).

    Everything needed is known before the email is composed, so nothing is lost
    by writing early — and the estimator's entire file surface depends on these
    child rows, so they must not live in a best-effort post-send patch.

    `recipients` items: `{estimator_id, email, full_name}`; de-duped on `email`
    to mirror the `(batch_id, email)` primary key. `file_ids` is de-duped to
    mirror the `(batch_id, file_id)` primary key. `summary` is the at-send-time
    category counts plus `{"addendum_numbers": [...]}`, stored verbatim.

    `section_notes` (0077) is the per-section "what changed" text keyed by
    `file_categories.section_key(category, doc_type)` — a property of THIS send,
    stored once on the batch rather than copied onto every file in the section.
    Already validated by the caller; `{}` when the send has none.

    The unique partial index `file_send_batches_one_initial_idx` turns two racing
    initial sends into a 23505 here → HTTPException(409). Because the batch row
    is written first, that 409 fires before either email is composed.
    """
    sb = get_supabase()
    try:
        batch = (
            sb.table("file_send_batches")
            .insert(
                {
                    "project_id": project_id,
                    "kind": kind,
                    "message": (message or None),
                    "sent_by": sent_by,
                    "summary": summary or {},
                    "section_notes": section_notes or {},
                }
            )
            .execute()
        ).data[0]
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — unique violation → racing initial send
        if _is_unique_violation(exc):
            raise HTTPException(
                status.HTTP_409_CONFLICT, _INITIAL_CLAIM_RACE_MESSAGE
            ) from exc
        raise
    batch_id = batch["id"]

    # Recipients — de-dupe on the delivered address (the (batch_id, email) PK),
    # so a double-add of the same person never trips a 23505 mid-write.
    seen: set[str] = set()
    recipient_rows: list[dict] = []
    for r in recipients or []:
        email = (r.get("email") or "").strip()
        if not email or email in seen:
            continue
        seen.add(email)
        recipient_rows.append(
            {
                "batch_id": batch_id,
                "estimator_id": r.get("estimator_id"),
                "email": email,
                "full_name": r.get("full_name"),
            }
        )
    if recipient_rows:
        sb.table("file_send_recipients").insert(recipient_rows).execute()

    # Batch-file links — de-dupe on the (batch_id, file_id) PK, preserving order.
    file_rows = [
        {"batch_id": batch_id, "file_id": fid}
        for fid in dict.fromkeys(fid for fid in (file_ids or []) if fid)
    ]
    if file_rows:
        sb.table("file_send_batch_files").insert(file_rows).execute()

    return batch


def abandon_batch(batch_id: str) -> None:
    """Delete a claimed batch whose email failed; recipients and file links
    cascade. Best-effort — a failed cleanup only leaves a harmless empty batch,
    never a wrong send."""
    try:
        get_supabase().table("file_send_batches").delete().eq("id", batch_id).execute()
    except Exception:  # noqa: BLE001
        logger.warning("abandon_batch: cleanup failed for %s", batch_id, exc_info=True)


def attach_email_log(batch_id: str, email: str, email_log_id: str) -> None:
    """Post-send patch of ONE recipient row's email_log_id. Best-effort — the
    mail is already delivered, so a failed write is logged and swallowed."""
    try:
        (
            get_supabase()
            .table("file_send_recipients")
            .update({"email_log_id": email_log_id})
            .eq("batch_id", batch_id)
            .eq("email", email)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "attach_email_log: failed for batch=%s email present", batch_id, exc_info=True
        )


def stamp_sent(file_ids: list[str]) -> None:
    """UPDATE project_files SET sent_to_estimators_at = now()
       WHERE id IN (...) AND sent_to_estimators_at IS NULL   <- first-send-wins.

    Best-effort. The NULL guard is load-bearing: a reassign batch re-sends
    already-sent files and must not reset their stamp (§2.4 invariant:
    sent_to_estimators_at == min(batch.sent_at))."""
    if not file_ids:
        return
    try:
        (
            get_supabase()
            .table("project_files")
            .update({"sent_to_estimators_at": "now()"})
            .in_("id", list(file_ids))
            .is_("sent_to_estimators_at", "null")
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.warning("stamp_sent: failed", exc_info=True)


def prior_batches(project_id: str) -> list[dict]:
    """[{kind, sent_at, summary}] oldest-first — the catch-up (reassign) email's
    "Update history" table."""
    rows = (
        get_supabase()
        .table("file_send_batches")
        .select("kind, sent_at, summary")
        .eq("project_id", project_id)
        .order("sent_at")
        .execute()
    ).data or []
    return [
        {"kind": r["kind"], "sent_at": r["sent_at"], "summary": r.get("summary") or {}}
        for r in rows
    ]


# ── Role-shaped reads ──────────────────────────────────────────────────────


def _load_batch_files(sb, batch_ids: list[str]) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Returns (file_ids-per-batch, project_files-row-by-id) for the given
    batches, in two `.in_` reads regardless of batch count."""
    files_per_batch: dict[str, list[str]] = {}
    files_by_id: dict[str, dict] = {}
    if not batch_ids:
        return files_per_batch, files_by_id
    links = (
        sb.table("file_send_batch_files")
        .select("batch_id, file_id")
        .in_("batch_id", batch_ids)
        .execute()
    ).data or []
    for link in links:
        files_per_batch.setdefault(link["batch_id"], []).append(link["file_id"])
    file_ids = list({link["file_id"] for link in links})
    if file_ids:
        rows = (
            sb.table("project_files")
            .select(_FILE_FIELDS)
            .in_("id", file_ids)
            .execute()
        ).data or []
        files_by_id = {r["id"]: r for r in rows}
    return files_per_batch, files_by_id


def _file_out(rec: dict, available: bool) -> dict:
    return {
        "file_id": rec["id"],
        "category": rec["category"],
        # 'drawing' | 'specification' | None — which document set a revision or
        # addendum belongs to (0077). None on the initial package (its category
        # already says) and on rows predating the column.
        "doc_type": rec.get("doc_type"),
        "filename": rec.get("filename") or "",
        "size_bytes": rec.get("size_bytes"),
        "note": rec.get("note"),
        "addendum_number": rec.get("addendum_number"),
        "addendum_issued_on": rec.get("addendum_issued_on"),
        "available": available,
    }


def build_log(project_id: str, user: CurrentUser) -> dict:
    """The role-shaped Plans & Specs Log payload (§3.5), shaped as a SendBatchLogOut.

    TWO code paths, never one payload post-filtered. The estimator path never
    selects the recipient/profile join, and its batch dicts omit the
    `recipients` and `sent_by_name` keys entirely (absent, not null) — so a
    router that returns this dict as-is cannot leak a co-assignee's identity.

    Returns `{"viewer": "internal"|"estimator", "batches": [...]}` newest-first.
    This function is a pure read: it writes no audit row. The send-batches
    handler is responsible for the `estimator.log_view` audit on external reads.
    """
    sb = get_supabase()
    is_estimator = user.role == Role.ESTIMATOR

    batches = (
        sb.table("file_send_batches")
        .select(
            "id, kind, sent_at, message, reconstructed, summary, section_notes, sent_by"
        )
        .eq("project_id", project_id)
        .order("sent_at", desc=True)
        .execute()
    ).data or []
    if is_estimator:
        # Only batches addressed to the caller. A batch sent before they were
        # assigned is not their record, and its existence would leak that other
        # estimators exist. Nothing is lost: a reassign batch always carries the
        # full package, so a late-assigned estimator's batches contain every
        # file they may read.
        allowed = _recipient_batch_ids(sb, user.id)
        batches = [b for b in batches if b["id"] in allowed]

    batch_ids = [b["id"] for b in batches]
    files_per_batch, files_by_id = _load_batch_files(sb, batch_ids)

    recipients_by_batch: dict[str, list[dict]] = {}
    senders: dict[str, str | None] = {}
    if not is_estimator and batch_ids:
        recip_rows = (
            sb.table("file_send_recipients")
            .select("batch_id, estimator_id, email, full_name")
            .in_("batch_id", batch_ids)
            .execute()
        ).data or []
        for r in recip_rows:
            recipients_by_batch.setdefault(r["batch_id"], []).append(r)
        sender_ids = list({b["sent_by"] for b in batches if b.get("sent_by")})
        if sender_ids:
            srows = (
                sb.table("profiles")
                .select("id, full_name")
                .in_("id", sender_ids)
                .execute()
            ).data or []
            senders = {s["id"]: s.get("full_name") for s in srows}

    if is_estimator:
        # Local import breaks the files.py ↔ file_sends import cycle (files.py's
        # send-batches handler imports this module). `_estimator_visible` is the
        # single source of truth for the per-file read gate.
        from app.routers.files import _estimator_visible

    out_batches: list[dict] = []
    for b in batches:
        kind = b["kind"]
        if is_estimator and kind == "reassign":
            # A "Re-assign" label on a row addressed to them announces that an
            # earlier send to someone else happened. Collapse to "initial".
            kind = "initial"

        files_out: list[dict] = []
        for fid in files_per_batch.get(b["id"], []):
            rec = files_by_id.get(fid)
            if rec is None:
                # Unreachable in practice: file_send_batch_files.file_id is FK
                # ON DELETE CASCADE, so a link with no row cannot persist.
                continue
            if is_estimator:
                # Emitted (not omitted) when hidden, so the row count still
                # matches the counts snapshot and nothing renders as a link that
                # 403s on download.
                available = _estimator_visible(rec, user.id)
            else:
                available = True
            files_out.append(_file_out(rec, available))

        batch_out: dict[str, Any] = {
            "id": b["id"],
            "kind": kind,
            "sent_at": b["sent_at"],
            "message": b.get("message"),
            "reconstructed": bool(b.get("reconstructed")),
            # Headline counts come from the snapshot, never the live join, so a
            # later file delete does not rewrite what was sent.
            "counts": _counts_from_summary(b.get("summary")),
            # Per-section "what changed" notes (0077), keyed by section_key().
            # NOT internal-only: telling the estimator what changed in the plans
            # vs in the specs is the whole point of capturing them.
            "section_notes": b.get("section_notes") or {},
            "files": files_out,
        }
        if not is_estimator:
            batch_out["recipients"] = [
                {
                    "estimator_id": r.get("estimator_id"),
                    "full_name": r.get("full_name"),
                    "email": r["email"],
                }
                for r in recipients_by_batch.get(b["id"], [])
            ]
            batch_out["sent_by_name"] = senders.get(b.get("sent_by"))
        out_batches.append(batch_out)

    return {
        "viewer": "estimator" if is_estimator else "internal",
        "batches": out_batches,
    }


def build_handoff(project_id: str, user: CurrentUser) -> dict:
    """The role-shaped compact hand-off summary (§3.4), shaped as a HandoffOut.

    SOURCE OF TRUTH for the FE button predicates. For the estimator every field
    is scoped to their own batches/assignment, `assignees` holds exactly the
    caller's own row with `email` blanked, and `staged` is `{}`.
    """
    sb = get_supabase()
    is_estimator = user.role == Role.ESTIMATOR

    proj_rows = (
        sb.table("projects")
        .select("due_from_estimator_at")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    due_back_at = proj_rows[0].get("due_from_estimator_at") if proj_rows else None

    # Scoped batches (id/kind/sent_at) drive package_sent_at / last_sent_at /
    # batch_count and the `sent` + `latest_addendum` derivations.
    batches = (
        sb.table("file_send_batches")
        .select("id, kind, sent_at")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if is_estimator:
        allowed = _recipient_batch_ids(sb, user.id)
        batches = [b for b in batches if b["id"] in allowed]
    batch_ids = [b["id"] for b in batches]
    sent_ats = sorted(b["sent_at"] for b in batches if b.get("sent_at"))
    package_sent_at = next(
        (b["sent_at"] for b in batches if b.get("kind") == "initial"), None
    )
    last_sent_at = sent_ats[-1] if sent_ats else None

    # `sent`: distinct files across the caller's visible batches, counted by the
    # file's current category, plus the latest addendum among them.
    sent: dict[str, int] = {}
    latest_addendum: dict | None = None
    distinct_file_ids: list[str] = []
    if batch_ids:
        links = (
            sb.table("file_send_batch_files")
            .select("file_id")
            .in_("batch_id", batch_ids)
            .execute()
        ).data or []
        distinct_file_ids = list({link["file_id"] for link in links})
    if distinct_file_ids:
        frows = (
            sb.table("project_files")
            .select("id, category, addendum_number, addendum_issued_on")
            .in_("id", distinct_file_ids)
            .execute()
        ).data or []
        for r in frows:
            sent[r["category"]] = sent.get(r["category"], 0) + 1
        dated = [
            r
            for r in frows
            if r["category"] == "addendum" and r.get("addendum_issued_on")
        ]
        if dated:
            best = max(dated, key=lambda r: r["addendum_issued_on"])
            latest_addendum = {
                "number": best.get("addendum_number") or "",
                "issued_on": best["addendum_issued_on"],
            }

    # Assignments — internal sees every row newest-first with emails; the
    # estimator sees exactly their own row with the email blanked.
    assign_rows = (
        sb.table("estimator_assignments")
        .select(
            "id, estimator_id, due_at, expires_at, revoked_at, sent_to_estimator_at, "
            "profiles!estimator_assignments_estimator_id_fkey(full_name, email)"
        )
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    my_access_expires_at: str | None = None
    my_due_at: str | None = None
    if is_estimator:
        mine = next((a for a in assign_rows if a.get("estimator_id") == user.id), None)
        assignees = [_assignee_out(mine, blank_email=True)] if mine else []
        if mine:
            my_access_expires_at = mine.get("expires_at")
            my_due_at = mine.get("due_at")
    else:
        assignees = [_assignee_out(a) for a in assign_rows]

    # `staged`: uploaded-but-never-emailed package files (internal only). A
    # drawing/spec counts as staged until the initial package exists (they are
    # frozen after and never re-uploaded); a revision/additional/addendum counts
    # until its own sent stamp is set.
    staged: dict[str, int] = {}
    if not is_estimator:
        package_sent = package_sent_at is not None
        pkg_rows = (
            sb.table("project_files")
            .select("category, sent_to_estimators_at")
            .eq("project_id", project_id)
            .in_("category", list(PACKAGE_CATEGORIES))
            .execute()
        ).data or []
        for r in pkg_rows:
            cat = r["category"]
            if cat in INITIAL_CATEGORIES:
                if not package_sent:
                    staged[cat] = staged.get(cat, 0) + 1
            elif cat in SENT_GATED_CATEGORIES:
                if not r.get("sent_to_estimators_at"):
                    staged[cat] = staged.get(cat, 0) + 1

    # Local import breaks the files.py ↔ file_sends cycle; handoff_locked is the
    # single source of truth mirrored by GET /files/lock.
    from app.routers.files import handoff_locked

    return {
        "package_sent_at": package_sent_at,
        "last_sent_at": last_sent_at,
        "batch_count": len(batches),
        "due_back_at": due_back_at,
        "assignees": assignees,
        "staged": staged,
        "sent": sent,
        "latest_addendum": latest_addendum,
        "locked": handoff_locked(project_id),
        "my_access_expires_at": my_access_expires_at,
        "my_due_at": my_due_at,
    }


def _assignee_out(a: dict, blank_email: bool = False) -> dict:
    prof = a.get("profiles") or {}
    return {
        "assignment_id": a["id"],
        "estimator_id": a["estimator_id"],
        "full_name": prof.get("full_name"),
        "email": None if blank_email else prof.get("email"),
        "due_at": a.get("due_at"),
        "expires_at": a.get("expires_at"),
        "revoked_at": a.get("revoked_at"),
        "sent_to_estimator_at": a.get("sent_to_estimator_at"),
    }

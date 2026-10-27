"""Revised proposals after submission (migrations 0178 and 0179).

A bid was submitted, then the GC pushed the bid date out and the team wants
to revise its numbers and send a "Revised Proposal". docs/PROPOSAL_REVISIONS.md
is the full design; the load-bearing rules:

- proposal_sends stays ONE row per (project, GC), "the bid this GC holds".
  Its row stays at 'sent' for the whole revision, and its submission record
  (sent_at, sent_by, sent_via, email_log_id, gc_email) is never overwritten.
  A revised document lives in proposal_revision_sends until it goes out;
  sending (or marking) it swaps the held document onto the live row in one
  transaction (apply_proposal_revision_send), which also snapshots the prior
  values, stamps revised_at / revised_by and appends a 'revision' event.
- Claim, swap and close are SQL functions that all lock the revision row
  first, so Done / Cancel can never close a revision under an email in flight.
- One open revision per project. While it is open, and only then, the locks
  that guard a submitted bid open up: scope lines, the GC amounts of the GCs
  in the revision (direct writer edits, no 0118 approval), and quote
  selection (workflow.in_post_submission_window answers False). Cost-side
  edits keep the bid at Submitted and require Executive approval inside the
  revision before GC pricing, generation or dispatch can continue.
- A GC whose revised document does not go out (unchecked, Done, Cancel) gets
  the per-GC overrides it had at the start back, so its figures match the
  document it holds.
- GCs added during the revision get a normal initial proposal through the
  existing Send Out generate/send path, with their figures set directly by
  the writer (see proposal_send.set_gc_amounts).
- No stage_events for any of it: audit_log rows only (proposal.revision_*).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from postgrest.exceptions import APIError

from app.core.supabase_client import get_supabase
from app.services import email_branding, graph_email, office_preview, proposal_send, storage
from app.services.notifications import audit, dismiss_notifications, notify_role
from app.services.proposal_docx import DOCX_MIME, build_filename
from app.services.proposal_send import (
    AMOUNT_KEYS,
    GC_NAME_TOKEN,
    OUTCOME_UNKNOWN_PREFIX,
    PENDING_APPROVAL_MESSAGE,
    SENDING_STALE_MINUTES,
    ProposalSendError,
)

logger = logging.getLogger(__name__)

WAITING_ON_VERIFY_MESSAGE = (
    "Cost changes in this revision are waiting on the Executive's re-verify. "
    "Nothing can be generated or sent until pricing is re-committed."
)
APPROVAL_PENDING_MESSAGE = (
    "The costs changed in this revision. An Executive must approve the new figures "
    "in Revise proposal before GC prices are set or anything is generated or sent."
)
APPROVAL_TYPE = "proposal_revision.approval_requested"
# A teammate's "Ask again" ping to the Executives, at most this often.
APPROVAL_PING_MINUTES = 10
OUTCOME_BLOCKED_MESSAGE = (
    "A revised proposal is open on this bid. Finish or cancel the revision "
    "before recording the win/loss."
)
RESEND_BLOCKED_MESSAGE = (
    "A revised proposal is being prepared for this GC. Send the revision from "
    "Revise proposal instead, or uncheck this GC there first."
)
NOT_OPEN_MESSAGE = "This revision is no longer open. Refresh and try again."
# The terminal state for "Graph accepted the email but the swap was refused":
# the GC has the revised document, the live row still names the old one.
# Never retried (a retry would email the GC twice); Done can close it.
DELIVERED_PREFIX = "email delivered but not recorded"
_SENDING_MESSAGE = (
    "A proposal email is being sent right now. Wait for it to finish; an email "
    f"interrupted by a crash is checked automatically after {SENDING_STALE_MINUTES} "
    "minutes."
)
SUBJECT_MAX = 300

_OVERRIDE_COLUMNS = tuple(f"proposal_{k}_amount" for k in AMOUNT_KEYS)

# Messages for the SQL functions' 'proposal_revision:<code>' refusals.
_RPC_MESSAGES = {
    "not_found": "That revised proposal is no longer on file. Refresh and try again.",
    "not_open": NOT_OPEN_MESSAGE,
    "excluded": "This GC is unchecked in this revision.",
    "not_sent": "This GC no longer holds a sent proposal, so the revision cannot replace it.",
    "no_document": "The revised document is missing. Generate it again.",
    "delivered": "This revised proposal already reached the GC by email but could not be "
    "recorded. It is not sent again; use Done to close the revision.",
    "resend_in_flight": "A re-send of this GC's proposal is in progress. Wait for it to "
    "finish, then send the revision.",
    "sending": _SENDING_MESSAGE,
    "went_out": "Part of this revision may already have reached a GC, so it cannot be "
    "cancelled. Use Done to close it.",
    "nothing_sent": "Nothing has gone out in this revision yet. Send or mark at least one "
    "proposal, or cancel the revision.",
}


def rpc_error_code(exc: Exception) -> str | None:
    """The <code> of a 'proposal_revision:<code>' error, None for anything
    else (a transport error, a different failure)."""
    text = " ".join(
        str(part)
        for part in (getattr(exc, "message", None), getattr(exc, "details", None), exc)
        if part
    )
    marker = "proposal_revision:"
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1]
    code = ""
    for ch in tail:
        if not (ch.isalnum() or ch == "_"):
            break
        code += ch
    return code or None


def _rpc(sb, name: str, params: dict):
    """Run one of the 0178 SQL functions. A known refusal becomes a
    ProposalSendError carrying the code; any other failure propagates."""
    try:
        data = sb.rpc(name, params).execute().data
    except APIError as exc:
        code = rpc_error_code(exc)
        if code is None:
            raise
        raise ProposalSendError(
            _RPC_MESSAGES.get(code, "The revision could not be updated. Refresh and try again."),
            code=code,
        ) from exc
    if isinstance(data, list):
        data = data[0] if data else None
    return data or None


# ── reads ──────────────────────────────────────────────────────────────────


def open_revision(project_id: str, sb=None) -> dict | None:
    """The project's open revision row, or None."""
    client = sb if sb is not None else get_supabase()
    rows = (
        client.table("proposal_revisions")
        .select("*")
        .eq("project_id", project_id)
        .eq("status", "open")
        .execute()
    ).data or []
    # proposal_revisions_one_open guarantees at most one row.
    return rows[0] if rows else None


def revision_open(project_id: str, sb=None) -> bool:
    return open_revision(project_id, sb) is not None


def costs_pending(rev: dict | None) -> bool:
    """A cost-side edit landed after the last Executive approval (0179)."""
    if not rev or not rev.get("costs_changed_at"):
        return False
    approved = rev.get("costs_approved_at")
    return not approved or _ts(approved) < _ts(rev["costs_changed_at"])


def _ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _rows(sb, revision_id: str) -> list[dict]:
    return (
        sb.table("proposal_revision_sends")
        .select("*")
        .eq("revision_id", revision_id)
        .order("gc_name")
        .execute()
    ).data or []


def _reached(row: dict) -> bool:
    """The row's revised document may have reached the GC: sent, or a failure
    whose outcome is unknown, or delivered but not recorded. Such a row is
    never cancelled away and its document is never deleted."""
    if row.get("status") == "sent":
        return True
    error = row.get("error") or ""
    return row.get("status") == "failed" and (
        error.startswith(OUTCOME_UNKNOWN_PREFIX) or error.startswith(DELIVERED_PREFIX)
    )


def unlocked_gc_ids(project_id: str, revision: dict | None = None, sb=None) -> set[str]:
    """GCs whose bid the open revision reopens: in the revision (checked)
    and their revised document not sent, sending or possibly delivered.
    Empty when no revision is open. `sb` lets a caller reuse its own client."""
    sb = sb if sb is not None else get_supabase()
    rev = revision if revision is not None else open_revision(project_id, sb)
    if rev is None:
        return set()
    return {
        r["gc_id"]
        for r in _rows(sb, rev["id"])
        if r.get("included") and r.get("status") != "sending" and not _reached(r)
    }


def _on_project(sb, project_id: str) -> set[str]:
    return {
        r["gc_id"]
        for r in (
            sb.table("project_gcs").select("gc_id").eq("project_id", project_id).execute()
        ).data
        or []
    }


def _send_out_head(sb, project_id: str) -> tuple[str | None, str | None]:
    from app.services import workflow

    head = workflow.load_category_state(project_id).get("send_out", {}).get("current_task")
    rows = (
        sb.table("projects").select("reverify_return_stage").eq("id", project_id)
        .limit(1).execute()
    ).data or []
    return head, (rows[0].get("reverify_return_stage") if rows else None)


def _in_revision_window(head: str | None, return_stage: str | None) -> bool:
    return head == "submitted" or (head == "verify" and return_stage == "submitted")


def _require_open(sb, project_id: str, revision_id: str | None = None) -> dict:
    rev = open_revision(project_id, sb)
    if rev is None or (revision_id is not None and rev["id"] != revision_id):
        raise ProposalSendError(NOT_OPEN_MESSAGE)
    return rev


def _require_dispatch_head(sb, project_id: str) -> str:
    """Generate / send / mark need the bid back at 'submitted': a cost change
    parks it at Verify until the Executive re-commits."""
    head = proposal_send.send_window_head(project_id)  # also refuses an abandoned bid
    if head == "verify":
        raise ProposalSendError(WAITING_ON_VERIFY_MESSAGE)
    if head != "submitted":
        raise ProposalSendError("This bid is not on Submitted, so a revision cannot go out.")
    if costs_pending(open_revision(project_id, sb)):
        raise ProposalSendError(APPROVAL_PENDING_MESSAGE)
    return head


def _any_sending(sb, project_id: str) -> bool:
    for table in ("proposal_sends", "proposal_send_events", "proposal_revision_sends"):
        if (
            sb.table(table).select("id").eq("project_id", project_id)
            .eq("status", "sending").limit(1).execute()
        ).data:
            return True
    return False


def reclaim_stale(project_id: str, sb=None) -> None:
    """Resolve every revision row stuck at 'sending' longer than
    SENDING_STALE_MINUTES (a crashed request): proven sent, delivered but not
    recorded, or failed with an unknown outcome. Runs on every read of the
    Revise proposal box and before every revision action, so a crashed send
    can never wedge the project. Best effort per row."""
    client = sb if sb is not None else get_supabase()
    rows = (
        client.table("proposal_revision_sends").select("*").eq("project_id", project_id)
        .eq("status", "sending").execute()
    ).data or []
    for row in rows:
        try:
            _reclaim_stuck(client, row)
        except Exception:  # noqa: BLE001 - one bad row must not break the read
            logger.exception("revision reclaim failed for %s", row.get("id"))


# ── per-GC overrides (M2: figures follow the document a GC holds) ──────────


def _snapshot_overrides(sb, project_id: str, gc_ids: list[str]) -> dict[str, dict]:
    rows = (
        sb.table("project_gcs").select("gc_id, " + ", ".join(_OVERRIDE_COLUMNS))
        .eq("project_id", project_id).in_("gc_id", gc_ids).execute()
    ).data or []
    return {r["gc_id"]: {c: r.get(c) for c in _OVERRIDE_COLUMNS} for r in rows}


def _restore_overrides(sb, project_id: str, row: dict) -> None:
    """Put a GC's per-GC overrides back to what they were when the revision
    started (its revised document is not going out, so it keeps the figures
    of the document it holds). No-op for a row without a snapshot."""
    snap = row.get("start_overrides")
    if not isinstance(snap, dict):
        return
    current = (
        sb.table("project_gcs").select(", ".join(_OVERRIDE_COLUMNS))
        .eq("project_id", project_id).eq("gc_id", row["gc_id"]).limit(1).execute()
    ).data or []
    if not current:
        return
    restore = {c: snap.get(c) for c in _OVERRIDE_COLUMNS}
    if all(_same(current[0].get(c), restore[c]) for c in _OVERRIDE_COLUMNS):
        return
    sb.table("project_gcs").update(restore).eq("project_id", project_id).eq(
        "gc_id", row["gc_id"]
    ).execute()
    audit(
        None, "proposal.revision_amounts_restored", "project", project_id,
        {"gc_id": row["gc_id"], "gc_name": row.get("gc_name"), "restored": restore},
    )


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return Decimal(str(a)) == Decimal(str(b))


def _clear_overrides(sb, project_id: str, gc_id: str) -> None:
    """A GC in the revision is priced from the current figures (0179)."""
    sb.table("project_gcs").update({c: None for c in _OVERRIDE_COLUMNS}).eq(
        "project_id", project_id
    ).eq("gc_id", gc_id).execute()


def _verification_row(sb, project_id: str) -> dict | None:
    rows = (
        sb.table("verifications").select("*").eq("project_id", project_id).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


# ── cost changes and the Executive's approval (0179) ───────────────────────


def prepare_cost_change(project_id: str, actor_id: str | None, reason: str, sb) -> None:
    """Persist the approval requirement before a cost mutation can run.

    Unlike the after-edit workflow hook, errors here propagate and prevent
    the mutation. The after-edit stamp remains necessary to catch an approval
    racing the write. Notifications wait until that hook sees the saved edit.
    """
    rev = open_revision(project_id, sb)
    if rev is None:
        return
    fields = {
        "costs_changed_at": datetime.now(timezone.utc).isoformat(),
        "costs_changed_by": actor_id,
        "costs_change_reason": (reason or "")[:500] or None,
    }
    if not costs_pending(rev):
        fields["approval_requested_at"] = None
    written = sb.table("proposal_revisions").update(fields).eq("id", rev["id"]).eq(
        "status", "open"
    ).execute().data
    if not written:
        raise ProposalSendError(NOT_OPEN_MESSAGE)


def record_cost_change(project_id: str, actor_id: str | None, reason: str, sb=None) -> bool:
    """A cost-side edit landed while a revision is open. The bid stays at
    Submitted (no Verify bounce, no stage_events): the revision records the
    change, and the Executives are asked to approve the new figures in the
    modal the first time it goes from approved (or untouched) to pending.
    Returns True when a revision is open (the caller skips its bounce)."""
    client = sb if sb is not None else get_supabase()
    rev = open_revision(project_id, client)
    if rev is None:
        return False
    was_pending = costs_pending(rev)
    client.table("proposal_revisions").update(
        {
            "costs_changed_at": datetime.now(timezone.utc).isoformat(),
            "costs_changed_by": actor_id,
            "costs_change_reason": (reason or "")[:500] or None,
        }
    ).eq("id", rev["id"]).eq("status", "open").execute()
    audit(
        actor_id, "proposal.revision_costs_changed", "project", project_id,
        {"revision_id": rev["id"], "revision_number": rev["revision_number"], "reason": reason},
    )
    if not was_pending or not rev.get("approval_requested_at"):
        _ask_executives(client, project_id, rev, reason)
    return True


def _ask_executives(sb, project_id: str, rev: dict, reason: str | None) -> None:
    """Bell (plus email / Teams per the notification routing) to every
    Executive. Best effort: the cost edit is already saved."""
    from app.core.roles import Role

    try:
        project = (
            sb.table("projects").select("name, number").eq("id", project_id).limit(1).execute()
        ).data or [{}]
        label = " ".join(x for x in (project[0].get("number"), project[0].get("name")) if x)
        message = (
            f"Revision {rev['revision_number']} of {label or 'a bid'} changed its costs"
            + (f" ({reason})" if reason else "")
            + ". Approve the new figures in Revise proposal so the revised proposals can go out."
        )
        dismiss_notifications(project_id=project_id, types=[APPROVAL_TYPE])
        notify_role(
            Role.EXECUTIVE, project_id, APPROVAL_TYPE, message,
            metadata={"revision_id": rev["id"]},
        )
        sb.table("proposal_revisions").update(
            {"approval_requested_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", rev["id"]).execute()
    except Exception:  # noqa: BLE001 - a bell must never undo the edit
        logger.exception("revision approval request failed for %s", project_id)


def request_approval(project_id: str, revision_id: str, user_id: str) -> None:
    """A teammate asks the Executives again (at most every
    APPROVAL_PING_MINUTES)."""
    sb = get_supabase()
    rev = _require_open(sb, project_id, revision_id)
    if not costs_pending(rev):
        raise ProposalSendError("Nothing is waiting on an Executive's approval.")
    last = rev.get("approval_requested_at")
    if last and datetime.now(timezone.utc) - _ts(last) < timedelta(minutes=APPROVAL_PING_MINUTES):
        raise ProposalSendError(
            f"The Executives were asked less than {APPROVAL_PING_MINUTES} minutes ago."
        )
    _ask_executives(sb, project_id, rev, rev.get("costs_change_reason"))
    audit(user_id, "proposal.revision_approval_requested", "project", project_id,
          {"revision_id": rev["id"]})


def approve_costs(project_id: str, revision_id: str, user, body=None, background=None) -> dict:
    """The Executive approves the revised costs, inside the revision: the
    verifications snapshot is re-committed through the Verify commit
    (pricing.commit_verify, so generation reads committed figures, with the
    same per-figure overrides the Verify panel takes), then the approval is
    stamped with the moment the commit STARTED, so an edit racing it leaves
    the revision pending again. Role-gated by the router (VERIFY_ROLES)."""
    from app.routers import pricing as pricing_router

    sb = get_supabase()
    rev = _require_open(sb, project_id, revision_id)
    if not costs_pending(rev):
        raise ProposalSendError("Nothing is waiting on approval: the costs have not changed.")
    started = datetime.now(timezone.utc).isoformat()
    # Keep dispatch blocked until the new committed snapshot is durable. Use
    # the start time so an edit racing the commit still requires approval.
    pricing_router.commit_verify(project_id, body, user, background)
    sb.table("proposal_revisions").update(
        {"costs_approved_at": started, "costs_approved_by": user.id}
    ).eq("id", rev["id"]).eq("status", "open").execute()
    if not costs_pending(open_revision(project_id, sb)):
        dismiss_notifications(project_id=project_id, types=[APPROVAL_TYPE])
        pricing_router._regenerate_stale_proposals_after_commit(project_id, user, background)
    audit(
        user.id, "proposal.revision_costs_approve", "project", project_id,
        {"revision_id": rev["id"], "revision_number": rev["revision_number"]},
    )
    return open_revision(project_id, sb) or rev


def tag_revision_file(project_id: str, revision_id: str | None, file_id: str) -> None:
    """A file uploaded from the modal (its X-Proposal-Revision header names
    the open revision) is recorded as part of that revision. Silently
    ignored for any other header value: the upload itself is not locked."""
    if not revision_id:
        return
    sb = get_supabase()
    rev = open_revision(project_id, sb)
    if rev is None or rev["id"] != revision_id:
        return
    sb.table("project_files").update({"proposal_revision_id": rev["id"]}).eq(
        "id", file_id
    ).eq("project_id", project_id).execute()


# ── start ──────────────────────────────────────────────────────────────────


def start_blocked_reason(project_id: str) -> str | None:
    """Why a revision cannot start right now (a user-facing sentence), or
    None when it can. The same checks start_revision runs."""
    try:
        _start_context(get_supabase(), project_id)
    except ProposalSendError as exc:
        return str(exc)
    return None


def _start_context(sb, project_id: str) -> tuple[dict, list[dict]]:
    rows = (
        sb.table("projects")
        .select(
            "id, name, number, current_stage, abandoned_at, failed_to_bid_at,"
            " reverify_return_stage, internal_bid_at, actual_bid_at"
        )
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise ProposalSendError("Project not found", status_code=404)
    project = rows[0]
    if project.get("failed_to_bid_at"):
        raise ProposalSendError("This bid was marked failed to bid, so it cannot be revised.")
    if project.get("abandoned_at"):
        raise ProposalSendError("This bid has been abandoned, so it cannot be revised.")
    if project.get("current_stage") == "declined":
        raise ProposalSendError("This bid was declined, so it cannot be revised.")
    from app.services import workflow

    head = workflow.load_category_state(project_id).get("send_out", {}).get("current_task")
    if head == "bid_outcome":
        raise ProposalSendError(
            "A win/loss outcome is recorded for this bid, so it can no longer be revised."
        )
    if not _in_revision_window(head, project.get("reverify_return_stage")):
        raise ProposalSendError(
            "The bid has not been submitted yet. A proposal can be revised once it is "
            "on Submitted."
        )
    if (
        sb.table("bid_outcomes").select("project_id").eq("project_id", project_id)
        .limit(1).execute()
    ).data:
        raise ProposalSendError(
            "A win/loss outcome is recorded for this bid, so it can no longer be revised."
        )
    current = open_revision(project_id, sb)
    if current is not None:
        raise ProposalSendError(
            f"Revision {current['revision_number']} is already open on this bid."
        )
    reclaim_stale(project_id, sb)
    if _any_sending(sb, project_id):
        raise ProposalSendError(_SENDING_MESSAGE)
    on_project = _on_project(sb, project_id)
    sent = [
        r
        for r in (
            sb.table("proposal_sends").select("*").eq("project_id", project_id)
            .eq("status", "sent").execute()
        ).data
        or []
        if r["gc_id"] in on_project
    ]
    if not sent:
        raise ProposalSendError(
            "No GC on this project holds a sent proposal, so there is nothing to revise."
        )
    return project, sent


def start_revision(
    project_id: str,
    user_id: str,
    reason: str,
    *,
    new_bid_dates: dict | None = None,
) -> dict:
    """Open Rev N. Every GC that holds a sent proposal (and is still on the
    project) gets a revision row, checked by default, with a snapshot of its
    per-GC overrides. The bid dates the start form asked for are recorded
    here; the router applies them right after, through the project PATCH."""
    sb = get_supabase()
    text = " ".join((reason or "").split())
    if not text:
        raise ProposalSendError("Say what changed before starting the revision.", 400)
    project, sent = _start_context(sb, project_id)
    previous = (
        (
            sb.table("proposal_revisions").select("*")
            .eq("project_id", project_id).order("revision_number", desc=True)
            .limit(1).execute()
        ).data
        or []
    )
    prior = previous[0] if previous else None
    number = (prior["revision_number"] if prior else 0) + 1
    # Cancel and Done retain upstream edits. Unapproved costs must therefore
    # follow the project into its next revision, rather than becoming approved
    # merely because the earlier revision closed.
    pending_costs = (
        {key: prior.get(key) for key in (
            "costs_changed_at", "costs_changed_by", "costs_change_reason"
        )}
        if costs_pending(prior) else {}
    )
    after = new_bid_dates or {}
    try:
        rev = (
            sb.table("proposal_revisions")
            .insert(
                {
                    "project_id": project_id,
                    "revision_number": number,
                    "status": "open",
                    "reason": text[:2000],
                    "previous_internal_bid_at": project.get("internal_bid_at"),
                    "previous_actual_bid_at": project.get("actual_bid_at"),
                    "new_internal_bid_at": after.get("internal_bid_at"),
                    "new_actual_bid_at": after.get("actual_bid_at"),
                    "started_by": user_id,
                    "start_gc_ids": sorted(_on_project(sb, project_id)),
                    # The committed pricing the GCs hold: the approval step's
                    # "held" column (0179).
                    "start_verification": _verification_row(sb, project_id),
                    **pending_costs,
                }
            )
            .execute()
        ).data[0]
    except APIError as exc:
        if getattr(exc, "code", None) != "23505":
            raise
        raise ProposalSendError(
            "Another revision was started on this bid at the same time. Refresh."
        ) from exc
    overrides = _snapshot_overrides(sb, project_id, [r["gc_id"] for r in sent])
    try:
        sb.table("proposal_revision_sends").insert(
            [
                {
                    "revision_id": rev["id"],
                    "project_id": project_id,
                    "gc_id": r["gc_id"],
                    "proposal_send_id": r["id"],
                    "gc_name": r["gc_name"],
                    "included": True,
                    "status": "pending",
                    "start_overrides": overrides.get(r["gc_id"]),
                }
                for r in sent
            ]
        ).execute()
    except Exception:
        # Never leave an open revision with no GCs behind.
        sb.table("proposal_revisions").delete().eq("id", rev["id"]).execute()
        raise
    # Revision GC prices start from the CURRENT computed figures, exactly like
    # the initial GC Pricing step: the per-GC overrides each GC was bid with
    # are kept in start_overrides (shown as the held reference, and restored
    # if its revision does not go out) and cleared on the link (0179).
    for r in sent:
        _clear_overrides(sb, project_id, r["gc_id"])
    audit(
        user_id, "proposal.revision_start", "project", project_id,
        {
            "revision_id": rev["id"],
            "revision_number": number,
            "reason": text[:2000],
            "gcs": sorted(r["gc_name"] for r in sent),
            "new_internal_bid_at": after.get("internal_bid_at"),
            "new_actual_bid_at": after.get("actual_bid_at"),
        },
    )
    return rev


def clear_requested_bid_dates(revision_id: str) -> None:
    """The start form's bid date change was refused after the revision
    opened: the revision stays open, its record no longer claims new dates."""
    get_supabase().table("proposal_revisions").update(
        {"new_internal_bid_at": None, "new_actual_bid_at": None}
    ).eq("id", revision_id).execute()


# ── GC selection ───────────────────────────────────────────────────────────


def set_included_gcs(
    project_id: str, revision_id: str, user_id: str, gc_ids: list[str]
) -> None:
    """Check / uncheck the GCs that get this revision. An unchecked GC keeps
    its current proposal and gets its start-of-revision figures back (it can
    be part of a later revision). A GC whose revised document is sending or
    may have reached it can no longer be unchecked."""
    sb = get_supabase()
    rev = _require_open(sb, project_id, revision_id)
    reclaim_stale(project_id, sb)
    wanted = set(gc_ids)
    rows = _rows(sb, rev["id"])
    known = {r["gc_id"] for r in rows}
    if wanted - known:
        raise ProposalSendError(
            "Only GCs that already hold a sent proposal can be part of the revision. "
            "Add a new GC with Add GC instead.",
            400,
        )
    changed = []
    for r in rows:
        include = r["gc_id"] in wanted
        if bool(r.get("included")) == include:
            continue
        if not include and (r.get("status") == "sending" or _reached(r)):
            raise ProposalSendError(
                f"{r['gc_name']}'s revised proposal already went out (or is going out), "
                "so it stays in this revision."
            )
        written = (
            sb.table("proposal_revision_sends").update({"included": include})
            .eq("id", r["id"]).in_("status", ["pending", "generated", "failed"]).execute()
        ).data
        if not written:
            continue  # a send claimed it meanwhile; nothing changed
        if not include:
            _restore_overrides(sb, project_id, r)
        else:
            _clear_overrides(sb, project_id, r["gc_id"])
        changed.append({"gc_id": r["gc_id"], "gc_name": r["gc_name"], "included": include})
    if changed:
        audit(
            user_id, "proposal.revision_gcs", "project", project_id,
            {"revision_id": rev["id"], "changes": changed},
        )
        maybe_auto_complete(project_id, user_id)


# ── generate ───────────────────────────────────────────────────────────────


def generate_revision_documents(
    project_id: str, revision_id: str, user_id: str, gc_ids: list[str] | None = None
) -> list[dict]:
    """Render each checked GC's revised document at the CURRENT verified
    numbers, its per-GC overrides and the latest approved lines. Follows the
    Send Out generate rules (committed pricing, latest approved draft, no send
    in flight, no pending approval, all-or-nothing render) and never touches
    a GC whose revised document already went out. A checked GC that was
    removed from the project is skipped (refused when asked for by id)."""
    from app.routers.pricing import _get_one, _materials_rows, _verify_originals, section_summary

    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    rev = _require_open(sb, project_id, revision_id)
    head = _require_dispatch_head(sb, project_id)
    verification = _get_one("verifications", project_id)
    if not verification or not verification.get("committed_at"):
        raise ProposalSendError(WAITING_ON_VERIFY_MESSAGE)
    latest = (
        sb.table("proposal_drafts").select("*").eq("project_id", project_id)
        .order("created_at", desc=True).limit(1).execute()
    ).data or []
    draft = latest[0] if latest else None
    if not draft or not draft.get("approved_at") or not draft.get("lines_json"):
        raise ProposalSendError("Proposal lines must be approved first")
    reclaim_stale(project_id, sb)
    if _any_sending(sb, project_id):
        raise ProposalSendError(_SENDING_MESSAGE)

    live_gcs = {gc["id"]: gc for gc in proposal_send._project_gcs(project_id)}
    rows = _rows(sb, rev["id"])
    by_gc = {r["gc_id"]: r for r in rows}
    if gc_ids is not None:
        wanted = set(gc_ids)
        if wanted - set(by_gc):
            raise ProposalSendError("That GC is not part of this revision.", 404)
        if any(not by_gc[g].get("included") for g in wanted):
            raise ProposalSendError("That GC is unchecked in this revision. Check it first.")
        if any(_reached(by_gc[g]) for g in wanted):
            raise ProposalSendError(
                "This GC's revised proposal has already been sent (or may have reached it)."
            )
        missing = [by_gc[g]["gc_name"] for g in wanted if g not in live_gcs]
        if missing:
            raise ProposalSendError(f"{missing[0]} is no longer on this project.")
        targets = [by_gc[g] for g in wanted]
    else:
        targets = [
            r for r in rows
            if r.get("included") and r["status"] in ("pending", "generated", "failed")
            and not _reached(r) and r["gc_id"] in live_gcs
        ]
    if not targets:
        raise ProposalSendError("Every checked GC's revised proposal has already been sent.")

    target_gcs = []
    for r in targets:
        gc = live_gcs[r["gc_id"]]
        if gc.get("pricing_approval_status") == "pending":
            raise ProposalSendError(PENDING_APPROVAL_MESSAGE)
        target_gcs.append(gc)
    bad = [gc["name"] for gc in live_gcs.values() if "<" in gc["name"] or ">" in gc["name"]]
    if bad:
        raise ProposalSendError(
            "GC names cannot contain '<' or '>': rename " + ", ".join(bad), 400
        )
    names = [build_filename(project["number"], gc["name"]) for gc in live_gcs.values()]
    if len(names) != len(set(names)):
        raise ProposalSendError(
            "Two GCs on this project produce the same proposal filename. Rename one of "
            "the GCs before generating.",
            400,
        )

    amounts = proposal_send.proposal_amounts(_verify_originals(project_id), verification)
    includes_generator = section_summary(_materials_rows(project_id))["gear"][
        "includes_generator"
    ]
    base_ctx = proposal_send.build_base_context(
        project, draft, proposal_send.omit_zero_sections(amounts), includes_generator
    )
    rendered = proposal_send.render_gc_documents(
        project,
        target_gcs,
        base_ctx,
        amounts,
        includes_generator,
        proposal_send._all_project_gc_names(project_id),
        revision_number=rev["revision_number"],
    )
    digest = proposal_send.lines_hash(list(draft["lines_json"]))

    created: list[dict] = []
    for gc, filename, docx, gc_amounts in rendered:
        prior = by_gc[gc["id"]]
        path = storage.build_object_path(project_id, "proposal", filename)
        storage.upload_file(path, docx, DOCX_MIME)
        file_row = (
            sb.table("project_files")
            .insert(
                {
                    "project_id": project_id,
                    "category": "proposal",
                    "storage_path": path,
                    "filename": filename,
                    "gc_id": gc["id"],
                    "uploaded_by": user_id,
                    "mime_type": DOCX_MIME,
                    "size_bytes": len(docx),
                    "preview_status": "pending",
                }
            )
            .execute()
        ).data[0]
        fields = {
            "draft_id": draft["id"],
            "gc_name": gc["name"],
            "file_id": file_row["id"],
            "lines_hash": digest,
            **_stamp_columns(gc_amounts),
            "status": "generated",
            "error": None,
            "gc_email": None,
            "claimed_at": None,
            "claimed_by": None,
        }
        # Compare-and-set on the row we read: still unsent, still carrying the
        # document we replace (a racing generate or send leaves nothing to do).
        query = (
            sb.table("proposal_revision_sends").update(fields).eq("id", prior["id"])
            .in_("status", ["pending", "generated", "failed"])
        )
        query = (
            query.eq("file_id", prior["file_id"]) if prior.get("file_id")
            else query.is_("file_id", "null")
        )
        replaced = query.execute().data
        if not replaced:
            proposal_send._delete_file_row(file_row["id"])
            raise ProposalSendError(
                f"{gc['name']}'s revised proposal changed while generating. Refresh and "
                "try again."
            )
        # A Done / Cancel that landed while rendering already cleaned up this
        # revision's unsent documents: do not leave a new one behind it.
        if not revision_open(project_id, sb):
            sb.table("proposal_revision_sends").update({"file_id": None}).eq(
                "id", prior["id"]
            ).eq("file_id", file_row["id"]).execute()
            proposal_send._delete_file_row(file_row["id"])
            raise ProposalSendError(NOT_OPEN_MESSAGE)
        if prior.get("file_id") and prior["file_id"] != file_row["id"]:
            proposal_send._delete_file_row(prior["file_id"])
        audit(
            user_id, "proposal.revision_generate", "project_file", file_row["id"],
            {
                "project_id": project_id, "revision_id": rev["id"],
                "revision_number": rev["revision_number"], "gc_id": gc["id"],
                "filename": filename, "draft_id": draft["id"],
            },
        )
        created.append({**replaced[0], "_file": file_row})
    audit(
        user_id, "proposal.revision_generate_docs", "project", project_id,
        {"revision_id": rev["id"], "revision_number": rev["revision_number"],
         "gcs": len(created), "head": head},
    )
    return created


def _stamp_columns(amounts: dict[str, Decimal | None]) -> dict[str, str | None]:
    return {
        f"{key}_amount": str(amounts[key]) if amounts.get(key) is not None else None
        for key in AMOUNT_KEYS
    }


# ── send ───────────────────────────────────────────────────────────────────


def clean_subject(subject: str | None, project: dict) -> str:
    """The subject the sender typed (one line, trimmed), or the default
    Revised Proposal subject when none was given."""
    if subject is None:
        return proposal_send.revised_cover_subject(project)
    text = " ".join(subject.split())
    if not text:
        raise ProposalSendError("The email subject cannot be blank.", 400)
    if len(text) > SUBJECT_MAX:
        raise ProposalSendError(f"The email subject must be {SUBJECT_MAX} characters or fewer.", 400)
    return text


def _apply(sb, row: dict, *, user_id: str | None, via: str, email_log_id: str | None,
           sent_at: str | None, recipients: str | None, cc: list[dict] | None,
           subject: str | None) -> dict | None:
    """apply_proposal_revision_send: the one-transaction swap onto the live
    row. None = the row was no longer claimable (a racing request finished
    it). A refusal raises ProposalSendError with its code; a transport error
    propagates as is (the caller treats it as transient)."""
    return _rpc(
        sb,
        "apply_proposal_revision_send",
        {
            "p_revision_send_id": row["id"],
            "p_user_id": user_id,
            "p_via": via,
            "p_email_log_id": email_log_id,
            "p_sent_at": sent_at,
            "p_recipients": recipients,
            "p_cc": cc,
            "p_subject": subject,
        },
    )


def _mark_delivered_unrecorded(sb, row: dict, reason: str) -> dict:
    """Graph accepted the email, the swap was refused for good: a terminal,
    never-retried failure the UI shows plainly and Done can close."""
    message = f"{DELIVERED_PREFIX}: {reason}"[:500]
    updated = (
        sb.table("proposal_revision_sends").update({"status": "failed", "error": message})
        .eq("id", row["id"]).eq("status", "sending").execute()
    ).data or []
    audit(
        row.get("claimed_by"), "proposal.revision_send_unrecorded", "proposal_revision_send",
        row["id"], {"project_id": row["project_id"], "gc_id": row["gc_id"], "error": message},
    )
    return updated[0] if updated else {**row, "status": "failed", "error": message}


def _reclaim_stuck(sb, row: dict) -> dict:
    """A revision row stuck at 'sending' means a request crashed mid-send.
    email_log proves it went out (exact recipients AND the exact subject the
    claim stored, logged after the claim): finish the swap, or, when the swap
    is refused for good, record it as delivered but not recorded. Without
    proof release it as failed, outcome unknown. Never resend on guesswork."""
    stamp_text = row.get("claimed_at") or row.get("updated_at") or ""
    try:
        stamp = datetime.fromisoformat(stamp_text.replace("Z", "+00:00"))
    except ValueError:
        stamp = datetime.now(timezone.utc)
    if datetime.now(timezone.utc) - stamp < timedelta(minutes=SENDING_STALE_MINUTES):
        return row
    query = (
        sb.table("email_log")
        .select("id, status, to_addrs, subject, created_at")
        .eq("project_id", row["project_id"])
        .eq("subject", row.get("subject") or "")
        .eq("status", "sent")
    )
    if row.get("claimed_at"):
        query = query.gte("created_at", row["claimed_at"])
    logs = (query.order("created_at", desc=True).limit(20).execute()).data or []
    proof = next(
        (log for log in logs if row.get("gc_email") and log["to_addrs"] == row["gc_email"]),
        None,
    )
    if proof:
        try:
            done = _apply(
                sb, row, user_id=row.get("claimed_by"), via="email",
                email_log_id=proof["id"], sent_at=proof["created_at"],
                recipients=row.get("gc_email"), cc=row.get("cc_recipients"),
                subject=row.get("subject"),
            )
        except ProposalSendError as exc:
            return _mark_delivered_unrecorded(sb, row, str(exc))
        if done:
            return done
        fresh = (
            sb.table("proposal_revision_sends").select("*").eq("id", row["id"])
            .limit(1).execute()
        ).data or []
        return fresh[0] if fresh else row
    return (
        sb.table("proposal_revision_sends")
        .update(
            {
                "status": "failed",
                "error": f"{OUTCOME_UNKNOWN_PREFIX}: send interrupted; check Sent Items "
                "before retrying.",
            }
        )
        .eq("id", row["id"])
        .eq("status", "sending")
        .execute()
    ).data[0]


def _load_send_rows(sb, rev: dict, ids: list[str]) -> list[dict]:
    rows = (
        sb.table("proposal_revision_sends").select("*").eq("revision_id", rev["id"])
        .in_("id", ids).execute()
    ).data or []
    if len(rows) != len(set(ids)):
        raise ProposalSendError(
            "The revised proposals changed since you opened the dialog. Review again.", 404
        )
    return rows


def _fresh_inputs(sb, project_id: str) -> dict:
    """The live figures and drafts every freshness check compares against."""
    from app.routers.pricing import _get_one, _materials_rows, _verify_originals, section_summary

    verification = _get_one("verifications", project_id)
    if not verification or not verification.get("committed_at"):
        raise ProposalSendError(WAITING_ON_VERIFY_MESSAGE)
    drafts = {
        d["id"]: d
        for d in (
            sb.table("proposal_drafts").select("*").eq("project_id", project_id).execute()
        ).data
        or []
    }
    return {
        "default_amounts": proposal_send.proposal_amounts(
            _verify_originals(project_id), verification
        ),
        "includes_generator": section_summary(_materials_rows(project_id))["gear"][
            "includes_generator"
        ],
        "drafts": drafts,
        "latest_draft": max(drafts.values(), key=lambda d: d["created_at"], default=None),
    }


def send_revision_documents(
    project_id: str,
    revision_id: str,
    user_id: str,
    revision_send_ids: list[str],
    *,
    subject: str | None = None,
    email_body: str | None = None,
    force: bool = False,
    contacts: dict[str, list[str]] | None = None,
    extra_file_ids: list[str] | None = None,
    cc: dict[str, list[str]] | None = None,
) -> dict:
    """Email each GC its revised document, crash-safe like send_proposals:
    claim in SQL (revision locked and open; status 'sending' with the exact
    recipients, subject and body written first) -> the bid back at Submitted
    and today's figures, re-read for EACH GC -> isolation checks on the exact
    bytes -> send -> the one-transaction swap onto the live row. Per-GC
    independent failure."""
    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    rev = _require_open(sb, project_id, revision_id)
    _require_dispatch_head(sb, project_id)
    rows = _load_send_rows(sb, rev, revision_send_ids)
    the_subject = clean_subject(subject, project)
    body_template = email_body or proposal_send.build_revised_cover_email(
        project, rev["revision_number"]
    )[1]
    extra_attachments = proposal_send._prepare_extra_attachments(sb, project_id, extra_file_ids)
    extras_bytes = sum(len(b) for _, b in extra_attachments)
    live_gcs = {gc["id"]: gc for gc in proposal_send._project_gcs(project_id)}
    pending_ids = {
        gid for gid, gc in live_gcs.items() if gc.get("pricing_approval_status") == "pending"
    }
    all_names = proposal_send._all_project_gc_names(project_id)
    _fresh_inputs(sb, project_id)  # refuse up front while pricing is uncommitted

    results = []
    for row in rows:
        if not row.get("included"):
            results.append(_result(row, "skipped", "This GC is unchecked in this revision."))
            continue
        if row["status"] == "sent":
            results.append(_result(row, "skipped", None))
            continue
        if row["status"] == "pending":
            results.append(_result(row, "failed", "Generate the revised proposal first."))
            continue
        if row["gc_id"] in pending_ids:
            results.append(_result(row, "skipped", PENDING_APPROVAL_MESSAGE))
            continue
        if row["status"] == "sending":
            row = _reclaim_stuck(sb, row)
            if row["status"] in ("sent", "sending"):
                results.append(_result(row, row["status"], row.get("error")))
                continue
        if (row.get("error") or "").startswith(DELIVERED_PREFIX):
            results.append(_result(row, "failed", _RPC_MESSAGES["delivered"]))
            continue
        if (
            row["status"] == "failed"
            and (row.get("error") or "").startswith(OUTCOME_UNKNOWN_PREFIX)
            and not force
        ):
            results.append(_result(row, "failed", row["error"]))
            continue

        live_gc = live_gcs.get(row["gc_id"]) or {}
        recipients: list[str] = []
        gc_cc_addrs: list[str] = []
        cc_snapshot: list[dict] | None = None
        resolve_error: ProposalSendError | None = None
        try:
            recipients = proposal_send.resolve_recipients(live_gc, (contacts or {}).get(row["id"]))
            gc_cc_addrs, cc_snapshot = proposal_send.resolve_cc(
                live_gc, (cc or {}).get(row["id"]), recipients
            )
        except ProposalSendError as exc:
            resolve_error = exc

        body = body_template.replace(GC_NAME_TOKEN, row["gc_name"])
        try:
            claimed = _rpc(
                sb,
                "claim_proposal_revision_send",
                {
                    "p_revision_send_id": row["id"],
                    "p_user_id": user_id,
                    "p_gc_email": proposal_send.join_recipients(recipients) or None,
                    "p_subject": the_subject,
                    "p_body": body,
                    "p_cc": cc_snapshot,
                },
            )
        except ProposalSendError as exc:
            results.append(_result(row, "failed" if exc.code != "not_open" else "skipped", str(exc)))
            continue
        if not claimed:
            results.append(_result(row, "skipped", "claimed by another request"))
            continue
        before_claim = row
        row = claimed
        if proposal_send._gc_pending_now(sb, project_id, row["gc_id"]):
            sb.table("proposal_revision_sends").update(
                {"status": before_claim["status"], "error": before_claim.get("error")}
            ).eq("id", row["id"]).eq("status", "sending").execute()
            results.append(_result(before_claim, "skipped", PENDING_APPROVAL_MESSAGE))
            continue

        sent_ok = False
        try:
            if resolve_error is not None:
                raise resolve_error
            # Re-read right before this GC's email: an earlier GC in the batch
            # may have taken a while, and a cost edit meanwhile parks the bid
            # at Verify or moves the figures.
            _require_dispatch_head(sb, project_id)
            fresh = _fresh_inputs(sb, project_id)
            live_gc = {gc["id"]: gc for gc in proposal_send._project_gcs(project_id)}.get(
                row["gc_id"]
            ) or {}
            file_row = None
            if row.get("file_id"):
                file_row = (
                    sb.table("project_files").select("*").eq("id", row["file_id"])
                    .single().execute()
                ).data
            if not file_row:
                raise ProposalSendError("The revised document is missing. Generate it again.")
            docx_bytes = storage.download_file(file_row["storage_path"])
            draft = fresh["drafts"].get(row.get("draft_id"))
            latest = fresh["latest_draft"]
            if latest is not None and draft is not None and draft["id"] != latest["id"]:
                raise ProposalSendError("The scope lines changed. Generate the revised proposal again.")
            proposal_send.assert_send_isolation(
                row=row,
                file_row=file_row,
                docx_bytes=docx_bytes,
                recipients=recipients,
                live_gc=live_gc,
                project=project,
                draft=draft,
                other_gc_names=tuple(n for n in all_names if n != row["gc_name"]),
                expected_amounts=(
                    proposal_send.omit_zero_sections(
                        proposal_send.resolve_gc_amounts(fresh["default_amounts"], live_gc)
                    )
                    if live_gc
                    else None
                ),
                includes_generator=fresh["includes_generator"],
                revision_number=rev["revision_number"],
            )
            try:
                pdf_bytes = office_preview.convert_for_send(docx_bytes, file_row["filename"])
            except office_preview.ConversionError as exc:
                raise ProposalSendError(
                    f"Could not convert the revised proposal to PDF. Retry. ({exc})"
                ) from exc
            if len(pdf_bytes) + extras_bytes >= graph_email._INLINE_ATTACHMENT_LIMIT:
                raise ProposalSendError(
                    "Proposal PDF plus the extra attachments are too large to email. "
                    "Remove some files in Modify Files."
                    if extra_attachments
                    else "Proposal PDF is too large to email. Contact support."
                )
            stamp = proposal_send.stamped_amounts(row)
            proposal_send.validate_pdf_isolation(
                office_preview.extract_pdf_text(pdf_bytes),
                gc_name=row["gc_name"],
                scope_lines=draft.get("lines_json") or (),
                other_gc_names=tuple(n for n in all_names if n != row["gc_name"]),
                amounts=proposal_send.stamp_figures(stamp) if stamp is not None else (),
                **proposal_send.section_validation_kwargs(stamp, fresh["includes_generator"]),
            )
            cc_addrs = [*gc_cc_addrs, *proposal_send.cc_recipients([*recipients, *gc_cc_addrs])]
            try:
                log = graph_email.send_mail(
                    to=recipients,
                    cc=cc_addrs,
                    subject=the_subject,
                    body_html=email_branding.render_proposal_email(body),
                    attachments=[
                        (office_preview.pdf_filename(file_row["filename"]), pdf_bytes),
                        *extra_attachments,
                    ],
                    inline_images=[
                        (
                            email_branding.LOGO_CONTENT_ID,
                            email_branding.LOGO_FILENAME,
                            email_branding.logo_bytes(),
                            "image/jpeg",
                        )
                    ],
                    project_id=project_id,
                    sent_by=user_id,
                )
            except proposal_send._OUTCOME_UNKNOWN_EXC as exc:
                raise ProposalSendError(
                    f"{OUTCOME_UNKNOWN_PREFIX} ({type(exc).__name__}): check Sent Items "
                    "before retrying."
                ) from exc
            sent_ok = True
            try:
                done = _apply(
                    sb, row, user_id=user_id, via="email", email_log_id=log["id"],
                    sent_at=None, recipients=row.get("gc_email"), cc=cc_snapshot,
                    subject=the_subject,
                )
            except ProposalSendError as exc:
                # Refused for good: the GC has the email, the live row cannot
                # be moved. Terminal, never retried.
                failed = _mark_delivered_unrecorded(sb, row, str(exc))
                results.append(_result(failed, "failed", failed["error"]))
                continue
            audit(
                user_id, "proposal.revision_send", "proposal_revision_send", row["id"],
                {
                    "project_id": project_id, "revision_id": rev["id"],
                    "revision_number": rev["revision_number"], "gc_id": row["gc_id"],
                    "to": proposal_send.join_recipients(recipients),
                    "cc": proposal_send.join_recipients(cc_addrs) or None,
                    "subject": the_subject, "file_id": row["file_id"],
                    "extra_attachments": [n for n, _ in extra_attachments] or None,
                },
            )
            results.append(_result(done or row, "sent", None))
        except Exception as exc:  # noqa: BLE001 - isolate failures per GC
            message = str(exc)[:500]
            if sent_ok:
                # The email left and the swap could not even be attempted (a
                # transport error): the row stays 'sending', the reclaim
                # proves it from email_log and finishes it. Never 'failed',
                # a retry would email the GC twice.
                sb.table("proposal_revision_sends").update({"error": message}).eq(
                    "id", row["id"]
                ).eq("status", "sending").execute()
                logger.exception("revision send recorded late for gc %s", row["gc_id"])
                results.append(_result(row, "sending", message))
            else:
                sb.table("proposal_revision_sends").update(
                    {"status": "failed", "error": message}
                ).eq("id", row["id"]).eq("status", "sending").execute()
                audit(
                    user_id, "proposal.revision_send_failed", "proposal_revision_send",
                    row["id"], {"project_id": project_id, "gc_id": row["gc_id"], "error": message},
                )
                logger.exception("revision send failed for gc %s", row["gc_id"])
                results.append(_result(row, "failed", message))
        time.sleep(1)  # Exchange throttling courtesy

    failed = [r for r in results if r["status"] == "failed"]
    if failed:
        from app.core.roles import Role

        notify_role(
            Role.ESTIMATING_ADMIN, project_id, "proposal_send_failed",
            f"{len(failed)} revised proposal send(s) failed. Retry from Revise proposal.",
        )
    elif any(r["status"] == "sent" for r in results):
        dismiss_notifications(project_id=project_id, types=["proposal_send_failed"])
    maybe_auto_complete(project_id, user_id)
    return {"results": results}


def mark_revision_submitted(
    project_id: str, revision_id: str, user_id: str, revision_send_ids: list[str]
) -> dict:
    """Record revised proposals as submitted outside the app (portal upload
    etc.), mirroring Send Out's Mark as submitted: no email, sent_via
    'external' on the revision row, the same freshness gate a send runs, and
    the same swap (which locks the revision and requires it open)."""
    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    rev = _require_open(sb, project_id, revision_id)
    _require_dispatch_head(sb, project_id)
    rows = _load_send_rows(sb, rev, revision_send_ids)
    live_gcs = {gc["id"]: gc for gc in proposal_send._project_gcs(project_id)}
    fresh = _fresh_inputs(sb, project_id)
    subject = proposal_send.revised_cover_subject(project)

    results = []
    for row in rows:
        if not row.get("included"):
            results.append(_result(row, "skipped", "This GC is unchecked in this revision."))
            continue
        if row["status"] == "sent":
            results.append(_result(row, "skipped", None))
            continue
        if row["status"] == "sending":
            results.append(_result(row, "skipped", "an email send is in progress for this GC"))
            continue
        if row["status"] == "pending":
            results.append(_result(row, "failed", "Generate the revised proposal first."))
            continue
        live_gc = live_gcs.get(row["gc_id"])
        if live_gc is None:
            results.append(_result(row, "failed", f"{row['gc_name']} is no longer on this project."))
            continue
        if live_gc.get("pricing_approval_status") == "pending":
            results.append(_result(row, "skipped", PENDING_APPROVAL_MESSAGE))
            continue
        try:
            proposal_send.assert_mark_ready(
                row=row,
                draft=fresh["drafts"].get(row.get("draft_id")),
                latest_draft_id=fresh["latest_draft"]["id"] if fresh["latest_draft"] else None,
                expected_amounts=proposal_send.omit_zero_sections(
                    proposal_send.resolve_gc_amounts(fresh["default_amounts"], live_gc)
                ),
            )
            done = _apply(
                sb, row, user_id=user_id, via="external", email_log_id=None,
                sent_at=None, recipients=None, cc=None, subject=subject,
            )
        except ProposalSendError as exc:
            results.append(_result(row, "failed", str(exc)))
            continue
        if not done:
            results.append(_result(row, "skipped", "claimed by another request"))
            continue
        audit(
            user_id, "proposal.revision_mark_submitted", "proposal_revision_send", row["id"],
            {
                "project_id": project_id, "revision_id": rev["id"],
                "revision_number": rev["revision_number"], "gc_id": row["gc_id"],
                "file_id": row["file_id"],
            },
        )
        results.append(_result(done, "marked", None))
    if any(r["status"] == "marked" for r in results):
        dismiss_notifications(project_id=project_id, types=["proposal_send_failed"])
    maybe_auto_complete(project_id, user_id)
    return {"results": results}


def _result(row: dict, status: str, error: str | None) -> dict:
    return {
        "revision_send_id": row["id"],
        "gc_id": row["gc_id"],
        "gc_name": row["gc_name"],
        "status": status,
        "error": error,
    }


# ── complete / cancel ──────────────────────────────────────────────────────


def _initial_events(sb, revision_id: str) -> list[dict]:
    """Initial proposals of GCs added and bid while this revision was open."""
    return (
        sb.table("proposal_send_events")
        .select("id, gc_id, via, status, sent_at, file_id, proposal_send_id")
        .eq("proposal_revision_id", revision_id)
        .eq("kind", "initial")
        .eq("status", "sent")
        .execute()
    ).data or []


def _initial_in_progress(sb, project_id: str, rev: dict) -> bool:
    """A GC the revision is still waiting on for its initial proposal:
    linked to the project after the revision started with no sent proposal
    yet (even before anything was generated for it), or any unsent GC whose
    initial document was generated (or failed, or is sending) after the start."""
    on_project = _on_project(sb, project_id)
    at_start = set(rev.get("start_gc_ids") or [])
    sends = (
        sb.table("proposal_sends").select("gc_id, status, updated_at")
        .eq("project_id", project_id).execute()
    ).data or []
    by_gc = {r["gc_id"]: r for r in sends}
    started = rev.get("started_at") or ""
    for gc_id in on_project:
        live = by_gc.get(gc_id)
        status = live.get("status") if live else None
        if status == "sent":
            continue
        if gc_id not in at_start:
            return True
        if status in ("generated", "failed", "sending") and (live.get("updated_at") or "") >= started:
            return True
    return False


def maybe_auto_complete(project_id: str, user_id: str) -> bool:
    """Close the open revision on its own once every checked GC still on the
    project holds its revised proposal and no GC added during it is waiting
    on its initial one. A revision with nothing checked stays open (Done or
    Cancel decides it)."""
    sb = get_supabase()
    rev = open_revision(project_id, sb)
    if rev is None:
        return False
    on_project = _on_project(sb, project_id)
    rows = [r for r in _rows(sb, rev["id"]) if r.get("included") and r["gc_id"] in on_project]
    if not rows or any(r["status"] != "sent" for r in rows):
        return False
    if _initial_in_progress(sb, project_id, rev):
        return False
    try:
        return _finish(sb, project_id, rev, user_id, "auto")
    except ProposalSendError:
        return False  # a send claimed a row meanwhile; the next action retries


def _finish(sb, project_id: str, rev: dict, user_id: str, mode: str) -> bool:
    """Close the revision in SQL (finish_proposal_revision: revision locked,
    refused while any row is sending, Cancel refused once anything may have
    reached a GC), then tidy up: GCs whose revised document did not go out
    get their start-of-revision figures back, and never-sent documents are
    deleted, except a sending one or one whose outcome is unknown."""
    _rpc(
        sb,
        "finish_proposal_revision",
        {"p_revision_id": rev["id"], "p_user_id": user_id, "p_mode": mode},
    )
    for r in _rows(sb, rev["id"]):
        if r.get("status") == "sent" or r.get("status") == "sending" or _reached(r):
            continue
        _restore_overrides(sb, project_id, r)
        if r.get("file_id"):
            sb.table("proposal_revision_sends").update({"file_id": None}).eq(
                "id", r["id"]
            ).neq("status", "sending").execute()
            proposal_send._delete_file_row(r["file_id"])
    rows = _rows(sb, rev["id"])
    sent = [r for r in rows if r.get("status") == "sent"]
    audit(
        user_id,
        "proposal.revision_cancel" if mode == "cancel" else "proposal.revision_complete",
        "project",
        project_id,
        {
            "revision_id": rev["id"],
            "revision_number": rev["revision_number"],
            "completion": None if mode == "cancel" else ("auto" if mode == "auto" else "manual"),
            "sent_gcs": sorted(r["gc_name"] for r in sent if r.get("sent_via") == "email"),
            "external_gcs": sorted(r["gc_name"] for r in sent if r.get("sent_via") == "external"),
            "unresolved_gcs": sorted(r["gc_name"] for r in rows if _reached(r) and r["status"] != "sent"),
        },
    )
    return True


def complete_revision(project_id: str, revision_id: str, user_id: str) -> None:
    """Done: close the revision with what went out so far. GCs not sent
    their revised proposal keep the proposal (and figures) they hold."""
    sb = get_supabase()
    rev = _require_open(sb, project_id, revision_id)
    reclaim_stale(project_id, sb)
    _finish(sb, project_id, rev, user_id, "done")


def cancel_revision(project_id: str, revision_id: str, user_id: str) -> None:
    """Cancel: only while nothing in the revision went out (or may have).
    Pricing edits made during it to the cost side stay in place (not rolled
    back); per-GC figures go back to their start-of-revision values."""
    sb = get_supabase()
    rev = _require_open(sb, project_id, revision_id)
    reclaim_stale(project_id, sb)
    _finish(sb, project_id, rev, user_id, "cancel")


# ── overview (GET) ─────────────────────────────────────────────────────────


def _names(sb, ids) -> dict[str, str | None]:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = sb.table("profiles").select("id, full_name").in_("id", ids).execute().data or []
    return {r["id"]: r.get("full_name") for r in rows}


def _files(sb, ids) -> dict[str, dict]:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = (
        sb.table("project_files").select("id, filename, preview_status").in_("id", ids)
        .execute()
    ).data or []
    return {r["id"]: r for r in rows}


def _total(row: dict, prefix: str = "") -> str | None:
    values = [row.get(f"{prefix}{k}_amount") for k in AMOUNT_KEYS]
    if row.get(f"{prefix}material_amount") is None and row.get(f"{prefix}labor_amount") is None:
        return None
    return str(sum((Decimal(str(v)) for v in values if v is not None), Decimal(0)))


def _amounts_wire(row: dict, prefix: str = "") -> dict:
    out = {f"{k}_amount": (str(row[f"{prefix}{k}_amount"]) if row.get(f"{prefix}{k}_amount")
                           is not None else None) for k in AMOUNT_KEYS}
    out["total"] = _total(row, prefix)
    return out


def _stale_reason(row: dict, fresh: dict | None, live_gc: dict | None) -> str | None:
    """Why a generated revised document can no longer go out as is (the same
    checks the send runs), or None. Display only: the send re-checks."""
    if row.get("status") not in ("generated", "failed") or not row.get("file_id") or _reached(row):
        return None
    if fresh is None:
        return "waiting_on_verify"
    if live_gc is None:
        return "gc_removed"
    latest = fresh["latest_draft"]
    if latest is None or latest["id"] != row.get("draft_id") or not latest.get("approved_at"):
        return "lines_changed"
    if proposal_send.lines_hash(list(latest.get("lines_json") or [])) != row.get("lines_hash"):
        return "lines_changed"
    stamped = proposal_send.stamped_amounts(row)
    expected = proposal_send.omit_zero_sections(
        proposal_send.resolve_gc_amounts(fresh["default_amounts"], live_gc)
    )
    if stamped is not None and any(stamped.get(k) != expected.get(k) for k in AMOUNT_KEYS):
        return "amounts_changed"
    return None


def _row_state(row: dict) -> str:
    """One word the UI branches on: the row's status, with the two terminal
    failures named (outcome_unknown needs the force retry; delivered_unrecorded
    is never retried)."""
    error = row.get("error") or ""
    if row.get("status") == "failed" and error.startswith(DELIVERED_PREFIX):
        return "delivered_unrecorded"
    if row.get("status") == "failed" and error.startswith(OUTCOME_UNKNOWN_PREFIX):
        return "outcome_unknown"
    return row.get("status") or "pending"


def _figures_wire(final: dict | None) -> dict | None:
    """The ten resolved pricing figures plus the per-section prices and total
    they make (amounts_from_final), as strings."""
    if final is None:
        return None
    out = {k: (str(v) if v is not None else None) for k, v in final.items()}
    out["prices"] = {
        k: (str(v) if v is not None else None)
        for k, v in proposal_send.amounts_from_final(final).items()
    }
    return out


def _costs_wire(sb, project_id: str, rev: dict) -> dict:
    """The approval step: the project's cost figures committed at the start,
    the figures its quotes, selection, labor and markup give right now, the
    committed snapshot today, what each GC holds (its live proposal's per-GC
    prices: the real "held" numbers), and the approval state."""
    from app.routers.pricing import _verify_originals

    start = rev.get("start_verification")
    held = (
        proposal_send.resolved_verify_numbers({}, start)
        if start and start.get("committed_at")
        else None
    )
    try:
        current = proposal_send.resolved_verify_numbers(_verify_originals(project_id), {})
    except Exception:  # noqa: BLE001 - a display read must never fail the box
        logger.exception("revision costs: current figures failed for %s", project_id)
        current = None
    committed_row = _verification_row(sb, project_id)
    committed = (
        proposal_send.resolved_verify_numbers({}, committed_row)
        if committed_row and committed_row.get("committed_at")
        else None
    )
    names = _names(sb, [rev.get("costs_changed_by"), rev.get("costs_approved_by")])
    # What each GC in the revision actually holds today: the live
    # proposal_sends stamps (per-GC prices), not the project-level snapshot.
    live = {
        r["gc_id"]: r
        for r in (
            sb.table("proposal_sends").select("*").eq("project_id", project_id)
            .eq("status", "sent").execute()
        ).data
        or []
    }
    held_by_gc = []
    for row in _rows(sb, rev["id"]):
        sent = live.get(row["gc_id"])
        if not sent:
            continue
        held_by_gc.append(
            {
                "gc_id": row["gc_id"],
                "gc_name": row["gc_name"],
                "included": bool(row.get("included")),
                "revision_number": sent.get("revision_number") or 0,
                **_amounts_wire(sent),
            }
        )
    totals = [Decimal(g["total"]) for g in held_by_gc if g.get("total") is not None]
    return {
        "pending": costs_pending(rev),
        "changed_at": rev.get("costs_changed_at"),
        "changed_by_name": names.get(rev.get("costs_changed_by")),
        "change_reason": rev.get("costs_change_reason"),
        "approved_at": rev.get("costs_approved_at"),
        "approved_by_name": names.get(rev.get("costs_approved_by")),
        "approval_requested_at": rev.get("approval_requested_at"),
        # The project-level cost figures: committed when the revision
        # started, the live upstream figures now, and the snapshot today.
        "start": _figures_wire(held),
        "current": _figures_wire(current),
        "committed": _figures_wire(committed),
        # What the GCs hold (per-GC prices from their live proposals).
        "held_by_gc": held_by_gc,
        "held_total_range": (
            {"min": str(min(totals)), "max": str(max(totals))} if totals else None
        ),
    }


def _revision_files(sb, revision_id: str) -> list[dict]:
    """The revised plans, specs and addenda uploaded from the modal."""
    return (
        sb.table("project_files")
        .select("id, filename, category, doc_type, addendum_number, created_at, preview_status")
        .eq("proposal_revision_id", revision_id)
        .order("created_at")
        .execute()
    ).data or []


def overview(project_id: str) -> dict:
    """Everything the Revise proposal box shows: the open revision (with its
    per-GC rows, their documents and statuses, and the GCs added during it),
    the history of past revisions, and whether a new one can start. Resolves
    crashed sends first (reclaim_stale), so a stuck row never outlives
    SENDING_STALE_MINUTES while anyone has the box open."""
    sb = get_supabase()
    project = (
        sb.table("projects").select("id, name, number").eq("id", project_id).limit(1).execute()
    ).data or []
    if not project:
        raise ProposalSendError("Project not found", status_code=404)
    project = project[0]
    reclaim_stale(project_id, sb)
    revisions = (
        sb.table("proposal_revisions").select("*").eq("project_id", project_id)
        .order("revision_number", desc=True).execute()
    ).data or []
    rev_ids = [r["id"] for r in revisions]
    all_rows: list[dict] = []
    events: list[dict] = []
    if rev_ids:
        all_rows = (
            sb.table("proposal_revision_sends").select("*").in_("revision_id", rev_ids)
            .order("gc_name").execute()
        ).data or []
        events = (
            sb.table("proposal_send_events").select("*").in_("proposal_revision_id", rev_ids)
            .eq("kind", "initial").eq("status", "sent").execute()
        ).data or []
    live_sends = (
        sb.table("proposal_sends").select("*").eq("project_id", project_id).execute()
    ).data or []
    live_by_gc = {r["gc_id"]: r for r in live_sends}
    gc_names = {r["gc_id"]: r["gc_name"] for r in live_sends}
    names = _names(
        sb,
        [r.get(k) for r in revisions for k in ("started_by", "completed_by", "cancelled_by")]
        + [r.get(k) for r in all_rows for k in ("sent_by", "prior_sent_by")],
    )
    files = _files(
        sb,
        [r.get(k) for r in all_rows for k in ("file_id", "prior_file_id")]
        + [e.get("file_id") for e in events]
        + [r.get("file_id") for r in live_sends],
    )

    def row_wire(r: dict, fresh=None, live_gc=None, contacts=None, detail=False) -> dict:
        file = files.get(r.get("file_id")) or {}
        prior_file = files.get(r.get("prior_file_id")) or {}
        live = live_by_gc.get(r["gc_id"]) or {}
        live_file = files.get(live.get("file_id")) or {}
        stale_at = None
        if r.get("status") == "sending" and r.get("claimed_at"):
            try:
                claimed = datetime.fromisoformat(r["claimed_at"].replace("Z", "+00:00"))
                stale_at = (claimed + timedelta(minutes=SENDING_STALE_MINUTES)).isoformat()
            except ValueError:
                stale_at = None
        out = {
            "id": r["id"],
            "gc_id": r["gc_id"],
            "gc_name": r["gc_name"],
            "included": bool(r.get("included")),
            "status": r["status"],
            "state": _row_state(r),
            "error": r.get("error"),
            "file_id": r.get("file_id"),
            "filename": file.get("filename"),
            "preview_status": file.get("preview_status"),
            **_amounts_wire(r),
            "subject": r.get("subject"),
            "sent_at": r.get("sent_at"),
            "sent_via": r.get("sent_via"),
            "sent_by_name": names.get(r.get("sent_by")),
            # A row at 'sending': when the automatic crash check may resolve
            # it (null once resolved). The UI shows it instead of a dead end.
            "reclaim_after": stale_at,
            "prior": {
                "revision_number": r.get("prior_revision_number"),
                "file_id": r.get("prior_file_id"),
                "filename": prior_file.get("filename"),
                "sent_at": r.get("prior_sent_at"),
                "sent_via": r.get("prior_sent_via"),
                **_amounts_wire(r, "prior_"),
            }
            if r.get("status") == "sent"
            else None,
            # What the GC holds right now (the live proposal_sends row): the
            # original submission time, and when it was last revised.
            "current": {
                "revision_number": live.get("revision_number") or 0,
                "file_id": live.get("file_id"),
                "filename": live_file.get("filename"),
                "sent_at": live.get("sent_at"),
                "revised_at": live.get("revised_at"),
                "sent_via": live.get("sent_via"),
                **_amounts_wire(live),
            }
            if live
            else None,
        }
        if detail:
            out["stale_reason"] = _stale_reason(r, fresh, live_gc)
            out["on_project"] = live_gc is not None
            out["gc"] = contacts
        return out

    open_rev = next((r for r in revisions if r["status"] == "open"), None)
    open_wire = None
    if open_rev is not None:
        head, return_stage = _send_out_head(sb, project_id)
        try:
            fresh = _fresh_inputs(sb, project_id)
        except ProposalSendError:
            fresh = None
        live_gcs = {gc["id"]: gc for gc in proposal_send._project_gcs(project_id)}
        selected = proposal_send.selected_contacts_by_gc(project_id)
        rows = [r for r in all_rows if r["revision_id"] == open_rev["id"]]
        in_revision = {r["gc_id"] for r in rows}
        at_start = set(open_rev.get("start_gc_ids") or [])

        def contacts_for(gc_id: str) -> dict | None:
            gc = live_gcs.get(gc_id)
            if gc is None:
                return None
            contacts = sorted(gc.get("contacts") or [], key=lambda c: (c.get("name") or "").lower())
            live_ids = {c["id"] for c in contacts}
            return {
                "id": gc["id"],
                "name": gc["name"],
                "contacts": contacts,
                "selected_contact_ids": [c for c in selected.get(gc_id, []) if c in live_ids],
            }

        added = []
        for gc in sorted(live_gcs.values(), key=lambda g: g["name"].lower()):
            if gc["id"] in in_revision:
                continue
            live = live_by_gc.get(gc["id"])
            status = live["status"] if live and live["status"] != "superseded" else "not_generated"
            if status == "sent" and not any(
                e["gc_id"] == gc["id"] and e["proposal_revision_id"] == open_rev["id"]
                for e in events
            ):
                continue  # bid before this revision and not part of it
            added.append(
                {
                    "gc_id": gc["id"],
                    "gc_name": gc["name"],
                    "proposal_id": live["id"] if live and live["status"] != "superseded" else None,
                    "status": status,
                    "added_during_revision": gc["id"] not in at_start,
                    "pricing_approval_status": gc.get("pricing_approval_status"),
                }
            )
        default_subject, default_body = proposal_send.build_revised_cover_email(
            project, open_rev["revision_number"]
        )
        pending = any(
            gc.get("pricing_approval_status") == "pending" for gc in live_gcs.values()
        )
        sending = any(r["status"] == "sending" for r in rows)
        reached = any(_reached(r) for r in rows) or any(
            e["proposal_revision_id"] == open_rev["id"] for e in events
        )
        open_wire = {
            **{k: v for k, v in open_rev.items() if k != "start_verification"},
            "started_by_name": names.get(open_rev.get("started_by")),
            "costs": _costs_wire(sb, project_id, open_rev),
            "files": _revision_files(sb, open_rev["id"]),
            "head": head,
            "reverify_return_stage": return_stage,
            "waiting_on_verify": head == "verify",
            "pricing_committed": fresh is not None,
            "pricing_approvals_pending": pending,
            "gcs": [
                row_wire(r, fresh, live_gcs.get(r["gc_id"]), contacts_for(r["gc_id"]), True)
                for r in rows
            ],
            "added_gcs": added,
            "any_sending": sending,
            "can_cancel": not sending and not reached,
            "can_complete": not sending and reached,
            "default_subject": default_subject,
            "default_body": default_body,
            "gc_name_token": GC_NAME_TOKEN,
            "cc": proposal_send.cc_recipients([]),
        }

    history = []
    for rev in revisions:
        if rev["status"] == "open":
            continue
        rows = [r for r in all_rows if r["revision_id"] == rev["id"]]
        history.append(
            {
                **{k: v for k, v in rev.items() if k != "start_verification"},
                "started_by_name": names.get(rev.get("started_by")),
                "completed_by_name": names.get(rev.get("completed_by")),
                "cancelled_by_name": names.get(rev.get("cancelled_by")),
                "gcs": [row_wire(r) for r in rows],
                "initial_gcs": [
                    {
                        "gc_id": e["gc_id"],
                        "gc_name": gc_names.get(e["gc_id"]),
                        "via": e.get("via"),
                        "sent_at": e.get("sent_at"),
                        "file_id": e.get("file_id"),
                        "filename": (files.get(e.get("file_id")) or {}).get("filename"),
                    }
                    for e in events
                    if e.get("proposal_revision_id") == rev["id"]
                ],
            }
        )
    blocked = start_blocked_reason(project_id) if open_rev is None else None
    return {
        "open": open_wire,
        "history": history,
        "can_start": open_rev is None and blocked is None,
        "start_blocked_reason": blocked,
        "next_revision_number": (revisions[0]["revision_number"] + 1) if revisions else 1,
    }


def open_revision_numbers(project_ids: list[str], sb=None) -> dict[str, int]:
    """project_id -> number of its open revision, for the project wire's
    "Revising" badge. Never fails a project read: trouble (0178 not applied
    yet, a read hiccup) logs and answers no badges."""
    if not project_ids:
        return {}
    try:
        rows = (
            (sb if sb is not None else get_supabase())
            .table("proposal_revisions")
            .select("project_id, revision_number")
            .in_("project_id", list(project_ids))
            .eq("status", "open")
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001 - a badge, never a gate
        logger.exception("proposal revisions: open revision lookup failed")
        return {}
    return {r["project_id"]: r["revision_number"] for r in rows}

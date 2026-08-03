"""PM mailbox email ingestion + project identification pipeline.

A background loop polls the configured mailbox (EMAIL_INGEST_MAILBOX) — Inbox
AND Sent Items — via Graph delta queries, persists every message, and advances
each through the identification rounds:

  received -> id_r1 -> id_r2 -> id_r3 -> processed   (or failed)

`status` names the NEXT step to run, which is what makes a crash recoverable:
the every-tick worklist sweep (`process_pending`) simply resumes each email at
its recorded step. The delta token is persisted only after a folder's batch is
durably inserted, and `graph_message_id` is UNIQUE, so re-pulls after a crash
or a DeltaExpired reset dedup harmlessly (terminal rows are never re-run, so
no duplicate LLM spend).

Rounds:
  R1  conversation map lookup (email_conversation_projects) — free.
  R2  deterministic subject match against ALL projects (email_match.r2_match).
  R3  OpenAI subject-only match with a confidence threshold; below-threshold
      guesses are stored as suggested_project_id for the triage UI. When the
      provider is out of credits, attempts are NOT burned: the row waits
      (next_attempt_at) and IT_ADMIN is notified once (deduped).

Every terminal write also stamps `pipeline_round` (fetch/r1/r2/r3/manual/
retro/rescan) — the step that actually decided the email. `status` can't say
it (a failure from any step reads 'failed') and neither can `matched_by`
(the retro-assign writes 'conversation', the rescan writes 'subject'/'llm'),
so this is the column the UI reads to show which round identified a message.

Learn-back: every assignment upserts the conversation map (manual outranks
auto — enforced by a conditional UPDATE, not just a read-check), manual
assignment retro-assigns the rest of the conversation, and project creation
triggers `rescan_unknown_for_project` (bid invites often arrive before the
project exists in BDR).

Multi-worker safety: a `graph_sync_state` lease row (`pm-mail:{mailbox}:lease`)
with a per-process holder token serializes the delta ingest and the sweep.
The holder renews the lease between folders and every few sweep rows, and
aborts if another runner has taken it — so a long sweep (backlog after
downtime) can't be double-processed. Assignment writes are additionally
guarded with project_id-is-null conditions, and the attachments table has a
(email_id, graph_attachment_id) unique index, so even a pathological overlap
cannot double-assign or duplicate rows.
"""

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import email_match, graph_inbox, llm, openai_text, storage
from app.services.llm_errors import is_out_of_tokens, user_message
from app.services.notifications import audit, notify_role

logger = logging.getLogger(__name__)

_SYNC_PREFIX = "pm-mail"
_FOLDERS = ("inbox", "sentitems")
_TERMINAL = ("processed", "failed")
_PENDING = ("received", "id_r1", "id_r2", "id_r3")
# `pipeline_round` (0084): which step decided the email, recorded on every
# terminal write because `status` collapses to 'failed' from any step and
# `matched_by` can't tell a live round from a retro-assign/rescan. Since
# `status` names the step ABOUT to run, an email's status IS the round it is
# on — so this map also names the round a failure happened in.
_ROUND_BY_STATUS = {
    "received": "fetch",
    "id_r1": "r1",
    "id_r2": "r2",
    "id_r3": "r3",
}
_SWEEP_BATCH = 200
_RENEW_EVERY = 20            # sweep rows between lease renewals
_PAGE = 1000                 # PostgREST pagination page size
_NOTIFY_DEDUP_HOURS = 6
# Exponential retry backoff for transient failures: 5 min → 6 h cap.
_BACKOFF_BASE_SECONDS = 300
_BACKOFF_CAP_SECONDS = 21600

# Per-process lease holder token: lets this runner renew/re-acquire its own
# lease while any other runner's renewal fails closed.
_RUNNER_TOKEN = uuid.uuid4().hex

_DELTA_SELECT = (
    "id,conversationId,internetMessageId,from,toRecipients,ccRecipients,"
    "subject,bodyPreview,receivedDateTime,sentDateTime,hasAttachments"
)
_FETCH_SELECT = "id,body,bodyPreview,hasAttachments"

_SWEEP_SELECT = (
    "id, mailbox, folder, direction, graph_message_id, conversation_id, "
    "from_address, subject, status, attempts, has_attachments, project_id"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def polling_loop() -> None:
    interval = get_settings().email_ingest_poll_interval_seconds
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:  # noqa: BLE001 — the loop must survive any tick failure
            logger.exception("Email ingest poll failed")
        await asyncio.sleep(interval)


def poll_once() -> None:
    settings = get_settings()
    mailbox = (settings.email_ingest_mailbox or "").strip().lower()
    if not mailbox or not settings.ms_client_id:
        return
    sb = get_supabase()

    lease_key = f"{_SYNC_PREFIX}:{mailbox}:lease"
    if not _acquire_lease(sb, lease_key):
        return

    for folder in _FOLDERS:
        try:
            _sync_folder(sb, mailbox, folder)
        except Exception:  # noqa: BLE001 — one folder failing must not stall the other
            logger.exception("Email ingest delta sync failed for %s/%s", mailbox, folder)
        if not _renew_lease(sb, lease_key):
            return  # lease stolen (we stalled too long) — stand down this tick

    process_pending(sb, lease_key=lease_key)


def _lease_until() -> str:
    settings = get_settings()
    return (
        _now() + timedelta(seconds=2 * settings.email_ingest_poll_interval_seconds)
    ).isoformat()


def _acquire_lease(sb, key: str) -> bool:
    """Fenced single-runner lease. Acquire when the lease is free, expired, or
    already ours (a runner may re-acquire its own live lease — no self-lockout
    between ticks). The conditional UPDATE makes theft between read and write
    impossible; the unique pk makes the first-insert race safe."""
    now_iso = _now().isoformat()
    rows = (sb.table("graph_sync_state").select("id").eq("id", key).execute()).data
    if not rows:
        try:
            sb.table("graph_sync_state").insert(
                {"id": key, "lease_until": _lease_until(), "holder": _RUNNER_TOKEN}
            ).execute()
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_unique_violation(exc):
                return False  # another runner created it first
            raise
    resp = (
        sb.table("graph_sync_state")
        .update(
            {"lease_until": _lease_until(), "holder": _RUNNER_TOKEN,
             "updated_at": now_iso}
        )
        .eq("id", key)
        .or_(
            f"lease_until.is.null,lease_until.lt.{now_iso},holder.eq.{_RUNNER_TOKEN}"
        )
        .execute()
    )
    return bool(resp.data)


def _renew_lease(sb, key: str) -> bool:
    """Extend our own lease; fails closed if another runner holds it now."""
    resp = (
        sb.table("graph_sync_state")
        .update({"lease_until": _lease_until(), "updated_at": _now().isoformat()})
        .eq("id", key)
        .eq("holder", _RUNNER_TOKEN)
        .execute()
    )
    if not resp.data:
        logger.warning("Email ingest lease %s lost mid-tick; aborting", key)
        return False
    return True


def _sync_folder(sb, mailbox: str, folder: str) -> None:
    """Pull the folder's delta and insert raw rows (status 'received').

    The new delta token is persisted ONLY when every message either inserted
    or was a known duplicate — a genuine insert failure leaves the old token
    so the next tick re-pulls the batch (already-inserted rows dedup).
    """
    settings = get_settings()
    key = f"{_SYNC_PREFIX}:{mailbox}:{folder}"
    rows = (sb.table("graph_sync_state").select("*").eq("id", key).execute()).data
    delta_link = rows[0].get("delta_link") if rows else None

    try:
        messages, new_delta = graph_inbox.delta_inbox(
            delta_link,
            mailbox=mailbox,
            folder=folder,
            since_days=settings.email_ingest_lookback_days,
            select=_DELTA_SELECT,
        )
    except graph_inbox.DeltaExpired:
        messages, new_delta = graph_inbox.delta_inbox(
            None,
            mailbox=mailbox,
            folder=folder,
            since_days=settings.email_ingest_reset_lookback_days,
            select=_DELTA_SELECT,
        )

    batch_failed = False
    for msg in messages:
        try:
            _insert_from_delta(sb, mailbox, folder, msg)
        except Exception:  # noqa: BLE001 — isolate; the batch flag re-pulls it next tick
            logger.exception("Failed to persist message %s", msg.get("id"))
            batch_failed = True

    if batch_failed:
        return
    sb.table("graph_sync_state").upsert(
        {"id": key, "delta_link": new_delta, "updated_at": _now().isoformat()}
    ).execute()


def _addr(entry: dict | None) -> tuple[str | None, str | None]:
    email = ((entry or {}).get("emailAddress")) or {}
    return email.get("name"), email.get("address")


def _recipients(entries: list | None) -> list[dict]:
    out = []
    for r in entries or []:
        name, address = _addr(r)
        if address or name:
            out.append({"name": name, "address": address})
    return out


def _insert_from_delta(sb, mailbox: str, folder: str, msg: dict) -> None:
    if "@removed" in msg or not msg.get("id"):
        return  # delta tombstone / bare change marker
    from_name, from_address = _addr(msg.get("from"))
    if folder == "inbox" and (from_address or "").lower() == mailbox:
        # Self-sent mail: the Sent Items copy is the record (self-conversation
        # dedup — otherwise a mail to yourself would ingest twice).
        return

    existing = (
        sb.table("ingested_emails")
        .select("id")
        .eq("graph_message_id", msg["id"])
        .execute()
    ).data
    if existing:
        return

    direction = "inbound" if folder == "inbox" else "outbound"
    message_at = (
        msg.get("sentDateTime") or msg.get("receivedDateTime")
        if direction == "outbound"
        else msg.get("receivedDateTime") or msg.get("sentDateTime")
    )
    try:
        sb.table("ingested_emails").insert(
            {
                "mailbox": mailbox,
                "folder": folder,
                "direction": direction,
                "graph_message_id": msg["id"],
                "internet_message_id": msg.get("internetMessageId"),
                "conversation_id": msg.get("conversationId"),
                "from_name": from_name,
                "from_address": from_address,
                "to_recipients": _recipients(msg.get("toRecipients")),
                "cc_recipients": _recipients(msg.get("ccRecipients")),
                "subject": msg.get("subject"),
                "body_preview": msg.get("bodyPreview"),
                "message_at": message_at,
                "has_attachments": bool(msg.get("hasAttachments")),
                "status": "received",
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            return  # lost a race with another runner — the row exists, fine
        raise


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text


# ── Pipeline sweep ─────────────────────────────────────────────────────────────


def process_pending(sb, lease_key: str | None = None) -> None:
    """Advance every non-terminal email whose backoff gate has passed. Runs
    every tick — this is crash recovery and retry in one query. Renews the
    runner lease periodically and aborts if another runner took it."""
    now_iso = _now().isoformat()
    rows = (
        sb.table("ingested_emails")
        .select(_SWEEP_SELECT)
        .in_("status", list(_PENDING))
        .or_(f"next_attempt_at.is.null,next_attempt_at.lte.{now_iso}")
        .order("created_at", desc=False)
        .limit(_SWEEP_BATCH)
        .execute()
    ).data or []

    allow_llm = True
    for i, email in enumerate(rows):
        if lease_key and i and i % _RENEW_EVERY == 0 and not _renew_lease(sb, lease_key):
            return
        try:
            outcome = _process_email(sb, email, allow_llm=allow_llm)
        except Exception:  # noqa: BLE001 — one poison row must not stall the sweep
            logger.exception("Email pipeline failed for %s", email.get("id"))
            continue
        if outcome == "llm_down":
            # Provider out of credits: stop burning R3 calls this tick. Rows
            # before R3 still advance (R1/R2 are free).
            allow_llm = False


def _process_email(sb, email: dict, *, allow_llm: bool = True) -> str | None:
    """Advance one email as far as it can go this tick. Returns 'llm_down'
    when the LLM provider is out of credits."""
    status = email["status"]
    while status not in _TERMINAL:
        if status == "received":
            status = _step_received(sb, email)
        elif status == "id_r1":
            status = _step_r1(sb, email)
        elif status == "id_r2":
            status = _step_r2(sb, email)
        elif status == "id_r3":
            if not allow_llm:
                return None
            status, llm_down = _step_r3(sb, email)
            if llm_down:
                return "llm_down"
        else:  # unknown status — leave it alone
            return None
        if status is None:  # CAS miss (manual assign won) or retry scheduled
            return None
    return None


def _cas(sb, email_id: str, expected_status: str, fields: dict,
         *, only_unassigned: bool = False) -> bool:
    """Compare-and-set on status so the sweep never clobbers a concurrent
    manual assignment. `only_unassigned` additionally requires project_id to
    still be null — used by every write that sets or finalizes an assignment.
    Returns True when the row was ours to move."""
    q = (
        sb.table("ingested_emails")
        .update(fields)
        .eq("id", email_id)
        .eq("status", expected_status)
    )
    if only_unassigned:
        q = q.is_("project_id", "null")
    return bool(q.execute().data)


def _finalize_if_assigned(sb, email_id: str, expected_status: str) -> bool:
    """A pipeline step lost its assignment CAS because the email already got a
    project (manual assign / retro-assign while mid-pipeline). Close it out as
    processed WITHOUT touching the assignment fields."""
    resp = (
        sb.table("ingested_emails")
        .update(
            {
                "status": "processed",
                "processed_at": _now().isoformat(),
                "error": None,
                "next_attempt_at": None,
            }
        )
        .eq("id", email_id)
        .eq("status", expected_status)
        .not_.is_("project_id", "null")
        .execute()
    )
    return bool(resp.data)


def _retry_or_fail(sb, email: dict, expected_status: str, exc: Exception) -> None:
    """Transient failure: exponential backoff; terminal after max attempts
    (the email still surfaces in the Unknown pool for manual triage)."""
    settings = get_settings()
    attempts = int(email.get("attempts") or 0) + 1
    error = str(exc)[:500]
    if attempts >= settings.email_match_max_attempts:
        if _cas(sb, email["id"], expected_status, {
            "status": "failed",
            "attempts": attempts,
            "error": error,
            "next_attempt_at": None,
            "processed_at": _now().isoformat(),
            # Terminal: pin the round it died in, which 'failed' would erase.
            "pipeline_round": _ROUND_BY_STATUS.get(expected_status),
        }):
            _notify_once(
                sb,
                "email_ingest.match_failed",
                "An ingested email failed processing after repeated attempts "
                "and needs manual triage in the Unknown emails page.",
            )
        return
    delay = min(_BACKOFF_BASE_SECONDS * (4 ** (attempts - 1)), _BACKOFF_CAP_SECONDS)
    _cas(sb, email["id"], expected_status, {
        "attempts": attempts,
        "error": error,
        "next_attempt_at": (_now() + timedelta(seconds=delay)).isoformat(),
    })


def _step_received(sb, email: dict) -> str | None:
    """Fetch the full plain-text body (+ attachments), then advance to R1."""
    settings = get_settings()
    try:
        full = graph_inbox.get_message(
            email["graph_message_id"],
            mailbox=email["mailbox"],
            select=_FETCH_SELECT,
            body_type="text",
        )
        if email.get("has_attachments"):
            _ingest_attachments(sb, email)
    except Exception as exc:  # noqa: BLE001
        _retry_or_fail(sb, email, "received", exc)
        return None

    body = ((full.get("body") or {}).get("content")) or ""
    truncated = len(body) > settings.email_body_max_chars
    if truncated:
        body = body[: settings.email_body_max_chars]
    ok = _cas(sb, email["id"], "received", {
        "body_text": body,
        "body_truncated": truncated,
        "status": "id_r1",
        "error": None,
        "next_attempt_at": None,
    })
    return "id_r1" if ok else None


def _attachment_path(email_id: str, att_id: str | None, filename: str) -> str:
    """Deterministic per-(email, attachment) object path so a retry or an
    overlapping runner overwrites (upsert) instead of orphaning new objects
    under fresh random names. Email-namespaced (NOT project-namespaced): the
    assignment can change after upload and the object must not move."""
    digest = hashlib.sha1((att_id or filename).encode()).hexdigest()[:16]
    return f"emails/{email_id}/{digest}-{filename.replace('/', '_')}"


def _ingest_attachments(sb, email: dict) -> None:
    """Store file attachments (metadata-only rows for skipped ones).
    Idempotent three ways: skip when rows exist, deterministic object paths
    with upsert, and duplicate-ignoring insert against the
    (email_id, graph_attachment_id) unique index."""
    existing = (
        sb.table("ingested_email_attachments")
        .select("id")
        .eq("email_id", email["id"])
        .limit(1)
        .execute()
    ).data
    if existing:
        return
    settings = get_settings()
    fetched, skipped = graph_inbox.list_attachments(
        email["graph_message_id"],
        mailbox=email["mailbox"],
        max_count=settings.inbound_attachment_max_count,
        max_bytes=settings.inbound_attachment_max_bytes,
    )
    import base64

    rows: list[dict] = []
    for att in fetched:
        name = att.get("name") or "attachment"
        content = base64.b64decode(att.get("contentBytes") or "")
        path = _attachment_path(email["id"], att.get("id"), name)
        storage.upload_file(
            path, content, att.get("contentType") or "application/octet-stream",
            upsert=True,
        )
        rows.append(
            {
                "email_id": email["id"],
                "graph_attachment_id": att.get("id"),
                "filename": name,
                "mime_type": att.get("contentType"),
                "size_bytes": len(content),
                "storage_path": path,
            }
        )
    for att in skipped:
        rows.append(
            {
                "email_id": email["id"],
                "graph_attachment_id": att.get("id"),
                "filename": att.get("name") or "attachment",
                "mime_type": att.get("contentType"),
                "size_bytes": att.get("size"),
                "storage_path": None,
                "skipped_reason": att.get("reason"),
            }
        )
    if rows:
        sb.table("ingested_email_attachments").upsert(
            rows, on_conflict="email_id,graph_attachment_id", ignore_duplicates=True
        ).execute()


def _step_r1(sb, email: dict) -> str | None:
    """Conversation-map lookup — a free indexed read.

    First, the submittal-response check: a vendor reply to a request we sent from
    this mailbox threads on the same conversationId. Recognizing it here (before
    the generic map) both flags the send as received and lets the normal _assign
    path file the email to the request's project and teach the map. The outbound
    Sent-Items copy shares the conversationId, so it too gets assigned — but the
    inbound + sender guards keep it from being counted as a reply.
    """
    conversation_id = email.get("conversation_id")
    if conversation_id:
        from app.services import submittal_ingest

        send = submittal_ingest.match_send(sb, conversation_id)
        if send and send.get("project_id"):
            if email.get("direction") == "inbound" and submittal_ingest.is_from_contact(email, send):
                submittal_ingest.record_response(sb, send, email)
            outcome = _assign(
                sb,
                email,
                send["project_id"],
                matched_by="conversation",
                expected_status="id_r1",
                update_map=True,
            )
            return "processed" if outcome else None

        hit = (
            sb.table("email_conversation_projects")
            .select("project_id")
            .eq("mailbox", email["mailbox"])
            .eq("conversation_id", conversation_id)
            .execute()
        ).data
        if hit:
            outcome = _assign(
                sb,
                email,
                hit[0]["project_id"],
                matched_by="conversation",
                expected_status="id_r1",
                update_map=False,  # the map already knows this conversation
            )
            return "processed" if outcome else None
    return "id_r2" if _cas(sb, email["id"], "id_r1", {"status": "id_r2"}) else None


def _step_r2(sb, email: dict) -> str | None:
    """Deterministic subject match against all projects."""
    projects = _all_projects(sb)
    project_id = email_match.r2_match(email.get("subject") or "", projects)
    if project_id:
        outcome = _assign(
            sb, email, project_id, matched_by="subject", expected_status="id_r2"
        )
        return "processed" if outcome else None
    return "id_r3" if _cas(sb, email["id"], "id_r2", {"status": "id_r3"}) else None


def _step_r3(sb, email: dict) -> tuple[str | None, bool]:
    """LLM subject-only match. Returns (next_status, llm_down)."""
    settings = get_settings()
    if not llm.is_configured("email_match", settings):
        # R3 disabled without a configured model — finalize as Unknown; manual
        # triage (and the R1 learn-back it feeds) still works.
        return _finalize_r3(sb, email, {}), False

    projects = _all_projects(sb)
    candidates = email_match.prefilter_candidates(
        email.get("subject") or "", projects, settings.email_match_llm_max_candidates
    )
    try:
        result = openai_text.match_subject_to_project(
            email.get("subject") or "", candidates
        )
    except Exception as exc:  # noqa: BLE001
        if is_out_of_tokens(exc):
            # Don't burn attempts on a dead account: wait out the outage and
            # tell IT once (deduped). The row resumes when credits return.
            _cas(sb, email["id"], "id_r3", {
                "error": user_message(exc, llm.active_model("email_match", settings)),
                "next_attempt_at": (
                    _now() + timedelta(seconds=settings.email_llm_outage_retry_seconds)
                ).isoformat(),
            })
            _notify_once(
                sb,
                "email_ingest.llm_outage",
                user_message(exc, llm.active_model("email_match", settings)),
            )
            return None, True
        _retry_or_fail(sb, email, "id_r3", exc)
        return None, False

    idx = result.get("candidate_index")
    confidence = _safe_float(result.get("confidence"))
    valid = isinstance(idx, int) and 0 <= idx < len(candidates)
    if valid and confidence >= settings.email_match_confidence_threshold:
        outcome = _assign(
            sb,
            email,
            candidates[idx]["id"],
            matched_by="llm",
            expected_status="id_r3",
            confidence=confidence,
            model=llm.active_model("email_match", settings),
        )
        return ("processed" if outcome else None), False

    # No confident match → Unknown pool; keep the below-threshold guess so the
    # triage UI can offer a one-click accept.
    fields: dict = {"match_model": llm.active_model("email_match", settings)}
    if valid:
        fields["suggested_project_id"] = candidates[idx]["id"]
        fields["suggested_confidence"] = round(confidence, 3)
    return _finalize_r3(sb, email, fields), False


def _finalize_r3(sb, email: dict, extra_fields: dict) -> str | None:
    """Close an R3 row out as Unknown — unless a concurrent manual assignment
    landed, in which case just mark it processed without touching it."""
    fields = {
        "status": "processed",
        "processed_at": _now().isoformat(),
        "next_attempt_at": None,
        "error": None,
        # Reached the end of the rounds without a confident match — R3 is where
        # this email gave up, and the Unknown pool is where it landed.
        "pipeline_round": "r3",
        **extra_fields,
    }
    if _cas(sb, email["id"], "id_r3", fields, only_unassigned=True):
        return "processed"
    if _finalize_if_assigned(sb, email["id"], "id_r3"):
        return "processed"
    return None


def _safe_float(value) -> float:
    """Model-reported confidence, clamped to [0, 1] — the column is
    numeric(4,3) and a rogue '10' from the model must not overflow it."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out:  # NaN
        return 0.0
    return min(max(out, 0.0), 1.0)


def _all_projects(sb) -> list[dict]:
    """Every project (bidding + PM + completed + abandoned) — an email about an
    abandoned bid still belongs to it. Paginated past PostgREST's row cap."""
    out: list[dict] = []
    start = 0
    while True:
        page = (
            sb.table("projects")
            .select("id, number, name")
            .order("created_at", desc=False)
            .range(start, start + _PAGE - 1)
            .execute()
        ).data or []
        out.extend(page)
        if len(page) < _PAGE:
            return out
        start += _PAGE


# ── Assignment (the single choke point) ───────────────────────────────────────


def _assign(
    sb,
    email: dict,
    project_id: str,
    *,
    matched_by: str,
    expected_status: str,
    confidence: float | None = None,
    model: str | None = None,
    update_map: bool = True,
) -> bool:
    """Automatic assignment. The write is guarded on BOTH the expected status
    and project_id still being null; when the guard misses because a manual
    assignment landed first, the row is finalized untouched (the manual
    decision wins — including its pipeline_round) and the map is NOT taught our
    candidate."""
    fields = {
        "project_id": project_id,
        "matched_by": matched_by,
        "match_confidence": round(confidence, 3) if confidence is not None else None,
        "match_model": model,
        "status": "processed",
        "processed_at": _now().isoformat(),
        "error": None,
        "next_attempt_at": None,
        # The live round is the step we were on — never 'retro'/'rescan',
        # which assign outside the sweep.
        "pipeline_round": _ROUND_BY_STATUS.get(expected_status),
    }
    if _cas(sb, email["id"], expected_status, fields, only_unassigned=True):
        if update_map and email.get("conversation_id"):
            _upsert_conversation_map(
                sb, email["mailbox"], email["conversation_id"], project_id,
                source=matched_by if matched_by in ("subject", "llm", "manual") else "llm",
            )
        return True
    return _finalize_if_assigned(sb, email["id"], expected_status)


def _upsert_conversation_map(
    sb,
    mailbox: str,
    conversation_id: str,
    project_id: str,
    *,
    source: str,
    created_by: str | None = None,
) -> None:
    """Insert-or-update the learn-back map. Manual always wins: an automatic
    source can never overwrite a manual row — enforced with a conditional
    UPDATE (.neq source manual), not just the read-check, so a manual
    assignment landing mid-flight cannot be clobbered."""
    existing = (
        sb.table("email_conversation_projects")
        .select("id, source")
        .eq("mailbox", mailbox)
        .eq("conversation_id", conversation_id)
        .execute()
    ).data
    if existing:
        if existing[0]["source"] == "manual" and source != "manual":
            return
        q = (
            sb.table("email_conversation_projects")
            .update({"project_id": project_id, "source": source, "created_by": created_by})
            .eq("id", existing[0]["id"])
        )
        if source != "manual":
            q = q.neq("source", "manual")  # empty result = a manual write won the race
        q.execute()
        return
    try:
        sb.table("email_conversation_projects").insert(
            {
                "mailbox": mailbox,
                "conversation_id": conversation_id,
                "project_id": project_id,
                "source": source,
                "created_by": created_by,
            }
        ).execute()
    except Exception as exc:  # noqa: BLE001 — unique race with another runner
        if not _is_unique_violation(exc):
            raise


# ── Manual triage (called from the router) ────────────────────────────────────


def _needs_content_fetch(email: dict) -> bool:
    """True when the email never completed the 'received' step — its body and
    attachments were never pulled from Graph (e.g. it exhausted fetch attempts
    during an outage and sits 'failed' with a null body)."""
    if email.get("status") == "received":
        return True
    return email.get("status") == "failed" and email.get("body_text") is None


def assign_manual(sb, email: dict, project_id: str, user_id: str) -> tuple[dict, int]:
    """Manually assign an email to a project. Works from ANY status (a manual
    decision short-circuits a mid-pipeline or failed email), teaches the
    conversation map, and retro-assigns the rest of the conversation.

    An email whose content was never fetched (died at 'received') keeps the
    fetch obligation: it goes back to status 'received' with fresh attempts so
    the sweep pulls its body/attachments; the finalize-if-assigned guard then
    closes it out without touching this assignment.

    Returns (updated_email_row, retro_assigned_count).
    """
    now_iso = _now().isoformat()
    fields = {
        "project_id": project_id,
        "matched_by": "manual",
        "match_confidence": None,
        "match_model": None,
        "suggested_project_id": None,
        "suggested_confidence": None,
        "assigned_by": user_id,
        "assigned_at": now_iso,
        "error": None,
        "next_attempt_at": None,
        "pipeline_round": "manual",
    }
    if _needs_content_fetch(email):
        fields.update({"status": "received", "attempts": 0})
    else:
        fields.update({"status": "processed", "processed_at": now_iso})
    updated = (
        sb.table("ingested_emails").update(fields).eq("id", email["id"]).execute()
    ).data[0]

    retro_count = 0
    if email.get("conversation_id"):
        _upsert_conversation_map(
            sb,
            email["mailbox"],
            email["conversation_id"],
            project_id,
            source="manual",
            created_by=user_id,
        )
        retro_count = _retro_assign_conversation(sb, email, project_id, now_iso)

    audit(
        user_id,
        "email.assign",
        "ingested_email",
        email["id"],
        {"project_id": project_id, "retro_assigned": retro_count},
    )
    return updated, retro_count


def _retro_assign_conversation(sb, email: dict, project_id: str, now_iso: str) -> int:
    """File every UNASSIGNED sibling of the conversation to the project —
    never stealing an email already filed elsewhere. Three cases:
    - terminal siblings with fetched content → processed;
    - siblings that never fetched content (failed-at-received) → revived to
      'received' with fresh attempts so the sweep pulls their bodies;
    - mid-pipeline siblings → assignment fields only; their in-flight step's
      guarded CAS misses and the finalize path closes them out.
    """
    def scoped(q):
        return (
            q.eq("mailbox", email["mailbox"])
            .eq("conversation_id", email["conversation_id"])
            .is_("project_id", "null")
            .neq("id", email["id"])
        )

    assign_fields = {
        "project_id": project_id,
        "matched_by": "conversation",
        "match_confidence": None,
        "match_model": None,
        "suggested_project_id": None,
        "suggested_confidence": None,
        "error": None,
        "next_attempt_at": None,
        # Not R1: these siblings never ran a round: they rode along on someone
        # else's manual decision.
        "pipeline_round": "retro",
    }
    # Terminal siblings whose content was fetched → fully processed.
    done = scoped(
        sb.table("ingested_emails").update(
            {**assign_fields, "status": "processed", "processed_at": now_iso}
        )
    ).in_("status", list(_TERMINAL)).not_.is_("body_text", "null").execute().data or []
    # Siblings that never fetched content → revive the fetch obligation.
    revived = scoped(
        sb.table("ingested_emails").update(
            {**assign_fields, "status": "received", "attempts": 0}
        )
    ).in_("status", ["received", "failed"]).is_("body_text", "null").execute().data or []
    # Mid-pipeline siblings → assignment only; the pipeline closes them out.
    pending = scoped(
        sb.table("ingested_emails").update(assign_fields)
    ).in_("status", ["id_r1", "id_r2", "id_r3"]).execute().data or []
    return len(done) + len(revived) + len(pending)


def unassign(sb, email: dict, user_id: str) -> dict:
    """Return an email to the Unknown pool, cleaning up the conversation map
    unless a sibling email still legitimately holds the mapping."""
    removed_project = email.get("project_id")
    updated = (
        sb.table("ingested_emails")
        .update(
            {
                "project_id": None,
                "matched_by": None,
                "match_confidence": None,
                "match_model": None,
                "assigned_by": None,
                "assigned_at": None,
                "status": "processed",
                "error": None,
                "next_attempt_at": None,
                # The decision is revoked, so the round that made it no longer
                # describes the row — cleared alongside matched_by.
                "pipeline_round": None,
            }
        )
        .eq("id", email["id"])
        .execute()
    ).data[0]

    conversation_id = email.get("conversation_id")
    if removed_project and conversation_id:
        map_rows = (
            sb.table("email_conversation_projects")
            .select("id, project_id")
            .eq("mailbox", email["mailbox"])
            .eq("conversation_id", conversation_id)
            .execute()
        ).data
        if map_rows and map_rows[0]["project_id"] == removed_project:
            siblings = (
                sb.table("ingested_emails")
                .select("id")
                .eq("mailbox", email["mailbox"])
                .eq("conversation_id", conversation_id)
                .eq("project_id", removed_project)
                .neq("id", email["id"])
                .limit(1)
                .execute()
            ).data
            if not siblings:
                # Nobody in the conversation is on this project anymore — drop
                # the mapping, or R1 would re-assign this email next tick.
                sb.table("email_conversation_projects").delete().eq(
                    "id", map_rows[0]["id"]
                ).execute()

    audit(user_id, "email.unassign", "ingested_email", email["id"],
          {"project_id": removed_project})
    return updated


# ── New-project rescan of the Unknown pool ────────────────────────────────────


def rescan_unknown_for_project(project_id: str) -> None:
    """Re-run identification of Unknown emails against ONE new project (hooked
    into project creation via BackgroundTasks — bid invites often arrive before
    the project exists). Deterministic R2 over the whole pool (with the full
    project book, so the ambiguity guardrail still applies); capped
    single-candidate LLM confirms for recent emails. Never raises."""
    try:
        _rescan_unknown_for_project(project_id)
    except Exception:  # noqa: BLE001 — a rescan failure must never surface anywhere
        logger.exception("Unknown-email rescan failed for project %s", project_id)


def _unassigned_terminal_emails(sb) -> list[dict]:
    """The whole Unknown pool, paginated past PostgREST's row cap."""
    out: list[dict] = []
    start = 0
    while True:
        page = (
            sb.table("ingested_emails")
            .select("id, mailbox, conversation_id, subject, status, message_at")
            .is_("project_id", "null")
            .in_("status", list(_TERMINAL))
            .order("message_at", desc=True)
            .range(start, start + _PAGE - 1)
            .execute()
        ).data or []
        out.extend(page)
        if len(page) < _PAGE:
            return out
        start += _PAGE


def _rescan_unknown_for_project(project_id: str) -> None:
    settings = get_settings()
    sb = get_supabase()
    projects = _all_projects(sb)
    project = next((p for p in projects if p["id"] == project_id), None)
    if project is None:
        return

    unknowns = _unassigned_terminal_emails(sb)
    if not unknowns:
        return

    remainder: list[dict] = []
    for email in unknowns:
        # Match against the FULL book: a subject naming two projects must stay
        # ambiguous here exactly as it would in the live R2 round.
        if email_match.r2_match(email.get("subject") or "", projects) == project_id:
            _rescan_assign(sb, email, project_id, matched_by="subject")
        else:
            remainder.append(email)

    if not llm.is_configured("email_match", settings):
        return
    cutoff = _now() - timedelta(days=settings.email_rescan_llm_days)
    calls = 0
    for email in remainder:
        if calls >= settings.email_rescan_llm_max:
            break
        message_at = email.get("message_at")
        if not message_at:
            continue
        try:
            at = datetime.fromisoformat(message_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if at < cutoff:
            continue
        calls += 1
        try:
            result = openai_text.confirm_subject_matches_project(
                email.get("subject") or "", project
            )
        except Exception:  # noqa: BLE001 — incl. out-of-credits: stop quietly,
            # the emails stay Unknown and manual assign remains the fallback.
            logger.exception("Rescan LLM confirm failed; aborting rescan loop")
            return
        if result.get("match") and (
            _safe_float(result.get("confidence"))
            >= settings.email_match_confidence_threshold
        ):
            _rescan_assign(
                sb, email, project_id,
                matched_by="llm",
                confidence=_safe_float(result.get("confidence")),
                model=llm.active_model("email_match", settings),
            )


def _rescan_assign(
    sb,
    email: dict,
    project_id: str,
    *,
    matched_by: str,
    confidence: float | None = None,
    model: str | None = None,
) -> None:
    """Assign an Unknown-pool email, guarded so a concurrent manual assignment
    (project_id no longer null) is never overwritten."""
    resp = (
        sb.table("ingested_emails")
        .update(
            {
                "project_id": project_id,
                "matched_by": matched_by,
                "match_confidence": round(confidence, 3) if confidence is not None else None,
                "match_model": model,
                "suggested_project_id": None,
                "suggested_confidence": None,
                "status": "processed",
                "processed_at": _now().isoformat(),
                "error": None,
                "next_attempt_at": None,
                # matched_by says subject/llm, but this ran outside the sweep —
                # against ONE new project, not the live round.
                "pipeline_round": "rescan",
            }
        )
        .eq("id", email["id"])
        .is_("project_id", "null")
        .execute()
    )
    if resp.data and email.get("conversation_id"):
        _upsert_conversation_map(
            sb, email["mailbox"], email["conversation_id"], project_id,
            source=matched_by if matched_by in ("subject", "llm", "manual") else "llm",
        )


# ── Notifications ─────────────────────────────────────────────────────────────


def _notify_once(sb, type_: str, message: str) -> None:
    """notify_role(IT_ADMIN, ...) at most once per dedup window, so a stuck
    pipeline doesn't spam the bell every tick."""
    since = (_now() - timedelta(hours=_NOTIFY_DEDUP_HOURS)).isoformat()
    recent = (
        sb.table("notifications")
        .select("id")
        .eq("type", type_)
        .gte("created_at", since)
        .limit(1)
        .execute()
    ).data
    if recent:
        return
    notify_role(Role.IT_ADMIN, None, type_, message)

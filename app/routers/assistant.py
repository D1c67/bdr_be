"""The assistant API (docs/ASSISTANT.md, UI and the phase 0 contract):
health, settings, conversations, messages and the skill index; phase 1
adds the approval cards and the Activity list
(docs/ASSISTANT_PHASE1_PLAN.md 3.8, 3.10 and section 5).

Every route requires an internal role (require_internal on the router) and
a working assistant for the caller (assistant_enabled_for over the kill
switches, Permissions item 15); a caller without one gets the bare 404 a
switched-off module gives. A message send creates the user row and the
pending assistant row, then enqueues one llm_jobs assistant_turn job
targeting the conversation; a second send while a turn is pending or
running is 409 turn_in_progress. The turn itself runs in the queue
(app/assistant/jobs.py), never in the request.

Plain def handlers: the Supabase SDK is synchronous. app/main.py mounts
this router, so this module imports only the pure assistant modules at load
time (access, repo, schemas, skills_index); the agent loop, and through it
the executor and the shim, is imported lazily inside the handlers that need
it. Importing app.main therefore never loads app.assistant.loop or
app.assistant.shim, and main.py never serves or registers the shim. The
approval service (app.assistant.approvals, which imports the executor) is
imported lazily the same way, through _approvals().

An approval is decided under the person's own session: approve and reject
go through the compare-and-set RPC (repo.decide_approval); an approval
enqueues one assistant_approval_execute job and never runs the write in the
request. A bdra_ principal can never reach these routes (the firewall
deny-lists them, and each handler refuses via_assistant_run_id too).

Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md 3.1, 3.2, 3.6.3 and section 5)
adds staging (POST, GET and DELETE /assistant/files: human only, the
handlers refuse a bdra_ principal and the firewall denies the POST), the
`file_ids` of a send (linked to the user row before the assistant row
exists), the `files` of user messages, GET /assistant/tasks/{id} with its
lazy repair, and GET /assistant/split/status, the one route here the
assistant reads (splitter_status). The staging service and the split
service (app.services.assistant_staging, app.services.split_handoff) are
imported lazily in the handlers that use them.

Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md 3.9.3 and 5.1): a send carries
`incognito` onto both rows, messages carry `incognito` and the notes the
answer used (`used_memories`, resolved at read time), the Chat list holds
panel, page and task conversations only (a routine's own conversation
opens from Routines), and reading a routine's conversation marks its
routine seen. The gates the phase 3 routers share live here too: the
assistant-only refusal (`_require_assistant_principal`) and the trusted
turn context of a housekeeping tool route (`_trusted_turn`).
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.assistant import repo, skills_index
from app.assistant.access import assistant_enabled_for, owner_check
from app.assistant.schemas import (
    ACTIVITY_MAX,
    AE_TASK_KINDS,
    ActivityOut,
    AeTaskOut,
    ApprovalOut,
    ApproveIn,
    ConversationCreateIn,
    ConversationDetailOut,
    ConversationListOut,
    ConversationOut,
    FindToolItemOut,
    FindToolsOut,
    HealthOut,
    MessageAcceptedOut,
    MessageCreateIn,
    MessageFileOut,
    MessageOut,
    RecheckOut,
    ResolveIn,
    SettingsOut,
    SettingsPatchIn,
    SkillOut,
    SkillSummaryOut,
    SplitStatusOut,
    StagedFileOut,
    TaskFileOut,
    TaskJobOut,
    TaskOut,
    TaskProjectOut,
    UsedMemoryOut,
)
from app.assistant.tools._common import is_uuid
from app.core import provenance
from app.core.config import get_settings
from app.core.deps import CurrentUser, mfa_challenge_code, require_internal, require_writer
from app.core.features import SubApp, enabled_map, require_feature
from app.core.ratelimit import large_upload_slot, upload_rate_limit
from app.core.roles import INTERNAL_ROLES
from app.services import llm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"], dependencies=[Depends(require_internal)])

REASON_DISABLED = "disabled"
REASON_OVER_CAP = "over_cap"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_QUEUE_OFF = "queue_off"
TURN_IN_PROGRESS = "turn_in_progress"
CONVERSATION_ARCHIVED = "conversation_archived"
ASSISTANT_UNAVAILABLE = "assistant_unavailable"
CONVERSATION_LIST_MAX = 100
# Equal to app.assistant.jobs.JOB_TYPE (pinned by a test); not imported from
# there because jobs imports the loop, which imports the shim.
JOB_TYPE = "assistant_turn"
# Equal to app.assistant.approvals_job.JOB_TYPE (pinned by a test), for the
# same reason.
EXECUTE_JOB_TYPE = "assistant_approval_execute"
EXECUTE_PRIORITY = 40          # a click claims before a turn (50)

# Approval error codes (docs/ASSISTANT_PHASE1_PLAN.md 5.3).
APPROVAL_NOT_FOUND = "approval_not_found"
CANNOT_APPROVE = "assistant_cannot_approve"
DECIDE_CODES = {
    "not_found": (status.HTTP_404_NOT_FOUND, APPROVAL_NOT_FOUND),
    "expired": (status.HTTP_409_CONFLICT, "approval_expired"),
    "superseded": (status.HTTP_409_CONFLICT, "approval_superseded"),
    "not_pending": (status.HTTP_409_CONFLICT, "approval_not_pending"),
    "stale": (status.HTTP_409_CONFLICT, "approval_stale"),
}
ENQUEUE_FAILED = "The change could not be started. Try again from a new request."

# Phase 2 (docs/ASSISTANT_PHASE2_PLAN.md section 5).
FILE_NOT_AVAILABLE = "file_not_available"
FILE_TOO_LARGE = "file_too_large"
TASK_NOT_FOUND = "task_not_found"
PROJECT_NOT_FOUND = "project_not_found"
# Equal to app.assistant.continue_job.JOB_TYPE (the queue registers it);
# named here for the same reason as JOB_TYPE above.
CONTINUE_JOB_TYPE = "assistant_continue"
CONTINUE_PRIORITY = 45
CONTINUE_MAX_ATTEMPTS = 3
# Lazy repair of GET /assistant/tasks/{id} (3.6.3). The pending rule uses
# split_handoff.PENDING_TASK_REPAIR_SECONDS.
RUNNING_STALE_SECONDS = 10 * 60
TASK_TIMEOUT_SECONDS = 6 * 3600

# Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md 3.7.3, 3.9.3 and section 5).
# The Chat list: a routine's conversation (surface 'schedule') opens from
# the Routines tab only.
CHAT_SURFACES = ("panel", "page", "task")
TURN_CONTEXT_UNAVAILABLE = "turn_context_unavailable"
# The executor sets this header to the ledger key on every housekeeping
# request (app/assistant/executor.py IDEMPOTENCY_HEADER, pinned by a test).
ASSISTANT_IDEMPOTENCY_HEADER = "X-Assistant-Idempotency-Key"

# Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.2.2, 3.5.4, 3.6.2, 3.16.2, 5).
# A sensitive_read card is approved only with a reason the human typed.
REASON_REQUIRED = "reason_required"
REASON_INVALID = "reason_invalid"
REASON_MIN = 10
SENSITIVE_READ = "sensitive_read"          # registry.EFFECT_SENSITIVE_READ (pinned by a test)
# Route literals the phase 4 tool specs repeat (find_tools, and the
# recover_request of plans_read_pages and run_ai_estimating).
FIND_TOOLS_ROUTE = "/assistant/tools/find"
TASK_BY_APPROVAL_ROUTE = "/assistant/tasks/by-approval/{approval_id}"
FIND_LIMIT_DEFAULT = 6
FIND_LIMIT_MAX = 10
FIND_DESCRIPTION_MAX = 600
FIND_QUERY_REQUIRED = "query_required"
FIND_HINT_FOUND = "These tools are available from your next step. Call the one that fits."
FIND_HINT_NONE = ("No further tool matches that. Answer with the tools you already have, "
                  "or tell the user what you cannot look up.")
# The lazy finalize of an AI Estimating task (3.5.4) runs at most once per
# 10 seconds per task in this process; ae_task_sweep covers the rest.
AE_FINALIZE_INTERVAL_SECONDS = 10.0
_AE_FINALIZE_MAX_TRACKED = 2000
_ae_finalize_lock = threading.Lock()
_ae_finalize_last: dict[str, float] = {}
_ae_finalize_clock = time.monotonic       # one seam for the tests


def _loop():
    """The agent loop, imported on first use so app.main never loads it."""
    from app.assistant import loop

    return loop


def _approvals():
    """The approval service, imported on first use so app.main never loads
    it (it imports the executor, which imports the shim)."""
    from app.assistant import approvals

    return approvals


# ── Gates and shapes ─────────────────────────────────────────────────────────


def _rows_for(user: CurrentUser) -> tuple[dict | None, dict | None]:
    return repo.settings_row(user.id), repo.profile_row(user.id)


def _require_assistant(user: CurrentUser) -> tuple[dict | None, dict | None]:
    settings_row, profile_row = _rows_for(user)
    if not assistant_enabled_for(user, settings_row, profile_row):
        # The same bare body a switched-off sub-app gives (core/features).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return settings_row, profile_row


def _default_display_name(user: CurrentUser) -> str:
    human = repo.human_profile(user.id) or {}
    first = (human.get("full_name") or "").strip().split(" ")[0] if human.get("full_name") else ""
    return provenance.display_actor(first or None, provenance.ACTOR_ASSISTANT)


def _settings_out(user: CurrentUser, profile_row: dict | None) -> SettingsOut:
    row = profile_row or {}
    return SettingsOut(
        display_name=row.get("display_name"),
        default_display_name=_default_display_name(user),
        enabled=bool(row.get("enabled", True)),
        timezone=str(row.get("timezone") or "America/Los_Angeles"),
        daily_cost_cap_usd=float(row.get("daily_cost_cap_usd") if row.get("daily_cost_cap_usd") is not None
                                 else _loop().DEFAULT_USER_CAP),
    )


def _conversation_out(row: dict, schedule_id: str | None = None) -> ConversationOut:
    return ConversationOut(
        id=str(row["id"]),
        mode=row.get("mode") or "normal",
        mailbox_tainted_at=row.get("mailbox_tainted_at"),
        title=row.get("title"),
        surface=str(row.get("surface") or "panel"),
        project_id=row.get("project_id"),
        created_at=str(row.get("created_at") or ""),
        last_message_at=row.get("last_message_at"),
        schedule_id=schedule_id,
    )


def _status_out(value: Any) -> str:
    value = str(value or "done")
    if value in ("pending", "running", "waiting_approval", "waiting_review", "done", "failed"):
        return value
    # An unknown status reads as running so the panel keeps polling.
    return "running"


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _file_state(row: dict) -> str:
    """staged, consumed or expired, for a staged row a message carried."""
    if row.get("consumed_at") or row.get("consumed_task_id"):
        return "consumed"
    expires = _parse_ts(row.get("expires_at"))
    if row.get("purged_at") or (expires is not None and expires <= datetime.now(timezone.utc)):
        return "expired"
    return "staged"


def _message_file_out(row: dict, state: str | None = None) -> MessageFileOut:
    return MessageFileOut(
        id=str(row["id"]),
        filename=str(row.get("filename") or ""),
        size_bytes=int(row.get("size_bytes") or 0),
        pages=row.get("page_count"),
        state=state or _file_state(row),
    )


def _files_by_message(user: CurrentUser, rows: list[dict]) -> dict[str, list[MessageFileOut]]:
    """{message id: its files} for the user rows among `rows`. Best effort:
    the chips are cosmetic next to the transcript."""
    ids = [str(r["id"]) for r in rows if r.get("role") == "user" and r.get("id")]
    if not ids:
        return {}
    try:
        files = repo.files_for_messages(user.id, ids)
    except Exception:  # noqa: BLE001 - a failed read shows the message without its chips
        logger.exception("assistant: files of %d messages could not be read", len(ids))
        return {}
    out: dict[str, list[MessageFileOut]] = {}
    for row in files:
        out.setdefault(str(row.get("message_id")), []).append(_message_file_out(row))
    return out


def _used_ids(row: dict) -> list[str]:
    value = row.get("used_memory_ids")
    return [str(i) for i in value if i] if isinstance(value, list) else []


def _used_memories(user: CurrentUser, rows: list[dict]) -> dict[str, UsedMemoryOut]:
    """{memory id: {id, title, kind}} for every note the answers among
    `rows` tagged, in one read (3.9.3). A deleted note is simply missing.
    Best effort: the chip is cosmetic next to the answer."""
    ids = list(dict.fromkeys(i for r in rows if r.get("role") == "assistant" for i in _used_ids(r)))
    if not ids:
        return {}
    try:
        found = repo.memories_by_ids(user.id, ids)
    except Exception:  # noqa: BLE001 - the answer still reads without its chips
        logger.exception("assistant: used notes of %d messages could not be read", len(rows))
        return {}
    return {str(m["id"]): UsedMemoryOut(id=str(m["id"]), title=str(m.get("title") or ""),
                                        kind=str(m.get("kind") or ""))
            for m in found if m.get("id")}


def _message_out(row: dict, files: list[MessageFileOut] | None = None,
                 used: dict[str, UsedMemoryOut] | None = None) -> MessageOut:
    blocks = row.get("blocks") if isinstance(row.get("blocks"), list) else []
    progress = row.get("tool_calls") if isinstance(row.get("tool_calls"), list) else []
    used = used or {}
    return MessageOut(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        role="assistant" if row.get("role") == "assistant" else "user",
        content=str(row.get("content") or ""),
        status=_status_out(row.get("status")),
        tool_calls=[p for p in progress if isinstance(p, dict)],
        blocks=_loop().ui_blocks(blocks),
        error=_loop().error_text(blocks) if row.get("status") == "failed" else None,
        created_at=str(row.get("created_at") or ""),
        files=list(files or []) if row.get("role") == "user" else [],
        incognito=bool(row.get("incognito")),
        used_memories=[used[i] for i in _used_ids(row) if i in used] if row.get("role") == "assistant" else [],
    )


def _queue_allows(job_type: str) -> bool:
    from app.services import llm_queue

    s = get_settings()
    if not getattr(s, "llm_queue_enabled", True):
        return False
    try:
        allowed = llm_queue.claim_types_allowed(s)
    except Exception:  # noqa: BLE001 - an unreadable restriction reads as "cannot run"
        logger.exception("assistant: claim types could not be read")
        return False
    return not allowed or job_type in allowed


def _queue_available() -> bool:
    """A turn runs only in the llm_jobs queue: with the queue off, or this
    deployment's worker excluded from claiming assistant_turn
    (LLM_QUEUE_CLAIM_TYPES), a message would sit pending forever."""
    return _queue_allows(JOB_TYPE)


def _execute_queue_available() -> bool:
    """The same check for the approval execute job. Approve checks it before
    the compare-and-set, because an approved card never goes back to
    pending."""
    return _queue_allows(EXECUTE_JOB_TYPE)


def _model_available() -> bool:
    try:
        return llm.is_configured("assistant")
    except KeyError:
        return False
    except Exception:  # noqa: BLE001 - a routing error is "unavailable", never a 500 on health
        logger.exception("assistant health: model check failed")
        return False


# ── Health and settings ──────────────────────────────────────────────────────


@router.get("/health", response_model=HealthOut)
def health(user: CurrentUser = Depends(require_internal)) -> HealthOut:
    settings_row, profile_row = _rows_for(user)
    if not assistant_enabled_for(user, settings_row, profile_row):
        return HealthOut(available=False, reason=REASON_DISABLED)
    if not _model_available():
        return HealthOut(available=False, reason=REASON_MODEL_UNAVAILABLE)
    if not _queue_available():
        return HealthOut(available=False, reason=REASON_QUEUE_OFF)
    try:
        # One source with the turn guard and the split card (phase 2, 3.8.4):
        # rpc assistant_spend_state, live holds counted.
        from app.assistant import spend

        if spend.state(user.id).over_cap:
            return HealthOut(available=False, reason=REASON_OVER_CAP)
    except Exception:  # noqa: BLE001 - a spend read failure must not hide the assistant
        logger.exception("assistant health: spend check failed")
    return HealthOut(available=True, reason=None)


@router.get("/settings", response_model=SettingsOut)
def get_assistant_settings(user: CurrentUser = Depends(require_internal)) -> SettingsOut:
    _require_assistant(user)
    return _settings_out(user, repo.ensure_profile(user.id))


@router.patch("/settings", response_model=SettingsOut)
def patch_assistant_settings(
    body: SettingsPatchIn, user: CurrentUser = Depends(require_internal)
) -> SettingsOut:
    _require_assistant(user)
    row = repo.update_profile(user.id, {"display_name": body.display_name})
    return _settings_out(user, row)


# ── Skills ───────────────────────────────────────────────────────────────────


@router.get("/skills", response_model=list[SkillSummaryOut])
def list_skills(user: CurrentUser = Depends(require_internal)) -> list[SkillSummaryOut]:
    _require_assistant(user)
    return [
        SkillSummaryOut(name=s.name, description=s.description, version=s.version)
        for s in skills_index.available(user.role, enabled_map(user.role))
    ]


@router.get("/skills/{name}", response_model=SkillOut)
def get_skill(name: str, user: CurrentUser = Depends(require_internal)) -> SkillOut:
    _require_assistant(user)
    skill = skills_index.get(name, user.role, enabled_map(user.role))
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Skill not found")
    return SkillOut(name=skill.name, description=skill.description, version=skill.version, body=skill.body)


# ── Conversations ────────────────────────────────────────────────────────────


@router.get("/conversations", response_model=ConversationListOut)
def list_conversations(
    limit: int = Query(default=20, ge=1, le=CONVERSATION_LIST_MAX),
    user: CurrentUser = Depends(require_internal),
) -> ConversationListOut:
    _require_assistant(user)
    rows = repo.list_conversations(user.id, limit, surfaces=CHAT_SURFACES)
    return ConversationListOut(items=[_conversation_out(r) for r in rows])


@router.post("/conversations", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
def create_conversation(
    body: ConversationCreateIn, user: CurrentUser = Depends(require_internal)
) -> ConversationOut:
    _require_assistant(user)
    from app.core.roles import EVIDENCE_ROLES

    _refuse_assistant_principal(user)
    if body.mode == "mailbox_review" and user.role not in EVIDENCE_ROLES:
        raise HTTPException(403, "Mailbox review is unavailable.")
    kwargs = {"surface": body.surface, "project_id": body.project_id or None}
    if body.mode != "normal":
        kwargs["mode"] = body.mode
    row = repo.create_conversation(user.id, **kwargs)
    return _conversation_out(row)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailOut)
def get_conversation(
    conversation_id: str, user: CurrentUser = Depends(require_internal)
) -> ConversationDetailOut:
    _require_assistant(user)
    row = repo.get_conversation(user.id, conversation_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    rows = [m for m in repo.list_messages(user.id, conversation_id) if m.get("role") in ("user", "assistant")]
    files = _files_by_message(user, rows)
    used = _used_memories(user, rows)
    messages = [_message_out(m, files.get(str(m.get("id"))), used) for m in rows]
    return ConversationDetailOut(conversation=_conversation_out(row, _schedule_seen(user, row)), messages=messages)


def _schedule_seen(user: CurrentUser, conversation: dict) -> str | None:
    """For a routine's conversation: its schedule id, and the person's read
    marks the routine seen (one of the two seen signals, 3.9.3). Best
    effort; an assistant principal reading it marks nothing."""
    if conversation.get("surface") != "schedule":
        return None
    try:
        schedule = repo.schedule_for_conversation(user.id, str(conversation["id"]))
    except Exception:  # noqa: BLE001 - the transcript still reads without the link
        logger.exception("assistant: schedule of conversation %s could not be read", conversation.get("id"))
        return None
    if schedule is None:
        return None
    schedule_id = str(schedule["id"])
    if not getattr(user, "via_assistant_run_id", None):
        try:
            repo.mark_schedules_seen(user.id, [schedule_id])
        except Exception:  # noqa: BLE001 - the unread count clears on the next read
            logger.exception("assistant: schedule %s could not be marked seen", schedule_id)
    return schedule_id


# ── Messages ─────────────────────────────────────────────────────────────────


def _turn_in_progress() -> HTTPException:
    # The code rides in X-Error-Code too (exposed through CORS), so the
    # panel can tell this 409 from the archived one without parsing detail.
    return HTTPException(status.HTTP_409_CONFLICT, {"code": TURN_IN_PROGRESS},
                         headers={"X-Error-Code": TURN_IN_PROGRESS})


def _unavailable() -> HTTPException:
    """503 {"detail": {"code": "assistant_unavailable"}}: the queue that runs
    turns is off or excluded here, or the enqueue itself failed."""
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"code": ASSISTANT_UNAVAILABLE},
                         headers={"X-Error-Code": ASSISTANT_UNAVAILABLE})


def _detail_error(status_code: int, code: str, message: str, **detail: Any) -> HTTPException:
    """{"detail": {"code", "message", **detail}} with the code in
    X-Error-Code (the phase 2 refusals that carry data, 5.3)."""
    return HTTPException(status_code, {"code": code, "message": message, **detail},
                         headers={"X-Error-Code": code})


def _file_not_available(file_ids: list[str]) -> HTTPException:
    return _detail_error(
        status.HTTP_409_CONFLICT, FILE_NOT_AVAILABLE,
        "A file is no longer staged for this message. Attach it again.", file_ids=file_ids,
    )


def _discard(user: CurrentUser, row: dict | None) -> None:
    """Best effort removal of a message row the send could not complete."""
    if not row or not row.get("id"):
        return
    try:
        repo.delete_message(user.id, str(row["id"]))
    except Exception:  # noqa: BLE001 - the 409 still stands; the row is at worst an orphan
        logger.exception("assistant: could not discard message %s", row.get("id"))


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) == "23505" or "23505" in text or "duplicate key" in text


def _enqueue_turn(user: CurrentUser, conversation_id: str, message_id: str) -> str | None:
    """One assistant_turn job for the conversation, max_attempts 1. Returns
    the job id; raises the 409 when a job is already active for it."""
    from app.services import llm_queue

    kwargs: dict[str, Any] = {}
    if "max_attempts_override" in inspect.signature(llm_queue.enqueue).parameters:
        kwargs["max_attempts_override"] = 1
    try:
        job = llm_queue.enqueue(
            JOB_TYPE,
            target_id=conversation_id,
            payload={"message_id": message_id, "user_id": user.id, "conversation_id": conversation_id},
            created_by=user.id,
            priority=50,
            raise_on_active=True,
            **kwargs,
        )
    except llm_queue.JobAlreadyActive:
        raise _turn_in_progress() from None
    return str(job.get("id")) if isinstance(job, dict) and job.get("id") else None


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def send_message(
    conversation_id: str, body: MessageCreateIn, user: CurrentUser = Depends(require_internal)
) -> MessageAcceptedOut:
    _require_assistant(user)
    conversation = repo.get_conversation(user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    if conversation.get("archived_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This conversation is archived",
                            headers={"X-Error-Code": CONVERSATION_ARCHIVED})
    if repo.active_turn(user.id, conversation_id) is not None:
        raise _turn_in_progress()
    if not _queue_available():
        # Nothing is inserted: the panel keeps the draft and shows the error.
        raise _unavailable()
    # Chat turns are created under the user's own JWT; the grant the worker
    # checks on every tool call is created here if it is missing. aal2 always,
    # aal1 only while mfa_required is off (dev and test, read per call). A
    # refusal answers like core/deps: step-up when a factor exists, otherwise
    # enrollment.
    try:
        repo.ensure_delegation(user.id, user.aal)
    except PermissionError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, mfa_challenge_code(user.mfa_enrolled)) from None
    repo.ensure_profile(user.id)

    # Staged files (3.2): every id must be live, unclaimed and unlinked
    # before anything is inserted.
    file_ids = list(body.file_ids)
    staged: dict[str, dict] = {}
    if file_ids:
        staged = {str(r["id"]): r for r in repo.staged_files_for_link(user.id, file_ids) if r.get("id")}
        missing = [i for i in file_ids if i not in staged]
        if missing:
            raise _file_not_available(missing)

    context = body.context.model_dump() if body.context is not None else None
    user_row = repo.insert_message(user.id, {
        "conversation_id": conversation_id,
        "role": "user",
        "content": body.content,
        "context": context,
        "status": "done",
        "incognito": body.incognito,
    })
    if file_ids:
        # Linked after the user row and BEFORE the assistant row: a short
        # count withdraws the question (the delete unlinks through the FK's
        # on delete set null) and no turn ever starts.
        try:
            linked = repo.link_files(user.id, file_ids, str(user_row["id"]), conversation_id)
        except Exception:
            _discard(user, user_row)
            raise
        if linked < len(file_ids):
            _discard(user, user_row)
            raise _file_not_available(_unlinkable(user, file_ids))
    try:
        assistant_row = repo.insert_message(user.id, {
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": "",
            "context": context,
            "status": "pending",
            "incognito": body.incognito,
        })
    except Exception as exc:  # noqa: BLE001 - only the one-active-turn index is expected
        if _is_unique_violation(exc):
            # The race the pre-check missed: the question is withdrawn so no
            # orphan user row is replayed as history on the next turn.
            _discard(user, user_row)
            raise _turn_in_progress() from exc
        raise
    try:
        job_id = _enqueue_turn(user, conversation_id, str(assistant_row["id"]))
    except HTTPException:
        # The queue already runs a turn for this conversation: no job was
        # created for this pair, so both rows are withdrawn.
        _discard(user, assistant_row)
        _discard(user, user_row)
        raise
    except Exception:  # noqa: BLE001 - the queue is down: the message must not hang pending
        logger.exception("assistant: enqueue failed for conversation %s", conversation_id)
        if file_ids:
            # The staged files were linked to the user row: withdraw the pair
            # (the delete unlinks them through the FK's on delete set null),
            # so the same chips send again once the queue is back.
            _discard(user, assistant_row)
            _discard(user, user_row)
            raise _unavailable() from None
        repo.update_message(user.id, str(assistant_row["id"]), {
            "status": "failed", "error_kind": "not_configured",
            "blocks": [{"type": "error", "text": "The assistant could not be started. Try again in a minute."}],
        }, expect_status="pending")
        raise _unavailable() from None
    if job_id:
        repo.update_message(user.id, str(assistant_row["id"]), {"llm_job_id": job_id})
    repo.update_conversation(user.id, conversation_id, {"last_message_at": user_row.get("created_at")})
    return MessageAcceptedOut(
        user_message=_message_out(user_row, [_message_file_out(staged[i], "staged") for i in file_ids]),
        assistant_message=_message_out({**assistant_row, "status": "pending"}),
        job_id=job_id,
    )


def _unlinkable(user: CurrentUser, file_ids: list[str]) -> list[str]:
    """After a short link count: the ids that a send could not take now
    (all of them when the re-read fails)."""
    try:
        free = {str(r.get("id")) for r in repo.staged_files_for_link(user.id, file_ids)}
    except Exception:  # noqa: BLE001 - the 409 still stands
        logger.exception("assistant: staged files could not be re-read after a short link")
        return list(file_ids)
    return [i for i in file_ids if i not in free] or list(file_ids)


@router.get("/messages/{message_id}", response_model=MessageOut)
def get_message(message_id: str, user: CurrentUser = Depends(require_internal)) -> MessageOut:
    _require_assistant(user)
    row = repo.get_message(user.id, message_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    return _message_out(row, _files_by_message(user, [row]).get(str(row.get("id"))), _used_memories(user, [row]))


# ── Approvals (phase 1) ──────────────────────────────────────────────────────


def _code_error(status_code: int, code: str, message: str | None = None) -> HTTPException:
    """{"detail": {"code": ..., "message"?: ...}} with the code in
    X-Error-Code (exposed through CORS)."""
    detail: dict[str, Any] = {"code": code}
    if message:
        detail["message"] = message
    return HTTPException(status_code, detail, headers={"X-Error-Code": code})


def _approval_id(value: str) -> str:
    """The id lands in a uuid column: anything else is the 404, never a
    22P02 from PostgREST (an unhandled 500 that loses its CORS headers)."""
    value = (value or "").strip()
    if not is_uuid(value):
        raise _code_error(status.HTTP_404_NOT_FOUND, APPROVAL_NOT_FOUND)
    return value


def _refuse_assistant_principal(user: CurrentUser) -> None:
    """Only the human decides: a bdra_ principal never approves, rejects,
    reverts or resolves (the firewall deny-lists these routes as well)."""
    if getattr(user, "via_assistant_run_id", None):
        raise _code_error(status.HTTP_403_FORBIDDEN, CANNOT_APPROVE)


def _require_assistant_principal(user: CurrentUser) -> None:
    """The assistant's own routes (the housekeeping tool routes, the
    recover reads): a human gets the bare 404, as if the route were not
    there (the routers/projects precedent)."""
    if not getattr(user, "via_assistant_run_id", None):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def _trusted_turn(request: Request, user: CurrentUser, tool_name: str) -> dict:
    """What a housekeeping tool route may trust about the turn that called
    it (docs/ASSISTANT_PHASE3_PLAN.md 3.7.3): the begun ledger row named by
    the idempotency header the executor set, for this run and this tool,
    plus its message and conversation. Nothing comes from model arguments.
    404 for a human; 409 turn_context_unavailable when the row is missing
    or was begun for another tool."""
    _require_assistant_principal(user)
    key = (request.headers.get(ASSISTANT_IDEMPOTENCY_HEADER) or "").strip()
    turn = repo.turn_context(user.id, user.via_assistant_run_id, key, tool_name=tool_name) if key else None
    if turn is None:
        raise _detail_error(
            status.HTTP_409_CONFLICT, TURN_CONTEXT_UNAVAILABLE,
            "This change has to come from a turn of the assistant. Try again from a new message.",
        )
    return turn


def _project_refs(user: CurrentUser, project_ids: list[Any]) -> dict[str, dict]:
    """{project id: {id, number, name}} for the projects a routine watches
    or a note is about, passed through redact_for_role; a project that is
    gone is simply missing. Best effort: the row still renders by id."""
    ids = sorted({str(i) for i in project_ids if i and is_uuid(str(i))})
    if not ids:
        return {}
    try:
        from app.core import supabase_client
        from app.routers.projects import redact_for_role

        rows = (supabase_client.get_supabase().table("projects").select("id, number, name")
                .in_("id", ids).limit(len(ids)).execute()).data or []
    except Exception:  # noqa: BLE001 - the row renders by id without the number
        logger.exception("assistant: %d projects could not be read", len(ids))
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        row = redact_for_role(dict(row), user.role)
        out[str(row["id"])] = {"id": str(row["id"]), "number": row.get("number"), "name": row.get("name")}
    return out


def _require_delegation(user: CurrentUser) -> None:
    """The grant the execute job and the preview reads run under, created
    here when missing; a refusal answers like core/deps (the detail string
    starts with mfa_, which the FE funnels to the 2FA hub)."""
    try:
        repo.ensure_delegation(user.id, user.aal)
    except PermissionError:
        code = mfa_challenge_code(user.mfa_enrolled)
        raise HTTPException(status.HTTP_403_FORBIDDEN, code, headers={"X-Error-Code": code}) from None


def _service_error(exc: Any) -> HTTPException:
    return _code_error(exc.status_code, exc.code, exc.message)


def _out(user: CurrentUser, row: dict) -> ApprovalOut:
    return ApprovalOut(**_approvals().out_for(user.id, row))


def _dismiss_bells(user: CurrentUser, approval_id: str) -> None:
    """Drop this card's bells (superseded, failed, expired) once the human
    acted on it. Best effort."""
    try:
        from app.services import notifications

        # dismiss_notifications only writes for the types or prefixes it is
        # given: without one, the call matched nothing and the bell stayed.
        notifications.dismiss_notifications(
            user_id=user.id, metadata_eq={"approval_id": approval_id}, type_prefixes=["assistant."],
        )
    except Exception:  # noqa: BLE001 - a stale bell never fails the click
        logger.exception("assistant: bells could not be dismissed for approval %s", approval_id)


def _settle(user: CurrentUser, message_id: Any) -> None:
    try:
        repo.settle_message(user.id, str(message_id) if message_id else None)
    except Exception:  # noqa: BLE001 - the message status is cosmetic next to the card
        logger.exception("assistant: message %s could not be settled", message_id)


def _decide(user: CurrentUser, approval_id: str, decision: str,
            payload_sha256: str | None, precondition: dict | None) -> dict:
    """The compare-and-set click (RPC assistant_approval_decide)."""
    res = repo.decide_approval(
        user.id, approval_id, decision=decision, payload_sha256=payload_sha256,
        precondition=precondition, session_aal=user.aal,
    )
    if res.get("ok"):
        return dict(res.get("row") or {}) or (repo.get_approval(user.id, approval_id) or {})
    reason = str(res.get("reason") or "")
    if reason == "expired" and isinstance(res.get("row"), dict):
        # The RPC itself may have just timed the card out.
        _settle(user, res["row"].get("message_id"))
    status_code, code = DECIDE_CODES.get(reason, (status.HTTP_409_CONFLICT, "approval_not_pending"))
    raise _code_error(status_code, code)


def _enqueue_execute(user: CurrentUser, approval_id: str, row: dict) -> str | None:
    """One assistant_approval_execute job for the approved card. A job
    already active for it counts as success. Any other failure fails the
    card (it can never go back to pending) and answers 503."""
    from app.services import llm_queue

    try:
        job = llm_queue.enqueue(
            EXECUTE_JOB_TYPE,
            target_id=approval_id,
            project_id=row.get("project_id"),
            payload={"approval_id": approval_id, "user_id": user.id},
            created_by=user.id,
            priority=EXECUTE_PRIORITY,
            raise_on_active=True,
            max_attempts_override=1,
        )
    except llm_queue.JobAlreadyActive as exc:
        job = exc.job
    except Exception:  # noqa: BLE001 - the queue is down: the card must not hang approved
        logger.exception("assistant: execute enqueue failed for approval %s", approval_id)
        try:
            if repo.mark_approval(user.id, approval_id, {
                "status": "failed", "result": {"ok": False, "error": ENQUEUE_FAILED},
            }, expect_status="approved"):
                _settle(user, row.get("message_id"))
        except Exception:  # noqa: BLE001 - recover_stranded repairs it on the next read
            logger.exception("assistant: approval %s could not be failed after the enqueue error", approval_id)
        raise _unavailable() from None
    return str(job.get("id")) if isinstance(job, dict) and job.get("id") else None


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
def read_approval(approval_id: str, user: CurrentUser = Depends(require_internal)) -> ApprovalOut:
    _require_assistant(user)
    approval_id = _approval_id(approval_id)
    # Lazily: a pending card past expires_at becomes expired (timed_out),
    # and an approved card whose job never started is failed.
    row = repo.expire_if_due(user.id, approval_id)
    if row is None:
        raise _code_error(status.HTTP_404_NOT_FOUND, APPROVAL_NOT_FOUND)
    row = _approvals().recover_stranded(user.id, row) or row
    return _out(user, row)


def _is_sensitive(user: CurrentUser, approval_id: str) -> bool:
    """Whether the card is a sensitive_read card (3.2.2): its action_kind
    names a registered spec of that effect. An unknown card or tool counts
    as not sensitive; the compare-and-set then answers for it."""
    row = repo.get_approval(user.id, approval_id)
    if not row or not row.get("action_kind"):
        return False
    from app.assistant import registry

    spec = registry.get(str(row["action_kind"]))
    return spec is not None and spec.effect == SENSITIVE_READ


def _decide_with_reason(user: CurrentUser, approval_id: str, payload_sha256: str | None,
                        precondition: dict | None, reason: str) -> dict:
    """The approve click of a sensitive_read card: the compare-and-set and
    the reason in one RPC (0180 A5). The reason is never logged."""
    res = repo.decide_with_reason(
        user.id, approval_id, payload_sha256=payload_sha256, precondition=precondition,
        session_aal=user.aal, reason=reason,
    )
    if res.get("ok"):
        return dict(res.get("row") or {}) or (repo.get_approval(user.id, approval_id) or {})
    code = str(res.get("reason") or "")
    if code == REASON_INVALID:
        raise _code_error(status.HTTP_422_UNPROCESSABLE_CONTENT, REASON_INVALID,
                          "Type a reason of 10 to 500 characters.")
    if code == "expired" and isinstance(res.get("row"), dict):
        _settle(user, res["row"].get("message_id"))
    status_code, mapped = DECIDE_CODES.get(code, (status.HTTP_409_CONFLICT, "approval_not_pending"))
    raise _code_error(status_code, mapped)


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalOut,
             status_code=status.HTTP_202_ACCEPTED)
def approve_approval(
    approval_id: str, body: ApproveIn, user: CurrentUser = Depends(require_internal)
) -> ApprovalOut:
    """Approve a card. A sensitive_read card (phase 4, 3.2.2) needs `reason`
    (10 or more characters after trimming, 422 reason_required), which the
    reason RPC stores on the approval once; every other card ignores it."""
    _require_assistant(user)
    _refuse_assistant_principal(user)
    approval_id = _approval_id(approval_id)
    sensitive = _is_sensitive(user, approval_id)
    reason = (body.reason or "").strip()
    if sensitive and len(reason) < REASON_MIN:
        raise _code_error(status.HTTP_422_UNPROCESSABLE_CONTENT, REASON_REQUIRED,
                          "Type why you need this person's evidence (at least 10 characters).")
    if not _execute_queue_available():
        # Before the compare-and-set: approved never goes back to pending.
        raise _unavailable()
    _require_delegation(user)
    if sensitive:
        row = _decide_with_reason(user, approval_id, body.payload_sha256, body.precondition, reason)
    else:
        row = _decide(user, approval_id, "approve", body.payload_sha256, body.precondition)
    job_id = _enqueue_execute(user, approval_id, row)
    try:
        repo.set_execute_job(user.id, approval_id, job_id)
    except Exception:  # noqa: BLE001 - the job records its own id when it claims the card
        logger.exception("assistant: execute job id could not be recorded on approval %s", approval_id)
    _dismiss_bells(user, approval_id)
    return _out(user, repo.get_approval(user.id, approval_id) or row)


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalOut)
def reject_approval(approval_id: str, user: CurrentUser = Depends(require_internal)) -> ApprovalOut:
    _require_assistant(user)
    _refuse_assistant_principal(user)
    approval_id = _approval_id(approval_id)
    row = _decide(user, approval_id, "reject", None, None)
    _settle(user, row.get("message_id"))
    _dismiss_bells(user, approval_id)
    return _out(user, row)


@router.post("/approvals/{approval_id}/revert", response_model=ApprovalOut,
             status_code=status.HTTP_201_CREATED)
def revert_approval(approval_id: str, user: CurrentUser = Depends(require_internal)) -> ApprovalOut:
    """A new pending card that undoes an executed change. The preview reads
    run through the shim in this threadpool thread (no running loop here)."""
    _require_assistant(user)
    _refuse_assistant_principal(user)
    approval_id = _approval_id(approval_id)
    _require_delegation(user)
    svc = _approvals()
    try:
        row = svc.propose_revert(user, approval_id)
    except svc.ApprovalServiceError as exc:
        raise _service_error(exc) from None
    return _out(user, row)


@router.post("/approvals/{approval_id}/recheck", response_model=RecheckOut)
def recheck_approval(approval_id: str, user: CurrentUser = Depends(require_internal)) -> RecheckOut:
    """For a card that may have partially run: the before values against
    the live record."""
    _require_assistant(user)
    _refuse_assistant_principal(user)
    approval_id = _approval_id(approval_id)
    _require_delegation(user)
    svc = _approvals()
    try:
        out = svc.recheck(user, approval_id)
    except svc.ApprovalServiceError as exc:
        raise _service_error(exc) from None
    return RecheckOut(**out)


@router.post("/approvals/{approval_id}/resolve", response_model=ApprovalOut)
def resolve_approval(
    approval_id: str, body: ResolveIn, user: CurrentUser = Depends(require_internal)
) -> ApprovalOut:
    """The human's verdict on a card that may have partially run."""
    _require_assistant(user)
    _refuse_assistant_principal(user)
    approval_id = _approval_id(approval_id)
    svc = _approvals()
    try:
        out = svc.resolve(user, approval_id, body.outcome)
    except svc.ApprovalServiceError as exc:
        raise _service_error(exc) from None
    return ApprovalOut(**out)


def _before_cursor(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "before must be an ISO 8601 time") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


@router.get("/activity", response_model=ActivityOut)
def get_activity(
    limit: int = Query(default=20, ge=1, le=ACTIVITY_MAX),
    before: str | None = Query(default=None, max_length=64),
    user: CurrentUser = Depends(require_internal),
) -> ActivityOut:
    """Every approval of the caller, newest first, every status (a pending
    card whose turn died still has a home here)."""
    _require_assistant(user)
    cursor = _before_cursor(before)
    svc = _approvals()
    rows = repo.list_activity(user.id, limit=limit, before=cursor)
    rows = [(svc.recover_stranded(user.id, r) or r) if r.get("status") == "approved" else r for r in rows]
    items = [ApprovalOut(**o) for o in svc.outs_for(user.id, rows)]
    next_before = str(rows[-1].get("created_at")) if rows and len(rows) >= limit else None
    return ActivityOut(items=items, next_before=next_before)


# ── Staged files (phase 2) ───────────────────────────────────────────────────


def _staging():
    """The staging service, imported on first use."""
    from app.services import assistant_staging

    return assistant_staging


def _staging_error(exc: Any) -> HTTPException:
    return _detail_error(exc.status, exc.code, exc.message, **exc.detail)


def _staged_out(row: dict, reused: bool) -> StagedFileOut:
    return StagedFileOut(
        id=str(row["id"]),
        filename=str(row.get("filename") or ""),
        size_bytes=int(row.get("size_bytes") or 0),
        pages=row.get("page_count"),
        sha256=str(row.get("sha256") or ""),
        expires_at=str(row.get("expires_at") or ""),
        reused=reused,
    )


@router.post("/files", response_model=StagedFileOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(upload_rate_limit)])
async def stage_file(
    response: Response,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_writer),
) -> StagedFileOut:
    """Stage one PDF for a later message (3.1.2): 201, or 200 with
    `reused: true` when the same bytes are already staged and unlinked.
    Writers only (the Accountant's assistant cannot file files); never an
    assistant principal. Async only for the capped multipart read: the
    assistant gate and the staging run in the threadpool (the Supabase SDK
    is sync). A conversation is set only when a send links the file."""
    _refuse_assistant_principal(user)
    await run_in_threadpool(_require_assistant, user)
    from app.routers import files as files_router

    svc = _staging()
    with large_upload_slot(user.id, file.size):
        try:
            content = await files_router._read_capped(file, get_settings().upload_max_bytes)
        except HTTPException as exc:
            if exc.status_code == 413:  # the 450 MB cap (files._read_capped)
                raise _detail_error(exc.status_code, FILE_TOO_LARGE, str(exc.detail)) from None
            raise
        try:
            row, reused = await run_in_threadpool(svc.stage, user, file.filename, content)
        except svc.StagingRefused as exc:
            raise _staging_error(exc) from None
        finally:
            del content
    if reused:
        response.status_code = status.HTTP_200_OK
    return _staged_out(row, reused)


@router.get("/files", response_model=list[StagedFileOut])
def list_staged_files(user: CurrentUser = Depends(require_internal)) -> list[StagedFileOut]:
    """The caller's tray: live staged rows not yet sent or claimed, newest
    first (the panel restores its chips from it after a reload)."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return [_staged_out(r, False) for r in _staging().list_tray(user)]


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_staged_file(file_id: str, user: CurrentUser = Depends(require_writer)) -> Response:
    """Remove one of the caller's unclaimed staged files (204, idempotent)."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    svc = _staging()
    file_id = (file_id or "").strip()
    if not is_uuid(file_id):
        raise _detail_error(status.HTTP_404_NOT_FOUND, svc.FILE_NOT_FOUND, "That file is not staged.")
    try:
        svc.remove(user, file_id)
    except svc.StagingRefused as exc:
        raise _staging_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Tasks and the splitter (phase 2) ─────────────────────────────────────────


def _split_handoff():
    """The split service, imported on first use (WI-3 fills it)."""
    from app.services import split_handoff

    return split_handoff


def _age_seconds(value: Any) -> float | None:
    at = _parse_ts(value)
    return None if at is None else (datetime.now(timezone.utc) - at).total_seconds()


def _enqueue_continue(user: CurrentUser, task_id: str) -> None:
    """One assistant_continue job for the task (3 attempts; the partial
    unique index collapses a wake already queued). Best effort: the next
    poll repairs again."""
    from app.services import llm_queue

    try:
        llm_queue.enqueue(
            CONTINUE_JOB_TYPE,
            target_id=task_id,
            payload={"task_id": task_id, "user_id": user.id},
            created_by=user.id,
            priority=CONTINUE_PRIORITY,
            max_attempts_override=CONTINUE_MAX_ATTEMPTS,
        )
    except Exception:  # noqa: BLE001 - a lost wake is repaired on the next read
        logger.exception("assistant: continuation could not be enqueued for task %s", task_id)


def _repair_task(user: CurrentUser, task: dict, progress: dict) -> bool:
    """The lazy repair of 3.6.3 (phase 2 has no sweeper and a wake can be
    lost). True when the task row may have changed (re-read it).

    - waiting_external and ready, or older than 6 hours: enqueue the
      continuation (it fails a timed-out task itself);
    - running untouched for 10 minutes: fence it back to waiting_external,
      then enqueue;
    - pending past PENDING_TASK_REPAIR_SECONDS (a split-drop whose own
      compensation failed): fail it stage_failed, release its hold and
      unclaim its staged rows. A pending task never has a run: dispatch
      happens only after the task leaves pending."""
    task_id = str(task["id"])
    state = task.get("status")
    if state == "waiting_external":
        age = _age_seconds(task.get("created_at"))
        if progress.get("ready") is True or (age is not None and age > TASK_TIMEOUT_SECONDS):
            _enqueue_continue(user, task_id)
        return False
    if state == "running":
        idle = _age_seconds(task.get("updated_at") or task.get("created_at"))
        if idle is None or idle <= RUNNING_STALE_SECONDS:
            return False
        if repo.update_task(user.id, task_id, {"status": "waiting_external"}, expect_status="running"):
            _enqueue_continue(user, task_id)
        return True
    if state == "pending":
        age = _age_seconds(task.get("created_at"))
        if age is None or age <= _split_handoff().PENDING_TASK_REPAIR_SECONDS:
            return False
        failed = repo.update_task(user.id, task_id, {
            "status": "failed", "failure_reason": "stage_failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }, expect_status="pending")
        if failed:
            try:
                from app.assistant import spend

                spend.release(user.id, task.get("spend_reservation_id"))
            except Exception:  # noqa: BLE001 - an unreleased hold expires by itself
                logger.exception("assistant: hold of task %s could not be released", task_id)
            try:
                repo.unclaim_staged_files(user.id, task_id)
            except Exception:  # noqa: BLE001 - the rows stay claimed; logged for the build log
                logger.exception("assistant: staged files of task %s could not be unclaimed", task_id)
        return True
    return False


def _task_progress(task: dict) -> dict:
    """split_handoff.task_progress_for, or {} when it cannot be read (the
    card keeps polling; nothing is repaired on a failed read)."""
    try:
        out = _split_handoff().task_progress_for(task)
    except Exception:  # noqa: BLE001 - progress is best effort; the task row still answers
        logger.exception("assistant: progress of task %s could not be read", task.get("id"))
        return {}
    return out if isinstance(out, dict) else {}


_TASK_FINISHED = ("done", "failed", "canceled")
_RECORDED_FILE_KEYS = ("status", "outcome", "documents", "categories", "reason")


def _recorded_progress(task: dict, progress: dict) -> dict:
    """A finished task's card shows what the split did, not what the project
    holds now: each file's outcome recorded in `task.result` when the task
    finished (continue_job.result_for) replaces the live read. Without this,
    removing a filed document afterwards (the Files panel is where Activity
    sends the user to undo a split) turns a split file into Waiting and,
    after the grace, Missing. File names stay live (the result keeps none);
    a file the result does not name keeps its live outcome."""
    if task.get("status") not in _TASK_FINISHED:
        return progress
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    recorded = {
        str(f["split_file_id"]): f
        for f in result.get("files") or []
        if isinstance(f, dict) and f.get("split_file_id")
    }
    files = progress.get("files")
    if not recorded or not isinstance(files, list):
        return progress
    merged = []
    for f in files:
        r = recorded.get(str(f.get("split_file_id"))) if isinstance(f, dict) else None
        merged.append({**f, **{k: r[k] for k in _RECORDED_FILE_KEYS if k in r}} if r else f)
    return {**progress, "files": merged}


def _project_ref(project_id: Any) -> TaskProjectOut | None:
    if not project_id:
        return None
    number = None
    try:
        from app.core import supabase_client

        rows = (supabase_client.get_supabase().table("projects").select("id, number")
                .eq("id", str(project_id)).limit(1).execute()).data or []
        number = rows[0].get("number") if rows else None
    except Exception:  # noqa: BLE001 - the card shows the task without the number
        logger.exception("assistant: project %s could not be read for a task", project_id)
    return TaskProjectOut(id=str(project_id), number=str(number) if number else None)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _task_out(task: dict, progress: dict) -> TaskOut:
    project = progress.get("project")
    if isinstance(project, dict) and project.get("id"):
        project_out = TaskProjectOut(id=str(project["id"]),
                                     number=str(project["number"]) if project.get("number") else None)
    else:
        project_out = _project_ref(task.get("project_id"))
    job = progress.get("job") if isinstance(progress.get("job"), dict) else None
    files = progress.get("files") if isinstance(progress.get("files"), list) else []
    return TaskOut(
        id=str(task["id"]),
        kind=str(task.get("kind") or "split_into_project"),
        status=str(task.get("status") or "pending"),
        title=task.get("title"),
        project=project_out,
        conversation_id=task.get("conversation_id"),
        approval_id=task.get("approval_id"),
        failure_reason=task.get("failure_reason"),
        created_at=str(task.get("created_at") or ""),
        finished_at=task.get("finished_at"),
        job=TaskJobOut(
            id=str(job.get("id")), status=str(job.get("status") or ""),
            file_count=_int(job.get("file_count")), files_done=_int(job.get("files_done")),
            files_failed=_int(job.get("files_failed")),
        ) if job and job.get("id") else None,
        files=[
            TaskFileOut(
                split_file_id=str(f.get("split_file_id") or ""),
                filename=str(f.get("filename") or ""),
                status=str(f.get("status") or ""),
                outcome=str(f.get("outcome") or "pending"),
                documents=_int(f.get("documents")),
                categories={str(k): _int(v) for k, v in (f.get("categories") or {}).items()}
                if isinstance(f.get("categories"), dict) else {},
                reason=f.get("reason"),
            )
            for f in files if isinstance(f, dict)
        ],
        ready=progress.get("ready") is True,
    )


# ── Phase 4: AI Estimating tasks (3.5.4, 3.6.2, 3.6.4) ───────────────────────


def _is_ae_task(task: dict) -> bool:
    return str(task.get("kind") or "") in AE_TASK_KINDS


def _ae_finalize_due(task_id: str) -> bool:
    """At most one lazy finalize per task per AE_FINALIZE_INTERVAL_SECONDS in
    this process (the panel polls every few seconds)."""
    now = _ae_finalize_clock()
    with _ae_finalize_lock:
        last = _ae_finalize_last.get(task_id)
        if last is not None and now - last < AE_FINALIZE_INTERVAL_SECONDS:
            return False
        if len(_ae_finalize_last) >= _AE_FINALIZE_MAX_TRACKED:
            cutoff = now - AE_FINALIZE_INTERVAL_SECONDS
            for key in [k for k, v in _ae_finalize_last.items() if v < cutoff]:
                _ae_finalize_last.pop(key, None)
            if len(_ae_finalize_last) >= _AE_FINALIZE_MAX_TRACKED:
                _ae_finalize_last.clear()
        _ae_finalize_last[task_id] = now
        return True


def _finalize_ae_task(user: CurrentUser, task: dict) -> bool:
    """The lazy finalize of an open AI Estimating task (3.5.4): calls
    plans_tasks.advance (WI-11) at most once per 10 seconds per task, and
    never for an owner who fails access.owner_check (no live delegation:
    the offboarding hook stopped those tasks; plan 3.24). True when the task
    may have changed. Best effort: a failure leaves the task for
    ae_task_sweep."""
    if str(task.get("status") or "") not in repo.OPEN_TASK_STATUSES:
        return False
    task_id = str(task["id"])
    if not _ae_finalize_due(task_id):
        return False
    try:
        if owner_check(str(user.id), sub_app=None).problem is not None:
            return False
        from app.assistant import plans_tasks

        return bool(plans_tasks.advance(str(user.id), task))
    except Exception:  # noqa: BLE001 - the sweep finalizes it; the card keeps polling
        logger.exception("assistant: lazy finalize of task %s failed", task_id)
        return False


def _plans_run_id(task: dict) -> str | None:
    """The AI Estimating run a task's card links to: payload.plans_run_id
    (both kinds; the composite run route sets it at phase `created`), else
    payload.run_id. WI-9a also writes the AE run id to the task's run_id
    column, but this read never relies on it (phase 2 split tasks use that
    column for the assistant run)."""
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    for key in ("plans_run_id", "run_id"):
        value = payload.get(key)
        if value and is_uuid(str(value)):
            return str(value)
    return None


def _ae_task_out(task: dict) -> AeTaskOut:
    base = _task_out(task, {})
    result = task.get("result") if isinstance(task.get("result"), dict) else None
    if result is None and task.get("kind") == "plans_read_pages":
        # While the read runs there is no settled result yet; the page count
        # from the card's payload lets the headline say "Reading N pages"
        # (review FE-3). No cost key, so the card never reads as done.
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        pages = payload.get("pages") if isinstance(payload.get("pages"), list) else []
        result = {"pages": len(pages)} if pages else None
    return AeTaskOut(**base.model_dump(), plans_run_id=_plans_run_id(task), result=result)


def _any_task_out(task: dict, progress: dict | None = None) -> TaskOut:
    if _is_ae_task(task):
        return _ae_task_out(task)
    return _task_out(task, progress or {})


# A task in one of these statuses changed nothing the card promised (3.5.2).
TASKS_WITHOUT_EFFECT = ("failed", "canceled", "cancelled")


@router.get(TASK_BY_APPROVAL_ROUTE.removeprefix("/assistant"), response_model=TaskOut | AeTaskOut)
def task_by_approval(approval_id: str, user: CurrentUser = Depends(require_internal)) -> TaskOut:
    """The task an approved card created: the recover read of
    plans_read_pages and run_ai_estimating (3.5.2, 3.6.2), which the execute
    job sends after a 5xx or a transport failure. The assistant's own: 404
    for a human, for a key that is not a uuid and for another user's card.
    A task that ended failed or canceled is 404 too: the route closed it
    without effect (task failed, hold released), so the card fails plainly
    instead of reading executed. A plain read: nothing is repaired or
    finalized here."""
    _require_assistant_principal(user)
    _require_assistant(user)
    key = (approval_id or "").strip()
    task = repo.task_by_approval(user.id, key) if is_uuid(key) else None
    if task is None or str(task.get("status") or "") in TASKS_WITHOUT_EFFECT:
        raise _detail_error(status.HTTP_404_NOT_FOUND, TASK_NOT_FOUND, "Task not found.")
    return _any_task_out(task)


@router.get("/tasks/{task_id}", response_model=TaskOut | AeTaskOut)
def read_task(task_id: str, user: CurrentUser = Depends(require_internal)) -> TaskOut:
    """The caller's own task with its per-file progress (3.6.3). The panel
    polls it, so the lazy repair runs here: the route writes (fences and
    enqueues), hence the assistant principal is refused. Phase 4: an AI
    Estimating task (plans_read_pages, ai_estimating_run) has no split
    progress and no continuation; it is lazily finalized instead and
    answers AeTaskOut (its AE run and its result)."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    task_id = (task_id or "").strip()
    if not is_uuid(task_id):
        raise _detail_error(status.HTTP_404_NOT_FOUND, TASK_NOT_FOUND, "Task not found.")
    task = repo.get_task(user.id, task_id)
    if task is None:
        raise _detail_error(status.HTTP_404_NOT_FOUND, TASK_NOT_FOUND, "Task not found.")
    if _is_ae_task(task):
        if _finalize_ae_task(user, task):
            task = repo.get_task(user.id, task_id) or task
        return _ae_task_out(task)
    progress = _task_progress(task)
    if _repair_task(user, task, progress):
        task = repo.get_task(user.id, task_id) or task
    return _task_out(task, _recorded_progress(task, progress))


# ── Phase 4: find_tools (3.16.2) ─────────────────────────────────────────────


def _find_mode(user: CurrentUser) -> str:
    """The conversation mode of the turn that called find_tools, from the
    run's own ledger rows (never from model arguments). Normal when it
    cannot be read: Mailbox review only narrows the toolset, and every tool
    the answer names is checked again by the loop and the executor."""
    try:
        conversation_id = repo.conversation_for_run(user.id, user.via_assistant_run_id)
        conversation = repo.get_conversation(user.id, conversation_id) if conversation_id else None
    except Exception:  # noqa: BLE001 - a lost read keeps the default mode
        logger.exception("assistant: the conversation of run %s could not be read", user.via_assistant_run_id)
        conversation = None
    if (conversation or {}).get("mailbox_tainted_at"):
        return "mailbox_review"
    return str((conversation or {}).get("mode") or "unavailable")


@router.get(FIND_TOOLS_ROUTE.removeprefix("/assistant"), response_model=FindToolsOut)
def find_tools(
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=FIND_LIMIT_DEFAULT, ge=1, le=FIND_LIMIT_MAX),
    user: CurrentUser = Depends(require_internal),
) -> FindToolsOut:
    """find_tools (3.16.2): the deferred tools of the caller's current
    toolset (live role, sub-app flags, disabled tools, the conversation
    mode) that best match `query`. The assistant's own: a human gets the
    bare 404. Core tools are never returned (they are always present)."""
    _require_assistant_principal(user)
    settings_row, _profile_row = _require_assistant(user)
    # Checked here, after the principal gate, so a human always gets the 404.
    text = (query or "").strip()
    if not text:
        raise _detail_error(status.HTTP_422_UNPROCESSABLE_CONTENT, FIND_QUERY_REQUIRED,
                            "Say in a few words what the tool should do.")
    from app.assistant import find, registry

    specs = registry.toolset_for(user, enabled_map(user.role), settings_row=settings_row, mode=_find_mode(user))
    deferred = {s.name: s for s in specs if s.deferred}
    found = [s for s in find.resolve(text, list(specs), limit) if s.name in deferred][:limit]
    return FindToolsOut(
        tools=[FindToolItemOut(name=s.name, description=str(s.description or "")[:FIND_DESCRIPTION_MAX])
               for s in found],
        hint=FIND_HINT_FOUND if found else FIND_HINT_NONE,
    )


@router.get("/split/status", response_model=SplitStatusOut,
            dependencies=[Depends(require_feature(SubApp.BIDDING))])
def split_status(
    project_id: str | None = Query(default=None, max_length=64),
    user: CurrentUser = Depends(require_internal),
) -> SplitStatusOut:
    """What the Bid File Splitter can do for the caller now (3.10.3): the
    splitter state, the caller's staged files in the turn's conversation,
    their manual jobs and tasks, the project's lock and the spend left. The
    assistant reads it through splitter_status, so a bdra_ principal is
    allowed here."""
    _require_assistant(user)
    pid = (project_id or "").strip() or None
    if pid is not None and not is_uuid(pid):
        raise _detail_error(status.HTTP_404_NOT_FOUND, PROJECT_NOT_FOUND, "Project not found.")
    svc = _split_handoff()
    try:
        out = svc.status_for(user, pid)
    except svc.SplitRefused as exc:
        raise _detail_error(exc.status, exc.code, exc.message, **exc.detail) from None
    return SplitStatusOut(**out)


def features_flag(user: CurrentUser) -> bool:
    """The "assistant" key of GET /features (docs/ASSISTANT.md, Permissions
    item 15): ASSISTANT_ENABLED and assistant_settings.enabled and
    assistant_profiles.enabled (default true with no row) and an internal
    role. main.py calls this through the router module, never through
    app.assistant, and it fails closed on any read error. GET /features is
    allowed at aal1; this reads two rows and nothing of the user's data."""
    if not getattr(get_settings(), "assistant_enabled", False):
        return False
    if user is None or user.role not in INTERNAL_ROLES:
        return False
    try:
        return assistant_enabled_for(user, *_rows_for(user))
    except Exception:  # noqa: BLE001 - a flag read must never break the shell
        logger.exception("assistant features flag: read failed")
        return False

"""The assistant's own tables (docs/ASSISTANT.md, Permissions item 14).

Every function takes `user_id` first, and every query on an assistant_*
table filters by it (the two queue adapters included: they take the job
owner's id from the job payload). The id always comes from the session or
the job owner, never from a tool argument. No other module in the app queries an
assistant_* table; tests/test_assistant_repo_isolation pins both rules.
The column names follow supabase/migrations/0170_assistant_core.sql; the
approvals functions (phase 1) also use 0172's reverts_approval_id,
taint_corpus and the decide and supersede RPCs. Phase 2
(docs/ASSISTANT_PHASE2_PLAN.md) adds the staged files, the split tasks and
the spend reservations of 0174. Phase 3 (docs/ASSISTANT_PHASE3_PLAN.md)
adds the schedules and their runs, memory, the sweep claims, the trusted
turn context of the housekeeping tool routes and the offboarding reads and
writes of 0176. Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md, WI-5) adds the
reason-carrying approve, the evidence access log, the report files, memory
search, the AE holds and tasks of the expensive tools, and the server_data
message flag of 0180.

System sweeps take `user_id: str | None`: None is the SYSTEM SWEEP across
every user, called only from the callers tests/test_assistant_repo_isolation
names (the scheduler and the maintenance riders); a person's id scopes the
same read to that person.

Plain `def` throughout: the Supabase SDK is synchronous and the callers are
threadpool route handlers or queue job bodies.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core import supabase_client

logger = logging.getLogger(__name__)

SETTINGS_TABLE = "assistant_settings"
PROFILES_TABLE = "assistant_profiles"
DELEGATIONS_TABLE = "assistant_delegations"
CONVERSATIONS_TABLE = "assistant_conversations"
MESSAGES_TABLE = "assistant_messages"
TOOL_RESULTS_TABLE = "assistant_tool_results"
TOOL_CALLS_TABLE = "assistant_tool_calls"
APPROVALS_TABLE = "assistant_approvals"
CALL_LOG_TABLE = "llm_call_log"
HUMAN_PROFILES_TABLE = "profiles"
BEGIN_RPC = "assistant_tool_call_begin"
DECIDE_RPC = "assistant_approval_decide"
SUPERSEDE_RPC = "assistant_approval_supersede"
ONE_REVERT_INDEX = "assistant_approvals_one_revert_uq"

ASSISTANT_FEATURES = ("assistant", "assistant_extract")
ACTIVE_STATUSES = ("pending", "running")
_PAGE = 1000
_MAX_PAGES = 5

CONVERSATION_COLUMNS = (
    "id, user_id, title, surface, mode, project_id, tainted_at, mailbox_tainted_at, "
    "last_message_at, archived_at, created_at, updated_at"
)
MESSAGE_COLUMNS = (
    "id, conversation_id, user_id, role, content, blocks, tool_calls, context, status, "
    "error_kind, llm_job_id, model, input_tokens, cached_tokens, output_tokens, "
    "reasoning_tokens, cost_usd, tainted, mailbox_tainted, incognito, used_memory_ids, "
    "created_at, updated_at, completed_at, server_data"
)
PROFILE_COLUMNS = (
    "user_id, display_name, enabled, timezone, daily_cost_cap_usd, quiet_hours, "
    "calendar_mode, legal_hold, created_at, updated_at"
)
SETTINGS_COLUMNS = (
    "id, enabled, model, global_daily_cap_usd, disabled_tools, holidays, proactive_enabled, updated_at"
)
# Every 0170 column of assistant_approvals plus 0172's reverts_approval_id.
APPROVAL_COLUMNS = (
    "id, user_id, user_label, conversation_id, message_id, tool_call_id, project_id, action_kind, "
    "payload, payload_sha256, preview, precondition, status, expired_reason, decided_by, decided_at, "
    "decided_session_aal, execute_job_id, executed_at, result, email_log_id, expires_at, "
    "superseded_by, created_at, updated_at, reverts_approval_id"
)
TOOL_CALL_COLUMNS = (
    "id, user_id, conversation_id, message_id, run_id, seq, idempotency_key, phase, tool_name, "
    "tool_version, effect, method, path_format, before_snapshot, after_snapshot, result_summary, "
    "ok, error_kind, project_id, approval_id, tainted_context, duration_ms, created_at"
)
# Approvals that keep their message waiting (settle_message).
LIVE_APPROVAL_STATUSES = ("pending", "approved", "executing")

# Phase 2 (0174).
FILES_TABLE = "assistant_files"
TASKS_TABLE = "assistant_tasks"
RESERVATIONS_TABLE = "assistant_spend_reservations"
SPEND_STATE_RPC = "assistant_spend_state"
SPEND_RESERVE_RPC = "assistant_spend_reserve"
TASK_APPROVAL_INDEX = "assistant_tasks_approval_uq"
SPLIT_TOOL = "split_files_into_project"
STAGING_TTL_SECONDS = 7 * 24 * 3600        # the column default (0170): staged files live 7 days
CONSUMED_PURGE_MARGIN_SECONDS = 30 * 60    # a claim this fresh may still be copying (> the 10 min pending repair)
FILE_COLUMNS = (
    "id, user_id, conversation_id, message_id, filename, storage_path, size_bytes, sha256, mime, "
    "page_count, consumed_at, consumed_project_id, consumed_task_id, expires_at, purged_at, created_at"
)
TASK_COLUMNS = (
    "id, user_id, kind, status, title, instruction, project_id, conversation_id, message_id, "
    "split_job_id, payload, result, failure_reason, created_by, finished_at, created_at, updated_at, "
    "approval_id, run_id, spend_reservation_id, result_message_id"
)
RESERVATION_COLUMNS = (
    "id, user_id, kind, run_id, task_id, approval_id, amount_usd, status, actual_usd, "
    "created_at, expires_at, settled_at"
)
# Tasks a splitter job may still wake (the hook, the continuation).
OPEN_TASK_STATUSES = ("pending", "waiting_external", "running")


def _sb():
    return supabase_client.get_supabase()


def _rows(resp: Any) -> list[dict]:
    data = getattr(resp, "data", None)
    return [r for r in (data or []) if isinstance(r, dict)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_after(seconds: float) -> str:
    """now() plus `seconds`, as an ISO 8601 UTC string (the fences that
    need a margin past now, which a PostgREST filter cannot compute)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _ids(values: Sequence[Any] | None) -> list[str]:
    return [str(v) for v in values or () if v]


def _uuids(values: Sequence[Any] | None) -> list[str]:
    """The uuid strings among `values`, deduped in order (a uuid[] argument
    with anything else would be a 22P02 from PostgREST)."""
    out: list[str] = []
    for value in values or ():
        try:
            text = str(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
        if text not in out:
            out.append(text)
    return out


def _money(value: Any) -> str:
    """A money amount for an RPC argument or a numeric column: a string, so
    the exact cents survive (a Decimal is not JSON serializable, a float
    rounds); PostgREST casts it to numeric."""
    return str(_dec(value))


class RevertExists(RuntimeError):
    """A live or executed revert of this approval already exists (the
    unique index assistant_approvals_one_revert_uq refused the insert)."""


class TaskExists(RuntimeError):
    """A task for this approval already exists (the partial unique index
    assistant_tasks_approval_uq refused the insert: a concurrent replay of
    the same card). The text keeps the database's own message, 23505 and
    the index name included."""

    code = "23505"


def _rpc_data(resp: Any) -> dict:
    data = getattr(resp, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else {}


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


# ── Settings, profiles, delegations ──────────────────────────────────────────


def settings_row(user_id: str) -> dict | None:
    """The company row (id = 1). `user_id` names the caller for the uniform
    signature; the row is global."""
    rows = _rows(_sb().table(SETTINGS_TABLE).select(SETTINGS_COLUMNS).eq("id", 1).limit(1).execute())
    return rows[0] if rows else None


def human_profile(user_id: str) -> dict | None:
    """The person's profiles row: the name the label derives from, the live
    role and the active flag."""
    rows = _rows(
        _sb().table(HUMAN_PROFILES_TABLE)
        .select("id, full_name, email, role, is_active, is_dev, rfp_mailboxes")
        .eq("id", user_id).limit(1).execute()
    )
    return rows[0] if rows else None


def profile_row(user_id: str) -> dict | None:
    rows = _rows(
        _sb().table(PROFILES_TABLE).select(PROFILE_COLUMNS).eq("user_id", user_id).limit(1).execute()
    )
    return rows[0] if rows else None


def ensure_profile(user_id: str) -> dict:
    """The person's assistant_profiles row, created with the column defaults
    on first use (every internal user gets one)."""
    row = profile_row(user_id)
    if row is not None:
        return row
    _sb().table(PROFILES_TABLE).upsert(
        {"user_id": user_id}, on_conflict="user_id", ignore_duplicates=True
    ).execute()
    row = profile_row(user_id)
    if row is None:
        raise RuntimeError("assistant profile could not be created")
    return row


def update_profile(user_id: str, patch: Mapping[str, Any]) -> dict:
    ensure_profile(user_id)
    rows = _rows(
        _sb().table(PROFILES_TABLE)
        .update({**dict(patch), "updated_at": _now_iso()})
        .eq("user_id", user_id).execute()
    )
    return rows[0] if rows else (profile_row(user_id) or {})


def live_delegation(user_id: str) -> dict | None:
    rows = _rows(
        _sb().table(DELEGATIONS_TABLE)
        .select("id, user_id, created_session_aal, created_at, expires_at, revoked_at")
        .eq("user_id", user_id).is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .limit(1).execute()
    )
    return rows[0] if rows else None


def delegation_session_aal_allowed(session_aal: str) -> bool:
    """Whether a session at this assurance level may create the grant
    (Permissions item 2). aal2 always may. aal1 may only while
    settings.mfa_required is False (dev and test; production refuses to boot
    that way unless break-glass is acknowledged). The setting is read from
    get_settings() on every call, never cached at import."""
    if session_aal == "aal2":
        return True
    from app.core.config import get_settings

    return session_aal == "aal1" and not get_settings().mfa_required


def ensure_delegation(user_id: str, session_aal: str) -> dict:
    """The person's live grant, created under their own session when none
    exists (Permissions item 2). aal2 always; aal1 only while mfa_required is
    off, recorded as created_session_aal 'aal1'. Refuses anything else with
    PermissionError. While mfa_required is on, a live grant minted at aal1
    (under the dev rule) is revoked with reason mfa_required and replaced."""
    if not delegation_session_aal_allowed(session_aal):
        raise PermissionError(f"assistant delegation needs an aal2 session (got {session_aal!r})")
    row = live_delegation(user_id)
    if row is not None and row.get("created_session_aal") == "aal1" and session_aal == "aal2":
        from app.core.config import get_settings

        if get_settings().mfa_required:
            _sb().table(DELEGATIONS_TABLE).update({
                "revoked_at": _now_iso(), "revoked_reason": "mfa_required",
            }).eq("id", row["id"]).is_("revoked_at", "null").execute()
            logger.info("assistant delegation %s revoked: aal1 grant while mfa_required is on", row["id"])
            row = None
    if row is not None:
        return row
    # The partial unique index allows one unrevoked row per user, so an
    # expired-but-unrevoked grant is revoked (reason expired) before a
    # fresh one is minted; otherwise the insert fails with 23505.
    _sb().table(DELEGATIONS_TABLE).update({
        "revoked_at": _now_iso(), "revoked_reason": "expired",
    }).eq("user_id", user_id).is_("revoked_at", "null").lt("expires_at", _now_iso()).execute()
    rows = _rows(
        _sb().table(DELEGATIONS_TABLE)
        .insert({"user_id": user_id, "created_session_aal": session_aal}).execute()
    )
    if session_aal == "aal1":
        # The reason is the setting itself: aal1 is admitted only with mfa_required off.
        logger.info("assistant delegation for %s created at aal1 because mfa_required is off", user_id)
    return rows[0] if rows else (live_delegation(user_id) or {})


# ── Conversations ────────────────────────────────────────────────────────────


def list_conversations(user_id: str, limit: int = 20, *,
                       surfaces: Sequence[str] | None = None) -> list[dict]:
    """The person's unarchived conversations, newest first; `surfaces`
    keeps only those surfaces (the Chat tab lists panel, page and task, so
    a routine's own conversation opens from Routines only)."""
    query = (
        _sb().table(CONVERSATIONS_TABLE).select(CONVERSATION_COLUMNS)
        .eq("user_id", user_id).is_("archived_at", "null")
    )
    if surfaces is not None:
        query = query.in_("surface", [str(s) for s in surfaces])
    return _rows(
        query.order("last_message_at", desc=True).order("created_at", desc=True)
        .limit(max(1, min(100, int(limit)))).execute()
    )


def get_conversation(user_id: str, conversation_id: str) -> dict | None:
    rows = _rows(
        _sb().table(CONVERSATIONS_TABLE).select(CONVERSATION_COLUMNS)
        .eq("user_id", user_id).eq("id", conversation_id).limit(1).execute()
    )
    return rows[0] if rows else None


def create_conversation(
    user_id: str, *, surface: str, project_id: str | None = None, title: str | None = None,
    mode: str = "normal",
) -> dict:
    rows = _rows(
        _sb().table(CONVERSATIONS_TABLE).insert({
            "user_id": user_id,
            "surface": surface,
            "mode": mode,
            "project_id": project_id,
            "title": title,
        }).execute()
    )
    if not rows:
        raise RuntimeError("conversation insert returned no row")
    return rows[0]


def update_conversation(user_id: str, conversation_id: str, patch: Mapping[str, Any]) -> None:
    _sb().table(CONVERSATIONS_TABLE).update({**dict(patch), "updated_at": _now_iso()}) \
        .eq("user_id", user_id).eq("id", conversation_id).execute()


def mark_conversation_tainted(user_id: str, conversation_id: str, *, mailbox: bool = False) -> None:
    """Set tainted_at (and mailbox_tainted_at) once; the trigger keeps them
    sticky, and this never writes over a value already there."""
    column = "mailbox_tainted_at" if mailbox else "tainted_at"
    _sb().table(CONVERSATIONS_TABLE).update({column: _now_iso(), "updated_at": _now_iso()}) \
        .eq("user_id", user_id).eq("id", conversation_id).is_(column, "null").execute()
    if mailbox:
        mark_conversation_tainted(user_id, conversation_id, mailbox=False)


# ── Messages ─────────────────────────────────────────────────────────────────


def active_turn(user_id: str, conversation_id: str) -> dict | None:
    rows = _rows(
        _sb().table(MESSAGES_TABLE).select(MESSAGE_COLUMNS)
        .eq("user_id", user_id).eq("conversation_id", conversation_id)
        .in_("status", list(ACTIVE_STATUSES)).limit(1).execute()
    )
    return rows[0] if rows else None


def list_messages(user_id: str, conversation_id: str, limit: int = 200) -> list[dict]:
    return _rows(
        _sb().table(MESSAGES_TABLE).select(MESSAGE_COLUMNS)
        .eq("user_id", user_id).eq("conversation_id", conversation_id)
        .order("created_at").order("id").limit(max(1, min(500, int(limit)))).execute()
    )


def get_message(user_id: str, message_id: str) -> dict | None:
    rows = _rows(
        _sb().table(MESSAGES_TABLE).select(MESSAGE_COLUMNS)
        .eq("user_id", user_id).eq("id", message_id).limit(1).execute()
    )
    return rows[0] if rows else None


def insert_message(user_id: str, row: Mapping[str, Any]) -> dict:
    """Insert one message for `user_id`. The partial unique index
    assistant_messages_one_active_uq refuses a second pending or running
    row per conversation; the caller maps that to 409 turn_in_progress.
    An explicit `id` in `row` is kept (the continuation's deterministic
    message id, docs/ASSISTANT_PHASE2_PLAN.md 3.7 step 7): a second insert
    with the same id fails with 23505 on the primary key."""
    rows = _rows(_sb().table(MESSAGES_TABLE).insert({**dict(row), "user_id": user_id}).execute())
    if not rows:
        raise RuntimeError("message insert returned no row")
    return rows[0]


def update_message(
    user_id: str, message_id: str, patch: Mapping[str, Any], *, expect_status: str | None = None
) -> bool:
    """Patch one message; with `expect_status` the update is fenced on the
    current status (a late worker never overwrites a finished row)."""
    query = (
        _sb().table(MESSAGES_TABLE).update({**dict(patch), "updated_at": _now_iso()})
        .eq("user_id", user_id).eq("id", message_id)
    )
    if expect_status is not None:
        query = query.eq("status", expect_status)
    return bool(_rows(query.execute()))


def claim_turn(user_id: str, message_id: str) -> bool:
    """pending -> running, exactly once."""
    return update_message(user_id, message_id, {"status": "running"}, expect_status="pending")


def delete_message(user_id: str, message_id: str) -> None:
    """Remove one message of `user_id` (the router undoes a user row whose
    answer could not be started, so no orphan question is replayed)."""
    _sb().table(MESSAGES_TABLE).delete().eq("user_id", user_id).eq("id", message_id).execute()


def _require_owner(user_id: str | None) -> str:
    """The queue adapters take the job owner's id (payload user_id); a call
    without one is refused instead of widening the filter (item 14)."""
    owner = str(user_id or "").strip()
    if not owner:
        raise ValueError("assistant queue adapter needs the job owner's user_id")
    return owner


def queue_mark_turn(user_id: str, conversation_id: str, fields: Mapping[str, Any]) -> None:
    """The llm_queue domain mark for job type assistant_turn (target =
    conversation id). `user_id` is the job owner from the job payload
    (app/assistant/jobs.py resolves it); the filter is the owner plus the
    conversation plus an active status."""
    owner = _require_owner(user_id)
    status = fields.get("status")
    patch: dict[str, Any] = {"updated_at": _now_iso()}
    if status == "failed":
        patch["status"] = "failed"
        patch["error_kind"] = "interrupted"
        patch["completed_at"] = _now_iso()
        error = fields.get("error")
        if error:
            patch["blocks"] = [{"type": "error", "text": str(error)}]
    elif status == "pending":
        patch["status"] = "pending"
    else:
        return
    _sb().table(MESSAGES_TABLE).update(patch) \
        .eq("user_id", owner).eq("conversation_id", conversation_id).eq("role", "assistant") \
        .in_("status", list(ACTIVE_STATUSES)).execute()


def queue_turn_status(user_id: str, conversation_id: str) -> str | None:
    """The status of the conversation's active turn, for the queue's
    current_status adapter (None when there is none). `user_id` is the job
    owner, as for queue_mark_turn."""
    owner = _require_owner(user_id)
    rows = _rows(
        _sb().table(MESSAGES_TABLE).select("status")
        .eq("user_id", owner).eq("conversation_id", conversation_id).eq("role", "assistant")
        .in_("status", list(ACTIVE_STATUSES)).limit(1).execute()
    )
    return rows[0].get("status") if rows else None


# ── Tool call ledger and out-of-line results ─────────────────────────────────


def tool_call_begin(user_id: str, **params: Any) -> dict:
    """assistant_tool_call_begin: inserts the begun row or returns the rows
    already recorded for the key. `params` are the RPC's p_* arguments
    without the prefix (user_id is always this function's first argument)."""
    args = {f"p_{k}": v for k, v in params.items()}
    args["p_user_id"] = user_id
    resp = _sb().rpc(BEGIN_RPC, args).execute()
    data = getattr(resp, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise RuntimeError("assistant_tool_call_begin returned no result")
    return data


def tool_call_finish(user_id: str, row: Mapping[str, Any]) -> dict:
    """The terminal ledger row (phase completed or failed); the fence
    trigger checks the begun row and the lease owner."""
    rows = _rows(_sb().table(TOOL_CALLS_TABLE).insert({**dict(row), "user_id": user_id}).execute())
    return rows[0] if rows else {}


def insert_tool_result(user_id: str, row: Mapping[str, Any]) -> dict:
    """One out-of-line result. `memory_ids` (phase 3, a memory tool's result:
    the full ids behind the short ids the model saw) is passed through
    deduped, or as None when empty; a permanent delete finds the result by
    it (0176 B6)."""
    values = {**dict(row), "user_id": user_id}
    if "memory_ids" in values:
        values["memory_ids"] = _uuids(values.get("memory_ids")) or None
    rows = _rows(_sb().table(TOOL_RESULTS_TABLE).insert(values).execute())
    return rows[0] if rows else {}


# ── Spend reservations (phase 2, 0174) ───────────────────────────────────────
#
# The admission and the state live in SQL (assistant_spend_reserve takes one
# advisory lock across both uvicorn workers); app/assistant/spend.py wraps
# these. Money goes in as strings and comes back as Decimal.


def rpc_spend_state(user_id: str, user_cap_default: Any, global_cap_default: Any) -> dict:
    """RPC assistant_spend_state: {user_cap, user_spent, user_held,
    global_cap, global_spent, global_held} for today (Pacific), live holds
    counted as their unused part. The defaults apply when the person has no
    assistant_profiles row or the company row is missing."""
    data = _rpc_data(_sb().rpc(SPEND_STATE_RPC, {
        "p_user_id": user_id,
        "p_user_cap_default": _money(user_cap_default),
        "p_global_cap_default": _money(global_cap_default),
    }).execute())
    if "user_cap" not in data:
        raise RuntimeError("assistant_spend_state returned no result")
    return data


def rpc_spend_reserve(
    user_id: str,
    kind: str,
    amount: Any,
    ttl_seconds: int,
    user_cap_default: Any,
    global_cap_default: Any,
    run_id: str | None = None,
    task_id: str | None = None,
    approval_id: str | None = None,
) -> dict:
    """RPC assistant_spend_reserve: {"ok", "reason" ("user_cap" |
    "global_cap" | None), "id" (the hold, None when refused), "state"}. The
    arguments follow the SQL function's order without the p_ prefix."""
    data = _rpc_data(_sb().rpc(SPEND_RESERVE_RPC, {
        "p_user_id": user_id,
        "p_kind": kind,
        "p_amount": _money(amount),
        "p_ttl_seconds": int(ttl_seconds),
        "p_user_cap_default": _money(user_cap_default),
        "p_global_cap_default": _money(global_cap_default),
        "p_run_id": str(run_id) if run_id else None,
        "p_task_id": str(task_id) if task_id else None,
        "p_approval_id": str(approval_id) if approval_id else None,
    }).execute())
    if "ok" not in data:
        raise RuntimeError("assistant_spend_reserve returned no result")
    return data


def logged_cost_for_hold(user_id: str, hold_id: str | None) -> Decimal:
    """The llm_call_log cost stamped with this hold (the calls it covered)."""
    if not hold_id:
        return Decimal("0")
    total = Decimal("0")
    for page in range(_MAX_PAGES):
        chunk = _rows(
            _sb().table(CALL_LOG_TABLE).select("id, cost_usd")
            .eq("user_id", user_id).eq("spend_reservation_id", str(hold_id))
            .order("id").range(page * _PAGE, (page + 1) * _PAGE - 1).execute()
        )
        for row in chunk:
            total += _dec(row.get("cost_usd"))
        if len(chunk) < _PAGE:
            return total
    logger.error("assistant spend: more than %d calls on hold %s; sum truncated", _PAGE * _MAX_PAGES, hold_id)
    return total


def get_reservation(user_id: str, hold_id: str | None) -> dict | None:
    if not hold_id:
        return None
    rows = _rows(
        _sb().table(RESERVATIONS_TABLE).select(RESERVATION_COLUMNS)
        .eq("user_id", user_id).eq("id", str(hold_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def close_reservation(user_id: str, hold_id: str | None, status: str, actual_usd: Any = None) -> bool:
    """active -> settled or released, once (fenced on status='active'), with
    actual_usd = the logged cost on the hold, or `actual_usd` when given
    (phase 4: the holds of plans_pages and ai_estimate close at the AE
    ledger's cost, because their calls carry no spend_reservation_id; 0180
    A8 counts a released hold's actual too). False when there is no such
    active hold (already closed, or not this person's)."""
    if status not in ("settled", "released"):
        raise ValueError(f"a hold closes as settled or released, not {status!r}")
    if not hold_id:
        return False
    actual = logged_cost_for_hold(user_id, hold_id) if actual_usd is None else _dec(actual_usd)
    if actual < 0:
        raise ValueError("a hold cannot close with a negative actual")
    return bool(_rows(
        _sb().table(RESERVATIONS_TABLE)
        .update({"status": status, "actual_usd": _money(actual), "settled_at": _now_iso()})
        .eq("user_id", user_id).eq("id", str(hold_id)).eq("status", "active").execute()
    ))


def hold_usage(user_id: str, hold_id: str | None) -> dict | None:
    """{status, amount_usd, logged_usd, expires_at} for the split hold guard
    (docs/ASSISTANT_PHASE2_PLAN.md 3.5.1); None when the hold is not this
    person's. The two amounts are Decimal."""
    row = get_reservation(user_id, hold_id)
    if row is None:
        return None
    return {
        "status": row.get("status"),
        "amount_usd": _dec(row.get("amount_usd")),
        "logged_usd": logged_cost_for_hold(user_id, hold_id),
        "expires_at": row.get("expires_at"),
    }


# ── Approvals (phase 1) ──────────────────────────────────────────────────────


def insert_approval(user_id: str, row: Mapping[str, Any]) -> dict:
    """Insert one approval card for `user_id` with the explicit id the
    proposal pre-generated. A 23505 on assistant_approvals_one_revert_uq
    raises RevertExists."""
    try:
        rows = _rows(_sb().table(APPROVALS_TABLE).insert({**dict(row), "user_id": user_id}).execute())
    except Exception as exc:
        if ONE_REVERT_INDEX in str(exc):
            raise RevertExists("a revert of this change already exists") from exc
        raise
    if not rows:
        raise RuntimeError("approval insert returned no row")
    return rows[0]


def supersede_approval(user_id: str, old_id: str, new_row: Mapping[str, Any]) -> dict:
    """RPC assistant_approval_supersede (0172): the old row goes from
    executing to expired (changed_since_preview, superseded_by = new id)
    and the new pending row is inserted, in one transaction. Returns
    {"ok", "reason", "row"}; reason "not_executing" when the old row has
    already left executing."""
    try:
        resp = _sb().rpc(SUPERSEDE_RPC, {
            "p_user_id": user_id,
            "p_old_id": old_id,
            "p_new": {**dict(new_row), "user_id": user_id},
        }).execute()
    except Exception as exc:
        if ONE_REVERT_INDEX in str(exc):
            raise RevertExists("a revert of this change already exists") from exc
        raise
    data = _rpc_data(resp)
    if "ok" not in data:
        raise RuntimeError("assistant_approval_supersede returned no result")
    return data


def get_approval(user_id: str, approval_id: str) -> dict | None:
    rows = _rows(
        _sb().table(APPROVALS_TABLE).select(APPROVAL_COLUMNS)
        .eq("user_id", user_id).eq("id", approval_id).limit(1).execute()
    )
    return rows[0] if rows else None


def approval_for_principal(user_id: str, approval_id: str) -> dict | None:
    """The slice the bdra_ principal checks for an approval-bound token."""
    rows = _rows(
        _sb().table(APPROVALS_TABLE).select("id, user_id, status, action_kind")
        .eq("user_id", user_id).eq("id", approval_id).limit(1).execute()
    )
    return rows[0] if rows else None


def list_approvals_for_message(user_id: str, message_id: str) -> list[dict]:
    return _rows(
        _sb().table(APPROVALS_TABLE).select(APPROVAL_COLUMNS)
        .eq("user_id", user_id).eq("message_id", message_id)
        .order("created_at").order("id").limit(50).execute()
    )


def list_activity(user_id: str, *, limit: int = 20, before: str | None = None) -> list[dict]:
    """The person's approvals, newest first; `before` is the created_at of
    the last item of the previous page."""
    query = _sb().table(APPROVALS_TABLE).select(APPROVAL_COLUMNS).eq("user_id", user_id)
    if before:
        query = query.lt("created_at", before)
    return _rows(
        query.order("created_at", desc=True).order("id", desc=True)
        .limit(max(1, min(50, int(limit)))).execute()
    )


def decide_approval(
    user_id: str,
    approval_id: str,
    *,
    decision: str,
    payload_sha256: str | None,
    precondition: Mapping[str, Any] | None,
    session_aal: str,
) -> dict:
    """RPC assistant_approval_decide (0172), the compare-and-set click.
    Returns {"ok", "reason", "row"}; reasons not_found, expired, superseded,
    not_pending and stale."""
    resp = _sb().rpc(DECIDE_RPC, {
        "p_user_id": user_id,
        "p_approval_id": approval_id,
        "p_decision": decision,
        "p_payload_sha256": payload_sha256,
        "p_precondition": dict(precondition) if precondition is not None else None,
        "p_session_aal": session_aal,
    }).execute()
    data = _rpc_data(resp)
    if "ok" not in data:
        raise RuntimeError("assistant_approval_decide returned no result")
    return data


def expire_if_due(user_id: str, approval_id: str) -> dict | None:
    """A pending row past expires_at becomes expired (timed_out), lazily on
    read. Returns the row as it now stands, or None when there is none."""
    rows = _rows(
        _sb().table(APPROVALS_TABLE)
        .update({"status": "expired", "expired_reason": "timed_out", "updated_at": _now_iso()})
        .eq("user_id", user_id).eq("id", approval_id).eq("status", "pending")
        .lte("expires_at", _now_iso()).execute()
    )
    return rows[0] if rows else get_approval(user_id, approval_id)


def mark_approval(
    user_id: str, approval_id: str, patch: Mapping[str, Any], *, expect_status: str | None
) -> bool:
    """Patch one approval; with `expect_status` the update is fenced on the
    current status (the compare-and-set every transition uses). The
    immutability trigger still refuses a backward move."""
    query = (
        _sb().table(APPROVALS_TABLE).update({**dict(patch), "updated_at": _now_iso()})
        .eq("user_id", user_id).eq("id", approval_id)
    )
    if expect_status is not None:
        query = query.eq("status", expect_status)
    return bool(_rows(query.execute()))


def set_execute_job(user_id: str, approval_id: str, job_id: str | None) -> None:
    if not job_id:
        return
    _sb().table(APPROVALS_TABLE).update({"execute_job_id": job_id, "updated_at": _now_iso()}) \
        .eq("user_id", user_id).eq("id", approval_id).is_("execute_job_id", "null").execute()


def reverts_for(user_id: str, approval_ids: list[str]) -> dict[str, dict]:
    """{original approval id: its revert row}. A live or executed revert
    wins over a rejected, expired or failed one; otherwise the newest."""
    ids = [str(i) for i in approval_ids or () if i]
    if not ids:
        return {}
    rows = _rows(
        _sb().table(APPROVALS_TABLE).select(APPROVAL_COLUMNS)
        .eq("user_id", user_id).in_("reverts_approval_id", ids)
        .order("created_at").limit(500).execute()
    )
    out: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("reverts_approval_id"))
        current = out.get(key)
        live = row.get("status") in (*LIVE_APPROVAL_STATUSES, "executed")
        if current is None or live or current.get("status") not in (*LIVE_APPROVAL_STATUSES, "executed"):
            out[key] = row
    return out


def tool_calls_for_approvals(user_id: str, approval_ids: list[str]) -> dict[str, list[dict]]:
    """{approval id: its ledger rows, oldest first}."""
    ids = [str(i) for i in approval_ids or () if i]
    if not ids:
        return {}
    rows = _rows(
        _sb().table(TOOL_CALLS_TABLE).select(TOOL_CALL_COLUMNS)
        .eq("user_id", user_id).in_("approval_id", ids)
        .order("created_at").order("id").limit(1000).execute()
    )
    out: dict[str, list[dict]] = {i: [] for i in ids}
    for row in rows:
        out.setdefault(str(row.get("approval_id")), []).append(row)
    return out


def begun_without_terminal(user_id: str, approval_id: str) -> dict | None:
    """The approval's begun write row whose key has no completed or failed
    row: the write may have reached the route."""
    rows = tool_calls_for_approvals(user_id, [approval_id]).get(str(approval_id), [])
    finished = {r.get("idempotency_key") for r in rows if r.get("phase") in ("completed", "failed")}
    for row in rows:
        if row.get("phase") == "begun" and row.get("idempotency_key") not in finished:
            return row
    return None


def completed_write(user_id: str, approval_id: str) -> dict | None:
    """The approval's completed write row (the write committed), with
    "begun_id" set to its begun row's id when there is one; None when no
    write of the approval completed."""
    rows = tool_calls_for_approvals(user_id, [approval_id]).get(str(approval_id), [])
    done = next((r for r in rows if r.get("phase") == "completed" and r.get("ok") is not False), None)
    if done is None:
        return None
    begun = next((r for r in rows if r.get("phase") == "begun"
                  and r.get("idempotency_key") == done.get("idempotency_key")), None)
    return {**done, "begun_id": (begun or {}).get("id")}


def tainted_results(user_id: str, conversation_id: str | None, limit: int = 50) -> list[dict]:
    """The conversation's tainted tool results, newest first (the binding
    corpus, docs/ASSISTANT_PHASE1_PLAN.md 3.4)."""
    if not conversation_id:
        return []
    return _rows(
        _sb().table(TOOL_RESULTS_TABLE).select("tool_name, payload, taint_corpus")
        .eq("user_id", user_id).eq("conversation_id", conversation_id).eq("tainted", True)
        .order("created_at", desc=True).limit(max(1, min(200, int(limit)))).execute()
    )


def settle_message(user_id: str, message_id: str | None) -> bool:
    """waiting_approval -> done once no approval of the message is pending,
    approved or executing. A message parked in waiting_review (a sibling
    card partially ran) goes to done through `finish_message_review` once
    no card is live and every partial card has its review, so the last
    terminal transition of any card can leave the review state."""
    if not message_id:
        return False
    live = [r for r in list_approvals_for_message(user_id, message_id)
            if r.get("status") in LIVE_APPROVAL_STATUSES]
    if live:
        return False
    if update_message(user_id, message_id, {"status": "done"}, expect_status="waiting_approval"):
        return True
    return finish_message_review(user_id, message_id)


def park_message_for_review(user_id: str, message_id: str | None) -> bool:
    """The message goes to waiting_review (from waiting_approval or done):
    a write of one of its cards may have partially run."""
    if not message_id:
        return False
    rows = _rows(
        _sb().table(MESSAGES_TABLE).update({"status": "waiting_review", "updated_at": _now_iso()})
        .eq("user_id", user_id).eq("id", message_id)
        .in_("status", ["waiting_approval", "done"]).execute()
    )
    return bool(rows)


def finish_message_review(user_id: str, message_id: str | None) -> bool:
    """waiting_review -> done once no card of the message is a partial run
    without a review (the resolve route)."""
    if not message_id:
        return False
    for row in list_approvals_for_message(user_id, message_id):
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if row.get("status") in LIVE_APPROVAL_STATUSES:
            return False
        if row.get("status") == "failed" and result.get("may_have_partially_run") and not result.get("review"):
            return False
    return update_message(user_id, message_id, {"status": "done"}, expect_status="waiting_review")


def queue_approval_owner_status(user_id: str, approval_id: str) -> str | None:
    """The approval's status for the execute job's queue adapters (None
    when there is no row). `user_id` is the job owner from the payload."""
    owner = _require_owner(user_id)
    rows = _rows(
        _sb().table(APPROVALS_TABLE).select("status")
        .eq("user_id", owner).eq("id", approval_id).limit(1).execute()
    )
    return rows[0].get("status") if rows else None


# ── Staged files (phase 2, 0174) ─────────────────────────────────────────────
#
# A row is live while purged_at is null and expires_at > now(); unlinked
# while message_id is null (staged, not yet sent); unclaimed while
# consumed_at and consumed_task_id are null. The caps trigger (0170) counts
# unconsumed rows with expires_at > now(), so retiring a row sets
# expires_at = now(). Rows are never deleted: a row is the record that a
# message carried a file.


def _unclaimed(query: Any) -> Any:
    return query.is_("consumed_at", "null").is_("consumed_task_id", "null")


def _live_unclaimed(query: Any, *, floor: str | None = None) -> Any:
    return _unclaimed(query).is_("purged_at", "null").gt("expires_at", floor or _now_iso())


def insert_staged_file(user_id: str, row: Mapping[str, Any]) -> dict:
    """Insert one staged file for `user_id`. The caps trigger raises a
    check_violation whose message starts `file_cap_user` past 2 GB of live
    staging; the caller maps it to 409 staging_full."""
    rows = _rows(_sb().table(FILES_TABLE).insert({**dict(row), "user_id": user_id}).execute())
    if not rows:
        raise RuntimeError("staged file insert returned no row")
    return rows[0]


def reusable_staged_file_by_sha(user_id: str, sha256: str, *, min_ttl_seconds: int) -> dict | None:
    """The person's staged row holding these bytes that a new upload may
    reuse: unlinked, unclaimed, unpurged and live for more than
    `min_ttl_seconds` (so a card built on it cannot outlive it). Found, its
    expires_at is refreshed to 7 days (fenced on the same conditions) and
    the refreshed row is returned; None otherwise. A row already linked to
    a message is never reused."""
    floor = _iso_after(min_ttl_seconds)

    def fenced(query: Any) -> Any:
        return _live_unclaimed(
            query.eq("user_id", user_id).eq("sha256", sha256).is_("message_id", "null"), floor=floor,
        )

    rows = _rows(fenced(_sb().table(FILES_TABLE).select(FILE_COLUMNS))
                 .order("created_at", desc=True).limit(1).execute())
    if not rows:
        return None
    refreshed = _rows(
        fenced(_sb().table(FILES_TABLE).update({"expires_at": _iso_after(STAGING_TTL_SECONDS)}))
        .eq("id", rows[0]["id"]).execute()
    )
    return refreshed[0] if refreshed else None


def list_staged_files(user_id: str, *, limit: int = 50) -> list[dict]:
    """The tray: live rows that are unlinked and unclaimed, newest first."""
    return _rows(
        _live_unclaimed(_sb().table(FILES_TABLE).select(FILE_COLUMNS)
                        .eq("user_id", user_id).is_("message_id", "null"))
        .order("created_at", desc=True).limit(max(1, min(50, int(limit)))).execute()
    )


def staged_files_by_ids(user_id: str, file_ids: Sequence[str]) -> list[dict]:
    """The person's rows with these ids, in any state (the caller judges)."""
    ids = _ids(file_ids)
    if not ids:
        return []
    return _rows(
        _sb().table(FILES_TABLE).select(FILE_COLUMNS)
        .eq("user_id", user_id).in_("id", ids).limit(len(ids)).execute()
    )


def staged_files_for_link(user_id: str, file_ids: Sequence[str]) -> list[dict]:
    """The rows of these ids that a send may link: live, unclaimed and
    unlinked. The send route refuses unless every id comes back."""
    ids = _ids(file_ids)
    if not ids:
        return []
    return _rows(
        _live_unclaimed(_sb().table(FILES_TABLE).select(FILE_COLUMNS)
                        .eq("user_id", user_id).in_("id", ids).is_("message_id", "null"))
        .limit(len(ids)).execute()
    )


def link_files(user_id: str, file_ids: Sequence[str], message_id: str, conversation_id: str) -> int:
    """Link staged rows to the user message that carries them (fenced on
    unlinked, unclaimed and live). Returns how many rows were linked; a
    short count means one was taken in between."""
    ids = _ids(file_ids)
    if not ids:
        return 0
    return len(_rows(
        _live_unclaimed(_sb().table(FILES_TABLE)
                        .update({"message_id": message_id, "conversation_id": conversation_id})
                        .eq("user_id", user_id).in_("id", ids).is_("message_id", "null"))
        .execute()
    ))


def files_for_messages(user_id: str, message_ids: Sequence[str]) -> list[dict]:
    """Every staged row linked to one of these messages, in any state,
    oldest first. A list of rows (each carries its message_id); callers
    group them."""
    ids = _ids(message_ids)
    if not ids:
        return []
    return _rows(
        _sb().table(FILES_TABLE).select(FILE_COLUMNS)
        .eq("user_id", user_id).in_("message_id", ids)
        .order("created_at").order("id").limit(10 * len(ids)).execute()
    )


def staged_files_in_conversation(user_id: str, conversation_id: str, *, limit: int = 20) -> list[dict]:
    """The live, unclaimed rows linked to a message of this conversation,
    newest first (link_files sets conversation_id with message_id).
    splitter_status's staged list reads this instead of the conversation's
    messages, so a long conversation never hides its newest files."""
    return _rows(
        _live_unclaimed(_sb().table(FILES_TABLE).select(FILE_COLUMNS)
                        .eq("user_id", user_id).eq("conversation_id", conversation_id)
                        .not_.is_("message_id", "null"))
        .order("created_at", desc=True).order("id", desc=True)
        .limit(max(1, min(50, int(limit)))).execute()
    )


def claim_staged_files(user_id: str, file_ids: Sequence[str], *, task_id: str, project_id: str) -> int:
    """split-drop's claim (docs/ASSISTANT_PHASE2_PLAN.md 3.3.4 step 7): one
    update setting consumed_at, consumed_task_id and consumed_project_id,
    fenced on unclaimed, unpurged and live. Returns how many rows it took;
    a short count means another card got there first (the caller unclaims
    what it took with unclaim_staged_files)."""
    ids = _ids(file_ids)
    if not ids:
        return 0
    return len(_rows(
        _live_unclaimed(_sb().table(FILES_TABLE)
                        .update({"consumed_at": _now_iso(), "consumed_task_id": str(task_id),
                                 "consumed_project_id": str(project_id)})
                        .eq("user_id", user_id).in_("id", ids))
        .execute()
    ))


def unclaim_staged_files(user_id: str, task_id: str) -> int:
    """Undo a claim: the rows this task claimed go back to unclaimed
    (fenced on consumed_task_id = task_id and an object that still exists)."""
    if not task_id:
        return 0
    return len(_rows(
        _sb().table(FILES_TABLE)
        .update({"consumed_at": None, "consumed_task_id": None, "consumed_project_id": None})
        .eq("user_id", user_id).eq("consumed_task_id", str(task_id)).is_("purged_at", "null")
        .execute()
    ))


def retire_staged_file(user_id: str, file_id: str, *, purged: bool = False,
                       allow_linked: bool = False) -> bool:
    """The person removes an unclaimed staged row: expires_at = now() (the
    caps trigger stops counting it, and no claim can take it any more), and
    purged_at = now() when the object is already deleted. Fenced on
    unclaimed, and on unlinked unless `allow_linked` (the caller has checked
    that no open split card names a file sent with a message); False when
    the row is claimed, linked without `allow_linked`, or not this person's."""
    patch: dict[str, Any] = {"expires_at": _now_iso()}
    if purged:
        patch["purged_at"] = _now_iso()
    query = _sb().table(FILES_TABLE).update(patch).eq("user_id", user_id).eq("id", str(file_id))
    if not allow_linked:
        query = query.is_("message_id", "null")
    return bool(_rows(_unclaimed(query).execute()))


def mark_staged_purged(user_id: str, file_id: str) -> bool:
    """purged_at = now(), once: the row's storage object is gone."""
    return bool(_rows(
        _sb().table(FILES_TABLE).update({"purged_at": _now_iso()})
        .eq("user_id", user_id).eq("id", str(file_id)).is_("purged_at", "null").execute()
    ))


def legal_hold(user_id: str) -> bool:
    """assistant_profiles.legal_hold for this person (False with no row).
    Legal hold suspends every purge of the person's staged objects."""
    return bool((profile_row(user_id) or {}).get("legal_hold"))


def _legal_hold_owners(user_id: str | None) -> set[str]:
    """The owners whose objects no purge may delete: this person when on
    legal hold, or (user_id None, the system sweep) everyone on hold."""
    query = _sb().table(PROFILES_TABLE).select("user_id").eq("legal_hold", True)
    if user_id is not None:
        query = query.eq("user_id", user_id)
    return {str(r.get("user_id")) for r in _rows(query.limit(1000).execute())}


def staged_objects_to_purge(user_id: str | None, *, limit: int) -> list[dict]:
    """Rows whose storage object should go (docs/ASSISTANT_PHASE2_PLAN.md
    3.1.4): purged_at null and (consumed, or expired), oldest first, never a
    row whose owner is on legal hold. `user_id` is the person whose own
    rows are swept (GET /assistant/files); None is the SYSTEM SWEEP across
    every user, called only from assistant_staging.purge_expired (pinned by
    tests/test_assistant_repo_isolation)."""
    limit = max(1, min(200, int(limit)))
    held = _legal_hold_owners(user_id)
    if user_id is not None and str(user_id) in held:
        return []

    def scoped(query: Any) -> Any:
        query = query.is_("purged_at", "null")
        if user_id is not None:
            return query.eq("user_id", user_id)
        if held:
            return query.not_.in_("user_id", sorted(held))
        return query

    base = "id, user_id, storage_path, consumed_at, expires_at, created_at"
    # A claim newer than the margin may still be in split-drop's copy step
    # (claim at step 7, copy at step 10): its object is never swept. Step 13
    # purges a finished split's objects right away; the margin outlives the
    # pending-task repair, which unclaims a stuck split's rows.
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=CONSUMED_PURGE_MARGIN_SECONDS)
    consumed = _rows(scoped(_sb().table(FILES_TABLE).select(base)).not_.is_("consumed_at", "null")
                     .lte("consumed_at", cutoff.isoformat())
                     .order("created_at").limit(limit).execute())
    expired = _rows(scoped(_sb().table(FILES_TABLE).select(base)).lte("expires_at", _now_iso())
                    .order("created_at").limit(limit).execute())
    merged = {str(r["id"]): r for r in consumed + expired if r.get("id") and not _freshly_claimed(r, cutoff)}
    rows = sorted(merged.values(), key=lambda r: (str(r.get("created_at") or ""), str(r["id"])))
    return [r for r in rows if str(r.get("user_id")) not in held][:limit]


def _freshly_claimed(row: Mapping[str, Any], cutoff: datetime) -> bool:
    """A row claimed after `cutoff` (an unreadable stamp counts as fresh, so
    a sweep never deletes an object a split may still copy)."""
    stamp = row.get("consumed_at")
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when > cutoff


# ── Tasks (phase 2) ──────────────────────────────────────────────────────────


def insert_task(user_id: str, row: Mapping[str, Any]) -> dict:
    """Insert one task for `user_id` (an explicit id in `row` is kept). A
    23505 on assistant_tasks_approval_uq (one task per approved card) raises
    TaskExists."""
    try:
        rows = _rows(_sb().table(TASKS_TABLE).insert({**dict(row), "user_id": user_id}).execute())
    except Exception as exc:
        if TASK_APPROVAL_INDEX in str(exc):
            raise TaskExists(str(exc)) from exc
        raise
    if not rows:
        raise RuntimeError("task insert returned no row")
    return rows[0]


def get_task(user_id: str, task_id: str) -> dict | None:
    rows = _rows(
        _sb().table(TASKS_TABLE).select(TASK_COLUMNS)
        .eq("user_id", user_id).eq("id", str(task_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def task_by_approval(user_id: str, approval_id: str) -> dict | None:
    """The task an approved card created (the replay key)."""
    if not approval_id:
        return None
    rows = _rows(
        _sb().table(TASKS_TABLE).select(TASK_COLUMNS)
        .eq("user_id", user_id).eq("approval_id", str(approval_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def update_task(
    user_id: str,
    task_id: str,
    patch: Mapping[str, Any],
    *,
    expect_status: str | Sequence[str] | None = None,
) -> bool:
    """Patch one task; with `expect_status` (one status or several) the
    update is fenced on the current status, like update_message. True when
    a row was updated."""
    query = (
        _sb().table(TASKS_TABLE).update({**dict(patch), "updated_at": _now_iso()})
        .eq("user_id", user_id).eq("id", str(task_id))
    )
    if isinstance(expect_status, str):
        query = query.eq("status", expect_status)
    elif expect_status is not None:
        query = query.in_("status", [str(s) for s in expect_status])
    return bool(_rows(query.execute()))


def list_tasks(user_id: str, *, limit: int = 5) -> list[dict]:
    """The person's tasks, newest first."""
    return _rows(
        _sb().table(TASKS_TABLE).select(TASK_COLUMNS).eq("user_id", user_id)
        .order("created_at", desc=True).order("id", desc=True)
        .limit(max(1, min(50, int(limit)))).execute()
    )


def active_task_jobs(user_id: str) -> list[dict]:
    """The person's open split tasks that have a splitter job:
    [{id, status, split_job_id, project_id}]."""
    return _rows(
        _sb().table(TASKS_TABLE).select("id, status, split_job_id, project_id")
        .eq("user_id", user_id).in_("status", list(OPEN_TASK_STATUSES))
        .not_.is_("split_job_id", "null").limit(100).execute()
    )


# ── Split card helpers (phase 2) ─────────────────────────────────────────────


def open_split_card_file_ids(user_id: str, *, include_decided: bool = False) -> set[str]:
    """The staged file ids named by the person's pending, unexpired split
    cards (the preview's `in_open_card` problem). Read from the stored
    request body (full ids), with the stored args as the fallback.
    `include_decided` also counts approved and executing cards (their
    execute job still needs the files), as DELETE /assistant/files does."""
    rows = _rows(
        _sb().table(APPROVALS_TABLE).select("id, payload, expires_at")
        .eq("user_id", user_id).eq("action_kind", SPLIT_TOOL).eq("status", "pending")
        .gt("expires_at", _now_iso()).limit(200).execute()
    )
    if include_decided:
        rows = rows + _rows(
            _sb().table(APPROVALS_TABLE).select("id, payload, expires_at")
            .eq("user_id", user_id).eq("action_kind", SPLIT_TOOL).in_("status", ["approved", "executing"])
            .limit(200).execute()
        )
    out: set[str] = set()
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        body = request.get("json") if isinstance(request.get("json"), dict) else {}
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        ids = body.get("file_ids") if isinstance(body.get("file_ids"), list) else args.get("file_ids")
        out.update(str(i) for i in (ids if isinstance(ids, list) else []) if i)
    return out


def conversation_for_run(user_id: str, run_id: str | None) -> str | None:
    """The conversation a turn's run belongs to, from its tool-call ledger
    rows (the executor begins a row before a tool's requests run). None
    when there is no run or no row."""
    if not run_id:
        return None
    rows = _rows(
        _sb().table(TOOL_CALLS_TABLE).select("conversation_id")
        .eq("user_id", user_id).eq("run_id", str(run_id)).limit(1).execute()
    )
    value = rows[0].get("conversation_id") if rows else None
    return str(value) if value else None


# ── Phase 3 (0176): schedules, runs, memory, sweeps, offboarding ─────────────

SCHEDULES_TABLE = "assistant_schedules"
RUNS_TABLE = "assistant_schedule_runs"
MEMORIES_TABLE = "assistant_memories"
REVISIONS_TABLE = "assistant_memory_revisions"
SWEEPS_TABLE = "assistant_sweeps"
CLAIM_RPC = "claim_due_assistant_schedules"
RUN_CHANNEL_RPC = "assistant_schedule_run_channel"
MEMORY_APPLY_RPC = "assistant_memory_apply"
MEMORY_SIMILAR_RPC = "assistant_memory_similar"
MEMORY_TOUCH_RPC = "assistant_memory_touch"
SCHEDULE_APPROVAL_INDEX = "assistant_schedules_approval_uq"
RUN_OCCURRENCE_INDEX = "assistant_schedule_runs_occurrence_uq"
RUN_NOW_PENDING_INDEX = "assistant_schedule_runs_one_pending_run_now_uq"
SWEEPS_KEY = "assistant_sweeps_pkey"

SCHEDULE_COLUMNS = (
    "id, user_id, title, recipe, params, rrule, timezone, status, paused_reason, sub_app, delivery, "
    "only_if_changed, last_result_hash, project_id, conversation_id, next_run_at, last_run_at, "
    "consecutive_failures, unread_deliveries, ended_at, created_at, updated_at, dtstart, "
    "after_next_run_at, approval_id, paused_at, ended_reason, last_delivered_at, last_seen_at"
)
RUN_COLUMNS = (
    "id, schedule_id, user_id, user_label, scheduled_for, kind, status, claimed_by, started_at, "
    "finished_at, result_hash, delivered, message_id, error_kind, error, stats, created_at"
)
# Never tsv (generated) nor idempotency_key (the ledger's step key).
MEMORY_COLUMNS = (
    "id, user_id, kind, title, body, project_id, status, source, pinned, origin_tainted, "
    "conversation_id, message_id, last_used_at, use_count, archived_at, created_at, updated_at"
)
REVISION_COLUMNS = (
    "id, memory_id, user_id, title, body, origin_tainted, conversation_id, message_id, decision, "
    "created_at, decided_at, base_updated_at"
)

PAUSED_STATUSES = ("paused", "paused_feature_off")
OPEN_RUN_STATUSES = ("claimed", "running")
# Paused rows the scheduler's resume pass may restart on its own (3.4.2
# step 5); offboarding moves them to owner_offboarded (3.10.2 step 7).
RESUMABLE_PAUSE_REASONS = ("mfa_reset", "no_delegation")
RUN_OPEN_SECONDS = 30 * 60          # the claim's overlap window (0176 A4)
RUN_NOW_PICKUP_SECONDS = 10 * 60    # a Run now row nobody took by then is reaped
MEMORY_LIVE_STATUSES = ("active", "pending_review")
OFFBOARD_APPROVAL_STATUSES = ("pending", "approved")

_SCHEDULE_REFUSALS = ("schedule_cap_active", "schedule_interval", "schedule_rule")
_MEMORY_REFUSALS = (
    "memory_cap_active", "memory_cap_pinned", "memory_cap_pinned_chars", "memory_cap_pending",
    "memory_pin_user_only",
)
_RETRY_SUFFIX = re.compile(r":r\d+$")


class ScheduleRefused(RuntimeError):
    """The caps trigger refused a schedule write. `code` is the message
    prefix: schedule_cap_active (the route answers 409
    schedule_cap_reached), schedule_interval or schedule_rule (422
    bad_cadence)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class ScheduleExists(RuntimeError):
    """A schedule for this approval already exists (the unique index
    assistant_schedules_approval_uq): a replay of the same card."""

    code = "23505"


class RunExists(RuntimeError):
    """A run already holds this occurrence (UNIQUE(schedule_id,
    scheduled_for)); two Run now clicks in the same microsecond."""

    code = "23505"


class RunBusy(RuntimeError):
    """Another Run now row of this schedule is already waiting for pickup
    (0176 A3b); two overlapping Run now requests."""

    code = "23505"


class MemoryRefused(RuntimeError):
    """assistant_memory_apply refused the op. `code` is the RPC's code
    (memory_not_found, memory_stale, memory_wrong_status, memory_archived,
    memory_pinned_user_only, revision_not_found, legal_hold) or the caps and
    pin-guard prefix (memory_cap_active, memory_cap_pinned,
    memory_cap_pinned_chars, memory_cap_pending, memory_pin_user_only).
    `memory` is the current row when the RPC returned it (memory_stale)."""

    def __init__(self, code: str, memory: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.memory = dict(memory) if isinstance(memory, Mapping) else None


def _refusal(exc: Exception, codes: Sequence[str]) -> str | None:
    """The trigger's message prefix ("<code>: ...") among `codes`."""
    text = str(exc)
    return next((c for c in codes if f"{c}:" in text), None)


def _scalar(resp: Any) -> Any:
    data = getattr(resp, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    return data


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _fenced(query: Any, expect: Mapping[str, Any] | None) -> Any:
    """Equality fences: a None value means `is null`."""
    for col, value in (expect or {}).items():
        query = query.is_(col, "null") if value is None else query.eq(col, value)
    return query


def _status_filter(query: Any, status: str | Sequence[str] | None) -> Any:
    if isinstance(status, str):
        return query.eq("status", status)
    if status is not None:
        return query.in_("status", [str(s) for s in status])
    return query


def _scope(query: Any, user_id: str | None) -> Any:
    """A system sweep's filter: the person, or (None) everyone."""
    return query if user_id is None else query.eq("user_id", user_id)


# ── Schedules ────────────────────────────────────────────────────────────────


def insert_schedule(user_id: str, row: Mapping[str, Any]) -> dict:
    """Insert one schedule for `user_id` (the approval-bound create, 3.3.2).
    Raises ScheduleExists on the approval index (the replay) and
    ScheduleRefused when the caps trigger refuses the rule or the cap."""
    try:
        rows = _rows(_sb().table(SCHEDULES_TABLE).insert({**dict(row), "user_id": user_id}).execute())
    except Exception as exc:
        if SCHEDULE_APPROVAL_INDEX in str(exc):
            raise ScheduleExists(str(exc)) from exc
        code = _refusal(exc, _SCHEDULE_REFUSALS)
        if code:
            raise ScheduleRefused(code, str(exc)) from exc
        raise
    if not rows:
        raise RuntimeError("schedule insert returned no row")
    return rows[0]


def get_schedule(user_id: str, schedule_id: str) -> dict | None:
    rows = _rows(
        _sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS)
        .eq("user_id", user_id).eq("id", str(schedule_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def schedule_by_approval(user_id: str, approval_id: str) -> dict | None:
    """The schedule an approved card created (the create replay and the
    WriteSpec's recover read)."""
    if not approval_id:
        return None
    rows = _rows(
        _sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS)
        .eq("user_id", user_id).eq("approval_id", str(approval_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def schedule_for_conversation(user_id: str, conversation_id: str) -> dict | None:
    """The schedule whose deliveries this conversation holds (surface
    'schedule'), or None."""
    if not conversation_id:
        return None
    rows = _rows(
        _sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS)
        .eq("user_id", user_id).eq("conversation_id", str(conversation_id))
        .order("created_at").limit(1).execute()
    )
    return rows[0] if rows else None


def list_schedules(user_id: str, *, status: str | Sequence[str] | None = None, limit: int = 50) -> list[dict]:
    """The person's schedules, newest first; `status` is one status or
    several (the Routines tab's "paused" is both paused statuses)."""
    query = _status_filter(_sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS).eq("user_id", user_id), status)
    return _rows(
        query.order("created_at", desc=True).order("id", desc=True)
        .limit(max(1, min(50, int(limit)))).execute()
    )


def count_active_schedules(user_id: str) -> int:
    """How many of the person's schedules are active (the draft's
    active_count; the caps trigger allows 10)."""
    return len(_rows(
        _sb().table(SCHEDULES_TABLE).select("id").eq("user_id", user_id).eq("status", "active")
        .limit(100).execute()
    ))


def update_schedule(
    user_id: str,
    schedule_id: str,
    patch: Mapping[str, Any],
    *,
    expect_status: str | Sequence[str] | None = None,
    expect: Mapping[str, Any] | None = None,
) -> dict | None:
    """Patch one schedule, fenced on `expect_status` (one status or
    several) and on every column of `expect` (equality; None means is
    null), the compare-and-set every transition uses (3.3.4). Returns the
    updated row, or None when the fence was lost. A resume over the cap
    raises ScheduleRefused("schedule_cap_active")."""
    query = _fenced(
        _status_filter(
            _sb().table(SCHEDULES_TABLE).update({**dict(patch), "updated_at": _now_iso()})
            .eq("user_id", user_id).eq("id", str(schedule_id)),
            expect_status,
        ),
        expect,
    )
    try:
        rows = _rows(query.execute())
    except Exception as exc:
        code = _refusal(exc, _SCHEDULE_REFUSALS)
        if code:
            raise ScheduleRefused(code, str(exc)) from exc
        raise
    return rows[0] if rows else None


def fill_after_slot(user_id: str, schedule_id: str, *, next_run_at: str, after_next_run_at: str) -> bool:
    """The refill's write (3.4.2 step 1): after_next_run_at, fenced on the
    schedule still active, still at the next_run_at the caller read, and
    the slot still empty. Never touches next_run_at."""
    return update_schedule(
        user_id, schedule_id, {"after_next_run_at": after_next_run_at},
        expect_status="active", expect={"next_run_at": next_run_at, "after_next_run_at": None},
    ) is not None


def set_schedule_conversation(user_id: str, schedule_id: str, conversation_id: str) -> bool:
    """Link the schedule's conversation once (fenced on conversation_id is
    null). False means another run linked one first; the caller rereads the
    schedule and posts into that one (3.4.5)."""
    return update_schedule(
        user_id, schedule_id, {"conversation_id": str(conversation_id)}, expect={"conversation_id": None},
    ) is not None


def mark_schedules_seen(user_id: str, schedule_ids: Sequence[str]) -> int:
    """unread_deliveries = 0 and last_seen_at = now() on the person's rows
    among these ids. Returns how many rows were updated."""
    ids = _ids(schedule_ids)
    if not ids:
        return 0
    return len(_rows(
        _sb().table(SCHEDULES_TABLE)
        .update({"unread_deliveries": 0, "last_seen_at": _now_iso(), "updated_at": _now_iso()})
        .eq("user_id", user_id).in_("id", ids).execute()
    ))


def schedule_lines(user_id: str) -> list[dict]:
    """The rows the prompt's routine lines are built from (3.9.1): the
    active schedules by next run (at most 10, the cap) then the 5 newest
    paused ones. Every field the lines use is server-built."""
    active = _rows(
        _sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS)
        .eq("user_id", user_id).eq("status", "active")
        .order("next_run_at").order("created_at").limit(10).execute()
    )
    paused = _rows(
        _sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS)
        .eq("user_id", user_id).in_("status", list(PAUSED_STATUSES))
        .order("updated_at", desc=True).order("id", desc=True).limit(5).execute()
    )
    return active + paused


def pause_schedules(user_id: str, reason: str, *, include_resumable_paused: bool = False) -> int:
    """Offboarding step 7 (3.10.2). False: the person's active rows go to
    paused with `reason` (the mfa_reset form; the resume pass restarts them
    after the next grant). True: also every row that could restart on its
    own (paused_feature_off, and paused with reason mfa_reset or
    no_delegation), the deactivated, demoted_external and sweep form. One
    update per row, fenced on the status and paused_reason it was read with,
    paused_at = now(). Rows paused for any other reason keep it. Returns
    the count."""
    rows = _rows(
        _sb().table(SCHEDULES_TABLE).select("id, status, paused_reason")
        .eq("user_id", user_id).neq("status", "ended").limit(200).execute()
    )
    moved = 0
    for row in rows:
        status, current = row.get("status"), row.get("paused_reason")
        eligible = status == "active" or (
            include_resumable_paused
            and (status == "paused_feature_off"
                 or (status == "paused" and current in RESUMABLE_PAUSE_REASONS))
        )
        if not eligible:
            continue
        if update_schedule(
            user_id, row["id"], {"status": "paused", "paused_reason": reason, "paused_at": _now_iso()},
            expect_status=status, expect={"paused_reason": current},
        ) is not None:
            moved += 1
    return moved


def schedules_to_refill(user_id: str | None, *, limit: int = 100) -> list[dict]:
    """Active rows whose second slot is empty (3.4.2 step 1), soonest
    first. None is the system sweep (the scheduler's refill)."""
    return _rows(
        _scope(_sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS), user_id)
        .eq("status", "active").is_("after_next_run_at", "null")
        .order("next_run_at").limit(max(1, min(200, int(limit)))).execute()
    )


def claim_due_schedules(user_id: str | None, *, worker: str, limit: int, catchup_seconds: int) -> list[dict]:
    """RPC claim_due_assistant_schedules (0176 A4): the runs this call
    inserted or took (skipped rows included, status 'skipped' with
    error_kind missed or overlap). The claim spans every user, so only the
    system sweep (None) may call it."""
    if user_id is not None:
        raise ValueError("the schedule claim is a system sweep: pass user_id None")
    return _rows(_sb().rpc(CLAIM_RPC, {
        "p_worker": str(worker), "p_limit": int(limit), "p_catchup_seconds": int(catchup_seconds),
    }).execute())


def stale_runs(user_id: str | None, *, older_than_seconds: int, limit: int = 50) -> list[dict]:
    """Runs the reaper fails (3.4.2 step 4), oldest first: a claimed or
    running row a worker took more than `older_than_seconds` ago, and a Run
    now row nobody took within 10 minutes (claimed_by null: the caller
    records not_picked_up, else abandoned). None is the system sweep."""
    limit = max(1, min(200, int(limit)))
    taken = _rows(
        _scope(_sb().table(RUNS_TABLE).select(RUN_COLUMNS), user_id)
        .in_("status", list(OPEN_RUN_STATUSES)).not_.is_("claimed_by", "null")
        .lte("started_at", _iso_ago(older_than_seconds)).order("created_at").limit(limit).execute()
    )
    unpicked = _rows(
        _scope(_sb().table(RUNS_TABLE).select(RUN_COLUMNS), user_id)
        .eq("kind", "run_now").eq("status", "claimed").is_("claimed_by", "null")
        .lte("created_at", _iso_ago(RUN_NOW_PICKUP_SECONDS)).order("created_at").limit(limit).execute()
    )
    merged = {str(r["id"]): r for r in taken + unpicked if r.get("id")}
    return sorted(merged.values(), key=lambda r: (str(r.get("created_at") or ""), str(r["id"])))[:limit]


def paused_to_check(user_id: str | None, *, limit: int = 100) -> list[dict]:
    """Rows the resume pass may restart (3.4.2 step 5): paused_feature_off,
    and paused with reason mfa_reset or no_delegation, longest paused
    first. None is the system sweep."""
    limit = max(1, min(200, int(limit)))
    feature = _rows(
        _scope(_sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS), user_id)
        .eq("status", "paused_feature_off").order("paused_at").limit(limit).execute()
    )
    owner = _rows(
        _scope(_sb().table(SCHEDULES_TABLE).select(SCHEDULE_COLUMNS), user_id)
        .eq("status", "paused").in_("paused_reason", list(RESUMABLE_PAUSE_REASONS))
        .order("paused_at").limit(limit).execute()
    )
    return sorted(feature + owner, key=lambda r: (str(r.get("paused_at") or ""), str(r["id"])))[:limit]


# ── Schedule runs ────────────────────────────────────────────────────────────


def insert_run(user_id: str, row: Mapping[str, Any]) -> dict:
    """Insert one run for `user_id` (a Run now row, 3.3.5). A 23505 on the
    occurrence key raises RunExists; one on the pending Run now index
    (another Run now row is waiting) raises RunBusy."""
    try:
        rows = _rows(_sb().table(RUNS_TABLE).insert({**dict(row), "user_id": user_id}).execute())
    except Exception as exc:
        if RUN_OCCURRENCE_INDEX in str(exc):
            raise RunExists(str(exc)) from exc
        if RUN_NOW_PENDING_INDEX in str(exc):
            raise RunBusy(str(exc)) from exc
        raise
    if not rows:
        raise RuntimeError("schedule run insert returned no row")
    return rows[0]


def get_run(user_id: str, run_id: str) -> dict | None:
    rows = _rows(
        _sb().table(RUNS_TABLE).select(RUN_COLUMNS)
        .eq("user_id", user_id).eq("id", str(run_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def update_run(
    user_id: str, run_id: str, patch: Mapping[str, Any], *, expect_status: str | Sequence[str] | None = None
) -> bool:
    """Patch one run, fenced on `expect_status`; the forward trigger still
    refuses a backward move and any change to the identity columns."""
    query = _status_filter(
        _sb().table(RUNS_TABLE).update(dict(patch)).eq("user_id", user_id).eq("id", str(run_id)),
        expect_status,
    )
    return bool(_rows(query.execute()))


def open_run(user_id: str, schedule_id: str) -> dict | None:
    """The schedule's claimed or running run created in the last 30
    minutes (Run now's 409 schedule_busy), newest first."""
    rows = _rows(
        _sb().table(RUNS_TABLE).select(RUN_COLUMNS)
        .eq("user_id", user_id).eq("schedule_id", str(schedule_id))
        .in_("status", list(OPEN_RUN_STATUSES)).gt("created_at", _iso_ago(RUN_OPEN_SECONDS))
        .order("created_at", desc=True).limit(1).execute()
    )
    return rows[0] if rows else None


def last_run(user_id: str, schedule_id: str) -> dict | None:
    """The schedule's newest run by created_at (Run now's 5 minute limit
    and ScheduleOut.last_run)."""
    rows = _rows(
        _sb().table(RUNS_TABLE).select(RUN_COLUMNS)
        .eq("user_id", user_id).eq("schedule_id", str(schedule_id))
        .order("created_at", desc=True).order("id", desc=True).limit(1).execute()
    )
    return rows[0] if rows else None


def last_runs(user_id: str, schedule_ids: Sequence[str]) -> dict[str, dict]:
    """{schedule id: its newest run} for a list of schedules (one read, then
    a per-schedule read for any schedule the bounded page did not reach)."""
    ids = _ids(schedule_ids)
    if not ids:
        return {}
    rows = _rows(
        _sb().table(RUNS_TABLE).select(RUN_COLUMNS)
        .eq("user_id", user_id).in_("schedule_id", ids)
        .order("created_at", desc=True).order("id", desc=True).limit(1000).execute()
    )
    out: dict[str, dict] = {}
    for row in rows:
        out.setdefault(str(row.get("schedule_id")), row)
    for sid in ids:
        if sid not in out:
            row = last_run(user_id, sid)
            if row is not None:
                out[sid] = row
    return out


def list_runs(user_id: str, schedule_id: str, *, before: str | None = None, limit: int = 20) -> list[dict]:
    """The schedule's runs, newest occurrence first; `before` is the
    scheduled_for of the last item of the previous page."""
    query = (
        _sb().table(RUNS_TABLE).select(RUN_COLUMNS)
        .eq("user_id", user_id).eq("schedule_id", str(schedule_id))
    )
    if before:
        query = query.lt("scheduled_for", before)
    return _rows(
        query.order("scheduled_for", desc=True).order("id", desc=True)
        .limit(max(1, min(50, int(limit)))).execute()
    )


def run_channel(user_id: str, run_id: str, channel: str, action: str, value: Any = None) -> bool:
    """RPC assistant_schedule_run_channel (0176 A5): 'claim' takes the
    channel once while the run is running; 'done' records `value` (true, or
    the email_log id) and 'failed' records false, both only on a claimed
    channel. True when this call changed the row."""
    return bool(_scalar(_sb().rpc(RUN_CHANNEL_RPC, {
        "p_user_id": user_id, "p_run_id": str(run_id), "p_channel": channel, "p_action": action,
        "p_value": value,
    }).execute()))


# ── Holidays ─────────────────────────────────────────────────────────────────


def update_holidays(user_id: str, dates: Sequence[Any]) -> dict:
    """assistant_settings.holidays for the company row (id = 1), sorted
    ascending and deduped; `user_id` is the acting admin (updated_by). The
    route has validated the dates. Returns {holidays, updated_at}."""
    values = sorted({d.isoformat() if hasattr(d, "isoformat") else str(d) for d in dates or ()})
    rows = _rows(
        _sb().table(SETTINGS_TABLE)
        .update({"holidays": values, "updated_by": user_id, "updated_at": _now_iso()})
        .eq("id", 1).execute()
    )
    if not rows:
        raise RuntimeError("the assistant settings row is missing")
    return {"holidays": rows[0].get("holidays") or [], "updated_at": rows[0].get("updated_at")}


# ── Sweep claims ─────────────────────────────────────────────────────────────


def claim_sweep(user_id: str | None, step: str, sweep_date: Any, *, worker: str) -> bool:
    """Claim one maintenance step for one Pacific day (0176 E): True when
    this call inserted the row, False when the other worker holds it (the
    primary key). Sweeps are system-wide, so `user_id` is None."""
    if user_id is not None:
        raise ValueError("sweep claims are system-wide: pass user_id None")
    day = sweep_date.isoformat() if hasattr(sweep_date, "isoformat") else str(sweep_date)
    try:
        _sb().table(SWEEPS_TABLE).insert({"step": step, "sweep_date": day, "claimed_by": str(worker)}).execute()
    except Exception as exc:
        if SWEEPS_KEY in str(exc) or "23505" in str(exc):
            return False
        raise
    return True


def finish_sweep(user_id: str | None, step: str, sweep_date: Any, result: Mapping[str, Any]) -> bool:
    """finished_at and the step's counts on a claimed sweep row (once).
    Sweeps are system-wide, so `user_id` is None."""
    if user_id is not None:
        raise ValueError("sweep claims are system-wide: pass user_id None")
    day = sweep_date.isoformat() if hasattr(sweep_date, "isoformat") else str(sweep_date)
    return bool(_rows(
        _sb().table(SWEEPS_TABLE).update({"finished_at": _now_iso(), "result": dict(result)})
        .eq("step", step).eq("sweep_date", day).is_("finished_at", "null").execute()
    ))


# ── Approval sweeps (3.5 item 1) ─────────────────────────────────────────────


def pending_approvals_due(
    user_id: str | None, *, limit: int = 100, expires_after: Any = None,
) -> list[dict]:
    """Pending cards by expires_at, the ones past it first: the caller
    expires the timed-out ones and checks each owner and sub-app. None is
    the system sweep. `expires_after` (a timezone-aware time, default None:
    no bound) keeps only cards with expires_at at or after it, so a reader
    that wants cards still ahead of their expiry is never starved by a
    backlog of long-expired cards that are still pending."""
    query = _scope(_sb().table(APPROVALS_TABLE).select(APPROVAL_COLUMNS), user_id).eq("status", "pending")
    if expires_after is not None:
        query = query.gte("expires_at", _p7_iso(expires_after))
    return _rows(query.order("expires_at").order("id").limit(max(1, min(200, int(limit)))).execute())


def pending_approvals_with_project(user_id: str | None, *, limit: int = 100) -> list[dict]:
    """Pending cards that name a project (the closed-project sweep), oldest
    first. None is the system sweep."""
    return _rows(
        _scope(_sb().table(APPROVALS_TABLE).select(APPROVAL_COLUMNS), user_id)
        .eq("status", "pending").not_.is_("project_id", "null")
        .order("created_at").order("id").limit(max(1, min(200, int(limit)))).execute()
    )


def approved_stranded(user_id: str | None, *, older_than_seconds: int = 600, limit: int = 100) -> list[dict]:
    """Approved cards decided more than `older_than_seconds` ago that never
    moved on; the caller checks the execute job and recovers. None is the
    system sweep."""
    return _rows(
        _scope(_sb().table(APPROVALS_TABLE).select(APPROVAL_COLUMNS), user_id)
        .eq("status", "approved").lte("decided_at", _iso_ago(older_than_seconds))
        .order("decided_at").order("id").limit(max(1, min(200, int(limit)))).execute()
    )


# ── Memory sweeps (3.5 item 4) ───────────────────────────────────────────────


def memories_to_auto_archive(
    user_id: str | None, *, unused_days: int = 180, pending_days: int = 30, limit: int = 500
) -> list[dict]:
    """Rows the nightly step archives: active unpinned rows unused for
    `unused_days` (coalesce(last_used_at, updated_at)) and pending_review
    rows untouched for `pending_days`. None is the system sweep."""
    limit = max(1, min(1000, int(limit)))
    cols = "id, user_id, kind, status, pinned, project_id, last_used_at, updated_at"
    unused_before = _iso_ago(unused_days * 86400)

    def active_unpinned() -> Any:
        return (_scope(_sb().table(MEMORIES_TABLE).select(cols), user_id)
                .eq("status", "active").eq("pinned", False))

    used_long_ago = _rows(active_unpinned().not_.is_("last_used_at", "null")
                          .lt("last_used_at", unused_before).order("last_used_at").limit(limit).execute())
    never_used = _rows(active_unpinned().is_("last_used_at", "null")
                       .lt("updated_at", unused_before).order("updated_at").limit(limit).execute())
    pending = _rows(
        _scope(_sb().table(MEMORIES_TABLE).select(cols), user_id)
        .eq("status", "pending_review").lt("updated_at", _iso_ago(pending_days * 86400))
        .order("updated_at").limit(limit).execute()
    )
    return (used_long_ago + never_used + pending)[:limit]


def project_notes_for_review(user_id: str | None, *, limit: int = 500, offset: int = 0) -> list[dict]:
    """Live project notes (active or pending) for the nightly project check:
    moved on a merge, archived when the project is gone, abandoned, failed
    to bid or lost. None is the system sweep. `offset` pages through the
    stable (created_at, id) order: notes on healthy projects never leave
    the set, so the caller reads past them rather than reread one page."""
    n = max(1, min(1000, int(limit)))
    start = max(0, int(offset))
    return _rows(
        _scope(_sb().table(MEMORIES_TABLE).select("id, user_id, project_id, status, pinned"), user_id)
        .eq("kind", "project_note").in_("status", list(MEMORY_LIVE_STATUSES))
        .order("created_at").order("id").range(start, start + n - 1).execute()
    )


def memories_to_purge(user_id: str | None, *, archived_days: int = 365, limit: int = 500) -> list[dict]:
    """Archived rows older than `archived_days` (the caller deletes each
    through op 'delete' with actor 'system'), never a row whose owner is on
    legal hold. None is the system sweep."""
    held = _legal_hold_owners(user_id)
    if user_id is not None and str(user_id) in held:
        return []
    query = _scope(_sb().table(MEMORIES_TABLE).select("id, user_id, archived_at"), user_id)
    if user_id is None and held:
        query = query.not_.in_("user_id", sorted(held))
    rows = _rows(
        query.eq("status", "archived").lte("archived_at", _iso_ago(archived_days * 86400))
        .order("archived_at").limit(max(1, min(1000, int(limit)))).execute()
    )
    return [r for r in rows if str(r.get("user_id")) not in held]


def tool_results_to_purge(user_id: str | None, *, limit: int = 500) -> list[dict]:
    """Tool results past expires_at (the 30-day TTL), never a row whose
    owner is on legal hold: [{id, user_id}]. None is the system sweep."""
    held = _legal_hold_owners(user_id)
    if user_id is not None and str(user_id) in held:
        return []
    query = _scope(_sb().table(TOOL_RESULTS_TABLE).select("id, user_id, expires_at"), user_id)
    if user_id is None and held:
        query = query.not_.in_("user_id", sorted(held))
    rows = _rows(
        query.lte("expires_at", _now_iso()).order("expires_at")
        .limit(max(1, min(1000, int(limit)))).execute()
    )
    return [r for r in rows if str(r.get("user_id")) not in held]


def delete_tool_results(user_id: str, result_ids: Sequence[str]) -> int:
    """Delete the person's tool results among these ids (the nightly purge
    groups tool_results_to_purge rows by owner). Returns the count."""
    ids = _ids(result_ids)
    if not ids:
        return 0
    return len(_rows(_sb().table(TOOL_RESULTS_TABLE).delete().eq("user_id", user_id).in_("id", ids).execute()))


def owners_with_open_state(user_id: str | None, *, limit: int = 200) -> list[str]:
    """Users with a live delegation, a pending or approved card, an active
    schedule, an open task or an active hold (the orphan sweep, 3.5 item
    5), sorted. None is the system sweep."""
    limit = max(1, min(1000, int(limit)))
    reads = (
        _scope(_sb().table(DELEGATIONS_TABLE).select("user_id"), user_id).is_("revoked_at", "null"),
        _scope(_sb().table(APPROVALS_TABLE).select("user_id"), user_id).in_("status", list(OFFBOARD_APPROVAL_STATUSES)),
        _scope(_sb().table(SCHEDULES_TABLE).select("user_id"), user_id).eq("status", "active"),
        _scope(_sb().table(TASKS_TABLE).select("user_id"), user_id)
        .in_("status", [*OPEN_TASK_STATUSES, "waiting_approval"]),
        _scope(_sb().table(RESERVATIONS_TABLE).select("user_id"), user_id).eq("status", "active"),
    )
    owners: set[str] = set()
    for query in reads:
        owners.update(str(r["user_id"]) for r in _rows(query.limit(limit).execute()) if r.get("user_id"))
    return sorted(owners)[:limit]


def phase7_owners_with_open_state(user_id: str | None, *, limit: int = 200) -> list[str]:
    """Users with phase 7 state that offboarding clears (6.8): an own
    subscription override (role null), a project mute, an undelivered
    immediate or digest inbox row, or a pending external send. The orphan
    sweep reads it next to owners_with_open_state, which never sees these
    tables. Sorted. None is the system sweep."""
    limit = max(1, min(1000, int(limit)))
    reads = (
        _scope(_sb().table(SUBSCRIPTIONS_TABLE).select("user_id"), user_id).is_("role", "null"),
        _scope(_sb().table(PROJECT_MUTES_TABLE).select("user_id"), user_id),
        _scope(_sb().table(INBOX_TABLE).select("user_id"), user_id).in_("delivery", ["immediate", "digest"])
        .is_("delivered_at", "null").is_("dismissed_at", "null"),
        _scope(_sb().table(EXTERNAL_SENDS_TABLE).select("user_id"), user_id).eq("status", "pending"),
    )
    owners: set[str] = set()
    for query in reads:
        owners.update(str(r["user_id"]) for r in _rows(query.limit(limit).execute()) if r.get("user_id"))
    return sorted(owners)[:limit]


# ── Memory ───────────────────────────────────────────────────────────────────


def memory_apply(
    user_id: str,
    op: str,
    *,
    actor: str,
    memory_id: str | None = None,
    expected_updated_at: str | None = None,
    fields: Mapping[str, Any] | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """RPC assistant_memory_apply (0176 B6): one memory op and its event in
    one transaction. Ops per actor: 'assistant' (the tool routes) create,
    revise_pending, propose_revision, forget; 'user' (Settings) create, edit,
    approve, confirm, reject, archive, restore, pin, unpin,
    approve_revision, reject_revision, delete; 'system' (the nightly step)
    archive, move, delete. Returns {"ok": true, "replay", "memory",
    "revision"} (or {"ok": true, "deleted": true, "memory_id"} for delete).
    Raises MemoryRefused for {"ok": false} (legal_hold included) and for
    the caps and pin-guard check_violations."""
    try:
        resp = _sb().rpc(MEMORY_APPLY_RPC, {
            "p_user_id": user_id,
            "p_op": op,
            "p_memory_id": str(memory_id) if memory_id else None,
            "p_expected_updated_at": expected_updated_at or None,
            "p_fields": dict(fields or {}),
            "p_actor": actor,
            "p_conversation_id": str(conversation_id) if conversation_id else None,
            "p_message_id": str(message_id) if message_id else None,
            "p_idempotency_key": idempotency_key or None,
        }).execute()
    except Exception as exc:
        code = _refusal(exc, _MEMORY_REFUSALS)
        if code:
            raise MemoryRefused(code) from exc
        raise
    data = _rpc_data(resp)
    if "ok" not in data:
        raise RuntimeError("assistant_memory_apply returned no result")
    if not data.get("ok"):
        raise MemoryRefused(str(data.get("code") or "memory_refused"), data.get("memory"))
    return data


def memory_similar(user_id: str, title: str, *, threshold: float = 0.5, limit: int = 5) -> list[dict]:
    """RPC assistant_memory_similar (0176 B5): the person's live notes whose
    title is close to `title` (pg_trgm), closest first: [{id, title,
    status, kind, similarity}]."""
    if not str(title or "").strip():
        return []
    return _rows(_sb().rpc(MEMORY_SIMILAR_RPC, {
        "p_user_id": user_id, "p_title": str(title), "p_threshold": float(threshold), "p_limit": int(limit),
    }).execute())


def touch_memories(user_id: str, memory_ids: Sequence[str]) -> int:
    """RPC assistant_memory_touch (0176 B7): last_used_at and use_count on
    the person's active rows among these ids, without moving updated_at.
    Returns how many rows were touched."""
    ids = _uuids(memory_ids)
    if not ids:
        return 0
    value = _scalar(_sb().rpc(MEMORY_TOUCH_RPC, {"p_user_id": user_id, "p_ids": ids}).execute())
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def get_memory(user_id: str, memory_id: str) -> dict | None:
    rows = _rows(
        _sb().table(MEMORIES_TABLE).select(MEMORY_COLUMNS)
        .eq("user_id", user_id).eq("id", str(memory_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def list_memories(
    user_id: str,
    *,
    status: str | Sequence[str] | None = None,
    kind: str | None = None,
    limit: int = 300,
    before: str | None = None,
) -> list[dict]:
    """The person's notes, most recently changed first; `status` is one
    status or several. `before` is the updated_at of the last item of the
    previous page (the Memory tab pages Archived, which has no cap)."""
    query = _status_filter(_sb().table(MEMORIES_TABLE).select(MEMORY_COLUMNS).eq("user_id", user_id), status)
    if kind:
        query = query.eq("kind", kind)
    if before:
        query = query.lt("updated_at", before)
    return _rows(
        query.order("updated_at", desc=True).order("id", desc=True)
        .limit(max(1, min(500, int(limit)))).execute()
    )


def load_prompt_memories(user_id: str) -> list[dict]:
    """Every active note of the person (the caps trigger keeps it at 300),
    for memory.select_for_prompt."""
    return _rows(
        _sb().table(MEMORIES_TABLE)
        .select("id, kind, title, body, project_id, status, pinned, origin_tainted, last_used_at, "
                "created_at, updated_at")
        .eq("user_id", user_id).eq("status", "active").limit(300).execute()
    )


def memories_by_ids(user_id: str, memory_ids: Sequence[str]) -> list[dict]:
    """[{id, title, kind}] for the person's notes among these ids, in the
    order given; a deleted note is simply missing (MessageOut.used_memories)."""
    ids = _uuids(memory_ids)
    if not ids:
        return []
    rows = _rows(
        _sb().table(MEMORIES_TABLE).select("id, title, kind")
        .eq("user_id", user_id).in_("id", ids).limit(len(ids)).execute()
    )
    by_id = {str(r.get("id")): r for r in rows}
    return [{"id": i, "title": by_id[i].get("title"), "kind": by_id[i].get("kind")} for i in ids if i in by_id]


def pending_revisions(user_id: str, memory_ids: Sequence[str]) -> dict[str, dict]:
    """{memory id: its undecided revision} (one at most per memory, the
    0176 partial unique index)."""
    ids = _ids(memory_ids)
    if not ids:
        return {}
    rows = _rows(
        _sb().table(REVISIONS_TABLE).select(REVISION_COLUMNS)
        .eq("user_id", user_id).in_("memory_id", ids).is_("decided_at", "null")
        .order("created_at").limit(len(ids) * 2).execute()
    )
    return {str(r.get("memory_id")): r for r in rows}


def memory_summary(user_id: str) -> dict:
    """The Memory tab's counts against the caps (MemorySummaryOut)."""
    rows = _rows(
        _sb().table(MEMORIES_TABLE).select("id, status, pinned, title, body")
        .eq("user_id", user_id).in_("status", list(MEMORY_LIVE_STATUSES)).limit(1000).execute()
    )
    pinned = [r for r in rows if r.get("status") == "active" and r.get("pinned")]
    revisions = _rows(
        _sb().table(REVISIONS_TABLE).select("id").eq("user_id", user_id).is_("decided_at", "null")
        .limit(1000).execute()
    )
    return {
        "active": sum(1 for r in rows if r.get("status") == "active"),
        "active_cap": 300,
        "pinned": len(pinned),
        "pinned_cap": 40,
        "pinned_chars": sum(len(r.get("title") or "") + len(r.get("body") or "") for r in pinned),
        "pinned_chars_cap": 6000,
        "pending_review": sum(1 for r in rows if r.get("status") == "pending_review"),
        "pending_revisions": len(revisions),
    }


# ── Trusted turn context (3.7.3) ─────────────────────────────────────────────


def turn_context(
    user_id: str, run_id: str | None, idempotency_key: str | None, *, tool_name: str | None = None
) -> dict | None:
    """What a housekeeping tool route may trust about the turn that called
    it, read from the server's own rows, never from model arguments: the
    begun ledger row for (user_id, run_id, idempotency_key), its assistant
    message, the conversation, and the latest user message before the
    answer (the loop's _history rule). Returns {conversation_id,
    message_id, tool_name, tainted, mailbox_tainted, mode, incognito,
    user_text, step_key, idempotency_key}, or None when the begun row or
    its message is missing, or when `tool_name` is given and the row was
    begun for another tool (the route answers 409 turn_context_unavailable).
    `tainted` is the begun row's tainted_context (the in-process taint at
    the moment of the call) or the conversation's tainted_at or the
    message's tainted flag; `step_key` is the ledger key without a trailing
    ":r<n>" retry suffix."""
    if not run_id or not idempotency_key:
        return None
    rows = _rows(
        _sb().table(TOOL_CALLS_TABLE).select(TOOL_CALL_COLUMNS)
        .eq("user_id", user_id).eq("run_id", str(run_id)).eq("idempotency_key", str(idempotency_key))
        .eq("phase", "begun").limit(1).execute()
    )
    begun = rows[0] if rows else None
    if begun is None or not begun.get("message_id"):
        return None
    if tool_name is not None and begun.get("tool_name") != tool_name:
        return None
    answer = get_message(user_id, str(begun["message_id"]))
    if answer is None:
        return None
    conversation_id = str(begun.get("conversation_id") or answer.get("conversation_id") or "") or None
    conversation = get_conversation(user_id, conversation_id) if conversation_id else None
    if not conversation or conversation.get("mode") not in ("normal", "mailbox_review"):
        return None
    asked = _rows(
        _sb().table(MESSAGES_TABLE).select("id, content, incognito, created_at")
        .eq("user_id", user_id).eq("conversation_id", conversation_id or "").eq("role", "user")
        .lte("created_at", str(answer.get("created_at") or _now_iso()))
        .order("created_at", desc=True).order("id", desc=True).limit(2).execute()
    ) if conversation_id else []
    question = asked[0] if asked else {}
    conversation = conversation or {}
    return {
        "conversation_id": conversation_id,
        "message_id": str(begun["message_id"]),
        "tool_name": begun.get("tool_name"),
        "tainted": bool(begun.get("tainted_context") or conversation.get("tainted_at") or answer.get("tainted")),
        "mailbox_tainted": bool(conversation.get("mailbox_tainted_at") or answer.get("mailbox_tainted")),
        "mode": conversation["mode"],
        "incognito": bool(answer.get("incognito") or question.get("incognito")),
        "user_text": str(question.get("content") or ""),
        "user_texts": [str(q.get("content") or "") for q in reversed(asked)],
        "step_key": _RETRY_SUFFIX.sub("", str(idempotency_key)),
        "idempotency_key": str(idempotency_key),
    }


# ── Offboarding (3.10.3) ─────────────────────────────────────────────────────


def revoke_delegation(user_id: str, reason: str) -> int:
    """Revoke the person's unrevoked grants (one at most, the partial unique
    index) with `reason`. Returns how many rows were revoked."""
    return len(_rows(
        _sb().table(DELEGATIONS_TABLE)
        .update({"revoked_at": _now_iso(), "revoked_reason": str(reason)})
        .eq("user_id", user_id).is_("revoked_at", "null").execute()
    ))


def open_approvals(user_id: str, statuses: Sequence[str] = OFFBOARD_APPROVAL_STATUSES) -> list[dict]:
    """The person's cards in these statuses, oldest first."""
    return _rows(
        _sb().table(APPROVALS_TABLE).select(APPROVAL_COLUMNS)
        .eq("user_id", user_id).in_("status", [str(s) for s in statuses])
        .order("created_at").order("id").limit(500).execute()
    )


def expire_approvals(user_id: str, approval_ids: Sequence[str], reason: str) -> list[dict]:
    """Expire these pending or approved cards with `reason`, each fenced on
    the status it was read with (the immutability trigger allows pending and
    approved to move to expired). Returns the rows that moved; the caller
    settles their messages."""
    ids = _ids(approval_ids)
    if not ids:
        return []
    rows = _rows(
        _sb().table(APPROVALS_TABLE).select("id, status")
        .eq("user_id", user_id).in_("id", ids).in_("status", list(OFFBOARD_APPROVAL_STATUSES))
        .limit(len(ids)).execute()
    )
    moved: list[dict] = []
    for row in rows:
        updated = _rows(
            _sb().table(APPROVALS_TABLE)
            .update({"status": "expired", "expired_reason": str(reason), "updated_at": _now_iso()})
            .eq("user_id", user_id).eq("id", row["id"]).eq("status", row["status"]).execute()
        )
        moved.extend(updated)
    return moved


def open_tasks(user_id: str) -> list[dict]:
    """The person's tasks that are not finished (the open statuses plus
    waiting_approval), oldest first."""
    return _rows(
        _sb().table(TASKS_TABLE).select(TASK_COLUMNS)
        .eq("user_id", user_id).in_("status", [*OPEN_TASK_STATUSES, "waiting_approval"])
        .order("created_at").order("id").limit(200).execute()
    )


def open_split_tasks(user_id: str | None, *, limit: int = 100) -> list[dict]:
    """Split tasks still waiting on or running their continuation (the
    statuses continue_job.stop_for_owner fences on), oldest first. The
    tasks rider (3.5 item 2) reads them while bidding is off. None is the
    system sweep."""
    return _rows(
        _scope(_sb().table(TASKS_TABLE).select(TASK_COLUMNS), user_id)
        .eq("kind", "split_into_project").in_("status", ["waiting_external", "running"])
        .order("created_at").order("id").limit(max(1, min(500, int(limit)))).execute()
    )


def active_holds(user_id: str) -> list[dict]:
    """The person's active spend holds (released by offboarding step 6)."""
    return _rows(
        _sb().table(RESERVATIONS_TABLE).select(RESERVATION_COLUMNS)
        .eq("user_id", user_id).eq("status", "active").order("created_at").limit(200).execute()
    )


def unpurged_staged_count(user_id: str) -> int:
    """How many of the person's staged rows still hold a storage object
    (purged_at null, storage_path set). Offboarding step 8 checks it after
    a hard delete's purge, so an object that would outlive its row is in
    the audit (offboarding map G10)."""
    rows = _rows(
        _sb().table(FILES_TABLE).select("id, storage_path").eq("user_id", user_id)
        .is_("purged_at", "null").limit(1000).execute()
    )
    return sum(1 for r in rows if str(r.get("storage_path") or "").strip())


def unconsumed_staged_files(user_id: str) -> list[dict]:
    """The person's live, unclaimed staged rows, linked or not (offboarding
    step 8 retires them)."""
    return _rows(
        _live_unclaimed(_sb().table(FILES_TABLE).select(FILE_COLUMNS).eq("user_id", user_id))
        .order("created_at").limit(500).execute()
    )


# ── Phase 4 (0180): reasons, evidence log, report files, AE holds and tasks ──

EVIDENCE_TABLE = "assistant_evidence_access"
REPORTS_TABLE = "assistant_reports"
DECIDE_WITH_REASON_RPC = "assistant_approval_decide_with_reason"
MEMORY_SEARCH_RPC = "assistant_memory_search"
REPORT_APPROVAL_INDEX = "assistant_reports_approval_uq"
AE_HOLD_KINDS = ("plans_pages", "ai_estimate")
AE_TASK_KINDS = ("plans_read_pages", "ai_estimating_run")
REPORT_STATUSES = ("ready", "purged")
MEMORY_SEARCH_MAX = 25
EVIDENCE_COLUMNS = (
    "id, reader_user_id, reader_label, reader_role, subject_kind, subject_user_id, subject_label, "
    "project_id, tool, view, filters, row_count, truncated, xlsx_report_id, reason, approval_id, "
    "conversation_id, run_id, created_at"
)
REPORT_COLUMNS = (
    "id, user_id, spec, params, title, filename, storage_path, row_count, truncated, bytes, "
    "conversation_id, run_id, approval_id, status, expires_at, purged_at, created_at"
)


class ReportExists(RuntimeError):
    """A report for this approval already exists (the partial unique index
    assistant_reports_approval_uq refused the insert: a replay of the same
    evidence card). The text keeps the database's own message."""

    code = "23505"


def decide_with_reason(
    user_id: str,
    approval_id: str,
    *,
    payload_sha256: str | None,
    precondition: Mapping[str, Any] | None,
    session_aal: str,
    reason: str,
) -> dict:
    """RPC assistant_approval_decide_with_reason (0180 A5): the approve CAS
    of decide_approval plus the human-typed reason of a sensitive_read card,
    in one statement. Approve only (a reject keeps decide_approval). Returns
    {"ok", "reason", "row"}; `reason` is the refusal code: reason_invalid
    (the reason is not 10 to 500 characters after trimming) or, by delegation
    to the 0172 classification, not_found, expired, superseded, not_pending
    and stale. The reason text is stored on the approval row once; it is
    never part of the hashed payload and never logged here."""
    resp = _sb().rpc(DECIDE_WITH_REASON_RPC, {
        "p_user_id": user_id,
        "p_approval_id": approval_id,
        "p_payload_sha256": payload_sha256,
        "p_precondition": dict(precondition) if precondition is not None else None,
        "p_session_aal": session_aal,
        "p_reason": str(reason or ""),
    }).execute()
    data = _rpc_data(resp)
    if "ok" not in data:
        raise RuntimeError("assistant_approval_decide_with_reason returned no result")
    return data


def approval_for_route(user_id: str, approval_id: str) -> dict | None:
    """What an approval-bound evidence POST route checks (3.2.3): {status,
    action_kind, payload, decision_reason, conversation_id}, or None when
    the approval is not this person's. `decision_reason` is the text the
    human typed on the card (None until the card was approved with one)."""
    if not approval_id:
        return None
    rows = _rows(
        _sb().table(APPROVALS_TABLE)
        .select("status, action_kind, payload, decision_reason, conversation_id")
        .eq("user_id", user_id).eq("id", str(approval_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def insert_evidence_access(user_id: str, row: Mapping[str, Any]) -> dict | None:
    """Append one row to the evidence access log (append-only, 0180 A3 and
    B4). `user_id` is the READER; it overrides any reader_user_id in `row`.
    Idempotent on approval_id (the unique index, ON CONFLICT DO NOTHING):
    returns the inserted row, or None when this approval was already logged
    (a replay), in which case nothing was written. The log is never updated
    and nothing here deletes from it."""
    payload = {**dict(row), "reader_user_id": user_id}
    if not payload.get("approval_id"):
        raise ValueError("an evidence access row names its approval")
    rows = _rows(
        _sb().table(EVIDENCE_TABLE)
        .upsert(payload, on_conflict="approval_id", ignore_duplicates=True).execute()
    )
    return rows[0] if rows else None


def evidence_access_for_approval(user_id: str, approval_id: str) -> dict | None:
    """The reader's own access row for an approval (a replay reads the
    xlsx_report_id of the first run from it)."""
    if not approval_id:
        return None
    rows = _rows(
        _sb().table(EVIDENCE_TABLE).select(EVIDENCE_COLUMNS)
        .eq("reader_user_id", user_id).eq("approval_id", str(approval_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def insert_report(user_id: str, row: Mapping[str, Any]) -> dict:
    """Insert one report file row for `user_id`. `row` carries an explicit
    `id`, because storage_path embeds it (assistant-reports/{user_id}/{id}.xlsx;
    the 0180 check refuses any other path). A second row for one approval
    raises ReportExists."""
    if not row.get("id"):
        raise ValueError("a report row needs an explicit id: its storage path embeds it")
    try:
        rows = _rows(_sb().table(REPORTS_TABLE).insert({**dict(row), "user_id": user_id}).execute())
    except Exception as exc:
        if REPORT_APPROVAL_INDEX in str(exc):
            raise ReportExists(str(exc)) from exc
        raise
    if not rows:
        raise RuntimeError("report insert returned no row")
    return rows[0]


def get_report(user_id: str, report_id: str) -> dict | None:
    """The person's own report row (any status), or None."""
    if not report_id:
        return None
    rows = _rows(
        _sb().table(REPORTS_TABLE).select(REPORT_COLUMNS)
        .eq("user_id", user_id).eq("id", str(report_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def report_for_approval(user_id: str, approval_id: str) -> dict | None:
    """The report an approved evidence card built (the replay key)."""
    if not approval_id:
        return None
    rows = _rows(
        _sb().table(REPORTS_TABLE).select(REPORT_COLUMNS)
        .eq("user_id", user_id).eq("approval_id", str(approval_id)).limit(1).execute()
    )
    return rows[0] if rows else None


def list_reports(user_id: str, *, limit: int = 20) -> list[dict]:
    """The person's report rows, newest first (GET /assistant/reports)."""
    return _rows(
        _sb().table(REPORTS_TABLE).select(REPORT_COLUMNS).eq("user_id", user_id)
        .order("created_at", desc=True).order("id", desc=True)
        .limit(max(1, min(100, int(limit)))).execute()
    )


def owner_reports(
    user_id: str,
    *,
    specs: Sequence[str] | None = None,
    statuses: Sequence[str] = ("ready",),
    limit: int = 500,
) -> list[dict]:
    """The person's report rows in these statuses (and, with `specs`, of
    these specs), oldest first. The offboarding purge reads the rows whose
    objects it deletes through this, and the orphan pass reads the ids that
    still have a row."""
    query = _sb().table(REPORTS_TABLE).select(REPORT_COLUMNS).eq("user_id", user_id)
    query = query.in_("status", [str(s) for s in statuses])
    if specs is not None:
        if not specs:
            return []
        query = query.in_("spec", [str(s) for s in specs])
    return _rows(query.order("created_at").order("id").limit(max(1, min(1000, int(limit)))).execute())


def reports_to_purge(user_id: str | None, *, limit: int = 200) -> list[dict]:
    """Ready report rows past expires_at, oldest expiry first, never a row
    whose owner is on legal hold (3.9.3): [{id, user_id, storage_path,
    expires_at, filename}]. `user_id` scopes the read to one person; None is
    the SYSTEM SWEEP across every user, called only from the maintenance
    rider (pinned by tests/test_assistant_repo_isolation)."""
    limit = max(1, min(200, int(limit)))
    held = _legal_hold_owners(user_id)
    if user_id is not None and str(user_id) in held:
        return []
    query = _scope(
        _sb().table(REPORTS_TABLE).select("id, user_id, storage_path, expires_at, filename"), user_id
    ).eq("status", "ready").lte("expires_at", _now_iso())
    if user_id is None and held:
        query = query.not_.in_("user_id", sorted(held))
    rows = _rows(query.order("expires_at").order("id").limit(limit).execute())
    return [r for r in rows if str(r.get("user_id")) not in held][:limit]


def mark_report_purged(user_id: str, report_id: str) -> bool:
    """ready -> purged with purged_at = now, once (fenced on status='ready').
    The caller has verified the storage object is gone. True when a row
    moved."""
    if not report_id:
        return False
    return bool(_rows(
        _sb().table(REPORTS_TABLE).update({"status": "purged", "purged_at": _now_iso()})
        .eq("user_id", user_id).eq("id", str(report_id)).eq("status", "ready").execute()
    ))


def search_memories(
    user_id: str,
    q: str,
    statuses: Sequence[str] | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """RPC assistant_memory_search (0180 A6): the person's own notes ranked
    against `q`, best first: [{id, kind, status, title, body, project_id,
    pinned, origin_tainted, updated_at, rank}]. `statuses` is a subset of
    active and pending_review (default both; archived never matches). A
    query under 2 characters answers []. The result is capped at 25."""
    text = str(q or "").strip()
    if len(text) < 2:
        return []
    wanted = [s for s in (statuses or MEMORY_LIVE_STATUSES) if s in MEMORY_LIVE_STATUSES]
    return _rows(_sb().rpc(MEMORY_SEARCH_RPC, {
        "p_user_id": user_id,
        "p_q": text[:200],
        "p_statuses": wanted or list(MEMORY_LIVE_STATUSES),
        "p_kind": kind or None,
        "p_limit": max(1, min(MEMORY_SEARCH_MAX, int(limit))),
    }).execute())


def tool_names_for_message(user_id: str, message_id: str | None) -> list[str]:
    """The distinct tool names an assistant message called (its begun
    ledger rows), sorted. The loop surfaces the deferred ones among them in
    round 1 of the next turn (3.16.3)."""
    if not message_id:
        return []
    rows = _rows(
        _sb().table(TOOL_CALLS_TABLE).select("tool_name")
        .eq("user_id", user_id).eq("message_id", str(message_id)).eq("phase", "begun")
        .limit(200).execute()
    )
    return sorted({str(r["tool_name"]) for r in rows if r.get("tool_name")})


def open_ae_tasks(user_id: str | None, *, limit: int = 100) -> list[dict]:
    """The AI Estimating tasks still running (plans_read_pages and
    ai_estimating_run in pending, waiting_external or running), oldest
    first. `user_id` scopes the read to one person (offboarding, the lazy
    finalize); None is the SYSTEM SWEEP across every user, called only from
    the maintenance rider ae_task_sweep (pinned by
    tests/test_assistant_repo_isolation)."""
    return _rows(
        _scope(_sb().table(TASKS_TABLE).select(TASK_COLUMNS), user_id)
        .in_("kind", list(AE_TASK_KINDS)).in_("status", list(OPEN_TASK_STATUSES))
        .order("created_at").order("id").limit(max(1, min(500, int(limit)))).execute()
    )


def ae_hold_by_approval(user_id: str, approval_id: str, kind: str) -> dict | None:
    """The person's AI Estimating hold (kind plans_pages or ai_estimate) for
    an approval, in ANY status, or None. 0180 A8 allows one such hold per
    approval, so a route replay looks here before reserving: an active hold
    with no task is reused, never reserved again."""
    if kind not in AE_HOLD_KINDS:
        raise ValueError(f"not an AI Estimating hold kind: {kind!r}")
    if not approval_id:
        return None
    rows = _rows(
        _sb().table(RESERVATIONS_TABLE).select(RESERVATION_COLUMNS)
        .eq("user_id", user_id).eq("approval_id", str(approval_id)).eq("kind", kind).limit(1).execute()
    )
    return rows[0] if rows else None



# Phase 5: application-mail claims and audit. SQL owns cross-worker atomicity.
def _mail_rpc(user_id: str, name: str, **params: Any) -> dict:
    if not user_id:
        raise ValueError("owner required")
    data = _scalar(_sb().rpc(name, {"p_user_id": str(user_id), **{
        f"p_{key}": value for key, value in params.items()
    }}).execute())
    if not isinstance(data, dict):
        raise RuntimeError("mail operation returned no result")
    return data


def mail_send_claim(user_id: str, approval_id: str, run_id: str, sender: str,
                    payload_sha256: str, effective_recipients: Sequence[str] = ()) -> dict:
    return _mail_rpc(user_id, "assistant_mail_send_claim", approval_id=approval_id,
                     run_id=run_id, sender=sender, payload_sha256=payload_sha256,
                     effective_recipients=list(effective_recipients))


def mail_send_finish(user_id: str, send_id: str, status: str, error_code: str | None = None) -> dict:
    return _mail_rpc(user_id, "assistant_mail_send_finish", send_id=send_id,
                     status=status, error_code=error_code)


def mailbox_access_claim(user_id: str, approval_id: str, run_id: str,
                         target_upn: str, payload_sha256: str) -> dict:
    return _mail_rpc(user_id, "assistant_mailbox_access_claim", approval_id=approval_id,
                     run_id=run_id, target_upn=target_upn, payload_sha256=payload_sha256)


def mailbox_access_outcome(user_id: str, access_id: str, status: str,
                           message_ids: Sequence[str] = (), error_code: str | None = None) -> dict:
    return _mail_rpc(user_id, "assistant_mailbox_access_outcome", access_id=access_id,
                     status=status, message_ids=list(message_ids), error_code=error_code)


def mailbox_access_list(user_id: str, *, limit: int = 100, before: str | None = None) -> list[dict]:
    stamp, before_id = _mail_cursor(before)
    result = _mail_rpc(user_id, "assistant_mailbox_access_page", limit=max(1, min(200, limit)),
                       before=stamp, before_id=before_id)
    if not result.get("ok"):
        raise ValueError("access log unavailable")
    rows = (result.get("rows") or [])[:200]
    if not rows:
        return []
    # The RPC authorized this exact bounded page for the live IT Admin.
    # Never query arbitrary client-supplied access IDs or expose raw message IDs.
    ids = [str(row["id"]) for row in rows if row.get("id")]
    outcomes = _rows(_sb().table("assistant_mailbox_outcomes")
                     .select("access_id,status").in_("access_id", ids).limit(len(ids)).execute()) if ids else []
    by_id = {str(row["access_id"]): row.get("status") for row in outcomes}
    return [{**row, "outcome": by_id.get(str(row.get("id")))} for row in rows]


def evidence_access_list(user_id: str, *, limit: int = 100, before: str | None = None) -> list[dict]:
    stamp, before_id = _mail_cursor(before)
    result = _mail_rpc(user_id, "assistant_evidence_access_page", limit=max(1, min(200, limit)),
                       before=stamp, before_id=before_id)
    if not result.get("ok"):
        raise ValueError("access log unavailable")
    return result.get("rows") or []


def calendar_preferences_get(user_id: str) -> dict:
    rows = _rows(_sb().table("assistant_calendar_preferences").select("enabled,mode,lead_minutes")
                 .eq("user_id", user_id).limit(1).execute())
    return rows[0] if rows else {"enabled": False, "mode": "context", "lead_minutes": 15}


def calendar_preferences_set(user_id: str, *, enabled: bool, mode: str, lead_minutes: int) -> dict:
    return _mail_rpc(user_id, "assistant_calendar_preferences_set", enabled=enabled,
                     mode=mode, lead_minutes=lead_minutes)


def calendar_owners(user_id: str | None, *, limit: int = 100) -> list[dict]:
    """None is the calendar maintenance sweep; every operation rechecks eligibility."""
    return _rows(_scope(_sb().table("assistant_calendar_preferences")
                       .select("user_id,enabled,mode,lead_minutes"), user_id)
                 .eq("enabled", True).order("user_id").limit(max(1, min(500, limit))).execute())


def calendar_reminder_claim(user_id: str, event_key: str, starts_at: str, lead_minutes: int,
                            *, expected_mode: str = "context", expected_email: str = "") -> bool:
    result = _mail_rpc(user_id, "assistant_calendar_reminder_claim", event_key=event_key,
                       starts_at=starts_at, lead_minutes=lead_minutes,
                       expected_mode=expected_mode, expected_email=expected_email)
    return bool(result.get("ok") and result.get("claimed"))


def mail_notice_claim(user_id: str, notice_key: str) -> bool:
    result = _mail_rpc(user_id, "assistant_mail_notice_claim", notice_key=notice_key)
    return bool(result.get("ok") and result.get("claimed"))


def mail_user_texts(user_id: str, run_id: str) -> list[str]:
    rows = _rows(_sb().table(TOOL_CALLS_TABLE).select("message_id,conversation_id")
                 .eq("user_id", user_id).eq("run_id", run_id).eq("phase", "begun")
                 .order("created_at", desc=True).limit(1).execute())
    if not rows or not rows[0].get("message_id"):
        return []
    answer = get_message(user_id, str(rows[0]["message_id"]))
    if not answer or not answer.get("created_at"):
        return []
    conversation = get_conversation(user_id, str(answer.get("conversation_id") or ""))
    if not conversation or conversation.get("mode") != "normal":
        return []
    questions = _rows(_sb().table(MESSAGES_TABLE).select("content")
                      .eq("user_id", user_id).eq("conversation_id", answer["conversation_id"])
                      .eq("role", "user").lte("created_at", answer["created_at"])
                      .order("created_at", desc=True).order("id", desc=True).limit(2).execute())
    return [str(row.get("content") or "") for row in reversed(questions)]


def mail_contacts(user_id: str, gc_contact_ids: Sequence[str] = (),
                  vendor_contact_ids: Sequence[str] = ()) -> list[dict]:
    """Current internal BDR contacts only; model IDs never imply authority."""
    profile = human_profile(user_id)
    from app.core.roles import INTERNAL_ROLES
    if not profile or profile.get("is_active") is not True or profile.get("role") not in INTERNAL_ROLES:
        return []
    result = []
    for table, ids, source in (("gc_contacts", gc_contact_ids, "gc_contact"),
                                ("vendor_contacts", vendor_contact_ids, "vendor_contact")):
        wanted = _uuids(ids)[:100]
        if wanted:
            result.extend({**row, "source": source} for row in _rows(
                _sb().table(table).select("id,email").in_("id", wanted).execute()))
    return result


def calendar_context_candidates(user_id: str) -> list[dict]:
    """Internal project access only; no actual bid amounts/dates are exposed."""
    from app.core.roles import INTERNAL_ROLES
    profile = human_profile(user_id)
    if not profile or profile.get("is_active") is not True or profile.get("role") not in INTERNAL_ROLES:
        return []
    now = datetime.now(timezone.utc)
    projects = _rows(_sb().table("projects").select("id,name,internal_bid_at")
                     .gte("internal_bid_at", now.isoformat())
                     .lt("internal_bid_at", (now + timedelta(days=7)).isoformat())
                     .not_.in_("current_stage", ["pm_only", "cp_only", "declined", "submitted", "bid_outcome"])
                     .limit(200).execute())
    ids = [row["id"] for row in projects]
    if not ids:
        return []
    links = _rows(_sb().table("project_gcs").select("project_id,gc_id").in_("project_id", ids).limit(1000).execute())
    gc_ids = list({row["gc_id"] for row in links})
    contacts = _rows(_sb().table("gc_contacts").select("gc_id,email").in_("gc_id", gc_ids).limit(2000).execute()) if gc_ids else []
    return [{**project, "contact_emails": sorted({str(contact["email"]).lower()
              for link in links if link["project_id"] == project["id"]
              for contact in contacts if contact["gc_id"] == link["gc_id"] and contact.get("email")})}
            for project in projects]


def admin_conversation_read(user_id: str, owner_id: str, conversation_id: str | None, *,
                            reason_kind: str, reason: str, action: str = "read") -> dict:
    return _mail_rpc(user_id, "assistant_admin_conversation_read", owner_id=owner_id,
                     conversation_id=conversation_id, reason_kind=reason_kind, reason=reason, action=action)


def conversation_views(user_id: str, conversation_id: str) -> list[dict]:
    """Owner-visible audit metadata, never the reviewing person's private reason."""
    return _rows(_sb().table("assistant_conversation_views")
                 .select("id,viewer_label,action,created_at")
                 .eq("owner_id", user_id).or_(f"conversation_id.eq.{uuid.UUID(conversation_id)},conversation_id.is.null")
                 .order("created_at", desc=True).limit(100).execute())


def mail_notice_deliver(user_id: str, notice_key: str, title: str, why: str,
                        payload: Mapping[str, Any] | None = None) -> bool:
    result = _mail_rpc(user_id, "assistant_mail_notice_deliver", notice_key=notice_key,
                       title=title, why=why, payload=dict(payload or {}))
    return bool(result.get("ok") and result.get("delivered"))


def mail_audit_summary(user_id: str, start: str, end: str) -> dict:
    return _mail_rpc(user_id, "assistant_mail_audit_summary", start=start, end=end)


def mail_reviewers(user_id: str | None) -> list[dict]:
    """None is the deterministic audit digest sweep; no content is returned."""
    if user_id is not None:
        raise ValueError("reviewer sweep requires user_id None")
    return _rows(_sb().table("profiles").select("id,role")
                 .eq("is_active", True).in_("role", ["executive", "it_admin"]).limit(1000).execute())


def mail_exclusions_sync(user_id: str | None, exclusions: Sequence[str]) -> dict:
    """System-only effective configuration audit, atomically notified in SQL."""
    if user_id is not None:
        raise ValueError("configuration sweep requires user_id None")
    result = _scalar(_sb().rpc("assistant_mail_exclusions_sync", {
        "p_exclusions": sorted(set(exclusions)),
    }).execute())
    if not isinstance(result, dict):
        raise RuntimeError("configuration sync returned no result")
    return result


def mail_sensitive_context(user_id: str, run_id: str) -> bool:
    """Fail closed on missing provenance or any source taint in a mail draft.

    Mail content and PDFs cannot be reliably classified by text heuristics.
    The send service uses this server-derived flag to restrict recipients of
    tainted drafts, and freezes it in the approval authority snapshot.
    """
    if not run_id:
        return True
    rows = _rows(_sb().table(TOOL_CALLS_TABLE)
                 .select("message_id,conversation_id,tainted_context")
                 .eq("user_id", user_id).eq("run_id", str(run_id)).eq("phase", "begun")
                 .order("created_at", desc=True).limit(50).execute())
    if not rows or any(row.get("tainted_context") is True for row in rows):
        return True
    message_ids = {str(row.get("message_id") or "") for row in rows}
    conversation_ids = {str(row.get("conversation_id") or "") for row in rows}
    if len(message_ids) != 1 or len(conversation_ids) != 1 or "" in message_ids or "" in conversation_ids:
        return True
    message = get_message(user_id, next(iter(message_ids)))
    conversation = get_conversation(user_id, next(iter(conversation_ids)))
    if (not message or not conversation or conversation.get("mode") != "normal"
            or message.get("conversation_id") != conversation.get("id")):
        return True
    return bool(message.get("tainted") or message.get("mailbox_tainted")
                or conversation.get("tainted_at") or conversation.get("mailbox_tainted_at"))


def _mail_cursor(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    stamp, separator, row_id = str(value).partition("|")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cursor timezone required")
    return parsed.isoformat(), str(uuid.UUID(row_id)) if separator else None


# Phase 6: owner-scoped personal browser vault. Secret-bearing rows stay server-side.
def browser_credentials(user_id: str) -> list[dict]:
    return _rows(_sb().table("assistant_credentials").select("*")
                 .eq("user_id", user_id).order("service").limit(20).execute())


def browser_credential(user_id: str, service: str) -> dict | None:
    rows = _rows(_sb().table("assistant_credentials").select("*")
                 .eq("user_id", user_id).eq("service", service).limit(1).execute())
    return rows[0] if rows else None


def browser_credential_by_id(user_id: str, credential_id: str) -> dict | None:
    rows = _rows(_sb().table("assistant_credentials").select("*")
                 .eq("user_id", user_id).eq("id", credential_id).limit(1).execute())
    return rows[0] if rows else None


def browser_credential_put(user_id: str, service: str, expected_version: int, record: dict) -> dict:
    return _rpc_data(_sb().rpc("assistant_browser_credential_put", {
        "p_user_id": user_id, "p_service": service, "p_expected_version": expected_version,
        "p_record": record,
    }).execute())


def browser_credential_revoke(user_id: str, service: str, expected_version: int) -> dict:
    return _rpc_data(_sb().rpc("assistant_browser_credential_revoke", {
        "p_user_id": user_id, "p_service": service, "p_expected_version": expected_version,
    }).execute())


def browser_revoke_owner(user_id: str) -> Any:
    return _sb().rpc("assistant_browser_revoke_owner", {"p_user_id": user_id}).execute().data


def browser_jobs(user_id: str, *, limit: int = 50) -> list[dict]:
    return _rows(_sb().table("browser_agent_jobs").select("*")
                 .eq("principal_kind", "user").eq("principal_user_id", user_id)
                 .order("created_at", desc=True).order("id", desc=True).limit(min(limit, 50)).execute())


def browser_job(user_id: str, job_id: str) -> dict | None:
    rows = _rows(_sb().table("browser_agent_jobs").select("*")
                 .eq("principal_kind", "user").eq("principal_user_id", user_id)
                 .eq("id", job_id).limit(1).execute())
    return rows[0] if rows else None


# Phase 6 execution adapters. User identity is always the first argument.
def browser_platform_config(user_id: str) -> tuple[dict, dict]:
    types = _rows(_sb().table('browser_agent_types').select('*').eq('key', 'user_browser_read').limit(1).execute())
    settings = _rows(_sb().table('browser_agent_settings').select('*').eq('id', 1).limit(1).execute())
    return (types[0] if types else {}), (settings[0] if settings else {})


def browser_job_by_approval(user_id: str, approval_id: str) -> dict | None:
    rows = _rows(_sb().table('browser_agent_jobs').select('*').eq('principal_kind', 'user')
                 .eq('principal_user_id', user_id).eq('assistant_approval_id', approval_id).limit(1).execute())
    return rows[0] if rows else None


def browser_rpc(user_id: str, name: str, params: Mapping[str, Any]) -> dict:
    if not name.startswith('assistant_browser_'):
        raise ValueError('Not a personal browser RPC')
    return _rpc_data(_sb().rpc(name, dict(params)).execute())


def browser_worker_job(user_id: str | None, job_id: str) -> dict | None:
    # A worker authenticates and fences this candidate before seeing content.
    query = _sb().table('browser_agent_jobs').select('*').eq('principal_kind', 'user').eq('id', job_id)
    if user_id is not None:
        query = query.eq('principal_user_id', user_id)
    rows = _rows(query.limit(1).execute())
    return rows[0] if rows else None


def browser_session(user_id: str, credential_id: str, version: int, origin: str) -> dict | None:
    rows = _rows(_sb().table('assistant_browser_sessions').select('*').eq('user_id', user_id)
                 .eq('credential_id', credential_id).eq('credential_version', version).eq('origin', origin)
                 .gt('expires_at', _now_iso()).limit(1).execute())
    return rows[0] if rows else None


# Encrypted user browser records and human help. All private reads bind owner
# and job; only the audited inspection RPC can read another owner's content.
def browser_record_put(user_id: str, job_id: str, worker_id: str, boot_id: str, epoch: int, record: dict) -> dict:
    return _rpc_data(_sb().rpc("assistant_browser_record_put", {
        "p_user_id": user_id, "p_job_id": job_id, "p_worker_id": worker_id,
        "p_boot_id": boot_id, "p_epoch": epoch, "p_record": record,
    }).execute())


def browser_records(user_id: str, job_id: str, *, kinds: tuple[str, ...],
                    before: tuple[str, str] | None, limit: int = 50) -> list[dict]:
    query = (_sb().table("assistant_browser_records").select("*").eq("user_id", user_id)
             .eq("job_id", job_id).in_("kind", list(kinds)))
    if before:
        stamp, identifier = before
        stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).isoformat()
        identifier = str(uuid.UUID(identifier))
        query = query.or_(f"created_at.lt.{stamp},and(created_at.eq.{stamp},id.lt.{identifier})")
    return _rows(query.order("created_at", desc=True).order("id", desc=True)
                 .limit(max(1, min(201, limit))).execute())


def browser_record(user_id: str, job_id: str, record_id: str) -> dict | None:
    rows = _rows(_sb().table("assistant_browser_records").select("*").eq("user_id", user_id)
                 .eq("job_id", job_id).eq("id", record_id).limit(1).execute())
    return rows[0] if rows else None


def browser_help_state(user_id: str, job_id: str) -> dict | None:
    rows = _rows(_sb().table("assistant_browser_help_state").select("*").eq("user_id", user_id)
                 .eq("job_id", job_id).order("created_at", desc=True).order("id", desc=True).limit(1).execute())
    return rows[0] if rows else None


def browser_help_records(user_id: str, job_id: str, help_id: str) -> list[dict]:
    return _rows(_sb().table("assistant_browser_records").select("*").eq("user_id", user_id)
                 .eq("job_id", job_id).eq("help_id", help_id).order("created_at").order("id").limit(101).execute())


def browser_help_open(user_id: str, job_id: str, worker_id: str, boot_id: str, epoch: int,
                      help_id: str, record: dict, login_attempt_id: str | None) -> dict:
    return _rpc_data(_sb().rpc("assistant_browser_help_open", {
        "p_job": job_id, "p_worker": worker_id, "p_boot": boot_id, "p_epoch": epoch,
        "p_help_id": help_id, "p_record": record, "p_login_attempt_id": login_attempt_id,
    }).execute())


def browser_help_reply(user_id: str, job_id: str, help_id: str, client_msg_id: str,
                       action: str, record: dict) -> dict:
    return _rpc_data(_sb().rpc("assistant_browser_help_reply", {
        "p_user_id": user_id, "p_job_id": job_id, "p_help_id": help_id,
        "p_client_msg_id": client_msg_id, "p_action": action, "p_record": record,
    }).execute())


def browser_help_consume(user_id: str, job_id: str, worker_id: str, boot_id: str, epoch: int,
                         help_id: str, record_id: str, login_attempt_id: str) -> dict:
    return _rpc_data(_sb().rpc("assistant_browser_help_consume", {
        "p_job": job_id, "p_worker": worker_id, "p_boot": boot_id, "p_epoch": epoch,
        "p_help_id": help_id, "p_record_id": record_id, "p_login_attempt_id": login_attempt_id,
    }).execute())


def browser_inspect(user_id: str, viewer_id: str, job_id: str, reason: str, *, kind: str = "all") -> dict:
    return _rpc_data(_sb().rpc("assistant_browser_inspect", {
        "p_viewer_id": viewer_id, "p_user_id": user_id, "p_job_id": job_id,
        "p_reason": reason, "p_kind": kind,
    }).execute())


def browser_views(user_id: str, job_id: str) -> list[dict]:
    return _rows(_sb().table("assistant_browser_access").select("id,created_at")
                 .eq("user_id", user_id).eq("resource_kind", "job").eq("resource_id", job_id)
                 .order("created_at", desc=True).order("id", desc=True).limit(50).execute())


def browser_records_purge(user_id: str | None, *, limit: int = 100) -> dict:
    if user_id is not None:
        raise ValueError("browser record purge requires system scope")
    return _rpc_data(_sb().rpc("assistant_browser_records_purge", {"p_limit": max(1, min(500, limit))}).execute())


def browser_turn_history(user_id: str, job_id: str, *, before_turn: int) -> list[dict]:
    return _rows(_sb().table('assistant_browser_turns').select('*').eq('user_id', user_id)
                 .eq('job_id', job_id).eq('status', 'done').lt('turn_no', before_turn)
                 .order('turn_no', desc=True).limit(4).execute())


def browser_secret_hashes(user_id: str, job_id: str) -> dict[str, list[str]]:
    rows = _rows(_sb().table('assistant_browser_records').select('secret_hash,secret_key_id')
                 .eq('user_id', user_id).eq('job_id', job_id).eq('kind', 'help_secret').limit(100).execute())
    out: dict[str, list[str]] = {}
    for row in rows:
        if row.get('secret_hash') and row.get('secret_key_id'):
            out.setdefault(row['secret_key_id'], []).append(row['secret_hash'])
    return out


# ── Phase 7: the proactive router (docs/ASSISTANT_PHASE7_PLAN.md, WI-3) ──────
#
# Thin wrappers over the 0190 tables and RPCs. Every function takes user_id
# first. A function that spans every user (the event and send claims, the
# cursors, the slot ledger, the purge, the feedback report) is a SYSTEM SWEEP:
# `user_id: str | None`, only None is accepted, anything else raises. A
# function that serves one person filters by that person. The router writes
# one inbox row per person, never from one person's rows to another's.
# RPC wrappers always pass p_now (every 0190 router RPC writes its times from
# it, so the dev tick script can time-travel) except the two subscription
# RPCs, which have no clock argument. PostgREST upserts target only full
# unique constraints (the partial indexes of assistant_subscriptions are
# reached through the RPCs). Payloads, titles and whys are the callers'
# server templates; nothing here copies free text.

EVENTS_TABLE = "assistant_events"
INBOX_TABLE = "assistant_inbox"
SUBSCRIPTIONS_TABLE = "assistant_subscriptions"
DIGESTS_TABLE = "assistant_digests"
PROJECT_MUTES_TABLE = "assistant_project_mutes"
ROUTER_CURSORS_TABLE = "assistant_router_cursors"
PROACTIVE_SLOTS_TABLE = "assistant_proactive_slots"
EXTERNAL_SENDS_TABLE = "assistant_external_sends"
BELLS_TABLE = "notifications"

EVENTS_CLAIM_RPC = "assistant_events_claim"
EVENT_FINISH_RPC = "assistant_event_finish"
INBOX_DELIVER_RPC = "assistant_inbox_deliver"
DIGEST_DELIVER_RPC = "assistant_digest_deliver"
INBOX_MARK_READ_RPC = "assistant_inbox_mark_read"
INBOX_FEEDBACK_RPC = "assistant_inbox_feedback"
INBOX_SNOOZE_RPC = "assistant_inbox_snooze"
SUBSCRIPTION_SET_RPC = "assistant_subscription_set"
SUBSCRIPTION_RESET_RPC = "assistant_subscription_reset"
INBOX_DISMISS_RPC = "assistant_inbox_dismiss"
FEEDBACK_REPORT_RPC = "assistant_inbox_feedback_report"
LINK_BELL_RPC = "assistant_inbox_link_bell"
SENDS_CLAIM_RPC = "assistant_external_sends_claim"
SEND_SETTLE_RPC = "assistant_external_send_settle"
INBOX_FOLD_RPC = "assistant_inbox_fold"
PROACTIVE_PURGE_RPC = "assistant_proactive_purge"

# The full unique constraints PostgREST may target (0170 and 0190). The
# partial unique indexes of assistant_subscriptions are never upserted.
EVENTS_CONFLICT = "dedupe_key"
INBOX_CONFLICT = "user_id,dedupe_key"
PROJECT_MUTES_CONFLICT = "user_id,project_id"
SLOTS_CONFLICT = "step,slot_date"
SENDS_CONFLICT = "target_kind,target_id,channel"

EVENT_COLUMNS = (
    "id, event_type, dedupe_key, project_id, subject_user_id, payload, occurred_at, processed_at, "
    "created_at, status, source, claimed_by, claimed_at, attempts, next_attempt_at, last_error, routed"
)
INBOX_COLUMNS = (
    "id, user_id, dedupe_key, event_id, severity, title, why, payload, project_id, delivered_at, "
    "read_at, feedback, feedback_at, created_at, event_type, delivery, precedence, deliver_after, "
    "digest_id, notification_id, source_notification_id, dismissed_at, dismissed_reason, snooze_count"
)
SUBSCRIPTION_COLUMNS = (
    "id, role, user_id, event_type, enabled, severity, created_at, updated_at, source, updated_by"
)
DIGEST_COLUMNS = (
    "id, user_id, digest_date, slot, slot_at, since_at, item_count, record_count, notification_id, "
    "claimed_by, created_at"
)
SLOT_COLUMNS = "step, slot_date, claimed_by, claimed_at, finished_at, result"
CURSOR_COLUMNS = "name, cursor_at, cursor_id, updated_by, updated_at"
SEND_COLUMNS = (
    "id, user_id, target_kind, target_id, channel, status, not_before, claimed_by, claimed_at, "
    "attempts, next_attempt_at, sent_at, error_kind, email_log_id, created_at, updated_at"
)

# Columns a caller may set when it inserts (the rest are the router's RPCs').
_EVENT_INSERT_COLUMNS = (
    "event_type", "dedupe_key", "project_id", "subject_user_id", "payload", "occurred_at", "source",
    "status", "claimed_by", "claimed_at", "attempts", "next_attempt_at",
)
_INBOX_INSERT_COLUMNS = (
    "dedupe_key", "event_id", "severity", "title", "why", "payload", "project_id", "delivered_at",
    "event_type", "delivery", "precedence", "deliver_after", "source_notification_id", "created_at",
)
ROUTER_INBOX_DELIVERIES = ("immediate", "digest", "record")
PROACTIVE_STEPS = ("digest_am", "digest_pm", "purge")
ROUTER_CURSOR_NAMES = ("notifications", "audit_log")
DISMISS_REASONS = ("user", "project_closed", "owner_offboarded", "superseded", "muted")
FEEDBACK_VALUES = ("useful", "not_useful", "mute", "mute_project")
EVENT_FINISH_STATUSES = ("processed", "skipped", "failed", "retry")
SEND_OUTCOMES = ("sent", "transient", "permanent", "uncertain", "skipped", "deferred")
SEND_ERROR_KINDS = (
    "transient", "permanent", "uncertain", "lease_expired", "channel_off", "opted_out", "stale",
    "offboarded", "quiet_hours",
)
SLOT_LEASE_SECONDS = 15 * 60               # longer than the 600 s event lease (5.2)
_P7_IN_CHUNK = 100


def _p7_system_only(user_id: str | None, what: str) -> None:
    if user_id is not None:
        raise ValueError(f"{what} is a system sweep: pass user_id None")


def _p7_dt(value: Any) -> datetime:
    """A timezone-aware time as UTC; a naive one is refused (storage is UTC)."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("a timezone-aware time is required")
    return dt.astimezone(timezone.utc)


def _p7_iso(value: Any = None) -> str:
    """An ISO 8601 UTC string for p_now and the time columns; None is now."""
    return _now_iso() if value is None else _p7_dt(value).isoformat()


def _p7_iso_or_none(value: Any) -> str | None:
    return None if value is None else _p7_dt(value).isoformat()


def _p7_day(value: Any) -> str:
    """A Pacific office date as YYYY-MM-DD (a datetime is refused: which
    zone it is in would be a guess)."""
    if isinstance(value, datetime):
        raise ValueError("pass a date, not a datetime")
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _p7_chunks(values: Sequence[str]) -> list[list[str]]:
    return [list(values[i:i + _P7_IN_CHUNK]) for i in range(0, len(values), _P7_IN_CHUNK)]


def _p7_clamp(value: Any, low: int, high: int, default: int) -> int:
    return max(low, min(high, int(default if value is None else value)))


# -- Settings -----------------------------------------------------------------


def set_proactive_enabled(user_id: str, enabled: bool) -> dict:
    """assistant_settings.proactive_enabled for the company row (id = 1);
    `user_id` is the acting admin (updated_by). Returns {proactive_enabled,
    updated_at}."""
    rows = _rows(
        _sb().table(SETTINGS_TABLE)
        .update({"proactive_enabled": bool(enabled), "updated_by": user_id, "updated_at": _now_iso()})
        .eq("id", 1).execute()
    )
    if not rows:
        raise RuntimeError("the assistant settings row is missing")
    return {"proactive_enabled": bool(rows[0].get("proactive_enabled")), "updated_at": rows[0].get("updated_at")}


# -- Events (the 0170 outbox, claim and status columns of 0190 A2) ------------


def insert_events(user_id: str | None, rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Insert outbox events, ignoring duplicates on dedupe_key (a full unique
    constraint). Returns the rows this call inserted; a duplicate returns
    nothing. A slot holder inserts its events already claimed (status
    'claimed', claimed_by, claimed_at, attempts 1; 5.2). Unknown keys are
    dropped. System sweep: the emitters (tails and sweeper) pass None."""
    _p7_system_only(user_id, "inserting events")
    out: list[dict] = []
    batch: list[dict] = []
    for row in rows or ():
        if not row.get("event_type") or not row.get("dedupe_key"):
            raise ValueError("an event needs event_type and dedupe_key")
        # One key set per row: PostgREST sends the union of the keys as the
        # column list and writes NULL into a row that lacks one (no
        # missing=default), which would trip the NOT NULL columns.
        item = {k: row.get(k) for k in _EVENT_INSERT_COLUMNS}
        if item["payload"] is None:
            item["payload"] = {}
        if item["occurred_at"] is None:
            item["occurred_at"] = _p7_iso(None)
        if item["status"] is None:
            item["status"] = "pending"
        if item["attempts"] is None:
            item["attempts"] = 0
        for key in ("occurred_at", "claimed_at", "next_attempt_at"):
            if item.get(key) is not None:
                item[key] = _p7_iso(item[key])
        if item.get("payload") is not None:
            item["payload"] = dict(item["payload"])
        batch.append(item)
    for start in range(0, len(batch), 200):
        out.extend(_rows(
            _sb().table(EVENTS_TABLE)
            .upsert(batch[start:start + 200], on_conflict=EVENTS_CONFLICT, ignore_duplicates=True).execute()
        ))
    return out


def events_by_dedupe_keys(user_id: str | None, dedupe_keys: Sequence[str]) -> list[dict]:
    """Events by dedupe_key (a slot holder looks its slot's events up, 5.2
    step 2, and checks they all settled). System sweep."""
    _p7_system_only(user_id, "reading events by key")
    out: list[dict] = []
    for chunk in _p7_chunks(_ids(dedupe_keys)):
        out.extend(_rows(_sb().table(EVENTS_TABLE).select(EVENT_COLUMNS).in_("dedupe_key", chunk).execute()))
    return out


def events_claim(
    user_id: str | None, *, worker: str, limit: int, lease_seconds: int = 600, now: Any = None,
    ids: Sequence[Any] | None = None,
) -> list[dict]:
    """RPC assistant_events_claim (0190 A11.1): retire poison events as
    failed lease_exhausted, then claim pending or lease-expired events
    (skip locked; attempts rise at claim). `ids` takes exactly a slot's
    events (the p_ids claim); None is the general claim. The claim spans
    every user: system sweep."""
    _p7_system_only(user_id, "the event claim")
    wanted = None if ids is None else _uuids(ids)
    if wanted is not None and not wanted:
        return []
    return _rows(_sb().rpc(EVENTS_CLAIM_RPC, {
        "p_worker": str(worker), "p_limit": int(limit), "p_lease_seconds": int(lease_seconds),
        "p_now": _p7_iso(now), "p_ids": wanted,
    }).execute())


def event_finish(
    user_id: str | None, event_id: str, *, worker: str, status: str, routed: Mapping[str, Any] | None = None,
    error: str | None = None, retry_seconds: int = 60, now: Any = None,
) -> bool:
    """RPC assistant_event_finish (0190 A11.2), fenced on the claim:
    processed, skipped, failed, or retry (re-queued with a backoff, dead
    letter at five attempts). `routed` is counts only; `error` is an error
    kind, never a message. True when this worker's claim was settled.
    System sweep."""
    _p7_system_only(user_id, "finishing an event")
    if status not in EVENT_FINISH_STATUSES:
        raise ValueError(f"invalid event status: {status}")
    return bool(_scalar(_sb().rpc(EVENT_FINISH_RPC, {
        "p_event_id": str(event_id), "p_worker": str(worker), "p_status": status,
        "p_routed": None if routed is None else dict(routed), "p_error": error,
        "p_retry_seconds": int(retry_seconds), "p_now": _p7_iso(now),
    }).execute()))


# -- Inbox rows ---------------------------------------------------------------


def insert_inbox_row(user_id: str, row: Mapping[str, Any], *, now: Any = None) -> dict | None:
    """Insert one router-owned inbox row for `user_id`, ignoring a duplicate
    (user_id, dedupe_key; a full unique constraint). Returns the inserted
    row, or None when it already existed. The key starts with `p7:` and the
    delivery is immediate, digest or record (direct rows belong to the 0184
    and 0187 functions); immediate and digest rows carry deliver_after.
    created_at defaults to `now`."""
    key = str(row.get("dedupe_key") or "")
    if not key.startswith("p7:"):
        raise ValueError("router inbox keys start with p7:")
    delivery = row.get("delivery")
    if delivery not in ROUTER_INBOX_DELIVERIES:
        raise ValueError(f"router inbox rows are {', '.join(ROUTER_INBOX_DELIVERIES)}")
    if delivery != "record" and row.get("deliver_after") is None:
        raise ValueError("immediate and digest rows need deliver_after")
    if not row.get("title"):
        raise ValueError("an inbox row needs a title")
    item = {k: row[k] for k in _INBOX_INSERT_COLUMNS if k in row}
    for key_name in ("delivered_at", "deliver_after", "created_at"):
        if item.get(key_name) is not None:
            item[key_name] = _p7_iso(item[key_name])
    item.setdefault("created_at", _p7_iso(now))
    if delivery == "record":  # delivered by definition (6.2 step 8)
        item.setdefault("delivered_at", _p7_iso(now))
    item["user_id"] = user_id
    item["payload"] = dict(item.get("payload") or {})
    rows = _rows(_sb().table(INBOX_TABLE).upsert(item, on_conflict=INBOX_CONFLICT, ignore_duplicates=True).execute())
    return rows[0] if rows else None


def inbox_row(user_id: str, inbox_id: str) -> dict | None:
    rows = _rows(_sb().table(INBOX_TABLE).select(INBOX_COLUMNS).eq("user_id", user_id).eq("id", inbox_id)
                 .limit(1).execute())
    return rows[0] if rows else None


def inbox_row_by_key(user_id: str, dedupe_key: str) -> dict | None:
    rows = _rows(_sb().table(INBOX_TABLE).select(INBOX_COLUMNS).eq("user_id", user_id)
                 .eq("dedupe_key", dedupe_key).limit(1).execute())
    return rows[0] if rows else None


def inbox_due_immediate(
    user_id: str | None, *, now: Any = None, dedupe_key: str | None = None, limit: int = 100,
) -> list[dict]:
    """Immediate rows ready to ring: delivery immediate, not delivered, not
    dismissed, deliver_after <= now, oldest first (6.2 step 10, 8.4). A
    person's id (with dedupe_key) is the router's re-select after an
    insert; None is the per-tick release step across every user, a
    system sweep that also picks up rows a crashed worker left behind."""
    query = (
        _scope(_sb().table(INBOX_TABLE).select(INBOX_COLUMNS), user_id)
        .eq("delivery", "immediate").is_("delivered_at", "null").is_("dismissed_at", "null")
        .lte("deliver_after", _p7_iso(now))
    )
    if dedupe_key is not None:
        query = query.eq("dedupe_key", dedupe_key)
    return _rows(query.order("deliver_after").order("id").limit(_p7_clamp(limit, 1, 500, 100)).execute())


def inbox_folded_pending(user_id: str | None, *, limit: int = 100) -> list[dict]:
    """Rows A11.14 folded into the AM digest and that have not gone out
    (digest, precedence quiet_hours, undelivered, undismissed): the release
    step passes each back to inbox_fold to un-fold. None is the system sweep."""
    return _rows(
        _scope(_sb().table(INBOX_TABLE).select(INBOX_COLUMNS), user_id)
        .eq("delivery", "digest").eq("precedence", "quiet_hours")
        .is_("delivered_at", "null").is_("dismissed_at", "null")
        .order("deliver_after").order("id").limit(_p7_clamp(limit, 1, 500, 100)).execute()
    )


def inbox_pending_digest_users(user_id: str | None, *, slot_at: Any) -> list[str]:
    """Distinct people with a digest row due at `slot_at` (digest, not
    delivered, not dismissed, deliver_after <= slot_at), the people a slot
    delivers to. None is the system sweep."""
    seen: dict[str, None] = {}
    for page in range(_MAX_PAGES):
        rows = _rows(
            _scope(_sb().table(INBOX_TABLE).select("user_id"), user_id)
            .eq("delivery", "digest").is_("delivered_at", "null").is_("dismissed_at", "null")
            .lte("deliver_after", _p7_iso(slot_at)).order("user_id").order("id")
            .range(page * _PAGE, (page + 1) * _PAGE - 1).execute()
        )
        for row in rows:
            seen.setdefault(str(row["user_id"]), None)
        if len(rows) < _PAGE:
            break
    return list(seen)


def inbox_pending_digest(user_id: str, *, slot_at: Any) -> list[dict]:
    """One person's digest rows due at `slot_at`, oldest first (the
    delivery-time recheck, 6.4, runs over these before the digest call)."""
    return _rows(
        _sb().table(INBOX_TABLE).select(INBOX_COLUMNS).eq("user_id", user_id).eq("delivery", "digest")
        .is_("delivered_at", "null").is_("dismissed_at", "null").lte("deliver_after", _p7_iso(slot_at))
        .order("created_at").order("id").limit(_PAGE).execute()
    )


def inbox_digest_items(user_id: str, digest_id: str) -> list[dict]:
    """The rows attached to one digest (the Inbox digest view and the
    digest email), oldest first. No render limit (owner question 16)."""
    return _rows(
        _sb().table(INBOX_TABLE).select(INBOX_COLUMNS).eq("user_id", user_id).eq("digest_id", digest_id)
        .order("created_at").order("id").limit(_PAGE).execute()
    )


def inbox_list(
    user_id: str, *, segment: str = "all", before: tuple[str, str] | None = None, limit: int = 50,
) -> list[dict]:
    """One person's delivered, undismissed rows, newest first. `segment`:
    all, unread (router-owned rows with read_at null; the route also
    checks the router's own bell, 6.3) or digests (rows attached to a
    digest). `before` is the keyset (created_at, id) of the last row seen.
    A record row whose source bell was dismissed is hidden by the route
    (notification_states). Up to 201 rows."""
    query = (
        _sb().table(INBOX_TABLE).select(INBOX_COLUMNS).eq("user_id", user_id)
        .is_("dismissed_at", "null").not_.is_("delivered_at", "null")
    )
    if segment == "unread":
        query = query.in_("delivery", ["immediate", "digest"]).is_("read_at", "null")
    elif segment == "digests":
        query = query.eq("delivery", "digest").not_.is_("digest_id", "null")
    elif segment != "all":
        raise ValueError(f"unknown inbox segment: {segment}")
    if before:
        stamp = _p7_iso(before[0])
        identifier = str(uuid.UUID(str(before[1])))
        query = query.or_(f"created_at.lt.{stamp},and(created_at.eq.{stamp},id.lt.{identifier})")
    return _rows(query.order("created_at", desc=True).order("id", desc=True)
                 .limit(_p7_clamp(limit, 1, 201, 50)).execute())


def inbox_unread_rows(user_id: str, *, limit: int = 500) -> list[dict]:
    """The badge candidates: router-owned (immediate or digest), delivered,
    undismissed rows with read_at null. The route subtracts the ones whose
    own bell was read (id, severity, event_type, notification_id)."""
    return _rows(
        _sb().table(INBOX_TABLE).select("id, severity, event_type, notification_id, digest_id")
        .eq("user_id", user_id).in_("delivery", ["immediate", "digest"]).is_("read_at", "null")
        .not_.is_("delivered_at", "null").is_("dismissed_at", "null")
        .order("created_at", desc=True).limit(_p7_clamp(limit, 1, 1000, 500)).execute()
    )


def notification_states(user_id: str, notification_ids: Sequence[Any]) -> dict[str, dict]:
    """read_at and dismissed_at of this person's bells by id: the router's
    own bell (notification_id) and a record row's source bell
    (source_notification_id). A bell that is not theirs is not returned."""
    out: dict[str, dict] = {}
    for chunk in _p7_chunks(_uuids(notification_ids)):
        for row in _rows(_sb().table(BELLS_TABLE).select("id, read_at, dismissed_at")
                         .eq("user_id", user_id).in_("id", chunk).execute()):
            out[str(row["id"])] = {"read_at": row.get("read_at"), "dismissed_at": row.get("dismissed_at")}
    return out


def inbox_supersede_due_not_ready(
    user_id: str, project_id: str, *, keep_dedupe_key: str, now: Any = None,
) -> int:
    """Dismiss with reason superseded this person's older undelivered or
    snoozed bid.due_not_ready rows for the project, so a rolled or snoozed
    line never sits beside the fresh one (6.2 step 9). Returns the count."""
    return len(_rows(
        _sb().table(INBOX_TABLE)
        .update({"dismissed_at": _p7_iso(now), "dismissed_reason": "superseded"})
        .eq("user_id", user_id).eq("event_type", "bid.due_not_ready").eq("project_id", project_id)
        .in_("delivery", ["immediate", "digest"]).neq("dedupe_key", keep_dedupe_key)
        .is_("delivered_at", "null").is_("dismissed_at", "null").execute()
    ))


def inbox_deliver(
    user_id: str, inbox_id: str, *, bell_message: str | None, bell_metadata: Mapping[str, Any] | None = None,
    teams: bool = False, teams_not_before: Any = None, now: Any = None,
) -> dict:
    """RPC assistant_inbox_deliver (0190 A11.3): the row, its one bell and
    (teams) its one Teams ledger row in one transaction under the per-user
    lock. {delivered: False} when the row is not due or was delivered or
    dismissed (a lost fence is normal under two workers); otherwise
    {delivered, notification_id, teams_send_id}. Sends nothing itself."""
    return _rpc_data(_sb().rpc(INBOX_DELIVER_RPC, {
        "p_user_id": user_id, "p_inbox_id": str(inbox_id), "p_bell_message": bell_message,
        "p_bell_metadata": None if bell_metadata is None else dict(bell_metadata),
        "p_teams": bool(teams), "p_teams_not_before": _p7_iso_or_none(teams_not_before),
        "p_now": _p7_iso(now),
    }).execute())


def digest_deliver(
    user_id: str, *, digest_date: Any, slot: str, slot_at: Any, worker: str, email: bool = False,
    now: Any = None,
) -> dict:
    """RPC assistant_digest_deliver (0190 A11.4) for one person and slot:
    ledger claim, attach every due row, one bell and (email) one email
    ledger row. {claimed: False} for an empty slot or a lost claim."""
    return _rpc_data(_sb().rpc(DIGEST_DELIVER_RPC, {
        "p_user_id": user_id, "p_date": _p7_day(digest_date), "p_slot": slot, "p_slot_at": _p7_iso(slot_at),
        "p_worker": str(worker), "p_email": bool(email), "p_now": _p7_iso(now),
    }).execute())


def inbox_mark_read(user_id: str, ids: Sequence[Any] | None = None, *, now: Any = None) -> int:
    """RPC assistant_inbox_mark_read (0190 A11.5): router-owned delivered
    rows and their own bells; None is every such row. A record row's
    source bell is never written. Returns the rows marked."""
    return int(_scalar(_sb().rpc(INBOX_MARK_READ_RPC, {
        "p_user_id": user_id, "p_ids": None if ids is None else _uuids(ids), "p_now": _p7_iso(now),
    }).execute()) or 0)


def inbox_feedback(user_id: str, inbox_id: str, value: str, *, now: Any = None) -> dict:
    """RPC assistant_inbox_feedback (0190 A11.6, B2): useful, not_useful,
    mute or mute_project. {ok, feedback, event_type, auto_downgraded} or
    {ok: False, code: invalid_feedback | not_found | not_mutable}."""
    return _rpc_data(_sb().rpc(INBOX_FEEDBACK_RPC, {
        "p_user_id": user_id, "p_inbox_id": str(inbox_id), "p_value": value, "p_now": _p7_iso(now),
    }).execute())


def inbox_snooze(user_id: str, inbox_id: str, next_slot: Any, *, now: Any = None) -> dict:
    """RPC assistant_inbox_snooze (0190 A11.7): a delivered router-owned
    row returns, unread, at `next_slot` (at most three times). {ok, inbox_id,
    next_slot, snooze_count} or {ok: False, code: not_found | not_mutable |
    snooze_limit}; a slot not after now raises."""
    return _rpc_data(_sb().rpc(INBOX_SNOOZE_RPC, {
        "p_user_id": user_id, "p_inbox_id": str(inbox_id), "p_next_slot": _p7_iso(next_slot),
        "p_now": _p7_iso(now),
    }).execute())


def inbox_dismiss(user_id: str, ids: Sequence[Any], reason: str, *, now: Any = None) -> int:
    """RPC assistant_inbox_dismiss (0190 A11.9): dismiss this person's rows
    and mark the router's own linked bell read (a record row's source bell
    is left alone). Returns the rows dismissed; no ids is 0 with no call."""
    if reason not in DISMISS_REASONS:
        raise ValueError(f"invalid dismiss reason: {reason}")
    wanted = _uuids(ids)
    if not wanted:
        return 0
    return int(_scalar(_sb().rpc(INBOX_DISMISS_RPC, {
        "p_user_id": user_id, "p_ids": wanted, "p_reason": reason, "p_now": _p7_iso(now),
    }).execute()) or 0)


def inbox_link_bell(
    user_id: str, dedupe_key: str, *, notification_id: str, bell_at: Any, event_type: str, rank: int,
    title: str, why: str | None, payload: Mapping[str, Any] | None, project_id: str | None, now: Any = None,
) -> dict:
    """RPC assistant_inbox_link_bell (0190 A11.11, 0191): the email-ID
    collapse for record rows. One row per `p7:bell:` key; a later bell
    replaces or links by rank and time. A `severity` key in `payload` sets
    the row's severity (default normal) and a `precedence` key its
    precedence rule id (an R rule id, else null); neither is stored in the
    payload. {inbox_id, linked, source?, noop?, code?}."""
    return _rpc_data(_sb().rpc(LINK_BELL_RPC, {
        "p_user_id": user_id, "p_dedupe_key": dedupe_key, "p_notification_id": str(notification_id),
        "p_bell_at": _p7_iso(bell_at), "p_event_type": event_type, "p_rank": int(rank), "p_title": title,
        "p_why": why, "p_payload": None if payload is None else dict(payload), "p_project_id": project_id,
        "p_now": _p7_iso(now),
    }).execute())


def inbox_fold(
    user_id: str, inbox_id: str, *, am_date: Any = None, am_slot_at: Any = None, fold_until: Any = None,
    now: Any = None,
) -> str:
    """RPC assistant_inbox_fold (0190 A11.14, 0191), the quiet-hours fold
    and un-fold under the per-user lock: folded, deliver, wait or none. The
    un-fold restores the precedence the row had before the fold."""
    return str(_scalar(_sb().rpc(INBOX_FOLD_RPC, {
        "p_user_id": user_id, "p_inbox_id": str(inbox_id),
        "p_am_date": None if am_date is None else _p7_day(am_date),
        "p_am_slot_at": _p7_iso_or_none(am_slot_at), "p_fold_until": _p7_iso_or_none(fold_until),
        "p_now": _p7_iso(now),
    }).execute()) or "none")


# -- Subscriptions (person overrides, admin role defaults) --------------------


def user_subscriptions(user_id: str, *, event_type: str | None = None) -> list[dict]:
    """This person's own override rows (role null)."""
    query = _sb().table(SUBSCRIPTIONS_TABLE).select(SUBSCRIPTION_COLUMNS).eq("user_id", user_id).is_("role", "null")
    if event_type is not None:
        query = query.eq("event_type", event_type)
    return _rows(query.order("event_type").limit(_PAGE).execute())


def role_subscriptions(
    user_id: str | None, *, role: str | None = None, event_type: str | None = None,
) -> list[dict]:
    """Admin role defaults (user_id null), optionally for one role or kind.
    The rows belong to no person: the router reads them as a system sweep
    (user_id None); a request handler passes the caller's id, which only
    names who is asking (the route authorizes) and never filters the rows."""
    query = _sb().table(SUBSCRIPTIONS_TABLE).select(SUBSCRIPTION_COLUMNS).is_("user_id", "null")
    if role is not None:
        query = query.eq("role", role)
    if event_type is not None:
        query = query.eq("event_type", event_type)
    return _rows(query.order("role").order("event_type").limit(_PAGE).execute())


def subscription_set(
    user_id: str, event_type: str, *, enabled: bool, severity: str, source: str = "user",
    actor: str | None = None,
) -> dict:
    """RPC assistant_subscription_set (0190 A11.8) for a person's own
    override (source user, feedback_auto or admin). {ok, subscription} or
    {ok: False, code}. The RPC has no clock argument. A person's urgent is
    refused (invalid_mode)."""
    return _rpc_data(_sb().rpc(SUBSCRIPTION_SET_RPC, {
        "p_role": None, "p_user_id": user_id, "p_event_type": event_type, "p_enabled": bool(enabled),
        "p_severity": severity, "p_source": source, "p_actor": actor or user_id,
    }).execute())


def role_subscription_set(
    user_id: str | None, role: str, event_type: str, *, enabled: bool, severity: str, actor: str,
) -> dict:
    """RPC assistant_subscription_set for an admin role default (source
    admin; urgent allowed for the three router-owned kinds). The row
    belongs to no person: a system sweep when user_id is None; a request
    handler passes the acting admin's id (the route authorizes; `actor` is
    the audit name) and the id never reaches the row."""
    return _rpc_data(_sb().rpc(SUBSCRIPTION_SET_RPC, {
        "p_role": role, "p_user_id": None, "p_event_type": event_type, "p_enabled": bool(enabled),
        "p_severity": severity, "p_source": "admin", "p_actor": actor,
    }).execute())


def subscription_reset(user_id: str, event_type: str, *, actor: str | None = None) -> bool:
    """RPC assistant_subscription_reset (0190 B3): delete this person's
    override so the role default applies. True when a row was removed."""
    return bool(_scalar(_sb().rpc(SUBSCRIPTION_RESET_RPC, {
        "p_role": None, "p_user_id": user_id, "p_event_type": event_type, "p_actor": actor or user_id,
    }).execute()))


def role_subscription_reset(user_id: str | None, role: str, event_type: str, *, actor: str) -> bool:
    """RPC assistant_subscription_reset for an admin role default. This is a
    system sweep when user_id is None; a request handler passes the acting
    admin's id (the route authorizes; `actor` is the audit name)."""
    return bool(_scalar(_sb().rpc(SUBSCRIPTION_RESET_RPC, {
        "p_role": role, "p_user_id": None, "p_event_type": event_type, "p_actor": actor,
    }).execute()))


# -- Project mutes ------------------------------------------------------------


def project_mutes(user_id: str) -> list[dict]:
    return _rows(_sb().table(PROJECT_MUTES_TABLE).select("user_id, project_id, created_at")
                 .eq("user_id", user_id).order("created_at").limit(_PAGE).execute())


def project_mute_add(user_id: str, project_id: str, *, now: Any = None) -> bool:
    """Mute a project for this person (ignoring a duplicate on the primary
    key). True when this call inserted the row."""
    return bool(_rows(
        _sb().table(PROJECT_MUTES_TABLE)
        .upsert({"user_id": user_id, "project_id": str(project_id), "created_at": _p7_iso(now)},
                on_conflict=PROJECT_MUTES_CONFLICT, ignore_duplicates=True).execute()
    ))


def project_mute_remove(user_id: str, project_id: str) -> bool:
    """Unmute a project for this person. True when a row was removed."""
    return bool(_rows(_sb().table(PROJECT_MUTES_TABLE).delete()
                      .eq("user_id", user_id).eq("project_id", str(project_id)).execute()))


def project_muted_user_ids(user_id: str | None, project_id: str) -> list[str]:
    """The people who muted this project (the router drops their rows,
    6.2 step 5). None is the system sweep; a person's id answers only for
    that person."""
    return [str(r["user_id"]) for r in _rows(
        _scope(_sb().table(PROJECT_MUTES_TABLE).select("user_id"), user_id)
        .eq("project_id", str(project_id)).limit(_PAGE).execute()
    )]


# -- Digests ------------------------------------------------------------------


def digest(user_id: str, digest_id: str) -> dict | None:
    rows = _rows(_sb().table(DIGESTS_TABLE).select(DIGEST_COLUMNS).eq("user_id", user_id).eq("id", digest_id)
                 .limit(1).execute())
    return rows[0] if rows else None


def digest_for_slot(user_id: str, digest_date: Any, slot: str) -> dict | None:
    """The ledger row for this person, Pacific date and slot (am or pm)."""
    rows = _rows(_sb().table(DIGESTS_TABLE).select(DIGEST_COLUMNS).eq("user_id", user_id)
                 .eq("digest_date", _p7_day(digest_date)).eq("slot", slot).limit(1).execute())
    return rows[0] if rows else None


def digests_recent(user_id: str, *, limit: int = 20) -> list[dict]:
    """This person's digests, newest first (the summary's last_digest)."""
    return _rows(_sb().table(DIGESTS_TABLE).select(DIGEST_COLUMNS).eq("user_id", user_id)
                 .order("created_at", desc=True).order("id", desc=True)
                 .limit(_p7_clamp(limit, 1, 100, 20)).execute())


def digest_auto_downgrades(
    user_id: str, digest: Mapping[str, Any], *, retention_days: int | None = None,
) -> list[dict]:
    """The rows behind the 9.1 auto-downgrade lines one digest carries: this
    person's `feedback_auto` override rows written after the previous
    digest that rang its bell and up to this digest's created_at, oldest
    first. With no such digest (the first one, or the purge removed every
    earlier one) the window opens at created_at minus the digest retention
    days (None reads assistant_proactive_digest_retention_days): a purged
    digest was older than that, so a row it announced is not announced
    again. Two bounded reads (one ledger row, at most a page of override
    rows); the windows of two digests never overlap, so the text is the
    same whoever builds it."""
    created = digest.get("created_at")
    if not created:
        return []
    upto = _p7_iso(created)
    prev = _rows(_sb().table(DIGESTS_TABLE).select("created_at").eq("user_id", user_id)
                 .not_.is_("notification_id", "null").lt("created_at", upto)
                 .order("created_at", desc=True).order("id", desc=True).limit(1).execute())
    if prev and prev[0].get("created_at"):
        after = _p7_iso(prev[0]["created_at"])
    else:
        if retention_days is None:
            from app.core.config import get_settings

            retention_days = int(get_settings().assistant_proactive_digest_retention_days)
        after = (_p7_dt(created) - timedelta(days=max(1, int(retention_days)))).isoformat()
    return _rows(_sb().table(SUBSCRIPTIONS_TABLE).select(SUBSCRIPTION_COLUMNS).eq("user_id", user_id)
                 .is_("role", "null").eq("source", "feedback_auto").gt("created_at", after)
                 .lte("created_at", upto).order("created_at").order("event_type").limit(_PAGE).execute())


# -- Router cursors (the two tails) -------------------------------------------


def router_cursors(user_id: str | None) -> list[dict]:
    """Both tail cursors (notifications, audit_log). System sweep."""
    _p7_system_only(user_id, "reading the router cursors")
    return _rows(_sb().table(ROUTER_CURSORS_TABLE).select(CURSOR_COLUMNS).order("name").execute())


def advance_router_cursor(
    user_id: str | None, name: str, *, expect_at: Any, expect_id: str | None, new_at: Any,
    new_id: str | None = None, updated_by: str, now: Any = None,
) -> bool:
    """Move a cursor, fenced on its old value (cursor_at = expect_at and
    cursor_id is not distinct from expect_id; the values as the read
    returned them). False means the other worker advanced it first (the
    dedupe keys make the overlap harmless, 4.1). System sweep."""
    _p7_system_only(user_id, "advancing a router cursor")
    if name not in ROUTER_CURSOR_NAMES:
        raise ValueError(f"unknown router cursor: {name}")
    query = (
        _sb().table(ROUTER_CURSORS_TABLE)
        .update({"cursor_at": _p7_iso(new_at), "cursor_id": new_id, "updated_by": str(updated_by),
                 "updated_at": _p7_iso(now)})
        .eq("name", name).eq("cursor_at", expect_at if isinstance(expect_at, str) else _p7_iso(expect_at))
    )
    query = query.is_("cursor_id", "null") if expect_id is None else query.eq("cursor_id", str(expect_id))
    return bool(_rows(query.execute()))


def reseed_router_cursors(
    user_id: str | None, *, to_at: Any, updated_by: str, names: Sequence[str] | None = None, now: Any = None,
) -> int:
    """Unconditionally set the named cursors (both by default) to `to_at`
    with no tie-break id: the lag reseed at loop start and on the switch
    flip (4.1). Returns the cursors set. System sweep."""
    _p7_system_only(user_id, "reseeding the router cursors")
    chosen = list(names or ROUTER_CURSOR_NAMES)
    if any(n not in ROUTER_CURSOR_NAMES for n in chosen):
        raise ValueError("unknown router cursor")
    return len(_rows(
        _sb().table(ROUTER_CURSORS_TABLE)
        .update({"cursor_at": _p7_iso(to_at), "cursor_id": None, "updated_by": str(updated_by),
                 "updated_at": _p7_iso(now)})
        .in_("name", chosen).execute()
    ))


# -- Proactive slots (the daily step ledger with a lease) ---------------------


def proactive_slot(user_id: str | None, step: str, slot_date: Any) -> dict | None:
    """One slot row, or None. System sweep."""
    _p7_system_only(user_id, "reading a proactive slot")
    rows = _rows(_sb().table(PROACTIVE_SLOTS_TABLE).select(SLOT_COLUMNS).eq("step", step)
                 .eq("slot_date", _p7_day(slot_date)).limit(1).execute())
    return rows[0] if rows else None


def claim_proactive_slot(
    user_id: str | None, step: str, slot_date: Any, worker: str, now: Any = None, *,
    lease_seconds: int = SLOT_LEASE_SECONDS,
) -> dict | None:
    """Claim one step for one Pacific day (5.2). The row is inserted with
    claimed_at = now and result.attempts = 1; a duplicate key means another
    worker holds it. A row with finished_at null and claimed_at older than
    `lease_seconds` (15 minutes) is reclaimed by a fenced update on the old
    claimed_at, which bumps result.attempts. Returns the claimed row (the
    holder), or None when the slot is held, finished or lost. System sweep."""
    _p7_system_only(user_id, "a proactive slot claim")
    if step not in PROACTIVE_STEPS:
        raise ValueError(f"unknown proactive step: {step}")
    day, stamp = _p7_day(slot_date), _p7_iso(now)
    inserted = _rows(
        _sb().table(PROACTIVE_SLOTS_TABLE)
        .upsert({"step": step, "slot_date": day, "claimed_by": str(worker), "claimed_at": stamp,
                 "result": {"attempts": 1}}, on_conflict=SLOTS_CONFLICT, ignore_duplicates=True).execute()
    )
    if inserted:
        return inserted[0]
    held = proactive_slot(None, step, day)
    if held is None or held.get("finished_at") is not None:
        return None
    if _p7_dt(held["claimed_at"]) > _p7_dt(stamp) - timedelta(seconds=max(60, int(lease_seconds))):
        return None
    result = dict(held.get("result") or {})
    result["attempts"] = int(result.get("attempts") or 1) + 1
    taken = _rows(
        _sb().table(PROACTIVE_SLOTS_TABLE)
        .update({"claimed_by": str(worker), "claimed_at": stamp, "result": result})
        .eq("step", step).eq("slot_date", day).eq("claimed_at", held["claimed_at"])
        .is_("finished_at", "null").execute()
    )
    return taken[0] if taken else None


def record_proactive_slot(
    user_id: str | None, step: str, slot_date: Any, *, worker: str, result: Mapping[str, Any],
) -> bool:
    """Merge counts into an open slot's result (failures, error class),
    fenced on the holder. True when the holder's row was updated. A system sweep."""
    _p7_system_only(user_id, "recording a proactive slot")
    held = proactive_slot(None, step, slot_date)
    if held is None or held.get("finished_at") is not None or held.get("claimed_by") != str(worker):
        return False
    merged = {**dict(held.get("result") or {}), **dict(result)}
    return bool(_rows(
        _sb().table(PROACTIVE_SLOTS_TABLE).update({"result": merged})
        .eq("step", step).eq("slot_date", _p7_day(slot_date)).eq("claimed_by", str(worker))
        .is_("finished_at", "null").execute()
    ))


def finish_proactive_slot(
    user_id: str | None, step: str, slot_date: Any, *, worker: str, result: Mapping[str, Any] | None = None,
    now: Any = None,
) -> bool:
    """finished_at and the counts-only result on the holder's slot (once).
    The digest for the slot waits for this (5.2). System sweep."""
    _p7_system_only(user_id, "finishing a proactive slot")
    held = proactive_slot(None, step, slot_date)
    if held is None:
        return False
    merged = {**dict(held.get("result") or {}), **dict(result or {})}
    return bool(_rows(
        _sb().table(PROACTIVE_SLOTS_TABLE).update({"finished_at": _p7_iso(now), "result": merged})
        .eq("step", step).eq("slot_date", _p7_day(slot_date)).eq("claimed_by", str(worker))
        .is_("finished_at", "null").execute()
    ))


# -- External send ledger (8.5) -----------------------------------------------


def external_send_insert(user_id: str, row: Mapping[str, Any], *, now: Any = None) -> dict | None:
    """Insert one ledger row for this person, ignoring a duplicate
    (target_kind, target_id, channel; a full unique constraint). The
    router normally gets its rows from inbox_deliver and digest_deliver;
    this writes a row directly (for example one born skipped). Ids, enums
    and an error kind only. Returns the inserted row or None."""
    kind, channel = row.get("target_kind"), row.get("channel")
    if (kind, channel) not in (("inbox", "teams"), ("digest", "email")):
        raise ValueError("a send is an inbox row on teams or a digest on email")
    status = row.get("status") or "pending"
    if status not in ("pending", "skipped"):
        raise ValueError("a new send is pending or skipped")
    if row.get("error_kind") is not None and row["error_kind"] not in SEND_ERROR_KINDS:
        raise ValueError(f"invalid send error kind: {row['error_kind']}")
    stamp = _p7_iso(now)
    item = {
        "user_id": user_id, "target_kind": kind, "target_id": str(row["target_id"]), "channel": channel,
        "status": status, "not_before": _p7_iso(row.get("not_before") if row.get("not_before") else stamp),
        "attempts": 0, "error_kind": row.get("error_kind"), "created_at": stamp, "updated_at": stamp,
    }
    rows = _rows(_sb().table(EXTERNAL_SENDS_TABLE).upsert(item, on_conflict=SENDS_CONFLICT, ignore_duplicates=True)
                 .execute())
    return rows[0] if rows else None


def external_send(user_id: str, send_id: str) -> dict | None:
    rows = _rows(_sb().table(EXTERNAL_SENDS_TABLE).select(SEND_COLUMNS).eq("user_id", user_id).eq("id", send_id)
                 .limit(1).execute())
    return rows[0] if rows else None


def external_send_for_target(user_id: str, target_kind: str, target_id: str, channel: str) -> dict | None:
    rows = _rows(_sb().table(EXTERNAL_SENDS_TABLE).select(SEND_COLUMNS).eq("user_id", user_id)
                 .eq("target_kind", target_kind).eq("target_id", str(target_id)).eq("channel", channel)
                 .limit(1).execute())
    return rows[0] if rows else None


def external_sends_claim(
    user_id: str | None, *, worker: str, limit: int = 1, lease_seconds: int = 600, now: Any = None,
) -> list[dict]:
    """RPC assistant_external_sends_claim (0190 A11.12), one row per call in
    practice: a stale claim is retired as uncertain lease_expired and never
    handed out again; due pending rows are claimed (skip locked, attempts
    rise). The claim spans every user: system sweep."""
    _p7_system_only(user_id, "the send claim")
    return _rows(_sb().rpc(SENDS_CLAIM_RPC, {
        "p_worker": str(worker), "p_limit": int(limit), "p_lease_seconds": int(lease_seconds),
        "p_now": _p7_iso(now),
    }).execute())


def external_send_settle(
    user_id: str | None, send_id: str, *, worker: str, outcome: str, error_kind: str | None = None,
    email_log_id: str | None = None, retry_seconds: int = 60, now: Any = None,
) -> bool:
    """RPC assistant_external_send_settle (0190 A11.13), fenced on the
    claim: sent, transient (backoff, dead letter at five attempts),
    permanent, uncertain (terminal), skipped (with a channel_off,
    opted_out, stale or offboarded kind) or deferred (quiet hours: not_before
    moves by retry_seconds and the attempt is given back). The error kind is
    an enum, never an exception text. False when the fence was lost. A system sweep."""
    _p7_system_only(user_id, "settling a send")
    if outcome not in SEND_OUTCOMES:
        raise ValueError(f"invalid send outcome: {outcome}")
    if error_kind is not None and error_kind not in SEND_ERROR_KINDS:
        raise ValueError(f"invalid send error kind: {error_kind}")
    return bool(_scalar(_sb().rpc(SEND_SETTLE_RPC, {
        "p_id": str(send_id), "p_worker": str(worker), "p_outcome": outcome, "p_error_kind": error_kind,
        "p_email_log_id": None if email_log_id is None else str(email_log_id),
        "p_retry_seconds": int(retry_seconds), "p_now": _p7_iso(now),
    }).execute()))


# -- Offboarding (6.8), retention and reports ---------------------------------


def offboard_dismiss_undelivered(user_id: str, *, now: Any = None, limit: int = 500) -> int:
    """Dismiss with reason owner_offboarded this person's undelivered
    router rows (immediate and digest). Delivered history stays. Bounded;
    returns the rows dismissed."""
    ids = [r["id"] for r in _rows(
        _sb().table(INBOX_TABLE).select("id").eq("user_id", user_id).in_("delivery", ["immediate", "digest"])
        .is_("delivered_at", "null").is_("dismissed_at", "null").order("created_at")
        .limit(_p7_clamp(limit, 1, 1000, 500)).execute()
    )]
    return inbox_dismiss(user_id, ids, "owner_offboarded", now=now)


def offboard_skip_pending_sends(user_id: str, *, now: Any = None, limit: int = 500) -> int:
    """Set this person's pending ledger rows skipped with error kind
    offboarded. Claimed rows settle through their own fence. Bounded;
    returns the rows changed."""
    ids = [r["id"] for r in _rows(
        _sb().table(EXTERNAL_SENDS_TABLE).select("id").eq("user_id", user_id).eq("status", "pending")
        .order("created_at").limit(_p7_clamp(limit, 1, 1000, 500)).execute()
    )]
    if not ids:
        return 0
    return len(_rows(
        _sb().table(EXTERNAL_SENDS_TABLE)
        .update({"status": "skipped", "error_kind": "offboarded", "updated_at": _p7_iso(now)})
        .eq("user_id", user_id).eq("status", "pending").in_("id", ids).execute()
    ))


def offboard_delete_subscriptions(user_id: str, *, limit: int = 500) -> int:
    """Delete this person's own override rows (role null): a returning
    person starts from the role defaults. Bounded; returns the rows deleted."""
    ids = [r["id"] for r in _rows(
        _sb().table(SUBSCRIPTIONS_TABLE).select("id").eq("user_id", user_id).is_("role", "null")
        .limit(_p7_clamp(limit, 1, 1000, 500)).execute()
    )]
    if not ids:
        return 0
    return len(_rows(_sb().table(SUBSCRIPTIONS_TABLE).delete().eq("user_id", user_id).is_("role", "null")
                     .in_("id", ids).execute()))


def offboard_delete_project_mutes(user_id: str, *, limit: int = 500) -> int:
    """Delete this person's project mutes. Bounded; returns the rows deleted."""
    projects = [r["project_id"] for r in _rows(
        _sb().table(PROJECT_MUTES_TABLE).select("project_id").eq("user_id", user_id)
        .limit(_p7_clamp(limit, 1, 1000, 500)).execute()
    )]
    if not projects:
        return 0
    return len(_rows(_sb().table(PROJECT_MUTES_TABLE).delete().eq("user_id", user_id)
                     .in_("project_id", projects).execute()))


def proactive_purge(
    user_id: str | None, *, inbox_days: int, event_days: int, digest_days: int, send_days: int,
    limit: int = 200, now: Any = None,
) -> dict:
    """RPC assistant_proactive_purge (0190 B4): the retention purge,
    bounded by `limit` per table per call. Legal-hold users are skipped in
    every table; direct inbox rows, pending or claimed sends and the cursor
    rows are never touched. Returns the per-table counts. System sweep."""
    _p7_system_only(user_id, "the proactive purge")
    return _rpc_data(_sb().rpc(PROACTIVE_PURGE_RPC, {
        "p_now": _p7_iso(now), "p_inbox_days": int(inbox_days), "p_event_days": int(event_days),
        "p_digest_days": int(digest_days), "p_send_days": int(send_days), "p_limit": int(limit),
    }).execute())


def inbox_feedback_report(user_id: str | None, since: Any, *, floor: int = 3, now: Any = None) -> dict:
    """RPC assistant_inbox_feedback_report (0190 A11.10): counts only, per
    kind and role, with every cell under the floor (3 people) suppressed,
    plus the router health block (9.3). The one system read a request
    handler may make (the feedback-report route). System sweep."""
    _p7_system_only(user_id, "the feedback report")
    return _rpc_data(_sb().rpc(FEEDBACK_REPORT_RPC, {
        "p_since": _p7_iso(since), "p_floor": int(floor), "p_now": _p7_iso(now),
    }).execute())

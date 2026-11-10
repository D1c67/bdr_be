"""The llm_jobs job body for job type assistant_turn (docs/ASSISTANT.md,
Agent loop) and the _JobSpec entry the integration stage pastes into
services/llm_queue.py (JOB_SPEC_SNIPPET below, also in INTEGRATION_TODO.md).

A chat turn is one llm_jobs row: job_type assistant_turn, target_id = the
conversation id (the partial unique index llm_jobs_active_target_uq gives
one in-flight turn per conversation), payload {message_id, user_id,
conversation_id}, max_attempts 1. Assistant jobs are claimed in their own
pass with capacity 2 (modeled on _claim_plans). The body is a plain def:
the worker runs it through asyncio.to_thread, and the executor opens its own
event loop per tool call.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.assistant import loop, repo
from app.core.config import Settings
from app.services import llm_errors

logger = logging.getLogger(__name__)

JOB_TYPE = "assistant_turn"
FEATURE = "assistant"
CLAIM_CAPACITY = 2

MISSING_PAYLOAD = "The assistant job has no message to answer."


class BadJobPayload(ValueError):
    llm_error_kind = llm_errors.KIND_BAD_INPUT


def _lease_owner() -> str:
    """The claim generation that owns this attempt, from the queue's current
    job: worker token, job id and attempt. A unique value outside the queue
    (tests, the BackgroundTasks fallback)."""
    try:
        from app.services import llm_queue

        job = llm_queue.current_job.get()
    except Exception:  # noqa: BLE001 - the queue is optional to this module
        job = None
    if not job:
        return f"assistant:{uuid.uuid4()}"
    return f"{job.get('claimed_by') or 'worker'}:{job.get('id')}:{job.get('attempts') or 0}"[:200]


def _renew_lease() -> bool:
    try:
        from app.services import llm_queue

        return bool(llm_queue.renew_lease())
    except Exception:  # noqa: BLE001 - a transport failure is not a lost lease
        return True


def run_assistant_turn(payload: dict) -> None:
    """The job body: answers the pending assistant message in the payload.
    A browser follow-up (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.5, enqueued by
    assistant_browser_chat) carries origin browser_result; the loop then
    runs the text-only follow-up mode."""
    message_id = str((payload or {}).get("message_id") or "").strip()
    user_id = str((payload or {}).get("user_id") or "").strip()
    if not message_id or not user_id:
        raise BadJobPayload(MISSING_PAYLOAD)
    origin = str((payload or {}).get("origin") or "").strip()
    extra = {"origin": origin} if origin == loop.FOLLOW_UP_ORIGIN else {}
    loop.run_turn(message_id, user_id=user_id, lease_owner=_lease_owner(), renew_lease=_renew_lease, **extra)


def _owner_for(target: str) -> str | None:
    """The job owner of an assistant_turn target (a conversation id)."""
    return owner_for(JOB_TYPE, target)


def owner_for(job_type: str, target: str) -> str | None:
    """The job owner's user_id for a target of `job_type` (Permissions item
    14): the current job's payload when the queue is running this job (the
    failure handler inside _execute), else the newest job of that type for
    the target (the lease sweep and the retry path carry no current job).
    None when no job names an owner."""
    target = str(target)
    try:
        from app.services import llm_queue
    except Exception:  # noqa: BLE001 - the queue is optional to this module
        return None
    job = None
    try:
        job = llm_queue.current_job.get()
    except Exception:  # noqa: BLE001
        job = None
    if not job or str(job.get("target_id") or "") != target or (
        job.get("job_type") and job.get("job_type") != job_type
    ):
        try:
            job = llm_queue.latest_job(job_type, target)
        except Exception:  # noqa: BLE001 - a transport failure means no owner, never a wider filter
            logger.exception("assistant: could not read the %s job for target %s", job_type, target)
            job = None
    payload = (job or {}).get("payload") if isinstance(job, dict) else None
    owner = str((payload or {}).get("user_id") or "").strip() if isinstance(payload, dict) else ""
    return owner or None


def mark_turn(target: str, fields: dict) -> None:
    """The queue's domain mark: target is the conversation id. Without a
    job owner the mark is skipped (the row is never widened to every user);
    the loop itself has already written the terminal row in every in-turn
    failure, so the skip only loses the queue's own interrupted mark."""
    owner = _owner_for(target)
    if owner is None:
        logger.warning("assistant: no job owner for conversation %s; domain mark skipped", target)
        return
    repo.queue_mark_turn(owner, str(target), fields)


def turn_status(target: str) -> str | None:
    owner = _owner_for(target)
    if owner is None:
        return None
    return repo.queue_turn_status(owner, str(target))


def model_label(s: Settings) -> str:
    return loop.model_for(None, s)


def error_message(exc: Exception, model: str) -> str:
    if isinstance(exc, (loop.PermanentTurnFailure, BadJobPayload)):
        return str(exc)[:500]
    return llm_errors.user_message(exc, model)


def spec_kwargs() -> dict[str, Any]:
    """What llm_queue._JobSpec needs, for the integrator and the tests."""
    return {
        "feature": FEATURE,
        "run": run_assistant_turn,
        "mark": mark_turn,
        "current_status": turn_status,
        "model_label": model_label,
        "error_message": error_message,
    }


JOB_SPEC_SNIPPET = '''
    if job_type == JOB_ASSISTANT_TURN:
        # One job per pending assistant message (target = conversation id,
        # docs/ASSISTANT.md Agent loop). The body, marks and labels live in
        # app/assistant/jobs.py; this module never imports app.assistant at
        # load time.
        from app.assistant import jobs as assistant_jobs

        return _JobSpec(**assistant_jobs.spec_kwargs())
'''

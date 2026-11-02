"""Finalize for the assistant's AI Estimating tasks
(docs/ASSISTANT_PHASE4_PLAN.md 3.5.4, 3.6.4, 3.6.5, 3.24).

Two task kinds are finished here: `plans_read_pages` (a paid page read
answered by the AI Estimating chat) and `ai_estimating_run` (a run the
assistant started). Neither runs a model call of its own: the AE pipeline
does the work, and this module reads its state, posts one server-built
message, closes the spend hold at the AE ledger's actual cost and finishes
the task.

AE state is read only through `app.services.assistant_ai_estimate`
(`chat_message_state`, `run_state`), passing `sb=None` to both (this module
never names the client); assistant rows go only through `app.assistant.repo`;
holds close only through `spend.settle_external` with the AE actual cost
(a canceled, failed or timed-out job still spent its actual, which the
0180 spend state counts either way).

Callers:
  * `GET /assistant/tasks/{id}` (the lazy finalize) and the maintenance
    rider `ae_task_sweep` call `advance`; both skip owners with no live
    delegation before they call, so no message or bell reaches an offboarded
    person. `advance` itself does not, so the 3.24 offboarding hook can
    finalize a terminal run.
  * the offboarding hook and the stranded-route cleanup call `stop_task`.

Idempotency: every step is fenced. The result message has a deterministic id
(a second insert is a no-op), the hold close is fenced on `status='active'`
and the task update is fenced on the open statuses; only the caller whose
task update moved the row rings the bell. Plain `def` throughout (the
callers are sync).

Texts are fixed sentences and counts. The AE `error` text is never copied
into a message. The page-read answer is plan text and untrusted: it is
posted tainted and `server_data`, so the next turn replays it as a data
block (3.16.3), clipped, with control characters and dashes removed.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.assistant import repo, spend
from app.core.config import get_settings

logger = logging.getLogger(__name__)

KIND_PAGES = "plans_read_pages"
KIND_RUN = "ai_estimating_run"
AE_KINDS = (KIND_PAGES, KIND_RUN)
OPEN_STATUSES = ("pending", "waiting_external", "running")

# The AE run statuses that end a run (plans_reader_runs.TERMINAL_STATUSES).
TERMINAL_RUN_STATUSES = ("done", "done_with_errors", "failed", "canceled")

# The bell types phase 2 registered for task results (continue_job); the
# bell is metadata plus text, mirror_email False.
BELL_FINISHED = "assistant.task_finished"
BELL_FAILED = "assistant.task_failed"

# uuid5 namespace of the result message ids: one message per task, ever.
MESSAGE_NAMESPACE = uuid.UUID("c3a6d1e8-52f4-4b7a-9e0d-18b4f7a2c690")

# Failure reasons written to assistant_tasks.failure_reason.
FAILED_PAGE_READ = "page_read_failed"
FAILED_PAGE_MISSING = "page_read_missing"
FAILED_TIMED_OUT = "timed_out"
FAILED_RUN = "run_failed"
FAILED_RUN_MISSING = "run_missing"
CANCELED_RUN = "run_canceled"
FAILED_ROUTE = "route_interrupted"
FAILED_OWNER = "owner_changed"

ANSWER_MAX_CHARS = 3_600
CONTENT_MAX_CHARS = 3_900
NUMBER_MAX_CHARS = 40

TEXT_PAGES_HEADER = "Answer from the page read ({where}):"
TEXT_PAGES_NO_ANSWER = "The page read finished but returned no text."
TEXT_PAGES_FAILED = "The page read did not finish, so no answer was added. Ask again if you still need it."
TEXT_PAGES_MISSING = "The page read could not be found any more, so no answer was added."
TEXT_PAGES_TIMED_OUT = "The page read took too long and was stopped, so no answer was added."
TEXT_RUN_FINISHED = "AI Estimating finished for {number}: {outcome}, ${cost}. Open the run to review."
TEXT_RUN_MISSING = "The AI Estimating run for {number} no longer exists."
TEXT_ROUTE_INTERRUPTED = "The AI Estimating run for {number} did not finish starting and was stopped."
BELL_TEXT_PAGES_DONE = "Your page read finished. The answer is in your assistant chat."
BELL_TEXT_PAGES_FAILED = "Your page read did not finish."

RUN_OUTCOME_TEXT = {
    "done": "done",
    "done_with_errors": "done with errors",
    "failed": "failed",
    "canceled": "canceled",
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DASHES = re.compile(r"[ \t]*[" + chr(0x2013) + chr(0x2014) + r"][ \t]*")
_NUMBER_SAFE = re.compile(r"[^0-9A-Za-z._-]")
_TITLE_NUMBER = re.compile(r"\b(\d{2}\.\d{1,2}\.\d{3,5})\b")
_CENT = Decimal("0.01")


# ── Small helpers ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _dec(value: Any) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return out if out.is_finite() and out >= 0 else Decimal("0")


def _money(value: Any) -> str:
    return str(_dec(value).quantize(_CENT, rounding=ROUND_HALF_UP))


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_seconds(value: Any) -> float | None:
    ts = _parse_ts(value)
    return None if ts is None else (_now() - ts).total_seconds()


def message_id_for(task_id: str, kind: str = KIND_PAGES) -> str:
    """The result message's deterministic id (`uuid5("plans_pages:" + task
    id)` for a page read, `"ai_estimate:" + task id` for a run): a replay
    never posts twice."""
    prefix = "plans_pages:" if kind == KIND_PAGES else "ai_estimate:"
    return str(uuid.uuid5(MESSAGE_NAMESPACE, prefix + str(task_id)))


def project_number(task: dict) -> str:
    """The project number for the message: the payload's number, else the
    number in the server-built title ("AI Estimating run for 26.10.7212").
    Only number characters are kept (it is BDR data, never a tool result),
    else "the project"."""
    payload = _dict(task.get("payload"))
    candidates: list[Any] = [payload.get("project_number"), task.get("project_number")]
    found = _TITLE_NUMBER.search(str(task.get("title") or ""))
    if found:
        candidates.append(found.group(1))
    for raw in candidates:
        value = _NUMBER_SAFE.sub("", str(raw or "")).strip()[:NUMBER_MAX_CHARS]
        if value:
            return value
    return "the project"


def clean_answer(text: Any, limit: int = ANSWER_MAX_CHARS) -> str:
    """Plan-derived answer text for a message: control characters removed,
    dashes made commas, trailing space trimmed, clipped to `limit`."""
    value = _DASHES.sub(", ", _CONTROL.sub("", str(text or ""))).strip()
    return value[:limit].rstrip()


def _page_numbers(payload: dict) -> list[int]:
    out: list[int] = []
    for raw in payload.get("pages") or []:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n >= 1 and n not in out:
            out.append(n)
    return out


def _where(pages: list[int]) -> str:
    if not pages:
        return "pages"
    shown = ", ".join(str(p) for p in pages[:12])
    return f"page {shown}" if len(pages) == 1 else f"pages {shown}"


def _viewer_role(user_id: str) -> str:
    """The owner's live role (an unknown or missing role redacts)."""
    human = repo.human_profile(user_id)
    return str((human or {}).get("role") or "")


def _bell(user_id: str, task: dict, type_: str, text: str) -> None:
    """notify_user without an email mirror; best effort."""
    try:
        from app.services import notifications

        notifications.notify_user(
            user_id, task.get("project_id"), type_, text, mirror_email=False,
            metadata={"task_id": str(task.get("id")), "assistant_conversation_id": task.get("conversation_id")},
        )
    except Exception:  # noqa: BLE001 - a bell never fails the finalize
        logger.exception("assistant plans tasks: %s bell failed for %s", type_, task.get("id"))


def _post_message(user_id: str, task: dict, message_id: str, content: str, blocks: list[dict], *,
                  tainted: bool, server_data: bool) -> str | None:
    """Insert the result message once (deterministic id) and move the
    conversation to the top. None when the task's conversation is gone."""
    conversation_id = task.get("conversation_id")
    if not conversation_id:
        return None
    existing = repo.get_message(user_id, message_id)
    now = _now().isoformat()
    if existing is None:
        row = {
            "id": message_id,
            "conversation_id": str(conversation_id),
            "role": "assistant",
            "status": "done",
            "content": content[:CONTENT_MAX_CHARS],
            "blocks": blocks,
            "completed_at": now,
            "tainted": bool(tainted),
            "server_data": bool(server_data),
        }
        try:
            existing = repo.insert_message(user_id, row)
        except Exception:
            # A 23505 on the primary key: an earlier attempt posted it.
            existing = repo.get_message(user_id, message_id)
            if existing is None:
                raise
    try:
        repo.update_conversation(user_id, str(conversation_id), {
            "last_message_at": str((existing or {}).get("created_at") or now),
        })
    except Exception:  # noqa: BLE001 - the sort order is a convenience
        logger.warning("assistant plans tasks: conversation touch failed for %s", conversation_id)
    return message_id


def _is_open(user_id: str, task_id: str) -> dict | None:
    fresh = repo.get_task(user_id, task_id)
    return fresh if fresh is not None and fresh.get("status") in OPEN_STATUSES else None


# ── Stopping a task (failure, timeout, owner change) ─────────────────────────


def stop_task(user_id: str, task: dict, reason: str, *, actual_usd: Any, text: str | None = None,
              bell: bool = True, status: str = "failed") -> tuple[bool, bool]:
    """End an open AE task without an answer: post `text` (a fixed sentence,
    skipped when None), close the hold RELEASED with `actual_usd` (the AE
    cost read now; A8 counts it), then the task fenced on its open statuses.
    Returns (task changed, hold closed). A task that is no longer open
    changes nothing. `bell` False is the offboarding and owner-change path
    (no message and no bell reach a person who left). A repeat call is a
    no-op, so the sweeps and the hook may race."""
    task_id = str(task["id"])
    current = _is_open(user_id, task_id)
    if current is None:
        return False, False
    task = {**task, **current}
    kind = str(task.get("kind") or KIND_PAGES)
    actual = _dec(actual_usd)
    message_id = None
    if text:
        blocks = [{"type": "text", "text": text}, {"type": "task_result", "task_id": task_id}]
        message_id = _post_message(user_id, task, message_id_for(task_id, kind), text, blocks,
                                   tainted=False, server_data=False)
    closed = spend.settle_external(user_id, task.get("spend_reservation_id"), actual, release=True)
    result: dict[str, Any] = {"cost_usd": _money(actual), "outcome": reason}
    if kind == KIND_PAGES:
        result["pages"] = len(_page_numbers(_dict(task.get("payload"))))
    patch: dict[str, Any] = {
        "status": status,
        "failure_reason": reason,
        "finished_at": _now().isoformat(),
        "result": result,
    }
    if message_id:
        patch["result_message_id"] = message_id
    changed = repo.update_task(user_id, task_id, patch, expect_status=list(OPEN_STATUSES))
    if changed and bell:
        _bell(user_id, task, BELL_FAILED, BELL_TEXT_PAGES_FAILED if kind == KIND_PAGES else (text or ""))
    return changed, closed


# ── plans_read_pages (3.5.4) ─────────────────────────────────────────────────


def _timed_out(task: dict) -> bool:
    age = _age_seconds(task.get("created_at"))
    limit = int(get_settings().assistant_plans_page_read_timeout_minutes) * 60
    return age is not None and age >= limit


def cancel_queued_chat_job(message_id: str) -> bool:
    """Cancel the QUEUED plans_reader_chat job of a page read's answer
    (cancel only, never a claim). True when no job can still run and bill
    later: none is active, or the queued one was canceled now. False when
    the job is running (it finishes and is billed). Raises when the queue
    cannot be read, so a caller never closes a hold on a guess."""
    from app.services import llm_queue

    job = llm_queue.active_job(llm_queue.JOB_PLANS_READER_CHAT, message_id)
    if not job:
        return True
    if job.get("status") != "queued":
        return False
    return llm_queue.cancel(str(job["id"])) is not None


def _adopt_message(user_id: str, task: dict, payload: dict) -> str:
    """The answer id the execute route sent but never recorded (a crash or
    a failed update after the send), stored on the task; '' when none."""
    from app.services import assistant_ai_estimate

    try:
        message_id = assistant_ai_estimate.adopt_page_read_message(None, user_id, task) or ""
    except Exception:  # noqa: BLE001 - the next advance looks again
        logger.exception("assistant plans tasks: adopting the sent message of task %s failed", task.get("id"))
        return ""
    if message_id:
        repo.update_task(user_id, str(task["id"]), {"payload": {**payload, "plans_message_id": message_id}},
                         expect_status=list(OPEN_STATUSES))
    return message_id


def _advance_pages(user_id: str, task: dict) -> bool:
    from app.services import assistant_ai_estimate

    payload = _dict(task.get("payload"))
    message_id = str(payload.get("plans_message_id") or "").strip()
    if not message_id:
        # The execute route has not stored its chat message yet (or never
        # did): adopt one it sent, else wait, then time out like an
        # unanswered read.
        message_id = _adopt_message(user_id, task, payload)
        if message_id:
            payload = {**payload, "plans_message_id": message_id}
    if not message_id:
        if _timed_out(task):
            return stop_task(user_id, task, FAILED_TIMED_OUT, actual_usd="0", text=TEXT_PAGES_TIMED_OUT)[0]
        return False
    state = assistant_ai_estimate.chat_message_state(None, message_id, viewer_role=_viewer_role(user_id))
    status = state.get("status")
    cost = _dec(state.get("cost_usd"))
    if status == "done":
        return _finish_pages(user_id, task, payload, state, cost)
    if status == "failed":
        return stop_task(user_id, task, FAILED_PAGE_READ, actual_usd=cost, text=TEXT_PAGES_FAILED)[0]
    if status == "missing":
        return stop_task(user_id, task, FAILED_PAGE_MISSING, actual_usd=cost, text=TEXT_PAGES_MISSING)[0]
    if _timed_out(task):
        # A still-queued answer is canceled first, so no paid read runs after
        # its hold is closed; the cost is read again after the cancel. A job
        # already running finishes and is billed (closed with the cost now).
        try:
            cancel_queued_chat_job(message_id)
        except Exception:  # noqa: BLE001 - never close the hold while the job may still run
            logger.exception("assistant plans tasks: could not cancel the answer job of task %s", task.get("id"))
            return False
        state = assistant_ai_estimate.chat_message_state(None, message_id, viewer_role=_viewer_role(user_id))
        if state.get("status") == "done":
            return _finish_pages(user_id, task, payload, state, _dec(state.get("cost_usd")))
        cost = _dec(state.get("cost_usd"))
        return stop_task(user_id, task, FAILED_TIMED_OUT, actual_usd=cost, text=TEXT_PAGES_TIMED_OUT)[0]
    return False


def _finish_pages(user_id: str, task: dict, payload: dict, state: dict, cost: Decimal) -> bool:
    task_id = str(task["id"])
    pages = _page_numbers(payload)
    answer = clean_answer(state.get("content")) or TEXT_PAGES_NO_ANSWER
    content = TEXT_PAGES_HEADER.format(where=_where(pages)) + "\n\n" + answer
    blocks = [{"type": "text", "text": content}, {"type": "task_result", "task_id": task_id}]
    message_id = _post_message(user_id, task, message_id_for(task_id, KIND_PAGES), content, blocks,
                               tainted=True, server_data=True)
    spend.settle_external(user_id, task.get("spend_reservation_id"), cost)
    patch: dict[str, Any] = {
        "status": "done",
        "failure_reason": None,
        "finished_at": _now().isoformat(),
        "result": {"cost_usd": _money(cost), "pages": len(pages)},
    }
    if message_id:
        patch["result_message_id"] = message_id
    changed = repo.update_task(user_id, task_id, patch, expect_status=list(OPEN_STATUSES))
    if changed:
        _bell(user_id, task, BELL_FINISHED, BELL_TEXT_PAGES_DONE)
    return changed


# ── ai_estimating_run (3.6.4) ────────────────────────────────────────────────


def run_id_of(task: dict) -> str:
    """The AE run a task covers: the `run_id` column (0174), else the
    payload's, else ''."""
    return str(task.get("run_id") or _dict(task.get("payload")).get("run_id") or "").strip()


def _advance_run(user_id: str, task: dict) -> bool:
    from app.services import assistant_ai_estimate

    run_id = run_id_of(task)
    if not run_id:
        return False       # not created yet; the stranded-route sweep decides
    state = assistant_ai_estimate.run_state(None, run_id)
    status = state.get("status")
    cost = _dec(state.get("cost_usd"))
    number = project_number(task)
    if status == "missing":
        return stop_task(user_id, task, FAILED_RUN_MISSING, actual_usd=cost,
                         text=TEXT_RUN_MISSING.format(number=number))[0]
    if status not in TERMINAL_RUN_STATUSES:
        # Still running. After the 12 hour hold TTL the hold expires on its
        # own (counting stops, owner question 13); the task stays open and
        # the next finalize that sees the terminal status settles with the
        # actual (the hold is still 'active' until it is closed).
        return False
    task_id = str(task["id"])
    content = TEXT_RUN_FINISHED.format(
        number=number, outcome=RUN_OUTCOME_TEXT.get(str(status), str(status)), cost=_money(cost))
    blocks: list[dict] = [
        {"type": "text", "text": content},
        {"type": "task_result", "task_id": task_id},
        {"type": "citation", "label": "AI Estimating run", "project_id": None,
         "path": f"/ai-estimating?run={run_id}"},
    ]
    message_id = _post_message(user_id, task, message_id_for(task_id, KIND_RUN), content, blocks,
                               tainted=False, server_data=False)
    failed = status == "failed"
    canceled = status == "canceled"
    spend.settle_external(user_id, task.get("spend_reservation_id"), cost, release=failed or canceled)
    patch: dict[str, Any] = {
        "status": "failed" if failed else "canceled" if canceled else "done",
        "failure_reason": FAILED_RUN if failed else CANCELED_RUN if canceled else None,
        "finished_at": _now().isoformat(),
        "result": {"cost_usd": _money(cost), "status": status, "run_id": run_id},
    }
    if message_id:
        patch["result_message_id"] = message_id
    changed = repo.update_task(user_id, task_id, patch, expect_status=list(OPEN_STATUSES))
    if changed:
        _bell(user_id, task, BELL_FAILED if failed else BELL_FINISHED, content)
    return changed


# ── The entry point ──────────────────────────────────────────────────────────


def advance(user_id: str, task: dict) -> bool:
    """Move one `plans_read_pages` or `ai_estimating_run` task forward;
    True when the task changed.

    plans_read_pages (3.5.4): when the AE chat message is `done`, post a
    server-built message (id `uuid5("plans_pages:" + task_id)`,
    `server_data=true`, `tainted=True`, "Answer from the page read (...):"
    plus the answer clipped to 3,600 characters, blocks `[text, task_result
    {task_id}]`; the answer passes through the owner's live role first, so a
    pricing restricted answer reads redacted for a role that cannot see
    pricing), settle the hold to the message cost, mark the task `done` with
    `result {cost_usd, pages}`, bell. When `failed`: close the hold released
    with the actual cost, fail the task, post a fixed error line. Running
    longer than `ASSISTANT_PLANS_PAGE_READ_TIMEOUT_MINUTES`: fail
    `timed_out`, release with the actual cost read at that moment.

    ai_estimating_run (3.6.4): on a terminal run status (`done`,
    `done_with_errors`, `failed`, `canceled`) settle the hold to the run's
    `cost_usd` (released with that actual when canceled or failed), mark the
    task terminal, post "AI Estimating finished for <number>: <status>,
    $<cost>. Open the run to review." with a `task_result` block and a
    citation, bell. Not terminal: the task stays open, however old.

    The task is re-read first and a task that is not open does nothing. Every
    step is fenced on the task status and the active hold, so concurrent
    callers are safe. Callers (`ae_task_sweep`, the lazy finalize in
    `GET /assistant/tasks/{id}`) skip owners with no live delegation;
    `advance` itself does not, so the 3.24 offboarding hook can finalize a
    terminal run.
    """
    user_id = str(user_id)
    task_id = str((task or {}).get("id") or "")
    if not task_id:
        return False
    fresh = _is_open(user_id, task_id)
    if fresh is None:
        return False
    kind = fresh.get("kind")
    if kind == KIND_PAGES:
        return _advance_pages(user_id, fresh)
    if kind == KIND_RUN:
        return _advance_run(user_id, fresh)
    return False

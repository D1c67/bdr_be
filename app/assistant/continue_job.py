"""The llm_jobs job body for job type assistant_continue
(docs/ASSISTANT_PHASE2_PLAN.md 3.7).

One job per wake of a split task: target_id = the task id, payload
{task_id, user_id}, 3 attempts, priority 45. The bid_split hook enqueues it
after each file of an assistant job has filed its project rows, and GET
/assistant/tasks/{id} enqueues it again during lazy repair; the partial
unique index on active (job_type, target_id) collapses concurrent wakes.

No model call (a deviation from the design record, section 1 of the plan):
the job checks the owner, takes the task from waiting_external to running,
reads the per-file outcome through split_handoff.task_progress_for (the
service creates its own client: no module under app/assistant/ ever holds
one), and either hands the task back to waiting_external (the job is still
processing or a file is still landing) or finishes it: one server-built
assistant message with a split_result block, the hold settled, the task
done or failed, the bell rung, a bounded staging sweep.

The message content is built from counts and fixed sentences only. File
names and splitter segment names (cut from third-party PDF text) never
reach `content`, which later turns replay as history; they stay in the job
rows and reach the UI only through the split_result block, rendered escaped.

Idempotency: the CAS to running, the deterministic message id, the settle
fenced on an active hold and the final update fenced on running. The queue's
mark handler never fails a task: it hands a running task back to
waiting_external, so a transient error leaves it for the next wake or the
lazy repair. Plain `def` throughout: the worker runs it through
asyncio.to_thread.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.assistant import access, jobs, repo, spend
from app.core.config import Settings

logger = logging.getLogger(__name__)

JOB_TYPE = "assistant_continue"
FEATURE = "assistant_continue"
MODEL_LABEL = "assistant-continue"
MAX_ATTEMPTS = 3
PRIORITY = 45

# uuid5 namespace of the result message ids: one message per task, ever.
TASK_MESSAGE_NAMESPACE = uuid.UUID("5b1f3a52-8d0e-4c39-9a57-2f7c0e4d6a11")

TIMEOUT_SECONDS = 6 * 3600          # still processing this long after creation: timed_out
STALE_RUNNING_SECONDS = 10 * 60     # a running task this old is taken over (a crashed attempt)
STAGING_SWEEP_LIMIT = 50

OPEN_STATUSES = ("waiting_external", "running")
FINISHED_STATUSES = ("done", "failed", "canceled")

BELL_FINISHED = "assistant.task_finished"
BELL_FAILED = "assistant.task_failed"

# Task outcomes (assistant_tasks.failure_reason for a failed task).
OUTCOME_SPLIT = "split"
FAILED_WHOLE = "filed_whole"
FAILED_JOB = "job_failed"
FAILED_MISSING = "job_missing"
FAILED_TIMED_OUT = "timed_out"

# File outcomes (split_handoff.task_progress, 3.3.7).
FILE_SPLIT, FILE_WHOLE, FILE_FAILED, FILE_MISSING, FILE_PENDING = "split", "whole", "failed", "missing", "pending"

# ── Message templates (deterministic English, no file or segment names) ──────

# Counts are filled in as "1 file was" or "2 files were" (_count, _was), so
# the text never reads "1 documents" or "file(s)".
TEXT_ALL_SPLIT = (
    "The split for {number} is done: {files} {was} split into {docs} and filed by trade. "
    "Open the project to review them."
)
TEXT_SOME_WHOLE = (
    "The split for {number} finished, but not every file was split: {files} {was} split into "
    "{docs}, and {whole} {whole_was} filed whole, not split."
)
TEXT_NONE_SPLIT = "The files for {number} were filed whole, not split."
TEXT_NOTHING_FILED = "The split for {number} did not file any of the files."
TEXT_FAILURES = (
    "{files} could not be filed. Check them on the Bid Splitter page or upload them on the project."
)
TEXT_TIMED_OUT = "The split for {number} did not finish within 6 hours. Check the Bid Splitter page."
TEXT_JOB_MISSING = (
    "The splitter job for {number} no longer exists, so the split result cannot be reported. "
    "Check the Bid Splitter page."
)
# One sentence per distinct whole-file reason, in this order ("other" has none).
REASON_TEXT = {
    "package_sent": (
        "The hand-off package was sent while the split ran, so those files were added whole under "
        "Additional files and the estimators do not have them yet. Open Revisions on the To Estimator "
        "step to send them."
    ),
    "no_documents": "The splitter found no documents to cut in them.",
    "run_failed": "The splitter could not read them.",
    "refile_failed": "Their documents could not be filed by trade.",
}
# The bell sentence when the owner check stops a task (no message is posted).
OWNER_TEXT = {
    access.OWNER_CHANGED: "The split for {number} was stopped because your assistant access changed.",
    access.FEATURE_OFF: "The split for {number} was stopped because bidding is switched off.",
}

MISSING_PAYLOAD = "The continuation job has no task to check."
RETRY_MESSAGE = "The split result could not be checked yet. It is checked again automatically."

_NUMBER_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")
_TITLE_NUMBER = re.compile(r"\binto\s+(\S+)\s*$")


class BadJobPayload(jobs.BadJobPayload):
    pass


# ── Small helpers ────────────────────────────────────────────────────────────


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


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


def _age_seconds(value: Any, now: datetime) -> float | None:
    ts = _parse_ts(value)
    return None if ts is None else (now - ts).total_seconds()


def message_id_for(task_id: str) -> str:
    """The result message's deterministic id: a retry never posts twice."""
    return str(uuid.uuid5(TASK_MESSAGE_NAMESPACE, str(task_id)))


def project_number(task: dict, progress: dict | None = None) -> str:
    """The project number for the message: the progress task's project, the
    task's own project, else the number at the end of the server-built
    title ("Split <n> files into <number>"). Only number characters are
    kept (it is BDR data, never a tool result), else "the project"."""
    candidates: list[Any] = []
    for source in (_dict(_dict(progress).get("task")), task):
        candidates.append(_dict(source.get("project")).get("number"))
        candidates.append(source.get("project_number"))
    title = str(task.get("title") or "")
    found = _TITLE_NUMBER.search(title)
    if found:
        candidates.append(found.group(1))
    for raw in candidates:
        value = _NUMBER_SAFE.sub("", str(raw or "")).strip()[:40]
        if value:
            return value
    return "the project"


def _file_ids(task: dict, progress: dict | None) -> list[str]:
    """The task's bid_split_files ids: from the progress files, else from
    the task payload split-drop wrote (3.3.4 step 10)."""
    ids: list[str] = []
    for row in (_dict(progress).get("files") or []) + (_dict(task.get("payload")).get("files") or []):
        file_id = str(_dict(row).get("split_file_id") or "").strip()
        if file_id and file_id not in ids:
            ids.append(file_id)
    return ids


def cancel_queued_runs(file_ids: list[str]) -> int:
    """Cancel every queued bid_split llm_job of these files; returns how many
    are still running (a running model call is never canceled). Public for
    the offboarding service (phase 3, 3.10.2 step 4)."""
    from app.services import llm_queue

    running = 0
    for file_id in file_ids:
        job = llm_queue.active_job(llm_queue.JOB_BID_SPLIT, file_id)
        if not job:
            continue
        if job.get("status") == "queued":
            if llm_queue.cancel(str(job["id"])) is None:
                running += 1
        else:
            running += 1
    return running


# The phase 2 name, kept for existing callers.
_cancel_queued_runs = cancel_queued_runs


def _bell(user_id: str, task: dict, type_: str, text: str) -> None:
    """notify_user without an email mirror; best effort."""
    try:
        from app.services import notifications

        notifications.notify_user(
            user_id, task.get("project_id"), type_, text, mirror_email=False,
            metadata={"task_id": str(task.get("id")), "assistant_conversation_id": task.get("conversation_id")},
        )
    except Exception:  # noqa: BLE001 - a bell never fails the job
        logger.exception("assistant continue: %s bell failed for %s", type_, task.get("id"))


def _back_to_waiting(user_id: str, task_id: str) -> None:
    repo.update_task(user_id, task_id, {"status": "waiting_external"}, expect_status="running")


# ── Outcome and message ──────────────────────────────────────────────────────


def summarize(progress: dict) -> dict:
    """Counts over the progress files: {files, split, documents, whole,
    failed, missing, reasons (distinct whole reasons, in REASON_TEXT order
    then any other)}."""
    files = [_dict(f) for f in _dict(progress).get("files") or []]
    split = [f for f in files if f.get("outcome") == FILE_SPLIT]
    whole = [f for f in files if f.get("outcome") == FILE_WHOLE]
    reasons_seen = {str(f.get("reason") or "other") for f in whole}
    reasons = [r for r in REASON_TEXT if r in reasons_seen] + sorted(reasons_seen - set(REASON_TEXT))
    documents = 0
    for f in split:
        try:
            documents += max(0, int(f.get("documents") or 0))
        except (TypeError, ValueError):
            continue
    return {
        "files": len(files),
        "split": len(split),
        "documents": documents,
        "whole": len(whole),
        "failed": sum(1 for f in files if f.get("outcome") == FILE_FAILED),
        "missing": sum(1 for f in files if f.get("outcome") == FILE_MISSING),
        "reasons": reasons,
    }


def outcome_for(counts: dict) -> tuple[str, str | None]:
    """(task status, failure_reason) per 3.6.1: done when at least one file
    was split; failed filed_whole when none was split but one was filed
    whole; failed job_failed when nothing landed."""
    if counts["split"] > 0:
        return "done", None
    if counts["whole"] > 0:
        return "failed", FAILED_WHOLE
    return "failed", FAILED_JOB


def _count(n: int, word: str) -> str:
    """"1 file", "2 files"."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _was(n: int) -> str:
    return "was" if n == 1 else "were"


def _message_parts(number: str, counts: dict) -> list[str]:
    s, d, w = counts["split"], counts["documents"], counts["whole"]
    f = counts["failed"] + counts["missing"]
    if s > 0 and w == 0:
        parts = [TEXT_ALL_SPLIT.format(number=number, files=_count(s, "file"), was=_was(s),
                                       docs=_count(d, "document"))]
    elif s > 0:
        parts = [TEXT_SOME_WHOLE.format(number=number, files=_count(s, "file"), was=_was(s),
                                        docs=_count(d, "document"), whole=_count(w, "file"),
                                        whole_was=_was(w))]
    elif w > 0:
        parts = [TEXT_NONE_SPLIT.format(number=number)]
    else:
        parts = [TEXT_NOTHING_FILED.format(number=number)]
    if w > 0:
        parts.extend(REASON_TEXT[r] for r in counts["reasons"] if r in REASON_TEXT)
    if f > 0:
        parts.append(TEXT_FAILURES.format(files=_count(f, "file")))
    return parts


def build_message(number: str, counts: dict) -> str:
    """The result message (3.7 step 6): counts and fixed sentences only."""
    return " ".join(_message_parts(number, counts))


def headline(number: str, counts: dict) -> str:
    """The message's first sentence: the bell text."""
    return _message_parts(number, counts)[0]


def result_for(progress: dict, counts: dict, outcome: str) -> dict:
    """The task's result: the final progress without file names, plus the
    outcome and the counts (file names stay in the job rows)."""
    files = []
    for row in _dict(progress).get("files") or []:
        row = _dict(row)
        files.append({k: v for k, v in row.items() if k not in ("filename", "name", "segments")})
    job = _dict(_dict(progress).get("job"))
    return {
        "outcome": outcome,
        "counts": {k: v for k, v in counts.items() if k != "reasons"},
        "reasons": list(counts["reasons"]),
        "job": {k: job.get(k) for k in ("id", "status", "file_count", "files_done", "files_failed") if k in job},
        "files": files,
    }


# ── The job body ─────────────────────────────────────────────────────────────


def _post_message(user_id: str, task: dict, content: str) -> str | None:
    """Insert the result message once (deterministic id) and move the
    conversation to the top. None when the task's conversation is gone."""
    conversation_id = task.get("conversation_id")
    if not conversation_id:
        return None
    message_id = message_id_for(str(task["id"]))
    existing = repo.get_message(user_id, message_id)
    if existing is None:
        row = {
            "id": message_id,
            "conversation_id": str(conversation_id),
            "role": "assistant",
            "status": "done",
            "content": content,
            "blocks": [{"type": "text", "text": content}, {"type": "split_result", "task_id": str(task["id"])}],
            "completed_at": _now().isoformat(),
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
            "last_message_at": str((existing or {}).get("created_at") or _now().isoformat()),
        })
    except Exception:  # noqa: BLE001 - the sort order is a convenience
        logger.warning("assistant continue: conversation touch failed for %s", conversation_id)
    return message_id


def _finish(user_id: str, task: dict, *, status: str, failure_reason: str | None, result: dict,
            content: str | None, bell_text: str) -> bool:
    """Steps 7 to 10: message, settle, fenced final update, bell. False
    when another attempt finished the task first (no bell)."""
    message_id = _post_message(user_id, task, content) if content else None
    spend.settle(user_id, task.get("spend_reservation_id"))
    patch: dict[str, Any] = {
        "status": status,
        "result": result,
        "failure_reason": failure_reason,
        "finished_at": _now().isoformat(),
    }
    if message_id:
        patch["result_message_id"] = message_id
    if not repo.update_task(user_id, str(task["id"]), patch, expect_status="running"):
        return False
    _bell(user_id, task, BELL_FINISHED if status == "done" else BELL_FAILED, bell_text)
    return True


def stop_for_owner(user_id: str, task: dict, problem: str, *, bell: bool = True,
                   cancel: bool = True) -> None:
    """Step 2: the owner may no longer run assistant work. Cancel the queued
    runs while the task is still open (each canceled file's fallback then
    runs the scope's owner check and is not filed whole; bid_split also
    skips the fallback once the task failed for an owner reason), fail the
    task (fenced on its current open status), release the hold, ring the
    bell unless `bell` is False (offboarding stops tasks silently, phase 3
    3.10.2 step 3); no message. Runs canceled before a lost fence stay
    canceled: the owner is ineligible either way. `cancel` False skips the
    cancel (offboarding cancels first itself, so it sees a failure)."""
    if cancel:
        try:
            cancel_queued_runs(_file_ids(task, None))
        except Exception:  # noqa: BLE001 - logged; offboarding detects and retries its own cancel
            # A run left queued is NOT stopped by the run scope once the task
            # failed (bid_split._assistant_run returns None), so offboarding
            # cancels before calling here and fails its step on an error.
            logger.exception("assistant continue: queued runs of %s could not be canceled", task.get("id"))
    if not repo.update_task(user_id, str(task["id"]), {
        "status": "failed", "failure_reason": problem, "finished_at": _now().isoformat(),
        "result": {"outcome": problem},
    }, expect_status=list(OPEN_STATUSES)):
        return
    try:
        spend.release(user_id, task.get("spend_reservation_id"))
    except Exception:  # noqa: BLE001 - an unreleased hold expires by itself
        logger.exception("assistant continue: hold of %s could not be released", task.get("id"))
    if not bell:
        return
    number = project_number(task)
    _bell(user_id, task, BELL_FAILED, OWNER_TEXT.get(problem, OWNER_TEXT[access.OWNER_CHANGED]).format(number=number))


# The phase 2 name, kept for existing callers.
_stop_for_owner = stop_for_owner


def run_continue(payload: dict, *, now: datetime | None = None) -> None:
    """The job body (3.7 steps 1 to 11)."""
    task_id = str((payload or {}).get("task_id") or "").strip()
    user_id = str((payload or {}).get("user_id") or "").strip()
    if not task_id or not user_id:
        raise BadJobPayload(MISSING_PAYLOAD)
    if not _continue(task_id, user_id, _now(now)):
        return
    # 11. The bounded system sweep of staged objects (3.1.4), once the task
    #     finished here; best effort.
    try:
        from app.services import assistant_staging

        assistant_staging.purge_expired(user_id=None, limit=STAGING_SWEEP_LIMIT)
    except Exception:  # noqa: BLE001 - the sweep never fails the job
        logger.exception("assistant continue: staging sweep failed")


def _continue(task_id: str, user_id: str, now: datetime) -> bool:
    """Steps 1 to 10; True when this attempt finished the task."""
    # 1. Only an open task is continued (a re-settled job wakes nothing).
    task = repo.get_task(user_id, task_id)
    if task is None or task.get("status") not in OPEN_STATUSES:
        return False

    # 2. The owner must still be eligible.
    problem = access.owner_problem(user_id, sub_app="bidding")
    if problem is not None:
        stop_for_owner(user_id, task, problem)
        return False

    # 3. CAS to running: waiting_external, or a running task left by a
    #    crashed attempt.
    current = task.get("status")
    if current == "running":
        age = _age_seconds(task.get("updated_at"), now)
        if age is not None and age < STALE_RUNNING_SECONDS:
            return False
    if not repo.update_task(user_id, task_id, {"status": "running"}, expect_status=current):
        return False
    task = {**task, "status": "running"}

    # 4. Progress; hand the task back while anything can still land.
    number = project_number(task)
    if not task.get("split_job_id"):
        return _finish(user_id, task, status="failed", failure_reason=FAILED_MISSING,
                       result={"outcome": FAILED_MISSING}, content=TEXT_JOB_MISSING.format(number=number),
                       bell_text=TEXT_JOB_MISSING.format(number=number))
    from app.services import split_handoff

    progress = _dict(split_handoff.task_progress_for(task))
    number = project_number(task, progress)
    job = _dict(progress.get("job"))
    if not job.get("id"):
        return _finish(user_id, task, status="failed", failure_reason=FAILED_MISSING,
                       result={"outcome": FAILED_MISSING}, content=TEXT_JOB_MISSING.format(number=number),
                       bell_text=TEXT_JOB_MISSING.format(number=number))
    if job.get("status") == "processing":
        age = _age_seconds(task.get("created_at"), now)
        if age is None or age < TIMEOUT_SECONDS:
            _back_to_waiting(user_id, task_id)
            return False
        if cancel_queued_runs(_file_ids(task, progress)) > 0:
            _back_to_waiting(user_id, task_id)   # the running file's hook wakes it again
            return False
        counts = summarize(progress)
        text = TEXT_TIMED_OUT.format(number=number)
        return _finish(user_id, task, status="failed", failure_reason=FAILED_TIMED_OUT,
                       result=result_for(progress, counts, FAILED_TIMED_OUT), content=text, bell_text=text)
    if not progress.get("ready"):
        _back_to_waiting(user_id, task_id)
        # A sibling file's wake that arrived while this job was running was
        # absorbed by it (the queue's one-active-job index): read once more
        # after handing the task back, and finish here when it is now ready.
        progress = _dict(split_handoff.task_progress_for({**task, "status": "waiting_external"}))
        if (not progress.get("ready") or _dict(progress.get("job")).get("status") == "processing"
                or not _dict(progress.get("job")).get("id")):
            return False
        if not repo.update_task(user_id, task_id, {"status": "running"}, expect_status="waiting_external"):
            return False
        number = project_number(task, progress)

    # 5 to 10. Every file landed: the outcome, the message, settle, finish.
    counts = summarize(progress)
    status, failure_reason = outcome_for(counts)
    content = build_message(number, counts)
    return _finish(user_id, task, status=status, failure_reason=failure_reason,
                   result=result_for(progress, counts, failure_reason or OUTCOME_SPLIT),
                   content=content, bell_text=headline(number, counts))


# ── Queue adapters ───────────────────────────────────────────────────────────


def mark_task(target: str, fields: dict) -> None:
    """The queue's domain mark (a retry and a terminal failure). Never fails
    a task, whatever `fields` says: a running task goes back to
    waiting_external, so the next wake or the lazy repair picks it up."""
    owner = jobs.owner_for(JOB_TYPE, str(target))
    if owner is None:
        logger.warning("assistant continue: no job owner for task %s; mark skipped", target)
        return
    try:
        _back_to_waiting(owner, str(target))
    except Exception:  # noqa: BLE001 - the lazy repair re-enqueues a stale running task
        logger.exception("assistant continue: could not hand task %s back", target)


def task_status(target: str) -> str | None:
    """"done" for a finished task (the AI monitor's Retry never reruns it),
    else the task's status; None when there is no task or no owner."""
    owner = jobs.owner_for(JOB_TYPE, str(target))
    if owner is None:
        return None
    task = repo.get_task(owner, str(target))
    if task is None:
        return None
    status = str(task.get("status") or "")
    return "done" if status in FINISHED_STATUSES else status


def model_label(s: Settings) -> str:
    return MODEL_LABEL


def error_message(exc: Exception, label: str) -> str:
    if isinstance(exc, jobs.BadJobPayload):
        return str(exc)[:500]
    return RETRY_MESSAGE


def spec_kwargs() -> dict[str, Any]:
    """What llm_queue._JobSpec needs, for the registration and the tests."""
    return {
        "feature": FEATURE,
        "run": run_continue,
        "mark": mark_task,
        "current_status": task_status,
        "model_label": model_label,
        "error_message": error_message,
    }


__all__ = [
    "BELL_FAILED", "BELL_FINISHED", "FEATURE", "JOB_TYPE", "MAX_ATTEMPTS", "MODEL_LABEL", "PRIORITY",
    "TASK_MESSAGE_NAMESPACE", "build_message", "headline", "mark_task", "message_id_for",
    "outcome_for", "project_number", "result_for", "run_continue", "spec_kwargs", "summarize",
    "task_status",
]

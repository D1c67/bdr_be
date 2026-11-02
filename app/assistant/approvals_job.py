"""The llm_jobs job body for job type assistant_approval_execute
(docs/ASSISTANT_PHASE1_PLAN.md 3.8 and 3.13).

One job per approved card: target_id = the approval id, payload
{approval_id, user_id}, max_attempts 1, priority 40 (a click claims before
a turn). No model call. The body re-checks the owner, moves the card from
approved to executing, re-hashes the payload, re-reads the target, checks
the precondition (superseding the card when the record changed), and sends
exactly one write through the shim under a bdra_ token bound to the
approval. The ledger records the begun row with the before snapshot and the
terminal row with the after snapshot.

A write that may have reached the route (a 5xx, a transport failure, a
begun row without a terminal one) is never retried: the card shows
"partial" and the message waits for the human's review. `approval_status`
reads "done" for every existing row, so the AI monitor's Retry never runs a
write twice. Plain `def` throughout: the worker runs the body through
asyncio.to_thread and the executor opens its own event loop per request.

Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.2.3, 3.5.2, 3.12): an executed
sensitive_read card stores only its counts summary ({row_count, truncated,
view}) in the ledger, approval.result and after_snapshot, and its rows reach
the conversation through app/assistant/evidence_delivery.py; a 409
lane_moved supersedes the card like labor_changed; a begun write without a
terminal row runs the spec's recovery lookup first when it has one.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.assistant import access, approvals, jobs, registry, repo
from app.assistant.approvals import MAX_SUPERSEDE_DEPTH, precondition_for, sha256_hex
from app.assistant.executor import (
    Executor,
    LeaseLost,
    LedgerUnavailable,
    RequestFailed,
    WriteMayHavePartiallyRun,
)
from app.assistant.registry import ToolRequest, ToolSpec
from app.assistant.tools._common import ToolArgumentError
from app.core.config import Settings
from app.core.roles import Role
from app.services import llm_errors

logger = logging.getLogger(__name__)

JOB_TYPE = "assistant_approval_execute"
FEATURE = "assistant_execute"
MODEL_LABEL = "assistant-execute"

# The project gate's phase order (docs/ASSISTANT_PHASE1_PLAN.md 3.7): a card
# whose project moved forward since the preview expires as project_moved.
PHASE_RANK = {"open": 0, "verified": 1, "submitted": 2}

# A recovery lookup found the write when its body names the record: a row's
# `id`, or the split routes' `task_id` (split_drop_out) or `job_id`
# (job_outcome), which carry no top-level `id`.
RECOVERY_ID_KEYS = ("id", "task_id", "job_id")

# 409 codes whose compare-and-set refused before any write: the record moved,
# so the card is superseded by a fresh one (labor_changed: phase 1; lane_moved:
# advance_lane's expected_task, phase 4 3.12).
SUPERSEDE_CODES = ("labor_changed", "lane_moved")
# The only keys a sensitive_read result keeps in the ledger, approval.result
# and after_snapshot (3.2.3): no event row, name or text.
SENSITIVE_SUMMARY_KEYS = ("row_count", "truncated", "view")

BELL_SUPERSEDED = "assistant.approval_superseded"
BELL_FAILED = "assistant.approval_failed"
BELL_EXPIRED = "assistant.approval_expired"

TEXT_SUPERSEDED = "An assistant change was updated because the record changed. Review it again."
TEXT_FAILED = "An assistant change did not go through."
TEXT_PARTIAL = "An assistant change may have partially run. Check it in Activity."
TEXT_EXPIRED = "An assistant change was not made: {reason}."

# The reason sentence of the expired bell, per expired_reason.
REASON_TEXT = {
    "timed_out": "the card expired before it was approved",
    "changed_since_preview": "the record changed too many times",
    "project_closed": "the project was closed",
    "project_moved": "the project moved to Verify or was submitted after the card was prepared",
    "feature_off": "this part of BDR is switched off",
    "role_changed": "your role changed",
    "owner_changed": "your account or assistant access changed",
    # Phase 3 offboarding (docs/ASSISTANT_PHASE3_PLAN.md 3.10.2 step 2).
    "owner_offboarded": "your account or assistant access changed",
    "tool_removed": "this kind of change is no longer available",
}

MISSING_PAYLOAD = "The approval job has no approval to execute."
DEFAULT_FAILURE = "The change did not go through."


class ApprovalJobError(RuntimeError):
    """A job failure whose message is already user-safe."""

    llm_error_kind = llm_errors.KIND_BAD_INPUT


class BadJobPayload(jobs.BadJobPayload):
    pass


# ── Small helpers ────────────────────────────────────────────────────────────


def _dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_job_id() -> str | None:
    try:
        from app.services import llm_queue

        job = llm_queue.current_job.get()
    except Exception:  # noqa: BLE001 - the queue is optional to this module
        job = None
    return str(job.get("id")) if isinstance(job, dict) and job.get("id") else None


def _detail(payload: Any, status: int) -> str:
    """A short error sentence from a route's answer."""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail[:300]
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("code")
            if message:
                return str(message)[:300]
        if payload.get("error") == "transport":
            return "The request did not get an answer."
    return f"HTTP {status}" if status else "The request did not get an answer."


def _detail_code(payload: Any) -> str | None:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return str(detail.get("code")) if isinstance(detail, dict) and detail.get("code") else None


def _bell(user_id: str, row: dict, type_: str, message: str, *, approval_id: str | None = None) -> None:
    """notify_user without an email mirror; best effort."""
    try:
        from app.services import notifications

        notifications.notify_user(
            user_id, row.get("project_id"), type_, message, mirror_email=False,
            metadata={
                "approval_id": approval_id or str(row.get("id")),
                "assistant_conversation_id": row.get("conversation_id"),
            },
        )
    except Exception:  # noqa: BLE001 - a bell never fails the job
        logger.exception("assistant approvals: %s bell failed for %s", type_, row.get("id"))


def _expire(user_id: str, row: dict, reason: str, *, expect: str) -> bool:
    ok = repo.mark_approval(user_id, str(row["id"]), {"status": "expired", "expired_reason": reason},
                            expect_status=expect)
    if ok:
        _bell(user_id, row, BELL_EXPIRED, TEXT_EXPIRED.format(reason=REASON_TEXT.get(reason, "it could not run")))
        repo.settle_message(user_id, row.get("message_id"))
    return ok


def _fail(user_id: str, row: dict, result: dict, *, partial: bool = False, expect: str = "executing") -> bool:
    ok = repo.mark_approval(user_id, str(row["id"]), {"status": "failed", "result": result},
                            expect_status=expect)
    if ok:
        if partial:
            _bell(user_id, row, BELL_FAILED, TEXT_PARTIAL)
            repo.park_message_for_review(user_id, row.get("message_id"))
        else:
            _bell(user_id, row, BELL_FAILED, TEXT_FAILED)
            repo.settle_message(user_id, row.get("message_id"))
    return ok


# ── The job body ─────────────────────────────────────────────────────────────


def _owner_problem(user_id: str, row: dict) -> tuple[str | None, ToolSpec | None, Role | None, dict | None]:
    """(expired_reason or None, spec, role, human profile) for step 3. The
    owner half (account, role, switches, delegation) is access.owner_check,
    shared with the assistant_continue job and the split run scope (phase 2);
    the tool half (removed, role, sub-app) stays here."""
    check = access.owner_check(user_id)
    role, human = check.role, check.human
    if check.problem is not None:
        return check.problem, None, role, human
    assert role is not None
    spec = registry.get(str(row.get("action_kind") or ""))
    if spec is None or spec.write is None or not access.tool_enabled(check.settings_row, spec.name):
        return "tool_removed", spec, role, human
    if role not in spec.roles:
        return "role_changed", spec, role, human
    if spec.sub_app is not None:
        from app.core import features

        if not features.enabled_map(role).get(spec.sub_app, False):
            return "feature_off", spec, role, human
    return None, spec, role, human


def run_approval_execute(payload: dict) -> None:
    """The job body (docs/ASSISTANT_PHASE1_PLAN.md 3.8, steps 1 to 14)."""
    approval_id = str((payload or {}).get("approval_id") or "").strip()
    user_id = str((payload or {}).get("user_id") or "").strip()
    if not approval_id or not user_id:
        raise BadJobPayload(MISSING_PAYLOAD)

    row = repo.get_approval(user_id, approval_id)
    if row is None:
        return
    job_id = _current_job_id()
    if row.get("status") != "approved":
        if row.get("status") == "executing" and str(row.get("execute_job_id") or "") != str(job_id or ""):
            logger.warning("assistant approvals: %s is executing under another job; left alone", approval_id)
        return

    reason, spec, role, human = _owner_problem(user_id, row)
    if reason is not None:
        _expire(user_id, row, reason, expect="approved")
        return
    assert spec is not None and spec.write is not None

    patch: dict[str, Any] = {"status": "executing"}
    if job_id:
        patch["execute_job_id"] = job_id
    if not repo.mark_approval(user_id, approval_id, patch, expect_status="approved"):
        return
    row = {**row, **patch}

    body = _dict(row.get("payload"))
    try:
        intact = sha256_hex(body) == row.get("payload_sha256")
    except TypeError:
        intact = False
    if not intact:
        _fail(user_id, row, {"ok": False, "error": "hash_mismatch"})
        return
    if str(body.get("tool_version")) != str(spec.version):
        _fail(user_id, row, {"ok": False, "error": "tool_changed"})
        return

    args = _dict(body.get("args"))
    executor = Executor(
        user_id=user_id,
        user_label=str(row.get("user_label") or (human or {}).get("full_name") or user_id),
        run_id=str(uuid.uuid4()),
        specs={spec.name: spec},
        lease_owner=jobs._lease_owner(),
        conversation_id=row.get("conversation_id"),
        message_id=row.get("message_id") if spec.namespace in {"mail", "browser"} else None,
        approval_id=approval_id,
        tainted_context=bool(body.get("tainted")),
        renew_lease=jobs._renew_lease,
        role=role,
    )

    try:
        if spec.namespace in {"mail", "browser"}:
            executor.establish_preview_context(spec)
        reads = executor.read(spec.write.preview_requests(args), spec)
        target, gate = spec.write.snapshot(args, reads)
    except LeaseLost:
        raise
    except (RequestFailed, ToolArgumentError) as exc:
        _fail(user_id, row, {"ok": False, "error": f"The record could not be read: {exc}"[:500]})
        return

    gate = _dict(gate)
    old_gate = _dict(_dict(row.get("precondition")).get("gate"))
    if gate.get("closed"):
        _expire(user_id, row, "project_closed", expect="executing")
        return
    if gate.get("phase") in PHASE_RANK and old_gate.get("phase") in PHASE_RANK:
        if PHASE_RANK[gate["phase"]] > PHASE_RANK[old_gate["phase"]]:
            _expire(user_id, row, "project_moved", expect="executing")
            return

    try:
        fresh = precondition_for(target, gate)
    except TypeError:
        logger.exception("assistant approvals: %s snapshot is not canonical JSON", spec.name)
        _fail(user_id, row, {"ok": False, "error": "The record could not be compared."})
        return
    if fresh != _dict(row.get("precondition")):
        _supersede(user_id, row, body, spec, role)
        return

    req = _dict(body.get("request"))
    headers = {str(k): str(v) for k, v in _dict(req.get("headers")).items()}
    if spec.write.exec_headers is not None:
        headers.update({str(k): str(v) for k, v in (spec.write.exec_headers(args, reads) or {}).items()})
    request = ToolRequest(
        route=(str(req.get("method") or ""), str(req.get("path_format") or "")),
        path=str(req.get("path") or ""),
        params=_dict(req.get("params")),
        json=req.get("json"),
        headers=headers,
    )
    try:
        result = executor.write(spec, request, args=args, before_snapshot={"target": target, "gate": gate},
                                project_id=row.get("project_id"))
    except LeaseLost:
        raise
    except LedgerUnavailable:
        _fail(user_id, row, {"ok": False, "error": "ledger_unavailable"})
        return
    except WriteMayHavePartiallyRun:
        if spec.write.recover_request is not None and _recovered_after_crash(user_id, row, spec, executor, args):
            return
        _fail(user_id, row, {"ok": False, "error": "A step of this change may have run earlier.",
                             "may_have_partially_run": True}, partial=True)
        return
    except RequestFailed as exc:
        _fail(user_id, row, {"ok": False, "error": exc.kind})
        return

    _outcome(user_id, row, body, spec, role, executor, result, args)


def _outcome(user_id: str, row: dict, body: dict, spec: ToolSpec, role: Role | None,
             executor: Executor, result: Any, args: dict) -> None:
    """Step 11: record the terminal ledger row and the card's outcome."""
    approval_id = str(row["id"])
    status = int(result.status)
    error_kind = "transport" if status == 0 else f"http_{status}"
    finish = dict(spec=spec, result=result, args=args, project_id=row.get("project_id"))

    if 200 <= status < 300:
        sensitive = spec.effect == registry.EFFECT_SENSITIVE_READ
        summary = _summary_for(spec, result.payload)
        if sensitive:
            # A sensitive read changed nothing: no after read, and only the
            # counts summary is kept (3.2.3).
            after = {"response": summary}
        else:
            try:
                after_reads = executor.read(spec.write.preview_requests(args), spec)
                after_target, _gate = spec.write.snapshot(args, after_reads)
                after = {"target": after_target, "response": summary}
            except LeaseLost:
                raise
            except Exception:  # noqa: BLE001 - the after read never fails the write
                logger.exception("assistant approvals: after read failed for %s", approval_id)
                after = {"unavailable": True}
        executor.finish_write(ok=True, error_kind=None, result_summary=summary, after_snapshot=after, **finish)
        created = spec.write.created_ref(args, result.payload) if spec.write.created_ref else None
        if sensitive:
            # Post the evidence message before the card reads executed, so a
            # client that reloads the thread on that status finds it (review
            # FE-1). The message id is derived from the approval id, so a
            # replay by another worker inserts nothing.
            _deliver(user_id, row, spec, result.payload, executor)
        repo.mark_approval(user_id, approval_id, {
            "status": "executed",
            "executed_at": _now_iso(),
            "tool_call_id": result.ledger_id or None,
            "result": {"ok": True, "status_code": status, "summary": summary, "created": created,
                       "tool_call_id": result.ledger_id or None},
        }, expect_status="executing")
        repo.settle_message(user_id, row.get("message_id"))
        return

    if status == 409 and _detail_code(result.payload) in SUPERSEDE_CODES:
        # A compare-and-set refused (labor numbers or the lane's head moved):
        # nothing was written.
        executor.finish_write(ok=False, error_kind=error_kind,
                              result_summary={"error": _detail_code(result.payload)},
                              after_snapshot=None, **finish)
        _supersede(user_id, row, body, spec, role)
        return

    if 400 <= status < 500:
        executor.finish_write(ok=False, error_kind=error_kind,
                              result_summary={"error": _detail(result.payload, status)}, after_snapshot=None, **finish)
        _fail(user_id, row, {"ok": False, "status_code": status, "error": _detail(result.payload, status)})
        return

    if spec.effect == registry.EFFECT_SENSITIVE_READ:
        # A sensitive read changed nothing in BDR and is idempotent on the
        # approval id (3.2.3): a 5xx or transport failure is a plain failure,
        # never "may have partially run", and the read can be proposed again.
        executor.finish_write(ok=False, error_kind=error_kind,
                              result_summary={"error": _detail(result.payload, status)}, after_snapshot=None, **finish)
        _fail(user_id, row, {"ok": False, "status_code": status, "error": _detail(result.payload, status)})
        return

    # 5xx or transport: the write may have committed before a later step raised.
    executor.finish_write(ok=False, error_kind=error_kind,
                          result_summary={"error": _detail(result.payload, status)}, after_snapshot=None, **finish)
    if spec.write.recover_request is not None:
        state, hit = _recover(executor, spec, args, approval_id)
        if state == "missing":
            # The compensating delete ran or the insert never happened.
            _fail(user_id, row, {"ok": False, "status_code": status, "error": _detail(result.payload, status)})
            return
        if state == "found" and hit is not None:
            summary = _summary_for(spec, hit, fallback=dict(hit))
            created = spec.write.created_ref(args, hit) if spec.write.created_ref else None
            repo.mark_approval(user_id, approval_id, {
                "status": "executed",
                "executed_at": _now_iso(),
                "tool_call_id": result.ledger_id or None,
                "result": {"ok": True, "status_code": status, "recovered": True, "created": created,
                           "summary": summary, "tool_call_id": result.ledger_id or None},
            }, expect_status="executing")
            repo.settle_message(user_id, row.get("message_id"))
            return
    _fail(user_id, row, {
        "ok": False, "status_code": status, "error": _detail(result.payload, status),
        "may_have_partially_run": True, "tool_call_id": result.ledger_id or None,
    }, partial=True)


def _summary_for(spec: ToolSpec, payload: Any, *, fallback: dict | None = None) -> dict:
    """The spec's result_summary of `payload` ({} or `fallback` without
    one). A sensitive_read summary keeps only SENSITIVE_SUMMARY_KEYS, so no
    row, name or text reaches the ledger or the card (3.2.3)."""
    assert spec.write is not None
    if spec.write.result_summary is not None:
        summary = spec.write.result_summary(payload)
    else:
        summary = dict(fallback or {})
    summary = _dict(summary)
    if spec.effect == registry.EFFECT_SENSITIVE_READ:
        summary = {k: summary[k] for k in SENSITIVE_SUMMARY_KEYS if k in summary}
    return summary


def _deliver(user_id: str, row: dict, spec: ToolSpec, payload: Any, executor: Executor) -> None:
    """Post the evidence message of an executed sensitive card (3.2.3). A
    failure is logged and leaves the card executed: its counts are in
    approval.result.summary."""
    try:
        from app.assistant import evidence_delivery

        if spec.name == "read_mailbox":
            from app.services import assistant_mail_review
            assistant_mail_review.deliver(user_id, row, payload, executor)
        else:
            evidence_delivery.post_result(user_id, row, spec, payload, executor)
    except LeaseLost:
        raise
    except Exception:  # noqa: BLE001 - delivery never fails an executed read
        logger.exception("assistant approvals: evidence delivery failed for %s", row.get("id"))


def _recovered_after_crash(user_id: str, row: dict, spec: ToolSpec, executor: Executor, args: dict, *,
                           expect: str = "executing") -> bool:
    """A begun write without a terminal row (an earlier attempt crashed)
    whose spec can look itself up by approval (3.5.2): "found" marks the
    card executed as recovered, "missing" fails it plainly (nothing ran).
    Returns False for "unknown", so the caller keeps the partial failure.
    `expect` is the card status the marks are fenced on (`executing` in the
    job body; the card's own status in the queue's domain mark)."""
    approval_id = str(row["id"])
    state, hit = _recover(executor, spec, args, approval_id)
    if state == "missing":
        _fail(user_id, row, {"ok": False, "error": "The earlier attempt did not make this change."}, expect=expect)
        return True
    if state != "found" or hit is None:
        return False
    try:
        begun = repo.begun_without_terminal(user_id, approval_id)
    except Exception:  # noqa: BLE001 - the ledger link is a convenience
        logger.exception("assistant approvals: begun row read failed for %s", approval_id)
        begun = None
    tool_call_id = (begun or {}).get("id") or None
    summary = _summary_for(spec, hit, fallback=dict(hit))
    created = spec.write.created_ref(args, hit) if spec.write and spec.write.created_ref else None
    if repo.mark_approval(user_id, approval_id, {
        "status": "executed",
        "executed_at": _now_iso(),
        "tool_call_id": tool_call_id,
        "result": {"ok": True, "recovered": True, "created": created, "summary": summary,
                   "tool_call_id": tool_call_id},
    }, expect_status=expect):
        repo.settle_message(user_id, row.get("message_id"))
    return True


def _recover(executor: Executor, spec: ToolSpec, args: dict, approval_id: str) -> tuple[str, dict | None]:
    """("found", row), ("missing", None) on a 404, ("unknown", None) when
    the lookup itself failed (the caller falls through to partial)."""
    assert spec.write is not None and spec.write.recover_request is not None
    try:
        found = executor.read([spec.write.recover_request(args, approval_id)], spec)
    except LeaseLost:
        raise
    except RequestFailed as exc:
        if exc.kind == "http_404":
            return "missing", None
        logger.warning("assistant approvals: recovery lookup for %s failed: %s", approval_id, exc.kind)
        return "unknown", None
    except Exception:  # noqa: BLE001 - a failed lookup falls through to partial
        logger.exception("assistant approvals: recovery lookup failed for %s", approval_id)
        return "unknown", None
    hit = found[0] if found else None
    if isinstance(hit, dict) and any(hit.get(k) for k in RECOVERY_ID_KEYS):
        return "found", hit
    return "unknown", None


def _supersede(user_id: str, row: dict, body: dict, spec: ToolSpec, role: Role | None) -> None:
    """Step 12: a fresh card for the changed record, or an expiry when the
    chain is too long or the change is now a no-op."""
    approval_id = str(row["id"])
    depth = int(body.get("supersede_depth") or 0)
    if depth >= MAX_SUPERSEDE_DEPTH:
        _expire(user_id, row, "changed_since_preview", expect="executing")
        return
    executor = Executor(
        user_id=user_id,
        user_label=str(row.get("user_label") or user_id),
        run_id=str(uuid.uuid4()),
        specs={spec.name: spec},
        lease_owner=jobs._lease_owner(),
        conversation_id=row.get("conversation_id"),
        message_id=row.get("message_id") if spec.namespace in {"mail", "browser"} else None,
        tainted_context=bool(body.get("tainted")),
        renew_lease=jobs._renew_lease,
        role=role,
    )
    res = approvals.propose(
        executor, spec.name, _dict(body.get("args")), user_texts=(),
        conversation_id=row.get("conversation_id"), message_id=row.get("message_id"),
        reverts_approval_id=body.get("reverts_approval_id"), supersede_depth=depth + 1,
        skip_binding=True, supersedes_approval_id=approval_id,
        inherited_taint=bool(body.get("tainted")),
        inherited_taint_level=str(body.get("taint_level") or "none"),
        inherited_stripped=tuple(s for s in body.get("stripped") or () if isinstance(s, dict)),
    )
    if res.ok and res.approval is not None:
        _bell(user_id, row, BELL_SUPERSEDED, TEXT_SUPERSEDED, approval_id=str(res.approval.get("id")))
        return
    if res.error_kind == "not_executing":
        return
    if res.error_kind == "refused":
        _expire(user_id, row, "changed_since_preview", expect="executing")
        return
    error = str(res.model_output.get("error") or DEFAULT_FAILURE)
    _fail(user_id, row, {"ok": False, "error": f"The changed record could not be prepared again: {error}"[:500]})


# ── Queue adapters ───────────────────────────────────────────────────────────


def mark_approval(target: str, fields: dict) -> None:
    """The queue's domain mark (failure handler and lease sweep); target is
    the approval id. A failed job whose card is still approved or executing
    becomes executed when its write already has a completed ledger row (the
    job died after the write committed, for example on the card's own
    compare-and-set), failed: partial (and the message parked for review)
    when a begun write row has no terminal row, a clean failure otherwise.
    A pending mark is ignored (execute jobs never retry)."""
    if (fields or {}).get("status") != "failed":
        return
    owner = jobs.owner_for(JOB_TYPE, target)
    if owner is None:
        logger.warning("assistant approvals: no job owner for approval %s; domain mark skipped", target)
        return
    row = repo.get_approval(owner, str(target))
    if row is None or row.get("status") not in ("approved", "executing"):
        return
    completed = repo.completed_write(owner, str(target))
    if completed is not None:
        _mark_committed(owner, row, completed)
        return
    error = str(fields.get("error") or DEFAULT_FAILURE)[:500]
    begun = repo.begun_without_terminal(owner, str(target))
    if begun is not None and _recovered_in_sweep(owner, row):
        return
    if begun is not None:
        result = {"ok": False, "error": error, "may_have_partially_run": True, "tool_call_id": begun.get("id")}
    else:
        result = {"ok": False, "error": error}
    if not repo.mark_approval(owner, str(target), {"status": "failed", "result": result},
                              expect_status=row.get("status")):
        return
    if begun is not None:
        _bell(owner, row, BELL_FAILED, TEXT_PARTIAL)
        repo.park_message_for_review(owner, row.get("message_id"))
    else:
        _bell(owner, row, BELL_FAILED, TEXT_FAILED)
        repo.settle_message(owner, row.get("message_id"))


def _recovered_in_sweep(owner: str, row: dict) -> bool:
    """A crashed job's card with a begun write and no terminal row, whose
    spec can look itself up by approval (3.5.2): run that lookup as the
    owner before the card is called partial. "found" marks it executed as
    recovered, "missing" fails it plainly; True when either happened. An
    owner problem (removed tool, changed role, revoked delegation), an
    "unknown" lookup or any error returns False, keeping the partial path."""
    spec = registry.get(str(row.get("action_kind") or ""))
    if spec is None or spec.write is None or spec.write.recover_request is None:
        return False
    try:
        reason, _spec, role, human = _owner_problem(owner, row)
        if reason is not None:
            return False
        body = _dict(row.get("payload"))
        executor = Executor(
            user_id=owner,
            user_label=str(row.get("user_label") or (human or {}).get("full_name") or owner),
            run_id=str(uuid.uuid4()),
            specs={spec.name: spec},
            lease_owner=jobs._lease_owner(),
            conversation_id=row.get("conversation_id"),
            message_id=None,
            approval_id=str(row["id"]),
            tainted_context=bool(body.get("tainted")),
            renew_lease=None,
            role=role,
        )
        return _recovered_after_crash(owner, row, spec, executor, _dict(body.get("args")),
                                      expect=str(row.get("status") or "executing"))
    except Exception:  # noqa: BLE001 - the partial path is the safe fallback
        logger.exception("assistant approvals: recovery lookup in the sweep failed for %s", row.get("id"))
        return False


def _mark_committed(owner: str, row: dict, completed: dict) -> None:
    """The card of a write whose completed ledger row exists is executed,
    never a clean failure: the change is in BDR and Revert must work."""
    summary = _dict(completed.get("result_summary"))
    created = None
    spec = registry.get(str(row.get("action_kind") or ""))
    if spec is not None and spec.write is not None and spec.write.created_ref is not None:
        try:
            created = spec.write.created_ref(_dict(_dict(row.get("payload")).get("args")), summary)
        except Exception:  # noqa: BLE001 - the link is a convenience
            logger.exception("assistant approvals: created_ref failed while recovering %s", row.get("id"))
    tool_call_id = completed.get("begun_id") or completed.get("id")
    if not repo.mark_approval(owner, str(row["id"]), {
        "status": "executed",
        "executed_at": _now_iso(),
        "tool_call_id": tool_call_id,
        "result": {"ok": True, "recovered": True, "summary": summary,
                   "created": created, "tool_call_id": tool_call_id},
    }, expect_status=row.get("status")):
        return
    repo.settle_message(owner, row.get("message_id"))


def approval_status(target: str) -> str:
    """"done" for any existing row, "not_found" when there is none (or no
    owner): the AI monitor's Retry refuses both, so a write never re-runs."""
    owner = jobs.owner_for(JOB_TYPE, target)
    if owner is None:
        return "not_found"
    return "done" if repo.queue_approval_owner_status(owner, str(target)) is not None else "not_found"


def model_label(s: Settings) -> str:
    return MODEL_LABEL


def error_message(exc: Exception, label: str) -> str:
    if isinstance(exc, (ApprovalJobError, jobs.BadJobPayload)):
        return str(exc)[:500]
    return llm_errors.user_message(exc, label)


def spec_kwargs() -> dict[str, Any]:
    """What llm_queue._JobSpec needs, for the registration and the tests."""
    return {
        "feature": FEATURE,
        "run": run_approval_execute,
        "mark": mark_approval,
        "current_status": approval_status,
        "model_label": model_label,
        "error_message": error_message,
    }

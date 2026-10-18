"""Durable queue for the long-running AI jobs.

Replaces fire-and-forget FastAPI BackgroundTasks for BOQ extraction, the
general material estimate, and proposal scope lines. Routers insert an
llm_jobs row; a worker loop (started from the lifespan in every uvicorn
worker) claims due jobs atomically and runs them. What that buys over
BackgroundTasks:

- Nothing is lost on a deploy or crash: a running job's lease expires and
  the sweep requeues it instead of stranding the domain row at "running"
  forever (the old 15-minute lazy stale-fail is now only a backstop for
  queue-disabled mode).
- Transient failures (model server down / timeout / overloaded / provider
  5xx / unusable output) retry automatically on the schedule in
  LLM_QUEUE_RETRY_DELAYS. Permanent failures (scanned PDF, missing config,
  out of API tokens) fail immediately with a specific user-facing message.
- Load is queued instead of dropped: the worker runs a bounded number of
  jobs at once and everything else waits its turn, visible to the user as
  "queued (position N)" via poll_info() joined into the poll endpoints.

Multi-worker safety: claims go through the claim_llm_jobs RPC (FOR UPDATE
SKIP LOCKED), so the two prod uvicorn workers never claim the same job.
Attempts are counted at claim time, so a worker that dies mid-run has
still spent that attempt; the lease-expiry sweep requeues or terminally
fails the job without guessing what happened.

The domain status rows keep their existing vocabularies: 'pending' covers
queued and waiting-for-retry, 'running' an attempt in flight. The FE keeps
polling exactly as before and gets the queue detail alongside.

The RFP Ingestion sandbox (job type 'rfp_ingest', one row per run) rides the
same table and the same lease/retry machinery but is NOT an LLM job: it
spawns a subprocess per file for up to hours. Three things keep it from
starving or blocking the AI work:

- The worker claims in TWO passes per tick, each with its own capacity: the
  LLM job types against llm_queue_worker_concurrency, then ['rfp_ingest']
  against rfp_ingest_sandbox_concurrency minus the sandbox runs already in
  flight here. The claim RPC's job_types filter is what keeps the passes
  disjoint, so a sandbox run never occupies an LLM slot and vice versa.
- A running job is exposed to its runner through the `current_job`
  contextvar so the sandbox monitor loop can renew_lease() every 30 s (a
  lost renewal means the sweep already handed the job to another worker:
  the runner kills its child and stops) and requeue_self() on shutdown so
  the next worker resumes the run instead of waiting out the lease.
- Failures inside the sandbox runner arrive as app-authored exceptions that
  declare their own kind (llm_errors.declared_kind); _handle_failure uses
  the spec's model_label ("sandbox") instead of resolving an LLM route, and
  its whole body is guarded so an internal error there still reaches the
  terminal CAS. The spec also supplies its own error_message, so an exception
  the sandbox never declared is recorded with a sandbox-authored sentence
  rather than one about "the AI provider" and "the model".

AI Estimating (docs/PLANS_READER.md section 7) adds four LLM job types, one
job per model-call unit so leases, retries and parallelism are per call: a
sub, roll-up or escalation group (`plans_reader_agent`), the main
consolidation (`plans_reader_main`), a chat answer (`plans_reader_chat`) and
a deliverable modification (`plans_reader_modify`). They are claimed in
their own pass with their own capacity (PLANS_READER_CONCURRENCY per process),
never against llm_queue_worker_concurrency: one 600-page run queues dozens of
group jobs that each hold a slot for minutes, and the bidding pipeline's BOQ,
proposal and splitter jobs must not wait behind them. Inside that pass the
interactive types (chat, modify) are claimed first and the batch types
(agent, main) never take the last slot, so a chat answer waits at most one
group, not a whole run. The pass runs only while PLANS_READER_ENABLED is on.
A terminal group failure advances the run so it finishes as done_with_errors
instead of waiting forever.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from app.core.config import Settings, get_settings
from app.core.supabase_client import get_supabase
from app.services import llm_errors, llm_gate

logger = logging.getLogger(__name__)

# Per-process worker identity: leases are fenced on this token.
_WORKER_TOKEN = uuid.uuid4().hex

JOB_BOQ = "boq_extraction"
JOB_GENERAL_MATERIAL = "general_material"
JOB_PROPOSAL = "proposal_lines"
JOB_BID_SPLIT = "bid_split"
JOB_RFP_INGEST = "rfp_ingest"
JOB_RFP_HARVEST = "rfp_harvest"
JOB_PORTAL_SCAN = "rfp_portal_scan"
JOB_PORTAL_HARVEST = "rfp_portal_harvest"
JOB_RFP_CREATE_FILES = "rfp_create_files"
# AI Estimating (docs/PLANS_READER.md section 7). One job per model-call unit:
# a sub, roll-up or escalation group (agent), the main consolidation (main),
# one chat answer (chat) and one deliverable modification (modify).
JOB_PLANS_AGENT = "plans_reader_agent"
JOB_PLANS_MAIN = "plans_reader_main"
JOB_PLANS_CHAT = "plans_reader_chat"
JOB_PLANS_MODIFY = "plans_reader_modify"
# Batch: a run's pipeline groups. Interactive: someone is waiting on the page.
PLANS_BATCH_JOB_TYPES = (JOB_PLANS_AGENT, JOB_PLANS_MAIN)
PLANS_INTERACTIVE_JOB_TYPES = (JOB_PLANS_CHAT, JOB_PLANS_MODIFY)
PLANS_JOB_TYPES = PLANS_BATCH_JOB_TYPES + PLANS_INTERACTIVE_JOB_TYPES
# Long-form aliases (the services written in parallel may use either name).
JOB_PLANS_READER_AGENT = JOB_PLANS_AGENT
JOB_PLANS_READER_MAIN = JOB_PLANS_MAIN
JOB_PLANS_READER_CHAT = JOB_PLANS_CHAT
JOB_PLANS_READER_MODIFY = JOB_PLANS_MODIFY

# The job types that spend a slot of llm_queue_worker_concurrency (the
# bidding pipeline's AI work). The sandbox, harvest, portal and promotion
# types are deliberately not here, and neither are the AI Estimating types:
# each family is claimed in its own pass with its own capacity (see
# _claim_tick), so none of them can occupy the pipeline's slots.
LLM_JOB_TYPES = (JOB_BOQ, JOB_GENERAL_MATERIAL, JOB_PROPOSAL, JOB_BID_SPLIT)
# The portal request stream (docs/RFP_NGEM_PORTAL.md 2.2): the scan and the
# portal harvest share the harvest capacity with the Procore harvest, one
# human-paced session per worker at a time.
PORTAL_JOB_TYPES = (JOB_PORTAL_SCAN, JOB_PORTAL_HARVEST)
HARVEST_JOB_TYPES = (JOB_RFP_HARVEST,) + PORTAL_JOB_TYPES
# The RFP creation slice's document promotion (docs/RFP_CREATE.md 5): a
# storage-streaming job that rides the third pass beside the harvests and
# counts against the same capacity (minutes of streaming per project).
CREATE_JOB_TYPES = (JOB_RFP_CREATE_FILES,)
THIRD_PASS_JOB_TYPES = HARVEST_JOB_TYPES + CREATE_JOB_TYPES
NON_LLM_JOB_TYPES = (JOB_RFP_INGEST,) + THIRD_PASS_JOB_TYPES
# Every job type llm_jobs.job_type accepts (the CHECK in migration 0152).
ALL_JOB_TYPES = LLM_JOB_TYPES + PLANS_JOB_TYPES + NON_LLM_JOB_TYPES

# The default of PLANS_READER_CONCURRENCY (core/config.py), AI Estimating
# jobs per worker process (two uvicorn workers in prod, so twice this).
PLANS_DEFAULT_CONCURRENCY = 2

# Model label recorded for sandbox jobs in place of an LLM model name.
SANDBOX_MODEL_LABEL = "sandbox"

_FEATURE_BY_TYPE = {
    JOB_BOQ: "boq",
    JOB_GENERAL_MATERIAL: "estimate",
    JOB_PROPOSAL: "proposal",
    JOB_BID_SPLIT: "bid_split",
    JOB_RFP_INGEST: "rfp_ingest",
    JOB_RFP_HARVEST: "rfp_harvest",
    JOB_PORTAL_SCAN: "rfp_portal",
    JOB_PORTAL_HARVEST: "rfp_portal",
    JOB_RFP_CREATE_FILES: "rfp_create",
    JOB_PLANS_AGENT: "plans_sub",
    JOB_PLANS_MAIN: "plans_main",
    JOB_PLANS_CHAT: "plans_chat",
    JOB_PLANS_MODIFY: "plans_modify",
}

# The job whose spec.run is executing on this thread (None outside _execute,
# including the BackgroundTasks fallback, where renew_lease/requeue_self are
# no-ops). Set per _execute call; asyncio.to_thread copies the context, so
# concurrent jobs never see each other's row.
current_job: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "llm_queue_current_job", default=None
)

_ACTIVE_STATUSES = ("queued", "running")
_ERROR_MAX_CHARS = 500

_INTERRUPTED_MESSAGE = (
    "The run was interrupted (the server restarted or was redeployed). "
    "It has been queued to run again automatically."
)
_INTERRUPTED_FINAL_MESSAGE = (
    "The run was interrupted repeatedly (server restarts). Run it again; "
    "if this keeps happening, contact your IT Director."
)
_CANCELED_MESSAGE = "Canceled from the AI monitor page."


class JobAlreadyActive(RuntimeError):
    """An active (queued/running) job already exists for this target."""

    def __init__(self, job: dict):
        super().__init__("A job is already queued or running for this item.")
        self.job = job


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _JobSpec:
    feature: str
    run: Callable[[dict], None]  # raises on failure; owns running/done marks
    mark: Callable[[str, dict], None]  # patch the domain status row
    current_status: Callable[[str], str | None]  # domain row's status, None if gone
    # Label stored with failure messages. Defaults to the feature's active LLM
    # model (llm.active_model); non-LLM jobs supply their own so no route is
    # resolved for a feature llm.py has never heard of.
    model_label: Callable[[Settings], str] | None = None
    # (exception, model_label) -> the sentence stored in last_error and in the
    # domain row's error column. Defaults to llm_errors.user_message, which
    # talks about the AI provider and the model; a non-LLM job supplies its own
    # so an exception it never declared cannot describe a sandbox run as an
    # LLM call (or leak developer-facing text from a raw ValueError).
    error_message: Callable[[Exception, str], str] | None = None

    def __post_init__(self) -> None:
        if self.error_message is None:
            object.__setattr__(self, "error_message", llm_errors.user_message)
        if self.model_label is None:
            feature = self.feature

            def _active_model(s: Settings) -> str:
                from app.services import llm

                return llm.active_model(feature, s)

            object.__setattr__(self, "model_label", _active_model)


def _row_status(table: str, key_column: str, target_id: str) -> str | None:
    rows = (
        get_supabase()
        .table(table)
        .select("status")
        .eq(key_column, target_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0].get("status") if rows else None


def _spec(job_type: str) -> _JobSpec:
    """Runner + domain-row adapter per job type. Imports are lazy so this
    module stays importable from the routers without dragging every service
    (and their SDK imports) in at boot."""
    if job_type == JOB_BOQ:
        from app.services import boq_extraction as m

        return _JobSpec(
            "boq",
            lambda p: m.execute(p["analysis_id"]),
            lambda target, fields: m._mark(target, **fields),
            lambda target: _row_status("boq_analyses", "id", target),
        )
    if job_type == JOB_GENERAL_MATERIAL:
        from app.services import general_material as m

        return _JobSpec(
            "estimate",
            lambda p: m.execute(p["project_id"]),
            lambda target, fields: m._save(target, **fields),
            lambda target: _row_status("general_material_estimates", "project_id", target),
        )
    if job_type == JOB_PROPOSAL:
        from app.services import proposal_scope as m

        return _JobSpec(
            "proposal",
            lambda p: m.execute(p["draft_id"]),
            lambda target, fields: m._mark(target, **fields),
            lambda target: _row_status("proposal_drafts", "id", target),
        )
    if job_type == JOB_BID_SPLIT:
        from app.services import bid_split as m

        return _JobSpec(
            "bid_split",
            # forced_kind rides in the payload (a reprocess pins the user's
            # verdict), so lease requeues keep forcing the same way.
            lambda p: m.execute(p["file_id"], forced_kind=p.get("forced_kind")),
            lambda target, fields: m._mark(target, **fields),
            lambda target: _row_status("bid_split_files", "id", target),
        )
    if job_type == JOB_RFP_INGEST:
        from app.sandbox import protocol
        from app.services import rfp_ingest as m

        def _rfp_message(exc: Exception, _label: str) -> str:
            """Sandbox-authored text for every sandbox failure.

            The runner's two exception classes carry sentences the service
            wrote for users, so those pass through. Anything else escaping
            execute (a postgrest APIError from a mark, an httpx timeout, a
            raw ValueError) would otherwise be described by llm_errors as a
            problem with "the AI provider" and "the model" for a run that
            never touched an LLM, or stored verbatim as developer-facing
            text. Same mapping as rfp_ingest.run_in_background, so the queue
            path and the BackgroundTasks fallback record the same thing.
            """
            if isinstance(exc, (m.RfpIngestTransient, m.RfpIngestPermanent)):
                return str(exc)
            return protocol.VERDICT_MESSAGES[protocol.FAIL_INTERRUPTED]

        return _JobSpec(
            "rfp_ingest",
            # One job per RUN; the payload carries only the run id.
            lambda p: m.execute(p["run_id"]),
            # The queue only ever marks {"status": pending|failed, "error"}; the
            # service's mark is a CAS that ignores marks a canceled or finished
            # run must not take.
            lambda target, fields: m.mark_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            # Maps every terminal run status to 'done' so the AI monitor's
            # retry refuses; /rfp-ingest/runs/{id}/retry is the only retry path.
            lambda target: m.current_status(target),
            model_label=lambda s: SANDBOX_MODEL_LABEL,
            error_message=_rfp_message,
        )
    if job_type == JOB_RFP_HARVEST:
        from app.services import rfp_harvest as m

        return _JobSpec(
            "rfp_harvest",
            # One job per EMAIL row; `force` refreshes a complete harvest.
            lambda p: m.execute(p["email_id"], force=bool(p.get("force"))),
            lambda target, fields: m.mark_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            lambda target: m.current_status(target),
            model_label=lambda s: m.MODEL_LABEL,
            error_message=m.error_message,
        )
    if job_type == JOB_PORTAL_SCAN:
        from app.services import rfp_portal_ingest as m

        return _JobSpec(
            "rfp_portal",
            # One job per RUN row (docs/RFP_NGEM_PORTAL.md 2.2).
            lambda p: m.execute_scan(p["run_id"]),
            lambda target, fields: m.mark_scan_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            # Every terminal run status maps to 'done' so the AI monitor's
            # retry refuses; Run now is the retry path.
            lambda target: m.scan_status(target),
            model_label=lambda s: m.MODEL_LABEL,
            error_message=m.error_message,
        )
    if job_type == JOB_PORTAL_HARVEST:
        from app.services import rfp_portal_ingest as m

        return _JobSpec(
            "rfp_portal",
            # One job per INVITATION row; `force` refreshes a complete harvest.
            lambda p: m.execute_harvest(p["invitation_id"], force=bool(p.get("force"))),
            lambda target, fields: m.mark_harvest_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            lambda target: m.current_status(target),
            model_label=lambda s: m.MODEL_LABEL,
            error_message=m.error_message,
        )
    if job_type == JOB_RFP_CREATE_FILES:
        from app.services import rfp_create_files as m

        return _JobSpec(
            "rfp_create",
            # One job per PROJECT (docs/RFP_CREATE.md 5): the harvest's
            # verified documents into the project's files. `harvest_id` names
            # the harvest to promote when the payload carries one (4.6's
            # recent-project link promotes ITS invitation's harvest into a
            # project whose record belongs to another's); without it the job
            # falls back to the record's harvest, as it always has.
            lambda p: m.execute(p["project_id"], harvest_id=p.get("harvest_id")),
            lambda target, fields: m.mark_from_queue(
                target, fields.get("status"), fields.get("error")
            ),
            # complete and failed both map to 'done' so the AI monitor's
            # retry refuses; "Retry documents" on the page is the retry path.
            lambda target: m.current_status(target),
            model_label=lambda s: m.MODEL_LABEL,
            error_message=m.error_message,
        )
    if job_type in (JOB_PLANS_AGENT, JOB_PLANS_MAIN):
        return _JobSpec(
            _FEATURE_BY_TYPE[job_type],
            # One job per plans_reader_groups row (sub, rollup or escalation
            # for the agent type, the main consolidation for main). run_group
            # owns the running/done marks, writes the plans_reader_calls row
            # and advances the run; it leaves a group queued (and returns
            # normally) when the spend cap stops it.
            _run_plans_group,
            _mark_plans_group,
            _plans_group_status,
        )
    if job_type == JOB_PLANS_CHAT:
        return _JobSpec(
            "plans_chat",
            # One job per pending assistant message (target = message id).
            _run_plans_chat,
            _mark_plans_message,
            _plans_message_status,
        )
    if job_type == JOB_PLANS_MODIFY:
        return _JobSpec(
            "plans_modify",
            # One job per modify group row (target = group id); the payload
            # carries the run, the deliverable kind, the instruction and who
            # asked, so a lease requeue repeats the same request.
            _run_plans_modify,
            _mark_plans_group,
            _plans_group_status,
        )
    raise KeyError(f"Unknown llm job type: {job_type}")


# ── AI Estimating job adapters (docs/PLANS_READER.md section 7) ──────────
#
# The services are imported inside each adapter, like the specs above, so
# this module stays importable without the plans reader modules loaded.

# Group kinds whose terminal failure must move the run's state machine on
# (a failed group is terminal for advance(), which then finishes the run as
# done_with_errors instead of leaving it reading forever).
_PLANS_PIPELINE_KINDS = ("sub", "rollup", "escalation", "main")
_PLANS_ACTIVE_GROUP = ("queued", "running")
_PLANS_ACTIVE_MESSAGE = ("pending", "running")


class _JobActor(str):
    """The user a queued AI Estimating job acts for, rebuilt from the user id
    in its payload. A str (the id) that also answers `.id`, so a service that
    takes `user` works whether it reads `user.id` or stores `user` as the id.
    The worker has no request, so there is no role or email to carry."""

    @property
    def id(self) -> str:  # noqa: A003 - mirrors CurrentUser.id
        return str(self)


def _run_plans_group(payload: dict) -> None:
    from app.services import plans_reader_agents

    group_id = payload.get("group_id")
    if not group_id:
        raise ValueError("The AI Estimating job has no group to run.")
    plans_reader_agents.run_group(get_supabase(), str(group_id))


def _run_plans_chat(payload: dict) -> None:
    from app.services import plans_reader_chat

    message_id = payload.get("message_id")
    if not message_id:
        raise ValueError("The AI Estimating chat job has no message to answer.")
    plans_reader_chat.run_message(get_supabase(), str(message_id))


MSG_MODIFY_UNSAVED = (
    "The AI model answered, but the new version could not be saved. "
    "Ask for the change again."
)
MSG_MODIFY_STALE = (
    "This deliverable changed after the change was asked for, so it was not "
    "applied. Ask for it again on the latest version."
)


def _paid_modify_calls(sb: Any, group_id: str) -> int:
    """Successful model calls recorded for a modify group (plans_reader_calls
    rows with ok=true). A count that grew during an attempt means the model
    answered and was paid, whatever failed afterwards."""
    rows = (
        sb.table("plans_reader_calls")
        .select("id")
        .eq("group_id", group_id)
        .eq("ok", True)
        .execute()
    ).data or []
    return len(rows)


def _saved_modify_version(sb: Any, run_id: str, kind: str, instruction: str, since: str | None) -> dict | None:
    """The deliverable version an earlier attempt of this modify already
    saved (same run, kind and instruction, created since the group started),
    so a retry finishes the group instead of writing a second version."""
    if not since:
        return None
    rows = (
        sb.table("plans_reader_deliverables")
        .select("id, version")
        .eq("run_id", run_id)
        .eq("kind", kind)
        .eq("instruction", instruction)
        .gte("created_at", since)
        .order("version", desc=True)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _latest_deliverable_version(sb: Any, run_id: str, kind: str) -> int:
    rows = (
        sb.table("plans_reader_deliverables")
        .select("version")
        .eq("run_id", run_id)
        .eq("kind", kind)
        .order("version", desc=True)
        .limit(1)
        .execute()
    ).data or []
    return int(rows[0].get("version") or 0) if rows else 0


def _finish_modify_group(sb: Any, group_id: str, kind: str, row: dict, *, rerender: bool = False) -> None:
    """Close a modify group with the version it wrote. A re-render keeps
    `rerender: true` in its output, so the run payload can tell it from a
    model modification."""
    output: dict[str, Any] = {"kind": kind, "deliverable_id": row.get("id"), "version": row.get("version")}
    if rerender:
        output["rerender"] = True
    sb.table("plans_reader_groups").update(
        {
            "status": "done",
            "error": None,
            "finished_at": _now().isoformat(),
            "output": output,
        }
    ).eq("id", group_id).in_("status", list(_PLANS_ACTIVE_GROUP)).execute()


def _run_plans_modify(payload: dict) -> None:
    """Rewrite one deliverable per the user's instruction (a new version),
    or re-render it (payload `rerender` true, `instruction` null: no model
    call, see _run_plans_rerender).

    Paid at most once per request. A group already done (or canceled) is
    left alone, so a retried job never writes a second version. A failure
    after the model answered is not paid again: the parsed spec is kept on
    the group before rendering (plans_reader_agents._keep_pending_spec), so
    a failure while saving it (a storage upload, a worker that died) is
    retried by rendering the kept spec with no model call; without a kept
    spec, or when the renderer refused it (a ValueError, which would refuse
    again), the failure is permanent. The one other exception is an unusable
    answer (LlmBadOutput), where a fresh call is the remedy; the paid call
    is noted on the group (output.retry_ok_calls) so the retry is allowed. A
    retry that finds a paid call it cannot account for finishes the group
    from the version that attempt saved, else from the kept spec, or fails
    with a sentence asking for the change again.

    The edit applies to the version the user saw (payload base_version):
    when another change landed first, the job refuses instead of silently
    building on a version the user never asked about.

    The spend cap is permanent for this attempt: SpendCapReached becomes a
    ValueError carrying its app-authored sentence, so the group fails with
    that text instead of a generic provider message and the queue does not
    retry into the same cap.

    A run being deleted (claimed `canceled` by plans_reader_runs.delete) or
    gone stops the job before it writes anything: the group is canceled.
    The run is read again after the group is marked running, so either the
    delete's own check after its claim sees the running group (and refuses)
    or this check sees the claim."""
    from app.services import ai_spend, llm, plans_reader_agents

    sb = get_supabase()
    group_id = str(payload.get("group_id") or "")
    run_id = str(payload.get("run_id") or "")
    kind = str(payload.get("kind") or "")
    rerender = payload.get("rerender") is True and payload.get("instruction") is None
    instruction = "" if rerender else str(payload.get("instruction") or "").strip()
    if not (group_id and run_id and kind and (rerender or instruction)):
        raise ValueError("The AI Estimating modify job is missing its request.")
    groups = (
        sb.table("plans_reader_groups").select("*").eq("id", group_id).limit(1).execute()
    ).data or []
    if not groups or groups[0].get("status") not in _PLANS_ACTIVE_GROUP:
        return
    group = groups[0]
    runs = sb.table("plans_reader_runs").select("*").eq("id", run_id).limit(1).execute().data or []
    if not runs:
        raise ValueError("The AI Estimating run no longer exists.")
    if not _plans_run_open(sb, group_id, run_id, runs[0]):
        return
    actor = _JobActor(str(payload.get("user_id") or ""))
    if rerender:
        _run_plans_rerender(sb, group, runs[0], kind, payload, actor)
        return

    paid = _paid_modify_calls(sb, group_id)
    output = group.get("output") if isinstance(group.get("output"), dict) else {}
    accounted = int(output.get("retry_ok_calls") or 0)
    if paid > accounted:
        # An earlier attempt was paid and then lost (a worker died, the save
        # failed, or the done mark failed after the version was saved).
        saved = _saved_modify_version(sb, run_id, kind, instruction, group.get("started_at"))
        if saved:
            _finish_modify_group(sb, group_id, kind, saved)
            return
        kept = output.get(plans_reader_agents.PENDING_SPEC_KEY)
        if isinstance(kept, dict) and output.get("pending_kind") in (None, kind):
            _mark_modify_running(sb, group)
            if not _plans_run_open(sb, group_id, run_id):
                return
            row = plans_reader_agents.render_pending_spec(
                sb, runs[0], kind=kind, spec=kept, instruction=instruction, user=actor
            )
            _finish_modify_group(sb, group_id, kind, row or {})
            return
        raise ValueError(MSG_MODIFY_UNSAVED)

    base = payload.get("base_version")
    if base is not None and _latest_deliverable_version(sb, run_id, kind) != int(base):
        raise ValueError(MSG_MODIFY_STALE)

    _mark_modify_running(sb, group)
    if not _plans_run_open(sb, group_id, run_id):
        return
    try:
        row = plans_reader_agents.modify_deliverable(
            sb,
            runs[0],
            kind=kind,
            instruction=instruction,
            user=actor,
            group_id=group_id,
        )
    except ai_spend.SpendCapReached as exc:
        raise ValueError(str(exc)) from exc
    except Exception as exc:
        if isinstance(exc, ValueError) and not isinstance(exc, llm.LlmBadOutput):
            raise  # app-authored refusal (or the renderer refused the spec): already permanent
        if isinstance(exc, llm.LlmBadOutputPermanent):
            raise  # declared permanent; nothing will retry it
        try:
            now_paid = _paid_modify_calls(sb, group_id)
        except Exception:  # noqa: BLE001 - cannot tell; never pay twice on a guess
            logger.exception("llm queue: counting modify calls for group %s failed", group_id)
            raise ValueError(MSG_MODIFY_UNSAVED) from exc
        if now_paid <= paid:
            raise  # the model was never reached: the queue's own retry rule applies
        if isinstance(exc, llm.LlmBadOutput):
            sb.table("plans_reader_groups").update(
                {"output": {**output, "retry_ok_calls": now_paid}}
            ).eq("id", group_id).execute()
            raise
        if _has_pending_spec(sb, group_id, plans_reader_agents.PENDING_SPEC_KEY):
            raise  # the retry renders the kept spec (above) without a second call
        raise ValueError(MSG_MODIFY_UNSAVED) from exc
    _finish_modify_group(sb, group_id, kind, row or {})


def _plans_run_open(sb: Any, group_id: str, run_id: str, run: dict | None = None) -> bool:
    """True while the run of a modify group still exists and is not
    canceled (delete claims a run as canceled before it sweeps its files;
    a finished run is never canceled otherwise). False after canceling the
    group, so the job ends without writing a deliverable nobody owns. `run`
    is a row already read; without it the run is read now."""
    if run is None:
        rows = (
            sb.table("plans_reader_runs").select("id, status").eq("id", run_id).limit(1).execute()
        ).data or []
        run = rows[0] if rows else None
    if run is not None and run.get("status") != "canceled":
        return True
    sb.table("plans_reader_groups").update(
        {"status": "canceled", "finished_at": _now().isoformat()}
    ).eq("id", group_id).in_("status", list(_PLANS_ACTIVE_GROUP)).execute()
    return False


def _mark_modify_running(sb: Any, group: dict) -> None:
    sb.table("plans_reader_groups").update(
        {
            "status": "running",
            "error": None,
            "attempts": int(group.get("attempts") or 0) + 1,
            "started_at": group.get("started_at") or _now().isoformat(),
        }
    ).eq("id", group["id"]).in_("status", list(_PLANS_ACTIVE_GROUP)).execute()


def _has_pending_spec(sb: Any, group_id: str, key: str) -> bool:
    """True when the modify group holds a kept spec (best effort: an
    unreadable row counts as no spec, which keeps the failure permanent)."""
    try:
        rows = (
            sb.table("plans_reader_groups").select("output").eq("id", group_id).limit(1).execute()
        ).data or []
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("llm queue: reading the kept spec of group %s failed", group_id)
        return False
    output = rows[0].get("output") if rows and isinstance(rows[0].get("output"), dict) else {}
    return isinstance(output.get(key), dict)


def _saved_rerender_version(sb: Any, run_id: str, kind: str, base: int, since: str | None) -> dict | None:
    """The version an earlier attempt of this re-render already saved (one
    newer than the version the request saw, created since the group
    started), so a retry finishes the group instead of rendering twice. One
    change runs per run at a time (the modify group's unique index), so a
    newer version written since the start is this one."""
    if not since:
        return None
    rows = (
        sb.table("plans_reader_deliverables")
        .select("id, version, created_at")
        .eq("run_id", run_id)
        .eq("kind", kind)
        .gte("created_at", since)
        .order("version", desc=True)
        .limit(1)
        .execute()
    ).data or []
    if rows and int(rows[0].get("version") or 0) > int(base):
        return rows[0]
    return None


def _run_plans_rerender(sb: Any, group: dict, run: dict, kind: str, payload: dict, actor: "_JobActor") -> None:
    """A queued re-render (plans_reader_runs.rerender_deliverable): render
    the deliverable again as a new version with no model call. A renderer
    refusal is a RunRefused (ValueError, permanent, its sentence on the
    group); a storage hiccup is retried by the queue, and a retry that finds
    the version an earlier attempt saved finishes from it."""
    from app.services import plans_reader_runs

    base = int(payload.get("base_version") or 0)
    saved = _saved_rerender_version(sb, str(run["id"]), kind, base, group.get("started_at"))
    if saved:
        _finish_modify_group(sb, str(group["id"]), kind, saved, rerender=True)
        return
    _mark_modify_running(sb, group)
    if not _plans_run_open(sb, str(group["id"]), str(run["id"])):
        return
    row = plans_reader_runs.rerender_now(sb, run, kind, user=actor)
    _finish_modify_group(sb, str(group["id"]), kind, row or {}, rerender=True)


def _mark_plans_group(target: str, fields: dict) -> None:
    """Queue marks on a plans_reader_groups row: `pending` (a retry is
    scheduled) reads as `queued` there, `failed` is terminal. Fenced on the
    active statuses so a canceled or finished group never takes a mark. A
    terminal failure of a pipeline group then advances the run."""
    sb = get_supabase()
    status = fields.get("status")
    patch: dict[str, Any] = {"error": fields.get("error")}
    if status == "pending":
        patch["status"] = "queued"
    elif status == "failed":
        patch["status"] = "failed"
        patch["finished_at"] = _now().isoformat()
    elif status:
        patch["status"] = status
    rows = (
        sb.table("plans_reader_groups")
        .update(patch)
        .eq("id", target)
        .in_("status", list(_PLANS_ACTIVE_GROUP))
        .execute()
    ).data or []
    if status != "failed" or not rows:
        return
    group = rows[0]
    if group.get("kind") not in _PLANS_PIPELINE_KINDS or not group.get("run_id"):
        return
    try:
        from app.services import plans_reader_runs

        plans_reader_runs.advance(sb, str(group["run_id"]))
    except Exception:  # noqa: BLE001 - the mark itself landed; advance is idempotent and re-runs on the next group
        logger.exception("llm queue: advancing AI Estimating run %s after a failed group errored", group["run_id"])


def _plans_group_status(target: str) -> str | None:
    """The group's status for the monitor's retry guard. Every terminal
    status reads as done, so the AI monitor's Retry refuses: a canceled
    group belongs to a canceled run, and a failed one cannot be revived by a
    fresh job (the marks are fenced on queued/running and run_group skips a
    settled group, so the retry would report success and do nothing). The
    page's own paths are the retry: Resume or a new run for a pipeline
    group, asking again for a modify."""
    status = _row_status("plans_reader_groups", "id", target)
    return "done" if status in ("canceled", "failed") else status


def _mark_plans_message(target: str, fields: dict) -> None:
    """Queue marks on a pending assistant message (pending or failed),
    fenced so an answered message never flips back."""
    status = fields.get("status")
    patch: dict[str, Any] = {"error": fields.get("error")}
    if status:
        patch["status"] = status
    get_supabase().table("plans_reader_chat_messages").update(patch).eq("id", target).in_(
        "status", list(_PLANS_ACTIVE_MESSAGE)
    ).execute()


def _plans_message_status(target: str) -> str | None:
    """A failed answer reads as done for the same reason as a failed group
    (run_message leaves a failed message alone); the user sends the message
    again instead."""
    status = _row_status("plans_reader_chat_messages", "id", target)
    return "done" if status == "failed" else status


def _plans_concurrency(s: Settings) -> int:
    """AI Estimating jobs this process runs at once: PLANS_READER_CONCURRENCY
    (config validates 1 to 32). The fallback and the floor of 1 only guard
    a Settings built without validation (model_copy)."""
    try:
        value = int(s.plans_reader_concurrency)
    except (TypeError, ValueError):
        value = PLANS_DEFAULT_CONCURRENCY
    return max(1, value)


def _claim_plans(s: Settings, types: list[str]) -> list[dict]:
    """The AI Estimating pass (module docstring): skipped while
    PLANS_READER_ENABLED is off (queued jobs wait for the flag or an
    operator cancel, never spending on a feature that is switched off).
    Interactive jobs are claimed first; batch jobs never take the last slot
    when there is more than one, so a chat answer or modify waits at most
    for the next free slot, never behind a whole run."""
    if not getattr(s, "plans_reader_enabled", False):
        return []
    total = _plans_concurrency(s)
    running = [jt for jt in types if jt in PLANS_JOB_TYPES]
    free = total - len(running)
    if free <= 0:
        return []
    claimed = _claim(s, free, list(PLANS_INTERACTIVE_JOB_TYPES))
    free -= len(claimed)
    batch_slots = total - 1 if total > 1 else total
    running_batch = sum(1 for jt in running if jt in PLANS_BATCH_JOB_TYPES)
    batch_free = min(free, batch_slots - running_batch)
    if batch_free > 0:
        claimed.extend(_claim(s, batch_free, list(PLANS_BATCH_JOB_TYPES)))
    return claimed


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return getattr(exc, "code", None) == "23505" or "23505" in text or "duplicate key" in text


# ── Enqueue / lookup ─────────────────────────────────────────────────────


def enqueue(
    job_type: str,
    *,
    target_id: str,
    project_id: str | None = None,
    payload: dict | None = None,
    created_by: str | None = None,
    priority: int = 100,
    settings: Settings | None = None,
    raise_on_active: bool = False,
    next_attempt_at: datetime | str | None = None,
) -> dict:
    """Queue a job. If an active job already exists for the same target the
    existing job is returned instead (double-clicks and racing tabs collapse
    onto one run; the partial unique index is the authority). Callers that
    need to KNOW they collapsed (to warn the user) pass raise_on_active=True
    and catch JobAlreadyActive, which carries the existing job.
    `next_attempt_at` delays the first claim (the claim RPC only takes jobs
    due by now); None means due at once (the column default). AI Estimating
    uses it to pace group jobs against the account's tokens-per-minute
    limit."""
    s = settings or get_settings()
    sb = get_supabase()
    row = {
        "job_type": job_type,
        "feature": _FEATURE_BY_TYPE[job_type],
        "target_id": str(target_id),
        "project_id": project_id,
        "payload": payload or {},
        "created_by": created_by,
        "priority": priority,
        "max_attempts": 1 + len(s.llm_retry_delay_list),
    }
    if next_attempt_at is not None:
        row["next_attempt_at"] = (
            next_attempt_at.isoformat() if isinstance(next_attempt_at, datetime) else str(next_attempt_at)
        )
    try:
        resp = sb.table("llm_jobs").insert(row).execute()
        return resp.data[0]
    except Exception as exc:  # noqa: BLE001 - only the dup-key race is expected
        if not _is_unique_violation(exc):
            raise
        existing = active_job(job_type, target_id)
        if existing:
            if raise_on_active:
                raise JobAlreadyActive(existing) from exc
            return existing
        # The conflicting job went terminal between our insert and the lookup;
        # the partial-index slot is free again, so try once more.
        try:
            resp = sb.table("llm_jobs").insert(row).execute()
            return resp.data[0]
        except Exception as exc2:  # noqa: BLE001
            if _is_unique_violation(exc2):
                existing = active_job(job_type, target_id)
                if existing:
                    if raise_on_active:
                        raise JobAlreadyActive(existing) from exc2
                    return existing
            raise


def active_job(job_type: str, target_id: str) -> dict | None:
    """The queued/running job for this target, if any."""
    resp = (
        get_supabase()
        .table("llm_jobs")
        .select("*")
        .eq("job_type", job_type)
        .eq("target_id", str(target_id))
        .in_("status", list(_ACTIVE_STATUSES))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def latest_job(job_type: str, target_id: str) -> dict | None:
    resp = (
        get_supabase()
        .table("llm_jobs")
        .select("*")
        .eq("job_type", job_type)
        .eq("target_id", str(target_id))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def queue_position(job: dict) -> int | None:
    """1-based position among queued jobs; None once running/terminal."""
    if job.get("status") != "queued":
        return None
    resp = (
        get_supabase()
        .table("llm_jobs")
        .select("id, priority, created_at")
        .eq("status", "queued")
        .order("priority")
        .order("created_at")
        .limit(200)
        .execute()
    )
    for idx, row in enumerate(resp.data or []):
        if row["id"] == job["id"]:
            return idx + 1
    return None


def poll_info(job_type: str, target_id: str) -> dict | None:
    """Queue detail joined into the FE poll responses. None when this target
    has never been queued (legacy rows, queue-disabled mode)."""
    job = latest_job(job_type, target_id)
    if not job:
        return None
    queued = job["status"] == "queued"
    return {
        "state": job["status"],
        "attempts": job.get("attempts", 0),
        "max_attempts": job.get("max_attempts", 1),
        "position": queue_position(job) if queued else None,
        "next_attempt_at": job.get("next_attempt_at") if queued else None,
        "retrying": queued and (job.get("attempts") or 0) > 0,
        "error_kind": job.get("error_kind"),
        "last_error": job.get("last_error"),
    }


# ── Worker ───────────────────────────────────────────────────────────────


def _claim(s: Settings, max_jobs: int, job_types: list[str] | None = None) -> list[dict]:
    """Claim up to max_jobs due jobs through the claim_llm_jobs RPC. job_types
    narrows the claim to those types (the RPC's 4th parameter, migration
    0119); None claims any type, which only the tests and ad-hoc tooling use:
    the worker loop always names a pass."""
    params: dict[str, Any] = {
        "worker_id": _WORKER_TOKEN,
        "lease_seconds": s.llm_queue_lease_seconds,
        "max_jobs": max_jobs,
    }
    if job_types is not None:
        params["job_types"] = list(job_types)
    resp = get_supabase().rpc("claim_llm_jobs", params).execute()
    return resp.data or []


def _claim_tick(s: Settings, running_types: Iterable[str]) -> list[dict]:
    """One tick's claims, in passes with independent capacities: the LLM
    job types against llm_queue_worker_concurrency, then the sandbox type
    against rfp_ingest_sandbox_concurrency, the harvest family, and last the
    AI Estimating types against their own capacity (_claim_plans).
    `running_types` is the job type of every job still executing in this
    process. The sandbox pass is skipped while the feature flag is off (a
    disabled feature must never spawn a child; its queued jobs wait for the
    flag or an operator cancel)."""
    types = list(running_types)
    claimed: list[dict] = []
    llm_capacity = s.llm_queue_worker_concurrency - sum(
        1 for jt in types if jt in LLM_JOB_TYPES
    )
    if llm_capacity > 0:
        claimed.extend(_claim(s, llm_capacity, list(LLM_JOB_TYPES)))
    if s.rfp_ingest_enabled:
        rfp_capacity = s.rfp_ingest_sandbox_concurrency - sum(
            1 for jt in types if jt == JOB_RFP_INGEST
        )
        if rfp_capacity > 0:
            claimed.extend(_claim(s, rfp_capacity, [JOB_RFP_INGEST]))
    # Third pass: the harvests (docs/RFP_HARVEST.md 2.2 and
    # docs/RFP_NGEM_PORTAL.md 2.2), one job per worker by default so each
    # platform sees one human-paced session at a time. The Procore harvest,
    # the two portal jobs and the document promotion (docs/RFP_CREATE.md 5)
    # share the capacity; each family is claimed only while its slice is on
    # (queued jobs wait for the flag or a cancel). The promotion needs only
    # the master switch: it streams from storage, never from a platform.
    harvest_types: list[str] = []
    if s.rfp_ingest_enabled and s.rfp_harvest_enabled:
        harvest_types.append(JOB_RFP_HARVEST)
    if s.rfp_portal_any_enabled:  # NGEM or BuildingConnected (one shared scan job type)
        harvest_types.extend(PORTAL_JOB_TYPES)
    if s.rfp_ingest_enabled:
        harvest_types.extend(CREATE_JOB_TYPES)
    if harvest_types:
        harvest_capacity = s.rfp_harvest_concurrency - sum(
            1 for jt in types if jt in THIRD_PASS_JOB_TYPES
        )
        if harvest_capacity > 0:
            claimed.extend(_claim(s, harvest_capacity, harvest_types))
    claimed.extend(_claim_plans(s, types))
    return claimed


def _cas_job(sb: Any, job: dict, fields: dict) -> bool:
    """Update the job only if it is still in the state we think it is in.

    The fence is (id, status=running, claimed_by, attempts). attempts matters:
    claimed_by alone is per-PROCESS, so a stale attempt whose lease expired
    could otherwise clobber the SAME process's fresh reclaim of the job. The
    claim RPC increments attempts on every claim, making (claimed_by,
    attempts) unique per claim generation. Lost the race -> False."""
    resp = (
        sb.table("llm_jobs")
        .update(fields)
        .eq("id", job["id"])
        .eq("status", "running")
        .eq("claimed_by", job.get("claimed_by") or _WORKER_TOKEN)
        .eq("attempts", job.get("attempts") or 0)
        .execute()
    )
    return bool(resp.data)


def renew_lease(job: dict | None = None) -> bool:
    """Extend the lease of the job executing on this thread (or `job`) by
    another llm_queue_lease_seconds. Long runners (the sandbox monitor loop)
    call this every 30 s so the sweep never mistakes live work for a crash.

    Returns False when the CAS loses: the attempt is stale (the lease expired
    and the job was requeued and reclaimed, possibly by this very process
    with a higher attempts count), the job is no longer running, or it is not
    ours. The runner must treat False as "stop now": its writes would be
    fenced out anyway and a second worker may already own the run. Returns
    True with no write when no job is current (the BackgroundTasks fallback).

    Never raises. A transport failure is not a lost lease: the lease still
    has most of its window left, the next renewal retries, and the sweep is
    the backstop if the database stays unreachable for the whole lease.
    """
    job = job if job is not None else current_job.get()
    if job is None:
        return True
    try:
        lease = (_now() + timedelta(seconds=get_settings().llm_queue_lease_seconds)).isoformat()
        return _cas_job(get_supabase(), job, {"lease_expires_at": lease})
    except Exception:  # noqa: BLE001 - a DB hiccup must not abort hours of work
        logger.exception("llm queue: lease renewal for %s errored; keeping the lease", job["id"])
        return True


def requeue_self(job: dict | None = None) -> bool:
    """Hand the job executing on this thread (or `job`) back to the queue as
    interrupted, due immediately, so the next worker (or this one after a
    restart) resumes it. Used by the sandbox runner on shutdown: it kills its
    child, leaves the domain rows as they are (the run's own CAS marks carry
    the resume state) and returns normally; _execute then sees the job is no
    longer running and skips its success mark.

    The requeue is fenced like every other write (status running, our token,
    our attempt), so a job the sweep already requeued is left alone. Returns
    the CAS outcome; True with no write when no job is current.
    """
    job = job if job is not None else current_job.get()
    if job is None:
        return True
    ok = _cas_job(
        get_supabase(),
        job,
        {
            "status": "queued",
            "next_attempt_at": _now().isoformat(),
            "claimed_by": None,
            "lease_expires_at": None,
            "error_kind": "interrupted",
            "last_error": _INTERRUPTED_MESSAGE,
        },
    )
    if ok:
        # Mirror the row locally so _execute's success path knows the job
        # was handed on and does not log a spurious lost-lease warning.
        job["status"] = "queued"
        logger.warning("llm queue: job %s requeued itself (shutdown)", job["id"])
    return ok


def _mark_domain(spec: _JobSpec, job: dict, fields: dict) -> None:
    try:
        spec.mark(job["target_id"], fields)
    except Exception:  # noqa: BLE001 - a vanished domain row must not wedge the queue
        logger.exception(
            "llm queue: domain mark failed for %s %s", job["job_type"], job["target_id"]
        )


def _execute(job: dict) -> None:
    """Run one claimed job to a terminal or requeued state. Never raises:
    the failure handler is guarded end to end and the success mark's own
    database error is logged (the sweep requeues the job when its lease
    expires, and the rerun is idempotent)."""
    s = get_settings()
    spec = _spec(job["job_type"])
    sb = get_supabase()
    token = current_job.set(job)
    try:
        with llm_gate.tier(llm_gate.TIER_JOB, job_id=job["id"]):
            spec.run(job.get("payload") or {})
    except Exception as exc:  # noqa: BLE001 - classified below
        _handle_failure(sb, job, spec, exc, s)
        return
    finally:
        current_job.reset(token)
    if job.get("status") == "queued":
        # requeue_self() handed the job on (shutdown); nothing left to mark.
        return
    try:
        succeeded = _cas_job(
            sb,
            job,
            {
                "status": "succeeded",
                "finished_at": _now().isoformat(),
                "lease_expires_at": None,
                "claimed_by": None,
            },
        )
    except Exception:  # noqa: BLE001 - _execute never raises
        logger.exception("llm queue: success mark for %s errored", job["id"])
        return
    if not succeeded:
        # The lease expired mid-run and the sweep requeued the job. The work
        # itself completed (the domain row says done); the requeued run will
        # re-do it idempotently. Rare: lease >> real runtimes.
        logger.warning("llm queue: lost lease on %s before completion", job["id"])


_HANDLER_FAILED_MESSAGE = (
    "The run failed and its error could not be recorded. Run it again; if "
    "this keeps happening, contact your IT Director."
)


def _is_lease_lost(exc: BaseException) -> bool:
    """True for rfp_sandbox_runner.LeaseLost without importing the runner
    (it must stay optional here): match the class by name and module, or an
    explicit `llm_queue_lease_lost` attribute, and fall back to isinstance
    only when the runner module imports cleanly."""
    if getattr(exc, "llm_queue_lease_lost", False) is True:
        return True
    cls = type(exc)
    if cls.__name__ == "LeaseLost" and cls.__module__.endswith("rfp_sandbox_runner"):
        return True
    try:
        from app.services.rfp_sandbox_runner import LeaseLost
    except Exception:  # noqa: BLE001 - the runner is optional to this module
        return False
    return isinstance(exc, LeaseLost)


def _handle_failure(
    sb: Any, job: dict, spec: _JobSpec, exc: Exception, s: Settings
) -> None:
    """Requeue or terminally fail a job whose run raised. Never raises: an
    error inside the classification or the domain mark still reaches a
    terminal CAS, so a job can never be left running with a live lease and
    no worker (the sweep would eventually catch it, but with a misleading
    'interrupted' verdict instead of the real failure)."""
    if _is_lease_lost(exc):
        # The sweep already requeued the job and another claim owns it; our
        # fence would refuse every write anyway, and the domain row belongs
        # to the new owner. Leave everything alone.
        logger.warning(
            "llm queue: job %s (%s) lost its lease mid-run; no writes",
            job["id"],
            job["job_type"],
        )
        return
    try:
        _handle_failure_inner(sb, job, spec, exc, s)
    except Exception:  # noqa: BLE001 - the terminal CAS below is the last resort
        logger.exception(
            "llm queue: failure handling for %s (%s) errored; forcing terminal state",
            job["id"],
            job["job_type"],
        )
        try:
            # Fenced on status=running: a job the inner handler already
            # requeued or failed before erroring is left as it is, domain
            # row included (its new owner, or the earlier mark, governs it).
            forced = _cas_job(
                sb,
                job,
                {
                    "status": "failed",
                    "finished_at": _now().isoformat(),
                    "lease_expires_at": None,
                    "claimed_by": None,
                    "error_kind": llm_errors.KIND_UNKNOWN,
                    "last_error": _HANDLER_FAILED_MESSAGE,
                },
            )
            if forced:
                _mark_domain(
                    spec, job, {"status": "failed", "error": _HANDLER_FAILED_MESSAGE}
                )
        except Exception:  # noqa: BLE001 - nothing left to try; the sweep is the backstop
            logger.exception("llm queue: terminal CAS for %s errored", job["id"])


def _handle_failure_inner(
    sb: Any, job: dict, spec: _JobSpec, exc: Exception, s: Settings
) -> None:
    kind = llm_errors.classify(exc)
    model = spec.model_label(s)
    message = spec.error_message(exc, model)[:_ERROR_MAX_CHARS]
    attempt = job.get("attempts") or 1
    delays = s.llm_retry_delay_list

    if llm_errors.is_transient_kind(kind) and attempt <= len(delays):
        delay = delays[attempt - 1]
        requeued = _cas_job(
            sb,
            job,
            {
                "status": "queued",
                "next_attempt_at": (_now() + timedelta(seconds=delay)).isoformat(),
                "claimed_by": None,
                "lease_expires_at": None,
                "error_kind": kind,
                "last_error": message,
            },
        )
        if requeued:
            # Domain row back to pending: the FE keeps polling through the
            # retry window instead of showing a terminal failure.
            _mark_domain(spec, job, {"status": "pending", "error": None})
            logger.warning(
                "llm job %s (%s) attempt %s failed (%s), retrying in %ss",
                job["id"],
                job["job_type"],
                attempt,
                kind,
                delay,
                exc_info=exc,
            )
        return

    total = f" (failed after {attempt} attempts)" if attempt > 1 else ""
    failed = _cas_job(
        sb,
        job,
        {
            "status": "failed",
            "finished_at": _now().isoformat(),
            "lease_expires_at": None,
            "claimed_by": None,
            "error_kind": kind,
            "last_error": message,
        },
    )
    if failed:
        _mark_domain(
            spec, job, {"status": "failed", "error": (message + total)[:_ERROR_MAX_CHARS]}
        )
        logger.error(
            "llm job %s (%s) failed terminally after %s attempt(s): %s",
            job["id"],
            job["job_type"],
            attempt,
            kind,
            exc_info=exc,
        )
    else:
        # A superseding claim owns the job (our lease expired mid-run and the
        # sweep requeued it). Its outcome governs the domain row, not ours.
        logger.warning(
            "llm queue: job %s lost its lease before terminal fail; leaving domain row alone",
            job["id"],
        )


# ── Sweep: lease expiry + retention ──────────────────────────────────────

_last_prune: float = 0.0
_PRUNE_EVERY_SECONDS = 3600.0


def _sweep(s: Settings) -> None:
    """Requeue (or terminally fail) running jobs whose lease expired, then
    prune old ledger rows once an hour. Runs on every worker; all writes are
    conditional so double-execution is harmless."""
    sb = get_supabase()
    now_iso = _now().isoformat()
    expired = (
        sb.table("llm_jobs")
        .select("*")
        .eq("status", "running")
        .lt("lease_expires_at", now_iso)
        .limit(50)
        .execute()
    ).data or []
    for job in expired:
        spec = _spec(job["job_type"])
        attempt = job.get("attempts") or 1
        if attempt < (job.get("max_attempts") or 1):
            if _cas_job(
                sb,
                job,
                {
                    "status": "queued",
                    "next_attempt_at": now_iso,
                    "claimed_by": None,
                    "lease_expires_at": None,
                    "error_kind": "interrupted",
                    "last_error": _INTERRUPTED_MESSAGE,
                },
            ):
                _mark_domain(spec, job, {"status": "pending", "error": None})
                logger.warning("llm queue: requeued interrupted job %s", job["id"])
        else:
            if _cas_job(
                sb,
                job,
                {
                    "status": "failed",
                    "finished_at": now_iso,
                    "claimed_by": None,
                    "lease_expires_at": None,
                    "error_kind": "interrupted",
                    "last_error": _INTERRUPTED_FINAL_MESSAGE,
                },
            ):
                _mark_domain(
                    spec, job, {"status": "failed", "error": _INTERRUPTED_FINAL_MESSAGE}
                )
                logger.error("llm queue: job %s interrupted too many times", job["id"])

    global _last_prune
    if time.monotonic() - _last_prune >= _PRUNE_EVERY_SECONDS:
        _last_prune = time.monotonic()
        try:
            cutoff = (_now() - timedelta(days=s.llm_call_log_retention_days)).isoformat()
            sb.table("llm_call_log").delete().lt("created_at", cutoff).execute()
            sb.table("llm_jobs").delete().lt("created_at", cutoff).in_(
                "status", ["succeeded", "failed", "canceled"]
            ).execute()
        except Exception:  # noqa: BLE001 - one prune must not skip the other
            logger.exception("llm queue: ledger prune failed")
        if s.rfp_ingest_enabled:
            # Sandbox retention: derived outputs + quarantine copies of runs
            # past rfp_ingest_retention_days (rows and manifests are kept).
            try:
                from app.services import rfp_ingest

                rfp_ingest.prune_expired()
            except Exception:  # noqa: BLE001 - retention must never wedge the sweep
                logger.exception("llm queue: rfp ingest retention prune failed")


# ── Monitor actions ──────────────────────────────────────────────────────


def requeue_terminal(job: dict, created_by: str | None) -> dict:
    """Fresh job for a failed/canceled one (AI monitor "Retry"). The old row
    stays as history; the domain row goes back to pending so the FE polls.

    Guarded against stale retries: if the target has since completed (a later
    run or a manual override produced a result), or a newer job supersedes
    this one, retrying would clobber current data and is refused."""
    if job.get("status") not in ("failed", "canceled"):
        raise ValueError("Only failed or canceled jobs can be retried.")
    spec = _spec(job["job_type"])
    newest = latest_job(job["job_type"], job["target_id"])
    if newest and newest.get("id") != job.get("id"):
        raise ValueError(
            "A newer run exists for this item; retry that one instead."
        )
    domain_status = spec.current_status(job["target_id"])
    if domain_status in ("done", "not_found"):
        raise ValueError(
            "This item has since completed. Re-run it from the project page instead."
        )
    fresh = enqueue(
        job["job_type"],
        target_id=job["target_id"],
        project_id=job.get("project_id"),
        payload=job.get("payload") or {},
        created_by=created_by,
        priority=job.get("priority") or 100,
    )
    if fresh["id"] != job["id"]:
        _mark_domain(spec, job, {"status": "pending", "error": None})
    return fresh


def cancel(job_id: str) -> dict | None:
    """Cancel a QUEUED job (AI monitor). Running jobs cannot be canceled
    safely (the model call is already in flight), EXCEPT zombies whose lease
    already expired: their worker is gone, so the dev can clear them instead
    of waiting for the sweep. Returns the updated row, or None when the job
    was not cancelable."""
    sb = get_supabase()
    fields = {
        "status": "canceled",
        "finished_at": _now().isoformat(),
        "claimed_by": None,
        "lease_expires_at": None,
        "last_error": _CANCELED_MESSAGE,
    }
    resp = (
        sb.table("llm_jobs").update(fields).eq("id", job_id).eq("status", "queued").execute()
    )
    rows = resp.data or []
    if not rows:
        resp = (
            sb.table("llm_jobs")
            .update(fields)
            .eq("id", job_id)
            .eq("status", "running")
            .lt("lease_expires_at", _now().isoformat())
            .execute()
        )
        rows = resp.data or []
    if not rows:
        return None
    job = rows[0]
    _mark_domain(
        _spec(job["job_type"]), job, {"status": "failed", "error": _CANCELED_MESSAGE}
    )
    return job


_RESTORED_MESSAGE = "The project was deleted while this AI job was in flight. Run it again."


def release_restored_project_jobs(project_id: str) -> int:
    """After a project restore (docs/PROJECT_DELETE.md 4.3): the snapshot
    brings back jobs that were queued or running at delete time, but their
    worker and lease are long gone. Fail them (and their pending/running
    domain rows) so nothing reruns on its own and users can start again.
    Returns how many jobs were released."""
    sb = get_supabase()
    stranded = (
        sb.table("llm_jobs")
        .select("*")
        .eq("project_id", project_id)
        .in_("status", list(_ACTIVE_STATUSES))
        .execute()
    ).data or []
    released = 0
    for job in stranded:
        resp = (
            sb.table("llm_jobs")
            .update(
                {
                    "status": "failed",
                    "finished_at": _now().isoformat(),
                    "claimed_by": None,
                    "lease_expires_at": None,
                    "error_kind": "interrupted",
                    "last_error": _RESTORED_MESSAGE,
                }
            )
            .eq("id", job["id"])
            .in_("status", list(_ACTIVE_STATUSES))
            .execute()
        )
        if resp.data:
            released += 1
            try:
                spec = _spec(job["job_type"])
            except KeyError:
                logger.warning("llm queue: restored job %s has unknown type %s",
                               job["id"], job["job_type"])
                continue
            _mark_domain(spec, job, {"status": "failed", "error": _RESTORED_MESSAGE})
    return released


_DISABLED_MESSAGE = "The AI queue was disabled. Run it again."


def release_stranded_for_disabled_mode() -> None:
    """One-shot cleanup when LLM_QUEUE_ENABLED is turned off: with no worker
    loop, queued/running jobs would sit forever and their pending/running
    domain rows would block new starts. Fail them all so users can re-run
    inline. Safe to run from every worker (each update is conditional)."""
    sb = get_supabase()
    stranded = (
        sb.table("llm_jobs")
        .select("*")
        .in_("status", list(_ACTIVE_STATUSES))
        .limit(500)
        .execute()
    ).data or []
    for job in stranded:
        resp = (
            sb.table("llm_jobs")
            .update(
                {
                    "status": "failed",
                    "finished_at": _now().isoformat(),
                    "claimed_by": None,
                    "lease_expires_at": None,
                    "error_kind": "interrupted",
                    "last_error": _DISABLED_MESSAGE,
                }
            )
            .eq("id", job["id"])
            .in_("status", list(_ACTIVE_STATUSES))
            .execute()
        )
        if resp.data:
            _mark_domain(
                _spec(job["job_type"]), job, {"status": "failed", "error": _DISABLED_MESSAGE}
            )
            logger.warning("llm queue disabled: released stranded job %s", job["id"])


# ── Loop ─────────────────────────────────────────────────────────────────


async def worker_loop() -> None:
    """Claim and run due jobs forever. Runs in every uvicorn worker; the
    claim RPC keeps the workers disjoint, so more workers = more throughput.
    Mirrors the polling_loop convention (sync work via asyncio.to_thread,
    a tick can never kill the loop)."""
    logger.info("llm queue worker started (token %s)", _WORKER_TOKEN[:8])
    # task -> job_type, so each claim pass can count only its own kind.
    running: dict[asyncio.Task, str] = {}
    while True:
        s = get_settings()
        # The sweep and the claim are separate so a sweep that fails (one
        # stale pooled connection after the host slept, a transient 5xx)
        # does not also skip this tick's claims and leave due jobs waiting.
        try:
            await asyncio.to_thread(_sweep, s)
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("llm queue sweep failed")
        try:
            for job in await asyncio.to_thread(_claim_tick, s, list(running.values())):
                task = asyncio.create_task(asyncio.to_thread(_execute, job))
                running[task] = job["job_type"]
                task.add_done_callback(lambda t: running.pop(t, None))
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("llm queue tick failed")
        await asyncio.sleep(max(0.5, float(s.llm_queue_poll_interval_seconds)))

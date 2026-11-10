"""Browser agent job queue: enqueue, claim, and the BDR sweeper (SPEC.md 3.3, 3.4).

The queue is `browser_agent_jobs` (0157). Claims go through the
SECURITY INVOKER `claim_browser_agent_jobs` RPC (per-type advisory lock,
exact max_concurrency, SKIP LOCKED, epoch + 1, charged only on a retry).
Every later write by the worker is fenced in its RPC on (owner, epoch, boot,
status). This module adds the Python halves:

  enqueue      one job per (agent_type, dedupe_key); a duplicate returns the
               existing row instead of a second job.
  claim        trims the worker's advertised capacity to its free slots, runs
               the RPC, then `prepare(job)` per job (registry): a job that
               needs no browser is finished in the same fenced write, a
               prepare that raises releases the lease uncharged.
  sweep_once   the sweeper (every BROWSER_AGENT_SWEEP_SECONDS, both uvicorn
               workers). Every write is compare-and-set, so two sweepers
               racing are harmless:
                 1. reclaim expired running leases whose worker went silent
                    or that were taken under a boot the worker no longer
                    runs (uncharged 'interrupted'; fails
                    interrupted_repeatedly past max_interruptions), never
                    during startup grace;
                 2. park waiting_help jobs whose lease lapsed (help stays open);
                 3. reconcile applying jobs from their pm_rfi_sync_runs row;
                 4. fail queued retries already at max_attempts (crashed_repeatedly);
                 5. expire queued jobs past expires_at;
                 6. cancel jobs whose cancel was requested and whose lease lapsed;
                 7. spend alerts at 50% and 100% of the daily cap (once per window);
                 8. worker offline bell, then email (once each, re-armed when a
                    worker is seen again);
                 9. steps registered by later phases (help reminders and
                    expiry, secret purge, unreported logins, signed-query
                    stripping, contributions) through `register_sweep_step`.

A BDR outage alone never kills a live job: the reclaim needs the lease
expired AND the worker silent for a lease AND this process up for longer
than a lease plus 15 s (a restart must first let heartbeats arrive).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from postgrest.exceptions import APIError
from pydantic import ValidationError

from app.core import supabase_client
from app.core.config import get_settings
from app.core.roles import Role
from app.services import notifications
from app.services.browser_agents import protocol, registry

logger = logging.getLogger(__name__)

UTC = timezone.utc
JOBS = "browser_agent_jobs"
JOB_COLUMNS = "*"
STARTUP_GRACE_EXTRA_SECONDS = 15
PREPARE_RETRY_SECONDS = 60          # a prepare that raised is retried after this
SWEEP_BATCH = 200                   # rows per sweep step per tick
# Resolved help whose unread helper messages a resumed job still receives.
RESUMABLE_RESOLUTIONS = ("retry", "auto_resolved")
SETTINGS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "config_version": 1,
    "daily_cost_cap_usd": protocol.DAILY_COST_CAP_USD_DEFAULT,
    "per_job_cost_cap_usd": protocol.PER_JOB_COST_CAP_USD_DEFAULT,
    "help_hold_seconds": 900,
    "worker_offline_alert_minutes": 60,
    "worker_offline_email_hours": 4,
    "job_ttl_hours": 168,
    "desired_worker_version": None,
}

# monotonic() at import; reset by polling_loop() when the sweeper starts.
_started_monotonic = time.monotonic()


def _sb():
    return supabase_client.get_supabase()


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """A PostgREST-safe UTC literal (Z suffix: no '+' to URL-decode into a space)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "code", None) == "23505" or "23505" in str(exc) or "duplicate key" in str(exc)


def load_settings() -> dict[str, Any]:
    """The one browser_agent_settings row (defaults when the read fails or
    the row is missing, so a sweeper tick never dies on it)."""
    try:
        rows = _sb().table("browser_agent_settings").select("*").eq("id", 1).limit(1).execute().data or []
    except Exception:  # noqa: BLE001 - settings are advisory for the sweeper
        logger.exception("browser agents: settings read failed; using defaults")
        rows = []
    return {**SETTINGS_DEFAULTS, **(rows[0] if rows else {})}


def log_step(job_id: str, epoch: int, kind: str, summary: str, *, worker_id: str | None = None,
             actor: str = "system", result: dict[str, Any] | None = None) -> None:
    """One BDR-written ledger row (append-only; seq assigned by trigger).
    Never raises: a missing audit row must not undo the state change it
    describes, which already committed."""
    try:
        _sb().rpc("browser_agent_log_step", {
            "p_job": job_id, "p_epoch": int(epoch or 0), "p_worker": worker_id, "p_kind": kind,
            "p_actor": actor, "p_summary": summary[: protocol.STEP_SUMMARY_MAX], "p_result": result,
        }).execute()
    except Exception:  # noqa: BLE001
        logger.exception("browser agents: step %s for job %s not written", kind, job_id)


def notify_it_admin(type_: str, message: str, metadata: dict[str, Any], *,
                    email: bool = False, project_id: str | None = None) -> None:
    """A C3 notice to IT Admin (protocol.NOTIFICATION_TYPES). Best effort."""
    assert type_ in protocol.NOTIFICATION_TYPES, type_
    try:
        notifications.notify_role(Role.IT_ADMIN, project_id, type_, message,
                                  mirror_email=email, metadata=metadata)
    except Exception:  # noqa: BLE001 - a notice never breaks the state change
        logger.exception("browser agents: %s notice failed", type_)


def notify_job_failed(job: dict[str, Any], error_kind: str, why: str) -> None:
    if job.get("principal_kind", "org") != "org":
        owner = job.get("principal_user_id")
        if owner:
            notifications.notify_user(str(owner), None, "browser_agent.job_failed",
                                      "Your browser task did not finish.",
                                      metadata={"job_id": str(job["id"])})
        return
    notify_it_admin(
        "browser_agent.job_failed",
        f"Browser agent job failed ({error_kind}): {why}",
        {"job_id": str(job["id"]), "error_kind": error_kind},
        project_id=job.get("project_id"),
    )


# ── Enqueue ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnqueueResult:
    job: dict[str, Any]
    created: bool


def enqueue(
    agent_type: str,
    dedupe_key: str,
    input: dict[str, Any],
    *,
    priority: int | None = None,
    project_id: str | None = None,
    source_email_id: str | None = None,
    platform: str | None = None,
    created_by: str | None = None,
    ttl_hours: int | None = None,
    next_attempt_at: datetime | None = None,
) -> EnqueueResult:
    """Queue one job. The (agent_type, dedupe_key) unique index makes this
    idempotent: a second call returns the existing job (any status) with
    created=False, so a re-scanned email never queues the work twice.

    Raises registry.UnknownAgentType for an unregistered type and
    ValueError for a seed that does not fit the type's input model; both are
    programming errors at the call site, never a worker's doing."""
    spec = registry.get_type(agent_type)
    if spec.principal_kind != "org":
        raise ValueError("User jobs require an assistant approval")
    if not dedupe_key or len(dedupe_key) > 300:
        raise ValueError("dedupe_key must be 1 to 300 characters")
    try:
        seed = spec.validate_input(input).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(f"invalid {agent_type} seed: {exc.errors()[0].get('msg')}") from None
    settings_row = load_settings()
    sb = _sb()
    type_rows = sb.table("browser_agent_types").select("priority_default").eq("key", agent_type).limit(1).execute().data or []
    ttl = ttl_hours or int(settings_row.get("job_ttl_hours") or 168)
    now = now_utc()
    row = {
        "agent_type": agent_type,
        "dedupe_key": dedupe_key,
        "priority": priority if priority is not None else int((type_rows[0] if type_rows else {}).get("priority_default") or 100),
        "platform": platform or spec.platform,
        "project_id": project_id,
        "source_email_id": source_email_id,
        "input": seed,
        "created_by": created_by,
        "expires_at": iso(now + timedelta(hours=ttl)),
        "next_attempt_at": iso(next_attempt_at) if next_attempt_at else None,
    }
    try:
        inserted = (sb.table(JOBS).insert(row).execute()).data or []
    except APIError as exc:
        if not _is_unique_violation(exc):
            raise
        inserted = []
    except Exception as exc:  # noqa: BLE001 - the fake and older clients raise plain errors
        if not _is_unique_violation(exc):
            raise
        inserted = []
    if inserted:
        job = inserted[0]
        log_step(str(job["id"]), 0, "job.enqueued", f"Queued ({agent_type})", actor="bdr",
                 result={"dedupe_key": dedupe_key, "priority": job.get("priority")})
        return EnqueueResult(job=job, created=True)
    existing = (
        sb.table(JOBS).select(JOB_COLUMNS).eq("agent_type", agent_type).eq("dedupe_key", dedupe_key)
        .limit(1).execute()
    ).data or []
    if not existing:
        raise RuntimeError(f"enqueue of {agent_type}/{dedupe_key} neither inserted nor found")
    return EnqueueResult(job=existing[0], created=False)


# ── Claim ────────────────────────────────────────────────────────────────────


def _held_leases(worker_id: str) -> int:
    rows = (
        _sb().table(JOBS).select("id").eq("lease_owner", worker_id)
        .in_("status", ["running", "waiting_help"]).execute()
    ).data or []
    return len(rows)


def trim_capacity(capacity: dict[str, int], *, max_slots: int, held: int) -> dict[str, int]:
    """The worker's advertised free slots, cut to registered types and to
    the admin's max_slots minus the leases it already holds, handed out
    round-robin so one type cannot take every slot."""
    want = {k: max(0, min(int(v), 8)) for k, v in sorted(capacity.items()) if registry.has_type(k)}
    budget = max(0, max_slots - held)
    out = {k: 0 for k in want}
    while budget > 0 and any(out[k] < want[k] for k in want):
        for k in want:
            if budget > 0 and out[k] < want[k]:
                out[k] += 1
                budget -= 1
    return {k: v for k, v in out.items() if v > 0}


def unconsumed_help(job_ids: list[str]) -> dict[str, list[protocol.UnconsumedHelp]]:
    """Per job: the help requests it waits on, with every helper and system
    message it has not consumed yet. Open and answered requests, plus ones
    resolved by Retry now or a successful login within help_expire_hours:
    a helper often writes (and uploads a file) and then presses Retry, and
    that answer must still reach the resumed job. Secret messages come as a
    secret_ref only (the value is fetched once via /secrets/{id}/use)."""
    if not job_ids:
        return {}
    sb = _sb()
    waiters = sb.table("browser_agent_help_waiters").select("help_id, job_id").in_("job_id", job_ids).execute().data or []
    if not waiters:
        return {}
    help_ids = sorted({str(w["help_id"]) for w in waiters})
    hours = int(load_settings().get("help_expire_hours") or 48)
    recent = now_utc() - timedelta(hours=hours)
    helps = {
        str(h["id"]): h for h in (
            sb.table("browser_agent_help_requests").select("id, status, resolution, resolved_at")
            .in_("id", help_ids).execute().data or []
        )
        if h.get("status") in ("open", "answered") or (
            h.get("status") == "resolved" and h.get("resolution") in RESUMABLE_RESOLUTIONS
            and (parse_ts(h.get("resolved_at")) or recent) >= recent
        )
    }
    if not helps:
        return {}
    messages = (
        sb.table("browser_agent_help_messages")
        .select("id, help_id, author_kind, author_label, action, body, is_secret, "
                "secret_consumed_at, artifact_ids, created_at")
        .in_("help_id", list(helps)).in_("author_kind", ["helper", "system"]).order("id").execute()
    ).data or []
    reads = sb.table("browser_agent_help_reads").select("job_id, message_id").in_("job_id", job_ids).execute().data or []
    consumed = {(str(r["job_id"]), int(r["message_id"])) for r in reads}
    artifact_ids = sorted({str(a) for m in messages for a in (m.get("artifact_ids") or [])})
    files: dict[str, dict[str, Any]] = {}
    if artifact_ids:
        for a in sb.table("browser_agent_artifacts").select(
            "id, filename, size_bytes, mime_type, attachable"
        ).in_("id", artifact_ids).execute().data or []:
            files[str(a["id"])] = a
    out: dict[str, list[protocol.UnconsumedHelp]] = {}
    for w in waiters:
        jid, hid = str(w["job_id"]), str(w["help_id"])
        if hid not in helps:
            continue
        msgs = []
        for m in messages:
            if str(m["help_id"]) != hid or (jid, int(m["id"])) in consumed:
                continue
            msgs.append(protocol.WorkerHelpMessage(
                id=int(m["id"]),
                author_kind=m["author_kind"],
                author_label=m.get("author_label"),
                action=m.get("action") or "message",
                body=None if m.get("is_secret") else m.get("body"),
                secret_ref=(protocol.SecretRef(message_id=int(m["id"]),
                                               consumed=m.get("secret_consumed_at") is not None)
                            if m.get("is_secret") else None),
                files=[protocol.HelpFile(
                    artifact_id=files[str(a)]["id"], filename=files[str(a)].get("filename") or "file",
                    size_bytes=int(files[str(a)].get("size_bytes") or 0),
                    mime_type=files[str(a)].get("mime_type") or "application/octet-stream",
                    attachable=bool(files[str(a)].get("attachable")),
                ) for a in (m.get("artifact_ids") or []) if str(a) in files],
                created_at=m["created_at"],
            ))
        out.setdefault(jid, []).append(
            protocol.UnconsumedHelp(help_id=hid, status=helps[hid]["status"], messages=msgs)
        )
    return out


def issued_turn_counts(job_ids: list[str]) -> dict[str, int]:
    """Per job: the highest issued turn_no. jobs.turn_count is advanced in a
    separate write after a turn is issued and can lag it (a failed write, a
    process death); a resumed worker must start after the last issued turn
    or every attempt is refused 'issued under an earlier lease'."""
    if not job_ids:
        return {}
    rows = (
        _sb().table("browser_agent_turns").select("job_id, turn_no").in_("job_id", job_ids)
        .eq("status", "issued").execute()
    ).data or []
    out: dict[str, int] = {}
    for r in rows:
        jid = str(r["job_id"])
        out[jid] = max(out.get(jid, 0), int(r.get("turn_no") or 0))
    return out


def _release_after_prepare_error(row: dict[str, Any], worker_id: str, boot_id: str) -> None:
    """A prepare that raised must not cost the job an attempt: hand the
    lease back as 'interrupted' (fenced; fails past max_interruptions)."""
    try:
        if row.get("principal_kind") == "user":
            from app.assistant import repo
            repo.browser_rpc(row["principal_user_id"], "assistant_browser_job_requeue", {
                "p_job": str(row["id"]), "p_worker": worker_id, "p_boot": boot_id,
                "p_epoch": int(row["lease_epoch"]),
            })
            return
        _sb().rpc("browser_agent_requeue", {
            "p_job": str(row["id"]), "p_worker": worker_id, "p_boot": boot_id,
            "p_epoch": int(row["lease_epoch"]), "p_reason": "interrupted",
            "p_retry_at": iso(now_utc() + timedelta(seconds=PREPARE_RETRY_SECONDS)),
            "p_service_stop": False,
        }).execute()
    except Exception:  # noqa: BLE001 - the lease expires on its own; the sweeper reclaims it
        logger.exception("browser agents: releasing job %s after a prepare error failed", row.get("id"))


def _finish_prepared(row: dict[str, Any], worker_id: str, boot_id: str, result: registry.PrepareResult) -> None:
    if row.get("principal_kind") == "user":
        from app.assistant import repo
        repo.browser_rpc(row["principal_user_id"], "assistant_browser_job_finish", {
            "p_job": str(row["id"]), "p_worker": worker_id, "p_boot": boot_id,
            "p_epoch": int(row["lease_epoch"]), "p_status": result.status,
            "p_result_record": None, "p_error_code": "authority_unavailable",
        })
        return
    data = _sb().rpc("browser_agent_finish", {
        "p_job": str(row["id"]), "p_worker": worker_id, "p_boot": boot_id,
        "p_epoch": int(row["lease_epoch"]), "p_status": result.status,
        "p_outcome": result.outcome,
        # canceled carries error_kind 'canceled', like the sweeper's cancel step
        "p_error_kind": result.error_kind or ("canceled" if result.status == "canceled" else None),
        "p_error": result.summary if result.status == "failed" else None,
        "p_result": {"summary": result.summary, "prepared": True},
    }).execute().data
    if result.status == "failed" and data and result.notify:
        notify_job_failed(row, result.error_kind or "other", result.summary)


def _claimed_input(row: dict[str, Any]) -> dict[str, Any]:
    """The input a worker sees: the org seed as stored; the retired personal
    read type its three seed fields; a v2 personal job exactly
    UserBrowserInput (prepare() already validated it)."""
    data = row.get("input") or {}
    if row.get("principal_kind") != "user":
        return data
    if row.get("agent_type") == protocol.USER_BROWSER_TYPE:
        from app.services.browser_agents.types.user_browser import claimed_input
        return claimed_input(row)
    return {key: data.get(key) for key in ("service", "url", "objective")}


def claim(worker_id: str, boot_id: str, capacity: dict[str, int], *, max_slots: int, principal_kind: str = "org") -> list[protocol.ClaimedJob]:
    """POST /claim. RPC errors (stale_boot, revoked) propagate to the caller
    as APIError; the router maps them. Draining or a kill switch: []."""
    from app.core.agent_auth import rpc

    # Personal claims serve only the v2 type (0192 claim rewrite selects
    # user_browser only), so its gate is the v2 enabled() (plan 3.4).
    from app.services.browser_agents.types.user_browser import enabled as user_enabled
    if principal_kind not in ("org", "user") or (principal_kind == "user" and not user_enabled()):
        return []
    capacity = {key: value for key, value in capacity.items()
                if registry.has_type(key) and registry.get_type(key).principal_kind == principal_kind}
    want = trim_capacity(capacity, max_slots=max_slots, held=_held_leases(worker_id))
    if not want:
        return []
    rows = rpc("claim_browser_agent_jobs", {
        "p_worker": worker_id, "p_boot": boot_id, "p_capacity": want,
        "p_lease_seconds": get_settings().browser_agent_lease_seconds,
    }) or []
    ready: list[dict[str, Any]] = []
    for row in rows:
        if row.get("cancel_requested_at"):
            # The claim RPC skips cancel-requested jobs (0158); this stays
            # as a backstop for a cancel that landed during the claim.
            try:
                _finish_prepared(row, worker_id, boot_id, registry.PrepareResult(
                    action="finish", status="canceled", summary="Canceled before it started"))
            except Exception:  # noqa: BLE001 - the sweeper's cancel step closes it otherwise
                logger.exception("browser agents: closing canceled job %s failed", row.get("id"))
            continue
        try:
            spec = registry.get_type(row["agent_type"])
            if row.get("principal_kind", "org") != principal_kind or spec.principal_kind != principal_kind:
                raise ValueError("Job principal does not match its type")
            result = spec.prepare(row)
        except Exception:  # noqa: BLE001 - any prepare failure releases uncharged
            logger.exception("browser agents: prepare failed for job %s", row.get("id"))
            _release_after_prepare_error(row, worker_id, boot_id)
            continue
        if result.action == "finish":
            try:
                _finish_prepared(row, worker_id, boot_id, result)
            except Exception:  # noqa: BLE001
                logger.exception("browser agents: finishing prepared job %s failed", row.get("id"))
                _release_after_prepare_error(row, worker_id, boot_id)
            continue
        ready.append(row)
    org_ids = [str(r["id"]) for r in ready if r.get("principal_kind", "org") == "org"]
    helps = unconsumed_help(org_ids) if org_ids else {}
    issued = issued_turn_counts(org_ids) if org_ids else {}
    return [
        protocol.ClaimedJob(
            job_id=r["id"],
            agent_type=r["agent_type"],
            principal_kind=r.get("principal_kind", "org"),
            principal_user_id=r.get("principal_user_id"),
            epoch=int(r["lease_epoch"]),
            lease_expires_at=r["lease_expires_at"],
            input=_claimed_input(r),
            checkpoint={} if r.get("principal_kind") == "user" else r.get("checkpoint") or {},
            checkpoint_seq=int(r.get("checkpoint_seq") or 0),
            attempts=int(r.get("attempts") or 0),
            last_resume_reason=r.get("last_resume_reason"),
            turn_count=max(int(r.get("turn_count") or 0), issued.get(str(r["id"]), 0)),
            unconsumed_help=helps.get(str(r["id"]), []),
        )
        for r in ready
    ]


# ── Sweeper ─────────────────────────────────────────────────────────────────

SweepStep = Callable[[datetime, dict[str, Any]], None]
_EXTRA_STEPS: list[tuple[str, SweepStep]] = []


def register_sweep_step(name: str, fn: SweepStep) -> None:
    """Later phases plug their periodic work in here (help reminders and
    expiry, secret purge, unreported logins, signed-query stripping,
    contributions). `fn(now, settings_row)` must be compare-and-set safe:
    it runs in both uvicorn workers. Re-registering a name replaces it."""
    for i, (existing, _) in enumerate(_EXTRA_STEPS):
        if existing == name:
            _EXTRA_STEPS[i] = (name, fn)
            return
    _EXTRA_STEPS.append((name, fn))


def in_startup_grace(lease_seconds: int | None = None) -> bool:
    lease = lease_seconds or get_settings().browser_agent_lease_seconds
    return time.monotonic() - _started_monotonic < lease + STARTUP_GRACE_EXTRA_SECONDS


def _types_by_key() -> dict[str, dict[str, Any]]:
    rows = _sb().table("browser_agent_types").select(
        "key, max_attempts, max_interruptions"
    ).execute().data or []
    return {r["key"]: r for r in rows}


def _workers_state(worker_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Per worker: last_seen_at (datetime) and its current boot_id."""
    if not worker_ids:
        return {}
    rows = (
        _sb().table("browser_agent_workers").select("id, last_seen_at, boot_id").in_("id", worker_ids).execute()
    ).data or []
    return {str(r["id"]): {"last_seen_at": parse_ts(r.get("last_seen_at")), "boot_id": r.get("boot_id")}
            for r in rows}


def _stale_boot(job: dict[str, Any], state: dict[str, Any] | None) -> bool:
    """The lease was taken under a boot the worker no longer runs (a claim
    that committed after a newer /hello): its heartbeats can never renew it."""
    if state is None or not job.get("lease_boot_id") or not state.get("boot_id"):
        return False
    return str(job["lease_boot_id"]) != str(state["boot_id"])


def _cas_job(job: dict[str, Any], patch: dict[str, Any], *, status: str | list[str],
             expired_before: datetime | None = None) -> bool:
    """Compare-and-set on what the sweeper read: id, status, lease owner and
    epoch. A rival sweeper in between makes it a no-op. A heartbeat renews
    without touching those columns, so the lease steps also pass
    `expired_before`: a lease renewed in between is no longer expired and
    the write matches nothing."""
    if job.get("principal_kind", "org") != "org":
        return False
    q = _sb().table(JOBS).update(patch).eq("principal_kind", "org").eq("id", job["id"]).eq("lease_epoch", job["lease_epoch"])
    q = q.in_("status", status) if isinstance(status, list) else q.eq("status", status)
    q = q.eq("lease_owner", job["lease_owner"]) if job.get("lease_owner") else q.is_("lease_owner", "null")
    if expired_before is not None:
        q = q.lt("lease_expires_at", iso(expired_before))
    return bool((q.execute()).data)


_LEASE_CLEARED = {"lease_owner": None, "lease_boot_id": None, "lease_expires_at": None, "hold_until": None}


def reclaim_expired(now: datetime, settings_row: dict[str, Any]) -> int:
    """Step 1: running jobs whose lease expired while their worker went
    silent, or that were leased under a boot the worker no longer runs
    (never renewable, whatever last_seen_at says). Uncharged 'interrupted'
    with the epoch bumped (a late heartbeat from the old holder then finds
    nothing to renew)."""
    lease = get_settings().browser_agent_lease_seconds
    if in_startup_grace(lease):
        return 0
    jobs = (
        _sb().table(JOBS).select(JOB_COLUMNS).eq("principal_kind", "org").eq("status", "running").not_.is_("lease_owner", "null")
        .lt("lease_expires_at", iso(now)).limit(SWEEP_BATCH).execute()
    ).data or []
    if not jobs:
        return 0
    workers = _workers_state(sorted({str(j["lease_owner"]) for j in jobs}))
    types = _types_by_key()
    silent_before = now - timedelta(seconds=lease)
    done = 0
    for job in jobs:
        state = workers.get(str(job["lease_owner"]))
        last = (state or {}).get("last_seen_at")
        if last is not None and last >= silent_before and not _stale_boot(job, state):
            continue        # the worker is alive: its next heartbeat renews by epoch
        cap = int((types.get(job["agent_type"]) or {}).get("max_interruptions") or 20)
        epoch = int(job["lease_epoch"])
        count = int(job.get("interrupted_count") or 0) + 1
        if count > cap:
            ok = _cas_job(job, {**_LEASE_CLEARED, "status": "failed", "lease_epoch": epoch + 1,
                                "interrupted_count": count, "error_kind": "interrupted_repeatedly",
                                "error": "Interrupted too many times", "finished_at": iso(now)},
                          status="running", expired_before=now)
            if ok:
                log_step(str(job["id"]), epoch, "job.failed", "Failed: interrupted too many times "
                         "(the worker went silent)", worker_id=str(job["lease_owner"]))
                notify_job_failed(job, "interrupted_repeatedly", "the worker went silent too many times")
        else:
            ok = _cas_job(job, {**_LEASE_CLEARED, "status": "queued", "resume_reason": "interrupted",
                                "lease_epoch": epoch + 1, "interrupted_count": count},
                          status="running", expired_before=now)
            if ok:
                log_step(str(job["id"]), epoch, "job.reclaimed",
                         "Requeued: the lease expired and the worker went silent",
                         worker_id=str(job["lease_owner"]))
        done += int(ok)
    return done


def park_lapsed_holds(now: datetime, settings_row: dict[str, Any]) -> int:
    """Step 2: waiting_help jobs whose lease lapsed, once the hold is over
    or the worker is gone. The lease is released uncharged; the help request
    stays open and an answer requeues the job (help phase)."""
    lease = get_settings().browser_agent_lease_seconds
    if in_startup_grace(lease):
        return 0
    jobs = (
        _sb().table(JOBS).select(JOB_COLUMNS).eq("principal_kind", "org").eq("status", "waiting_help").not_.is_("lease_owner", "null")
        .lt("lease_expires_at", iso(now)).limit(SWEEP_BATCH).execute()
    ).data or []
    if not jobs:
        return 0
    workers = _workers_state(sorted({str(j["lease_owner"]) for j in jobs}))
    silent_before = now - timedelta(seconds=lease)
    done = 0
    for job in jobs:
        hold = parse_ts(job.get("hold_until"))
        state = workers.get(str(job["lease_owner"]))
        last = (state or {}).get("last_seen_at")
        worker_gone = last is None or last < silent_before or _stale_boot(job, state)
        if hold is not None and hold > now and not worker_gone:
            continue
        if _cas_job(job, dict(_LEASE_CLEARED), status="waiting_help", expired_before=now):
            log_step(str(job["id"]), int(job["lease_epoch"]), "help.parked",
                     "Parked: the hold ended without an answer", worker_id=str(job["lease_owner"]))
            done += 1
    return done


def reconcile_applying(now: datetime, settings_row: dict[str, Any]) -> int:
    """Step 3: an applying job follows its pm_rfi_sync_runs row (D6). done
    -> succeeded (outcome from run.stats.agent_outcome, else applied);
    failed -> failed/apply_failed; a run that disappeared -> failed too."""
    jobs = (
        _sb().table(JOBS).select(JOB_COLUMNS).eq("principal_kind", "org").eq("status", "applying").order("updated_at")
        .limit(SWEEP_BATCH).execute()
    ).data or []
    if not jobs:
        return 0
    run_ids = sorted({str(j["sync_run_id"]) for j in jobs if j.get("sync_run_id")})
    runs = {}
    if run_ids:
        runs = {
            str(r["id"]): r for r in (
                _sb().table("pm_rfi_sync_runs").select("id, status, stats, error").in_("id", run_ids).execute().data or []
            )
        }
    done = 0
    for job in jobs:
        run = runs.get(str(job.get("sync_run_id"))) if job.get("sync_run_id") else None
        epoch = int(job.get("lease_epoch") or 0)
        if run is not None and run.get("status") in ("queued", "running"):
            continue
        if run is not None and run.get("status") == "done":
            outcome = (run.get("stats") or {}).get("agent_outcome") or "applied"
            if outcome not in protocol.JobOutcome.__args__:  # type: ignore[attr-defined]
                outcome = "applied"
            patch = {"status": "succeeded", "outcome": outcome, "finished_at": iso(now)}
            if _cas_job(job, patch, status="applying"):
                log_step(str(job["id"]), epoch, "job.applied", f"Applied to the RFI log ({outcome})",
                         actor="bdr", result={"run_id": run["id"], "outcome": outcome})
                done += 1
            continue
        why = (run or {}).get("error") or ("the apply run failed" if run else "the apply run is gone")
        patch = {"status": "failed", "outcome": "apply_failed", "error_kind": "apply_failed",
                 "error": str(why)[: protocol.ERROR_TEXT_MAX], "finished_at": iso(now)}
        if _cas_job(job, patch, status="applying"):
            log_step(str(job["id"]), epoch, "job.failed", "Failed: the capture could not be applied",
                     actor="bdr", result={"run_id": (run or {}).get("id")})
            notify_job_failed(job, "apply_failed", "the capture could not be applied to the RFI log")
            done += 1
    return done


def fail_exhausted_retries(now: datetime, settings_row: dict[str, Any]) -> int:
    """Step 4: a queued retry (resume_reason 'retry') already at
    max_attempts would be charged past the cap by its next claim. The
    requeue RPC fails a crash at the cap itself; this catches every other
    path (a manual retry, an edited cap)."""
    jobs = (
        _sb().table(JOBS).select(JOB_COLUMNS).eq("principal_kind", "org").eq("status", "queued").eq("resume_reason", "retry")
        .limit(SWEEP_BATCH).execute()
    ).data or []
    if not jobs:
        return 0
    types = _types_by_key()
    done = 0
    for job in jobs:
        cap = int((types.get(job["agent_type"]) or {}).get("max_attempts") or 3)
        if int(job.get("attempts") or 0) < cap:
            continue
        patch = {"status": "failed", "error_kind": "crashed_repeatedly",
                 "error": "Failed too many times", "finished_at": iso(now)}
        if _cas_job(job, patch, status="queued"):
            log_step(str(job["id"]), int(job.get("lease_epoch") or 0), "job.failed",
                     f"Failed: {job.get('attempts')} charged attempts used")
            notify_job_failed(job, "crashed_repeatedly", "every charged attempt was used")
            done += 1
    return done


def expire_stale(now: datetime, settings_row: dict[str, Any]) -> int:
    """Step 5: queued jobs nobody claimed before expires_at (PC off for a
    week, type disabled) become expired."""
    jobs = (
        _sb().table(JOBS).select(JOB_COLUMNS).eq("principal_kind", "org").eq("status", "queued").lt("expires_at", iso(now))
        .limit(SWEEP_BATCH).execute()
    ).data or []
    done = 0
    for job in jobs:
        patch = {"status": "expired", "error_kind": "expired", "error": "Not run before it expired",
                 "finished_at": iso(now)}
        if _cas_job(job, patch, status="queued"):
            log_step(str(job["id"]), int(job.get("lease_epoch") or 0), "job.expired", "Expired before it ran")
            done += 1
    return done


def finish_cancels(now: datetime, settings_row: dict[str, Any]) -> int:
    """Step 6: a cancel is delivered to the holder by its heartbeat; once no
    live lease remains (never leased, parked, or lapsed) BDR closes it."""
    jobs = (
        _sb().table(JOBS).select(JOB_COLUMNS).eq("principal_kind", "org").not_.is_("cancel_requested_at", "null")
        .in_("status", ["queued", "running", "waiting_help"]).limit(SWEEP_BATCH).execute()
    ).data or []
    done = 0
    for job in jobs:
        expires = parse_ts(job.get("lease_expires_at"))
        if job.get("lease_owner") and expires is not None and expires >= now:
            continue
        patch = {**_LEASE_CLEARED, "status": "canceled", "error_kind": "canceled",
                 "lease_epoch": int(job.get("lease_epoch") or 0) + 1, "finished_at": iso(now)}
        if _cas_job(job, patch, status=job["status"]):
            log_step(str(job["id"]), int(job.get("lease_epoch") or 0), "job.canceled",
                     "Canceled (requested)", actor="bdr")
            done += 1
    return done


def spend_alerts(now: datetime, settings_row: dict[str, Any]) -> int:
    """Step 7 (O4): one IT Admin bell at 50% and one at 100% of the daily
    cap per window. The window is the later of Pacific midnight and the last
    IT Admin reset (browser_agent_spend_today); a flag set before the window
    started is stale and re-arms."""
    sb = _sb()
    spend = sb.rpc("browser_agent_spend_today", {}).execute().data or {}
    if isinstance(spend, list):
        spend = spend[0] if spend else {}
    cap = float(spend.get("daily_cap_usd") or 0)
    used = float(spend.get("spend_usd") or 0)
    window = parse_ts(spend.get("window_started_at"))
    if cap <= 0 or window is None:
        return 0
    sent = 0
    for fraction, column in ((1.0, "cap_hit_notified_at"), (0.5, "half_cap_notified_at")):
        if used < cap * fraction:
            continue
        claimed = (
            sb.table("browser_agent_runtime_state")
            .update({column: iso(now), "updated_at": iso(now)})
            .eq("id", 1).or_(f"{column}.is.null,{column}.lt.{iso(window)}").execute()
        ).data
        if claimed:
            notify_it_admin(
                "browser_agent.spend_alert",
                f"Browser agents have spent ${used:,.2f} of the ${cap:,.2f} daily cap "
                f"({int(fraction * 100)}%).",
                {"fraction": fraction, "spend_usd": round(used, 4), "cap_usd": cap},
                email=fraction >= 1.0,
            )
            sent += 1
    return sent


def worker_offline(now: datetime, settings_row: dict[str, Any]) -> int:
    """Step 8: no active worker seen for worker_offline_alert_minutes while a
    queued job has waited that long: one IT Admin bell; still nothing after
    worker_offline_email_hours: one email. Both re-arm once a worker is
    seen again."""
    sb = _sb()
    alert_after = timedelta(minutes=int(settings_row.get("worker_offline_alert_minutes") or 60))
    email_after = timedelta(hours=int(settings_row.get("worker_offline_email_hours") or 4))
    workers = sb.table("browser_agent_workers").select("id, last_seen_at").eq("principal_kind", "org").eq("status", "active").execute().data or []
    last_seen = max((parse_ts(w.get("last_seen_at")) for w in workers if w.get("last_seen_at")), default=None)
    state_rows = sb.table("browser_agent_runtime_state").select(
        "offline_notified_at, offline_emailed_at"
    ).eq("id", 1).limit(1).execute().data or []
    state = state_rows[0] if state_rows else {}
    if last_seen is not None and last_seen >= now - alert_after:
        if state.get("offline_notified_at") or state.get("offline_emailed_at"):
            sb.table("browser_agent_runtime_state").update(
                {"offline_notified_at": None, "offline_emailed_at": None, "updated_at": iso(now)}
            ).eq("id", 1).execute()
        return 0
    waiting = (
        sb.table(JOBS).select("id").eq("principal_kind", "org").eq("status", "queued").lt("created_at", iso(now - alert_after))
        .limit(1).execute()
    ).data or []
    if not waiting:
        return 0
    worker_id = str(workers[0]["id"]) if len(workers) == 1 else None
    sent = 0
    if not state.get("offline_notified_at"):
        claimed = (
            sb.table("browser_agent_runtime_state").update({"offline_notified_at": iso(now), "updated_at": iso(now)})
            .eq("id", 1).is_("offline_notified_at", "null").execute()
        ).data
        if claimed:
            notify_it_admin(
                "browser_agent.worker_offline",
                "No browser agent worker has checked in and jobs are waiting. Check the agent PC.",
                {"worker_id": worker_id},
            )
            sent += 1
    # Offline since the last heartbeat; a worker never seen at all counts
    # from the bell (the email follows it by worker_offline_email_hours).
    offline_since = last_seen or parse_ts(state.get("offline_notified_at")) or (now if sent else None)
    if not state.get("offline_emailed_at") and offline_since is not None and offline_since < now - email_after:
        claimed = (
            sb.table("browser_agent_runtime_state").update({"offline_emailed_at": iso(now), "updated_at": iso(now)})
            .eq("id", 1).is_("offline_emailed_at", "null").execute()
        ).data
        if claimed:
            notify_it_admin(
                "browser_agent.worker_offline",
                "The browser agent PC has been offline for hours and jobs are waiting. "
                "Check that it is powered on and online.",
                {"worker_id": worker_id},
                email=True,
            )
            sent += 1
    return sent


CORE_STEPS: tuple[tuple[str, SweepStep], ...] = (
    ("reclaim_expired", reclaim_expired),
    ("park_lapsed_holds", park_lapsed_holds),
    ("reconcile_applying", reconcile_applying),
    ("fail_exhausted_retries", fail_exhausted_retries),
    ("expire_stale", expire_stale),
    ("finish_cancels", finish_cancels),
    ("spend_alerts", spend_alerts),
    ("worker_offline", worker_offline),
)


def sweep_once(now: datetime | None = None) -> dict[str, int]:
    """One sweeper tick. Each step is isolated: one failing never stops the
    others or the loop. Returns {step: rows acted on} (failed steps: -1)."""
    now = now or now_utc()
    settings_row = load_settings()
    report: dict[str, int] = {}
    for name, fn in (*CORE_STEPS, *_EXTRA_STEPS):
        try:
            report[name] = int(fn(now, settings_row) or 0)
        except Exception:  # noqa: BLE001 - one step never kills the tick
            logger.exception("browser agents sweeper: step %s failed", name)
            report[name] = -1
    return report


async def polling_loop() -> None:
    """Lifespan task (main.py): sweep every BROWSER_AGENT_SWEEP_SECONDS in a
    thread (the Supabase SDK is synchronous). Runs in every uvicorn worker;
    every write is compare-and-set."""
    global _started_monotonic
    _started_monotonic = time.monotonic()
    interval = get_settings().browser_agent_sweep_seconds
    while True:
        try:
            await asyncio.to_thread(sweep_once)
        except Exception:  # noqa: BLE001 - a failed tick never kills the loop
            logger.exception("browser agents sweeper tick failed")
        await asyncio.sleep(interval)

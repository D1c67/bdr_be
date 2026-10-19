"""The human side of the browser agents, apart from help (docs/browser_agents/HUMAN_API.md).

Views (C2 shapes) and writes behind routers/browser_agents.py:

  summary          the Agents badge and Settings card numbers;
  jobs             list (newest first, opaque cursor), detail, steps (by seq),
                   turns (public action only), cancel, retry;
  admin            AdminState, global settings, the spend window reset (O4),
                   agent types (probe rule, ceiling rule, O1 enable rule),
                   secrets (write only), enrollment codes, workers (approve,
                   revoke, drain, slots, allowed types and IPs), the settings
                   history, the canary and Stop running jobs.

Every settings write goes through the browser_agent_update_settings RPC
(one transaction: the row, one change-log row per changed field, one
audit_log row); the spend reset through browser_agent_reset_spend_window.
Every other human write writes its own audit_log row. Secret values never
appear in any view: secrets are {set, fingerprint}.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from postgrest.exceptions import APIError

from app.core import supabase_client
from app.core.agent_auth import bytea_literal, new_enroll_code
from app.core.config import get_settings
from app.services import notifications
from app.services.browser_agents import help as help_flow
from app.services.browser_agents import protocol, queue, registry
from app.services.browser_agents.help import human_error

logger = logging.getLogger(__name__)

UTC = timezone.utc
JOBS = queue.JOBS
ONLINE_SECONDS = 2 * protocol.HEARTBEAT_SECONDS          # "seen within 2 heartbeats (30 s)"
CHROMIUM_STALE_DAYS = 30
ENROLL_TTL_MINUTES = 30
LIST_DEFAULT, LIST_MAX = 50, 200
STEPS_DEFAULT, STEPS_MAX = 200, 500
CURSOR_SLACK = 50                                        # rows sharing one created_at a page tolerates
RETRY_PRIORITY = 10
FINISHED = ["succeeded", "canceled", "expired"]
LIVE = ["queued", "running", "waiting_help"]

GLOBAL_FIELDS = ("enabled", "daily_cost_cap_usd", "per_job_cost_cap_usd", "help_hold_seconds",
                 "help_reminder_minutes", "help_claim_lapse_minutes", "help_expire_hours",
                 "worker_offline_alert_minutes", "worker_offline_email_hours", "job_ttl_hours",
                 "desired_worker_version")
TYPE_FIELDS = ("label", "enabled", "provider", "model", "price_in_per_mtok", "price_out_per_mtok",
               "max_concurrency", "max_attempts", "max_interruptions", "max_steps", "max_total_tokens",
               "max_active_seconds", "host_narrowing", "priority_default")
SECRET_NAMES = ("procore_password",)


def _sb():
    return supabase_client.get_supabase()


def _now() -> datetime:
    return queue.now_utc()


def _iso(dt: datetime) -> str:
    return queue.iso(dt)


def _ts(value: Any) -> datetime | None:
    return queue.parse_ts(value)


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _opt_num(value: Any) -> float | None:
    return None if value is None else _num(value)


def _one(data: Any) -> dict[str, Any]:
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def _rpc(name: str, params: dict[str, Any]) -> Any:
    """A settings RPC. Its error messages are protocol codes (invalid_request)
    or CHECK violations; both become a 422 with a fixed text."""
    try:
        return _sb().rpc(name, params).execute().data
    except APIError as exc:
        text = f"{getattr(exc, 'message', '')} {exc}"
        if "browser_agent_types_enabled_needs_model" in text:
            raise human_error(422, "type_needs_model",
                              "An enabled type needs a provider, a model and both prices.") from None
        if "invalid_request" in text or getattr(exc, "code", None) in ("23514", "22P02", "22023"):
            raise human_error(422, "invalid_request", "That change is not allowed.") from None
        raise


def _audit(user: Any, action: str, entity: str, entity_id: str | None, payload: dict[str, Any]) -> None:
    try:
        notifications.audit(str(user.id), action, entity, entity_id, payload)
    except Exception:  # noqa: BLE001 - the change-log row is the record
        logger.exception("browser agents: audit %s failed", action)


# ── Cursors ─────────────────────────────────────────────────────────────────────


def encode_cursor(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise human_error(422, "invalid_request", "The cursor is not valid.") from None
    if not isinstance(data, dict):
        raise human_error(422, "invalid_request", "The cursor is not valid.")
    return data


def clamp_limit(limit: int | None, default: int = LIST_DEFAULT, maximum: int = LIST_MAX) -> int:
    return max(1, min(int(limit or default), maximum))


def page_newest_first(query, cursor: str | None, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    """Keyset paging on (created_at desc, id desc) without an or-filter: the
    page reads rows at or before the cursor time (plus a little slack for
    ties) and drops the ones at or after the cursor key in Python."""
    after = decode_cursor(cursor)
    if after:
        query = query.lte("created_at", after.get("t"))
    rows = query.order("created_at", desc=True).order("id", desc=True).limit(limit + 1 + CURSOR_SLACK).execute().data or []

    def key(r: dict[str, Any]) -> tuple[datetime, str]:
        return (_ts(r.get("created_at")) or datetime.min.replace(tzinfo=UTC), str(r.get("id")))

    rows.sort(key=key, reverse=True)
    if after:
        mark = (_ts(after.get("t")) or datetime.min.replace(tzinfo=UTC), str(after.get("id")))
        rows = [r for r in rows if key(r) < mark]
    page = rows[:limit]
    more = len(rows) > limit
    nxt = encode_cursor({"t": page[-1].get("created_at"), "id": str(page[-1]["id"])}) if more and page else None
    return page, nxt


def _count(query) -> int:
    res = query.execute()
    count = getattr(res, "count", None)
    return int(count) if count is not None else len(res.data or [])


# ── Summary ─────────────────────────────────────────────────────────────────────


def spend_today() -> dict[str, Any]:
    return _one(_sb().rpc("browser_agent_spend_today", {}).execute().data)


def _online(worker: dict[str, Any], now: datetime) -> bool:
    seen = _ts(worker.get("last_seen_at"))
    return worker.get("status") == "active" and seen is not None and seen >= now - timedelta(seconds=ONLINE_SECONDS)


def summary() -> dict[str, Any]:
    sb = _sb()
    now = _now()
    settings_row = queue.load_settings()
    spend = spend_today()

    def jobs(status_: str) -> int:
        return _count(sb.table(JOBS).select("id", count="exact").eq("status", status_))

    workers = sb.table("browser_agent_workers").select("id, status, last_seen_at").eq("status", "active").execute().data or []
    online = sum(1 for w in workers if _online(w, now))
    return {
        "enabled": bool(settings_row.get("enabled")),
        "open_help": _count(sb.table(help_flow.HELP).select("id", count="exact").in_("status", help_flow.ACTIVE)),
        "waiting_jobs": jobs("waiting_help"),
        "running_jobs": jobs("running"),
        "queued_jobs": jobs("queued"),
        "worker_online": online > 0,
        "workers_online": online,
        "spend_today_usd": _num(spend.get("spend_usd")),
        "daily_cap_usd": _num(spend.get("daily_cap_usd", settings_row.get("daily_cost_cap_usd"))),
        "per_job_cap_usd": _num(spend.get("per_job_cap_usd", settings_row.get("per_job_cost_cap_usd"))),
    }


# ── Jobs ────────────────────────────────────────────────────────────────────────


def get_job(job_id: str) -> dict[str, Any] | None:
    try:
        uuid.UUID(str(job_id))
    except ValueError:
        return None
    rows = _sb().table(JOBS).select("*").eq("id", str(job_id)).limit(1).execute().data or []
    return rows[0] if rows else None


def _carried(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return (result or {}).get("cap_reset") or {}


def job_rows_view(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = help_flow.rfi_labels(jobs)
    names = help_flow.project_names({j["project_id"] for j in jobs if j.get("project_id")})
    out = []
    for j in jobs:
        carried = _carried(j)
        out.append({
            "id": str(j["id"]), "agent_type": j["agent_type"], "status": j["status"], "outcome": j.get("outcome"),
            "project_id": j.get("project_id"),
            "project_name": names.get(str(j.get("project_id"))) if j.get("project_id") else None,
            "rfi_label": labels.get(str(j["id"])),
            "attempts": int(j.get("attempts") or 0), "interrupted_count": int(j.get("interrupted_count") or 0),
            "created_at": j.get("created_at"), "finished_at": j.get("finished_at"),
            "waiting_help_id": j.get("waiting_help_id") if j.get("status") == "waiting_help" else None,
            "cost_usd": round(_num(j.get("cost_usd")) + _num(carried.get("cost_usd")), 6),
        })
    return out


def list_jobs(*, status_: str | None, agent_type: str | None, project_id: str | None, cursor: str | None,
              limit: int | None) -> dict[str, Any]:
    q = _sb().table(JOBS).select("*")
    if status_:
        if status_ == "needs_help":
            q = q.eq("status", "waiting_help")
        elif status_ == "finished":
            q = q.in_("status", FINISHED)
        elif status_ in protocol.JobStatus.__args__:  # type: ignore[attr-defined]
            q = q.eq("status", status_)
        else:
            raise human_error(422, "invalid_request", "Unknown status filter.")
    if agent_type:
        q = q.eq("agent_type", agent_type)
    if project_id:
        q = q.eq("project_id", project_id)
    rows, nxt = page_newest_first(q, cursor, clamp_limit(limit))
    return {"items": job_rows_view(rows), "next_cursor": nxt}


def _turn_totals(job_id: str) -> tuple[int, int, float]:
    rows = _sb().table("browser_agent_turns").select("input_tokens, output_tokens, cost_usd").eq("job_id", job_id).execute().data or []
    return (sum(int(r.get("input_tokens") or 0) for r in rows), sum(int(r.get("output_tokens") or 0) for r in rows),
            round(sum(_num(r.get("cost_usd")) for r in rows), 6))


def ledger_step_count(job_id: str) -> int:
    """Every ledger row of the job (BDR, worker, model, helper and admin
    steps, late ones included): the highest per-job seq, which the 0157
    trigger assigns gap-free from 1."""
    rows = (
        _sb().table("browser_agent_steps").select("seq").eq("job_id", job_id).order("seq", desc=True).limit(1)
        .execute()
    ).data or []
    return int(rows[0].get("seq") or 0) if rows else 0


def _cap_reached_help(job: dict[str, Any]) -> dict[str, Any] | None:
    if job.get("status") != "waiting_help" or not job.get("waiting_help_id"):
        return None
    help_row = help_flow.get_help(str(job["waiting_help_id"]))
    return help_row if help_row and help_row.get("reason_code") == "cap_reached" else None


def _is_cap_retry(job: dict[str, Any]) -> bool:
    return job.get("error_kind") == "cap_reached" or _cap_reached_help(job) is not None


def can_retry(job: dict[str, Any], user: Any) -> bool:
    st = job.get("status")
    if st in ("queued", "running", "applying"):
        return False
    if st == "waiting_help" and _cap_reached_help(job) is None:
        return False
    if st == "succeeded":
        return False
    if _is_cap_retry(job) and job.get("helper_retry_used") and not help_flow.is_admin(user):
        return False
    return True


def job_detail_view(job: dict[str, Any], user: Any) -> dict[str, Any]:
    row = job_rows_view([job])[0]
    seed = job.get("input") or {}
    snap = job.get("config_snapshot") or {}
    snap_type = snap.get("type") or {}
    email_subject = None
    if job.get("source_email_id"):
        try:
            em = (_sb().table("ingested_emails").select("subject").eq("id", str(job["source_email_id"]))
                  .limit(1).execute().data or [])
            email_subject = (em[0].get("subject") if em else None)
        except Exception:  # noqa: BLE001 - the subject is a label
            email_subject = None
    tokens_in, tokens_out, cost = _turn_totals(str(job["id"]))
    return {
        **row,
        "cost_usd": max(row["cost_usd"], cost),
        "input_summary": {
            "platform": seed.get("platform") or job.get("platform"),
            "host": seed.get("host"),
            "external_project_id": seed.get("external_project_id"),
            "rfi_id": seed.get("rfi_id"),
            "rfi_number_hint": seed.get("rfi_number_hint"),
            "event_hint": seed.get("event_hint"),
            "email_message_at": seed.get("email_message_at"),
        },
        "config_snapshot": {
            "provider": snap_type.get("provider"), "model": snap_type.get("model"),
            "prompt_version": snap.get("prompt_version") or snap_type.get("prompt_version"),
            "prompt_sha": snap.get("prompt_sha256"),
            "per_job_cap_usd": _opt_num(snap.get("per_job_cost_cap_usd")),
        } if snap else None,
        "lease_epoch": int(job.get("lease_epoch") or 0),
        "tokens_in": max(int(job.get("tokens_in") or 0), tokens_in),
        "tokens_out": max(int(job.get("tokens_out") or 0), tokens_out),
        "step_count": max(int(job.get("step_count") or 0), ledger_step_count(str(job["id"]))),
        "turn_count": int(job.get("turn_count") or 0),
        "active_seconds": int(job.get("active_seconds") or 0) + int(_carried(job).get("active_seconds") or 0),
        "sync_run_id": job.get("sync_run_id"),
        "error_kind": job.get("error_kind"),
        "error": job.get("error"),
        "source_email_id": job.get("source_email_id"),
        "source_email_subject": email_subject,
        "checkpoint_phase": (job.get("checkpoint") or {}).get("phase"),
        "cancel_requested_at": job.get("cancel_requested_at"),
        "can_retry": can_retry(job, user),
    }


STEP_COLUMNS = ("seq, epoch, late, kind, actor, actor_label, summary, tool_name, args, result, url_before, "
                "url_after, screenshot_artifact_id, turn_no, ok, duration_ms, at, redacted_at")


def job_steps(job_id: str, after_seq: int, limit: int | None) -> dict[str, Any]:
    lim = clamp_limit(limit, STEPS_DEFAULT, STEPS_MAX)
    rows = (
        _sb().table("browser_agent_steps").select(STEP_COLUMNS).eq("job_id", job_id).gt("seq", int(after_seq or 0))
        .order("seq").limit(lim + 1).execute()
    ).data or []
    page = rows[:lim]
    items = [{
        "seq": int(r["seq"]), "epoch": int(r.get("epoch") or 0), "late": bool(r.get("late")),
        "kind": r.get("kind"), "actor": r.get("actor"), "actor_label": r.get("actor_label"),
        "summary": r.get("summary") or "", "tool_name": r.get("tool_name"), "args": r.get("args"),
        "result": r.get("result"), "url_before": r.get("url_before"), "url_after": r.get("url_after"),
        "screenshot_artifact_id": r.get("screenshot_artifact_id"), "turn_no": r.get("turn_no"),
        "ok": r.get("ok"), "duration_ms": r.get("duration_ms"), "at": r.get("at"),
        "redacted_at": r.get("redacted_at"),
    } for r in page]
    return {"items": items, "next_after_seq": items[-1]["seq"] if len(rows) > lim and items else None}


def job_turns(job_id: str) -> dict[str, Any]:
    from app.services.browser_agents.turns import public_action

    rows = (
        _sb().table("browser_agent_turns").select(
            "turn_no, epoch, status, assistant_text, action, provider, model, input_tokens, output_tokens, "
            "cost_usd, billing_unknown, error_kind, created_at"
        ).eq("job_id", job_id).order("turn_no").execute()
    ).data or []
    return {"items": [{
        "turn_no": int(r["turn_no"]), "epoch": int(r.get("epoch") or 0), "status": r.get("status"),
        "assistant_text": r.get("assistant_text"), "action": public_action(r.get("action")),
        "provider": r.get("provider"), "model": r.get("model"), "input_tokens": r.get("input_tokens"),
        "output_tokens": r.get("output_tokens"), "cost_usd": _opt_num(r.get("cost_usd")),
        "billing_unknown": bool(r.get("billing_unknown")), "error_kind": r.get("error_kind"),
        "created_at": r.get("created_at"),
    } for r in rows]}


def cancel_job(job: dict[str, Any], user: Any) -> dict[str, Any]:
    """queued (or parked, no live lease): canceled at once. running or
    waiting_help with a lease: cancel_requested_at, delivered as `cancel`
    by the next heartbeat; the sweeper closes it once the lease is gone.
    Applying or finished: a no-op."""
    now = _now()
    label = help_flow.user_label(user)
    st = job.get("status")
    leased = bool(job.get("lease_owner"))
    changed = False
    if st == "queued" or (st == "waiting_help" and not leased):
        changed = help_flow.close_waiter(job, now, status_="canceled", error_kind="canceled",
                                         error="Canceled by a person") if st == "waiting_help" else bool(
            _sb().table(JOBS).update({
                "status": "canceled", "error_kind": "canceled", "error": "Canceled by a person",
                "finished_at": _iso(now), "cancel_requested_at": _iso(now), "cancel_requested_by": str(user.id),
                "lease_epoch": int(job.get("lease_epoch") or 0) + 1,
            }).eq("id", str(job["id"])).eq("status", "queued").execute().data)
        kind, summary = "job.canceled", "Canceled by a person"
    elif st in ("running", "waiting_help") and not job.get("cancel_requested_at"):
        changed = bool(
            _sb().table(JOBS).update({"cancel_requested_at": _iso(now), "cancel_requested_by": str(user.id)})
            .eq("id", str(job["id"])).in_("status", ["running", "waiting_help"]).is_("cancel_requested_at", "null")
            .execute().data
        )
        kind, summary = "job.cancel_requested", "Cancel requested by a person"
    if changed:
        help_flow.helper_step(job, kind, summary, user_id=str(user.id), label=label, actor="helper")
        _audit(user, "browser_agent.job_cancel", JOBS, str(job["id"]),
               {"status_before": st, "actor_label": label})
    return job_detail_view(get_job(str(job["id"])) or job, user)


def retry_job(job: dict[str, Any], user: Any) -> dict[str, Any]:
    """failed / canceled / expired, or waiting_help on cap_reached: back to
    the queue, resume_reason manual, priority 10, uncharged, with a fresh
    attempt budget. A cap_reached retry restarts the model counters once
    for a helper; after that only IT Admin (or a dev). A failed apply
    (apply_failed) re-applies the same capture instead of re-running."""
    st = job.get("status")
    if st in ("queued", "running", "applying") or st == "succeeded":
        raise human_error(409, "job_not_retryable", "This job is not in a state that can be retried.")
    cap_help = _cap_reached_help(job)
    if st == "waiting_help" and cap_help is None:
        raise human_error(409, "job_not_retryable", "Answer the job's help request instead.")
    cap_retry = _is_cap_retry(job)
    if cap_retry and job.get("helper_retry_used") and not help_flow.is_admin(user):
        raise human_error(403, "retry_admin_only", "This job was already retried once; IT Admin can retry it again.")
    now = _now()
    label = help_flow.user_label(user)
    if st == "failed" and job.get("outcome") == "apply_failed":
        from app.services.browser_agents.types import rfi_from_email

        try:
            run = rfi_from_email.retry_apply(str(job["id"]))
        except rfi_from_email.RetryRefused as exc:
            raise human_error(409, "job_not_retryable", str(exc)) from None
        _audit(user, "browser_agent.job_retry", JOBS, str(job["id"]),
               {"mode": "reapply", "run_id": run.get("id"), "actor_label": label})
        return job_detail_view(get_job(str(job["id"])) or job, user)
    extra: dict[str, Any] = {
        "attempts": 0, "interrupted_count": 0, "outcome": None, "error_kind": None, "error": None,
        "finished_at": None, "priority": RETRY_PRIORITY,
    }
    if cap_retry:
        extra.update(help_flow.cap_reset_patch(job, now, label))
    ttl = int(queue.load_settings().get("job_ttl_hours") or 168)
    expires = _ts(job.get("expires_at"))
    if expires is None or expires < now + timedelta(hours=1):
        extra["expires_at"] = _iso(now + timedelta(hours=ttl))
    ok = help_flow.requeue_waiter(job, now, resume_reason="manual", statuses=[st], extra=extra)
    if not ok:
        raise human_error(409, "job_not_retryable", "The job changed; reload it.")
    if cap_help is not None:
        others = [j for j in help_flow._jobs(help_flow.waiter_ids(str(cap_help["id"])))
                  if str(j["id"]) != str(job["id"]) and j.get("status") in LIVE]
        if not others:
            help_flow._resolve(cap_help, now, "retry", user_id=str(user.id))
    help_flow.helper_step(job, "job.retry", "Retried by a person" + (" (model counters reset)" if cap_retry else ""),
                          user_id=str(user.id), label=label, actor="helper",
                          result={"counters_reset": cap_retry, "status_before": st})
    _audit(user, "browser_agent.job_retry", JOBS, str(job["id"]),
           {"mode": "requeue", "status_before": st, "counters_reset": cap_retry, "actor_label": label})
    return job_detail_view(get_job(str(job["id"])) or job, user)


def artifact_url(artifact_id: str, user: Any) -> dict[str, Any]:
    from app.services.browser_agents import artifacts

    try:
        uuid.UUID(str(artifact_id))
    except ValueError:
        raise human_error(404, "not_found", "No such artifact.") from None
    rows = _sb().table("browser_agent_artifacts").select("*").eq("id", str(artifact_id)).limit(1).execute().data or []
    if not rows:
        raise human_error(404, "not_found", "No such artifact.")
    a = rows[0]
    if a.get("kind") in ("llm_request", "llm_response") and not help_flow.is_admin(user):
        raise human_error(403, "forbidden", "Model transcripts are for IT Admin.")
    ttl = 300
    return {"url": artifacts.signed_url(a["storage_path"], ttl), "filename": a.get("filename"),
            "mime_type": a.get("mime_type") or "application/octet-stream", "expires_in": ttl}


# ── Admin views ────────────────────────────────────────────────────────────────


def settings_view(row: dict[str, Any]) -> dict[str, Any]:
    labels = help_flow.profile_labels({row["updated_by"]}) if row.get("updated_by") else {}
    return {
        "enabled": bool(row.get("enabled")), "config_version": int(row.get("config_version") or 1),
        "daily_cost_cap_usd": _num(row.get("daily_cost_cap_usd")),
        "per_job_cost_cap_usd": _num(row.get("per_job_cost_cap_usd")),
        "help_hold_seconds": int(row.get("help_hold_seconds") or 900),
        "help_reminder_minutes": int(row.get("help_reminder_minutes") or 120),
        "help_claim_lapse_minutes": int(row.get("help_claim_lapse_minutes") or 30),
        "help_expire_hours": int(row.get("help_expire_hours") or 48),
        "worker_offline_alert_minutes": int(row.get("worker_offline_alert_minutes") or 60),
        "worker_offline_email_hours": int(row.get("worker_offline_email_hours") or 4),
        "job_ttl_hours": int(row.get("job_ttl_hours") or 168),
        "desired_worker_version": row.get("desired_worker_version"),
        "updated_by_label": labels.get(str(row.get("updated_by"))) if row.get("updated_by") else None,
        "updated_at": row.get("updated_at"),
    }


def caps_view(spend: dict[str, Any] | None = None) -> dict[str, Any]:
    spend = spend or spend_today()
    rs = _one(_sb().table("browser_agent_runtime_state").select("*").eq("id", 1).limit(1).execute().data)
    by = None
    if rs.get("cost_window_reset_by"):
        by = help_flow.profile_labels({rs["cost_window_reset_by"]}).get(str(rs["cost_window_reset_by"]))
    return {
        "daily_cap_usd": _num(spend.get("daily_cap_usd")),
        "per_job_cap_usd": _num(spend.get("per_job_cap_usd")),
        "window_started_at": spend.get("window_started_at"),
        "spend_today_usd": _num(spend.get("spend_usd")),
        "window_reset_by_label": by,
    }


def type_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": row["key"], "label": row.get("label") or row["key"], "enabled": bool(row.get("enabled")),
        "identity": row.get("identity"), "provider": row.get("provider"), "model": row.get("model"),
        "price_in_per_mtok": _opt_num(row.get("price_in_per_mtok")),
        "price_out_per_mtok": _opt_num(row.get("price_out_per_mtok")),
        "model_probe_ok_at": row.get("model_probe_ok_at"),
        "max_concurrency": int(row.get("max_concurrency") or 0), "max_attempts": int(row.get("max_attempts") or 3),
        "max_interruptions": int(row.get("max_interruptions") or 20), "max_steps": int(row.get("max_steps") or 0),
        "max_total_tokens": int(row.get("max_total_tokens") or 0),
        "max_active_seconds": int(row.get("max_active_seconds") or 0),
        "host_narrowing": row.get("host_narrowing"), "prompt_version": row.get("prompt_version") or "",
        "priority_default": int(row.get("priority_default") or 100), "updated_at": row.get("updated_at"),
    }


def _semver(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(v).split("."))
    except (TypeError, ValueError):
        return (0, 0, 0)


def _chromium_stale(version: str | None, releases: list[dict[str, Any]], now: datetime) -> bool:
    """A Chromium build older than 30 days: the oldest published release
    that shipped this Chromium is older than that. Unknown builds are not
    flagged (no date to go on)."""
    if not version:
        return False
    dates = [_ts(r.get("created_at")) for r in releases if r.get("chromium_version") == version]
    dates = [d for d in dates if d is not None]
    return bool(dates) and min(dates) < now - timedelta(days=CHROMIUM_STALE_DAYS)


def workers_view(rows: list[dict[str, Any]], releases: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if not rows:
        return []
    now = _now()
    releases = releases if releases is not None else _releases()
    ids = [str(w["id"]) for w in rows]
    held = _sb().table(JOBS).select("lease_owner").in_("lease_owner", ids).in_(
        "status", ["running", "waiting_help"]).execute().data or []
    running: dict[str, int] = {}
    for j in held:
        running[str(j["lease_owner"])] = running.get(str(j["lease_owner"]), 0) + 1
    labels = help_flow.profile_labels({w["approved_by"] for w in rows if w.get("approved_by")})
    return [{
        "id": str(w["id"]), "name": w.get("name"), "status": w.get("status"), "online": _online(w, now),
        "draining": bool(w.get("draining")), "max_slots": int(w.get("max_slots") or 1),
        "allowed_types": list(w.get("allowed_types") or []), "allowed_ips": [str(i) for i in w.get("allowed_ips") or []],
        "version": w.get("version"), "bad_versions": list(w.get("bad_versions") or []),
        "hostname": w.get("hostname"), "chromium_version": w.get("chromium_version"),
        "chromium_stale": _chromium_stale(w.get("chromium_version"), releases, now),
        "boot_at": w.get("boot_at"), "last_seen_at": w.get("last_seen_at"),
        "last_ip": str(w["last_ip"]) if w.get("last_ip") else None,
        "enroll_ip": str(w["enroll_ip"]) if w.get("enroll_ip") else None,
        "running_jobs": running.get(str(w["id"]), 0), "token_prefix": w.get("token_prefix"),
        "approved_at": w.get("approved_at"),
        "approved_by_label": labels.get(str(w.get("approved_by"))) if w.get("approved_by") else None,
        "revoked_at": w.get("revoked_at"), "created_at": w.get("created_at"),
    } for w in rows]


def _releases() -> list[dict[str, Any]]:
    return _sb().table("browser_agent_worker_releases").select(
        "version, min_version, sha256, chromium_version, notes, created_at"
    ).order("created_at", desc=True).execute().data or []


def release_view(r: dict[str, Any]) -> dict[str, Any]:
    return {"version": r["version"], "min_version": r["min_version"], "sha256": r["sha256"],
            "chromium_version": r.get("chromium_version"), "notes": r.get("notes"), "created_at": r.get("created_at")}


def procore_state() -> dict[str, Any]:
    row = _one(_sb().table("rfp_harvest_sessions").select(
        "locked_until, login_failures, last_login_source, last_error, logged_in_at"
    ).eq("provider", "procore").limit(1).execute().data)
    return {
        "email": (get_settings().procore_login_email or "").strip() or None,
        "locked_until": row.get("locked_until"), "login_failures": int(row.get("login_failures") or 0),
        "last_login_source": row.get("last_login_source"), "last_error": row.get("last_error"),
        "logged_in_at": row.get("logged_in_at"),
    }


def admin_state() -> dict[str, Any]:
    from app.services.browser_agents import llm
    from app.services.browser_agents import secrets as secret_store

    sb = _sb()
    settings_row = queue.load_settings()
    spend = spend_today()
    rs = _one(sb.table("browser_agent_runtime_state").select("*").eq("id", 1).limit(1).execute().data)
    releases = _releases()
    types = sb.table("browser_agent_types").select("*").order("key").execute().data or []
    workers = sb.table("browser_agent_workers").select("*").order("created_at").execute().data or []
    return {
        "settings": settings_view(settings_row),
        "runtime": {"spend_today_usd": _num(spend.get("spend_usd")), "window_started_at": spend.get("window_started_at"),
                    "half_cap_notified_at": rs.get("half_cap_notified_at"),
                    "cap_hit_notified_at": rs.get("cap_hit_notified_at")},
        "caps": caps_view(spend),
        "types": [type_view(t) for t in types if registry.has_type(t["key"])],
        "workers": workers_view(workers, releases),
        "secrets": secret_store.secret_rows(SECRET_NAMES),
        "procore": procore_state(),
        "releases": [release_view(r) for r in releases],
        "providers": llm.providers_info(),
        "ceilings": {k: list(v.get("nav_hosts") or []) for k, v in protocol.CEILINGS.items()},
        "pm_rfi_sync_active": bool(get_settings().pm_rfi_sync_active),
    }


# ── Admin writes ───────────────────────────────────────────────────────────────


def update_global(patch: dict[str, Any], user: Any) -> dict[str, Any]:
    patch = {k: v for k, v in patch.items() if k in GLOBAL_FIELDS}
    if not patch:
        return admin_state()
    if "desired_worker_version" in patch and patch["desired_worker_version"] is not None:
        version = str(patch["desired_worker_version"])
        releases = _releases()
        if not any(r["version"] == version for r in releases):
            raise human_error(409, "below_min_version", "That worker version was never published.")
        floor = max((_semver(r["min_version"]) for r in releases), default=(0, 0, 0))
        if _semver(version) < floor:
            raise human_error(409, "below_min_version",
                              "That version is below the minimum version a published release requires.")
    for key in ("daily_cost_cap_usd", "per_job_cost_cap_usd"):
        if key in patch and patch[key] is not None:
            patch[key] = round(float(patch[key]), 2)
    _rpc("browser_agent_update_settings", {
        "p_scope": "global", "p_target": None, "p_patch": patch, "p_actor": str(user.id),
        "p_actor_label": help_flow.user_label(user), "p_probe": None,
    })
    return admin_state()


def reset_spend(note: str | None, user: Any) -> dict[str, Any]:
    spend = _one(_rpc("browser_agent_reset_spend_window", {
        "p_actor": str(user.id), "p_actor_label": help_flow.user_label(user), "p_note": (note or "").strip() or None,
    }))
    return caps_view(spend if spend.get("window_started_at") else None)


def _type_row(key: str) -> dict[str, Any]:
    rows = _sb().table("browser_agent_types").select("*").eq("key", key).limit(1).execute().data or []
    if not rows or not registry.has_type(key):
        raise human_error(404, "not_found", "No such agent type.")
    return rows[0]


def update_type(key: str, patch: dict[str, Any], save_anyway: bool, user: Any) -> dict[str, Any]:
    from app.services.browser_agents.llm import models as llm_models

    current = _type_row(key)
    patch = {k: v for k, v in patch.items() if k in TYPE_FIELDS}
    if "host_narrowing" in patch and patch["host_narrowing"] is not None:
        ceiling = set((protocol.CEILINGS.get(key) or {}).get("nav_hosts") or [])
        hosts = list(dict.fromkeys(str(h).strip().lower() for h in patch["host_narrowing"]))
        if not set(hosts) <= ceiling:
            raise human_error(422, "host_not_in_ceiling", "Host narrowing may only remove hosts from the code ceiling.")
        patch["host_narrowing"] = hosts
    provider = patch.get("provider", current.get("provider"))
    model = patch.get("model", current.get("model"))
    probe: dict[str, Any] | None = None
    if ("provider" in patch or "model" in patch) and (provider, model) != (current.get("provider"), current.get("model")):
        if provider and model:
            passed = llm_models.recent_probe(key, provider, model)
            if passed:
                probe = {**passed, "provider": provider, "model": model}
                patch["model_probe_ok_at"] = passed.get("probed_at") or _iso(_now())
            elif save_anyway:
                probe = {"save_anyway": True, "provider": provider, "model": model}
                patch["model_probe_ok_at"] = None
            else:
                raise human_error(409, "probe_failed", "Run Test model for this provider and model first, "
                                                       "or confirm Save anyway.")
        else:
            patch["model_probe_ok_at"] = None
    enabled = patch.get("enabled", current.get("enabled"))
    if enabled:
        merged = {**current, **patch}
        if not (merged.get("provider") and merged.get("model") and merged.get("price_in_per_mtok") is not None
                and merged.get("price_out_per_mtok") is not None):
            raise human_error(422, "type_needs_model", "An enabled type needs a provider, a model and both prices.")
        if key == "rfi_from_email" and patch.get("enabled") and not get_settings().pm_rfi_sync_active:
            raise human_error(409, "pm_rfi_sync_off", "Turn on the PM RFI sync before enabling RFI agents.")
    for k in ("price_in_per_mtok", "price_out_per_mtok"):
        if patch.get(k) is not None:
            patch[k] = round(float(patch[k]), 4)
    if patch:
        _rpc("browser_agent_update_settings", {
            "p_scope": "agent_type", "p_target": key, "p_patch": patch, "p_actor": str(user.id),
            "p_actor_label": help_flow.user_label(user), "p_probe": probe,
        })
    return type_view(_type_row(key))


def models(provider: str) -> dict[str, Any]:
    from app.services.browser_agents.llm import models as llm_models

    if provider not in protocol.PROVIDERS:
        raise human_error(422, "invalid_request", "Unknown provider.")
    try:
        return llm_models.list_models(provider)
    except llm_models.ProviderKeyMissing:
        raise human_error(409, "provider_key_missing", f"No API key is configured for {provider}.") from None
    except Exception:  # noqa: BLE001 - provider text never reaches the UI
        logger.exception("browser agents: listing %s models failed", provider)
        raise human_error(502, "provider_unreachable", f"Could not list {provider} models right now.") from None


def probe(key: str, provider: str, model: str, user: Any) -> dict[str, Any]:
    from app.services.browser_agents.llm import models as llm_models

    _type_row(key)
    if provider not in protocol.PROVIDERS or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", model or ""):
        raise human_error(422, "invalid_request", "Unknown provider or model.")
    try:
        result = llm_models.probe(provider, model)
    except llm_models.ProviderKeyMissing:
        raise human_error(409, "provider_key_missing", f"No API key is configured for {provider}.") from None
    llm_models.record_probe(key, provider, model, result, actor_id=str(user.id),
                            actor_label=help_flow.user_label(user))
    return {"ok": bool(result.get("ok")), "latency_ms": int(result.get("latency_ms") or 0),
            "error": result.get("error"), "probed_at": result.get("probed_at")}


def set_secret(name: str, value: str, user: Any) -> dict[str, Any]:
    from app.services import procore_creds
    from app.services.browser_agents import secrets as secret_store

    if name not in SECRET_NAMES:
        raise human_error(404, "not_found", "No such secret.")
    try:
        row = secret_store.set_secret(name, value, actor_id=str(user.id), actor_label=help_flow.user_label(user))
    except secret_store.SecretInvalid as exc:
        raise human_error(422, "invalid_request", str(exc)) from None
    except secret_store.SecretsKeyMissing:
        raise human_error(409, "secrets_key_missing", "BROWSER_AGENT_SECRETS_KEY is not set on the server.") from None
    procore_creds.reset_cache()
    return row


def clear_secret(name: str, user: Any) -> None:
    from app.services import procore_creds
    from app.services.browser_agents import secrets as secret_store

    if name not in SECRET_NAMES:
        raise human_error(404, "not_found", "No such secret.")
    secret_store.delete_secret(name, actor_id=str(user.id), actor_label=help_flow.user_label(user))
    procore_creds.reset_cache()


def release_key_fingerprint() -> str:
    """SHA-256 of the raw Ed25519 release key, upper hex in groups of four,
    the same text install.ps1 prints (agent_worker/bdr_agent/release_key.py)."""
    text = (get_settings().browser_agent_release_pubkey or "").strip()
    if not text:
        return ""
    try:
        raw = base64.b64decode((text + "=" * (-len(text) % 4)).replace("-", "+").replace("_", "/"))
    except (binascii.Error, ValueError):
        return ""
    digest = hashlib.sha256(raw).hexdigest().upper()
    return " ".join(digest[i:i + 4] for i in range(0, len(digest), 4))


def create_enrollment(worker_name: str, user: Any, api_base: str) -> dict[str, Any]:
    name = (worker_name or "").strip()
    if not 2 <= len(name) <= 60:
        raise human_error(422, "invalid_request", "A worker name is 2 to 60 characters.")
    code, digest = new_enroll_code()
    now = _now()
    expires = now + timedelta(minutes=ENROLL_TTL_MINUTES)
    label = help_flow.user_label(user)
    row = _sb().table("browser_agent_enrollments").insert({
        "code_hash": bytea_literal(digest), "worker_name": name, "created_by": str(user.id),
        "expires_at": _iso(expires),
    }).execute().data[0]
    try:
        _sb().table("browser_agent_settings_changes").insert({
            "scope": "worker", "target": None, "field": "enrollment_code", "old_value": None,
            "new_value": {"worker_name": name, "enrollment_id": str(row["id"]), "expires_at": _iso(expires)},
            "changed_by": str(user.id), "changed_by_label": label,
        }).execute()
    except Exception:  # noqa: BLE001 - the audit row below still records it
        logger.exception("browser agents: enrollment change row failed")
    _audit(user, "browser_agent.enrollment_created", "browser_agent_enrollments", str(row["id"]),
           {"worker_name": name, "expires_at": _iso(expires), "actor_label": label})
    base = api_base.rstrip("/")
    install = (f"irm {base}{protocol.API_PREFIX}/installer.ps1 -OutFile $env:TEMP\\bdr-install.ps1; "
               "powershell -ExecutionPolicy Bypass -File $env:TEMP\\bdr-install.ps1")
    return {"code": code, "expires_at": _iso(expires), "install_command": install,
            "key_fingerprint": release_key_fingerprint()}


def _worker_row(worker_id: str) -> dict[str, Any]:
    try:
        uuid.UUID(str(worker_id))
    except ValueError:
        raise human_error(404, "not_found", "No such worker.") from None
    rows = _sb().table("browser_agent_workers").select("*").eq("id", str(worker_id)).limit(1).execute().data or []
    if not rows:
        raise human_error(404, "not_found", "No such worker.")
    return rows[0]


def _worker_update(worker_id: str, patch: dict[str, Any], user: Any) -> dict[str, Any]:
    _rpc("browser_agent_update_settings", {
        "p_scope": "worker", "p_target": str(worker_id), "p_patch": patch, "p_actor": str(user.id),
        "p_actor_label": help_flow.user_label(user), "p_probe": None,
    })
    return workers_view([_worker_row(worker_id)])[0]


def patch_worker(worker_id: str, patch: dict[str, Any], user: Any) -> dict[str, Any]:
    current = _worker_row(worker_id)
    if current.get("status") == "revoked":
        raise human_error(409, "invalid_request", "A revoked worker cannot be changed; enroll it again.")
    out: dict[str, Any] = {}
    if patch.get("name") is not None:
        name = str(patch["name"]).strip()
        if not 2 <= len(name) <= 60:
            raise human_error(422, "invalid_request", "A worker name is 2 to 60 characters.")
        out["name"] = name
    if patch.get("draining") is not None:
        out["draining"] = bool(patch["draining"])
    if patch.get("max_slots") is not None:
        slots = int(patch["max_slots"])
        if not 1 <= slots <= 8:
            raise human_error(422, "invalid_request", "max_slots is 1 to 8.")
        out["max_slots"] = slots
    if patch.get("allowed_types") is not None:
        types = list(dict.fromkeys(str(t) for t in patch["allowed_types"]))
        unknown = [t for t in types if not registry.has_type(t)]
        if unknown:
            raise human_error(422, "invalid_request", f"Unknown agent type: {unknown[0]}.")
        out["allowed_types"] = types
    if patch.get("allowed_ips") is not None:
        cidrs = []
        for raw in patch["allowed_ips"]:
            try:
                cidrs.append(str(ipaddress.ip_network(str(raw).strip(), strict=False)))
            except ValueError:
                raise human_error(422, "invalid_request", "Allowed IPs must be IP addresses or CIDR ranges.") from None
        out["allowed_ips"] = list(dict.fromkeys(cidrs))
    if not out:
        return workers_view([current])[0]
    return _worker_update(worker_id, out, user)


def approve_worker(worker_id: str, user: Any) -> dict[str, Any]:
    current = _worker_row(worker_id)
    if current.get("status") == "active":
        return workers_view([current])[0]
    if current.get("status") != "pending":
        raise human_error(409, "invalid_request", "Only a pending worker can be approved.")
    patch: dict[str, Any] = {"status": "active"}
    if not current.get("allowed_types"):
        # A fresh worker may run every built-in type; IT Admin narrows it
        # with PATCH allowed_types. An empty list would never claim anything.
        patch["allowed_types"] = sorted(registry.type_keys())
    view = _worker_update(worker_id, patch, user)
    _audit(user, "browser_agent.worker_approved", "browser_agent_workers", str(worker_id),
           {"name": current.get("name"), "hostname": current.get("hostname"),
            "enroll_ip": str(current.get("enroll_ip") or ""), "actor_label": help_flow.user_label(user)})
    return view


def revoke_worker(worker_id: str, user: Any) -> dict[str, Any]:
    """Instant: the RPC clears the token hash, so the next call from that PC
    is refused; its leases lapse and the sweeper requeues them uncharged."""
    current = _worker_row(worker_id)
    if current.get("status") == "revoked":
        return workers_view([current])[0]
    view = _worker_update(worker_id, {"status": "revoked"}, user)
    _audit(user, "browser_agent.worker_revoked", "browser_agent_workers", str(worker_id),
           {"name": current.get("name"), "actor_label": help_flow.user_label(user)})
    return view


def history(cursor: str | None, limit: int | None) -> dict[str, Any]:
    lim = clamp_limit(limit)
    q = _sb().table("browser_agent_settings_changes").select("*")
    after = decode_cursor(cursor)
    if after:
        q = q.lt("id", int(after.get("id") or 0))
    rows = q.order("id", desc=True).limit(lim + 1).execute().data or []
    page = rows[:lim]
    items = [{
        "id": int(r["id"]), "scope": r.get("scope"), "target": r.get("target"), "field": r.get("field"),
        "old_value": r.get("old_value"), "new_value": r.get("new_value"), "probe": r.get("probe"),
        "changed_by_label": r.get("changed_by_label") or "", "changed_at": r.get("changed_at"),
    } for r in page]
    nxt = encode_cursor({"id": items[-1]["id"]}) if len(rows) > lim and items else None
    return {"items": items, "next_cursor": nxt}


def run_canary(worker_id: str | None, user: Any) -> dict[str, Any]:
    """Queue a self-test. The claim RPC cannot pin a job to one PC, so a
    named worker is checked (exists, active, allowed to run canary) and
    recorded; any worker allowed the canary may claim it."""
    from app.services.browser_agents.types import canary

    if worker_id:
        w = _worker_row(worker_id)
        if w.get("status") != "active" or "canary" not in (w.get("allowed_types") or []):
            raise human_error(409, "invalid_request", "That worker is not active or not allowed to run the self-test.")
    job = canary.enqueue_canary(created_by=str(user.id))
    label = help_flow.user_label(user)
    help_flow.helper_step(job, "job.canary", "Self-test queued from Settings", user_id=str(user.id), label=label,
                          actor="admin", result={"worker_id": worker_id})
    _audit(user, "browser_agent.canary", JOBS, str(job["id"]), {"worker_id": worker_id, "actor_label": label})
    return {"job_id": str(job["id"])}


def stop_running(user: Any) -> dict[str, Any]:
    now = _now()
    rows = (
        _sb().table(JOBS).update({"cancel_requested_at": _iso(now), "cancel_requested_by": str(user.id)})
        .in_("status", ["running", "waiting_help"]).is_("cancel_requested_at", "null").execute()
    ).data or []
    label = help_flow.user_label(user)
    for job in rows:
        help_flow.helper_step(job, "job.cancel_requested", "Stop running jobs: cancel requested",
                              user_id=str(user.id), label=label, actor="admin")
    _audit(user, "browser_agent.stop_running", JOBS, None, {"cancel_requested": len(rows), "actor_label": label})
    return {"cancel_requested": len(rows)}


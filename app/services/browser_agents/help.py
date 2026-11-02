"""Help requests: a stuck browser agent asks a person (docs/browser_agents/SPEC.md 6, O5).

One request per real problem. The scope comes from the reason code
(protocol.help_scope_key): `login:<identity>` for login trouble (shared by
every job that logs in as that account), `link:<project>` for access denied
or not found, `job:<id>` for everything else. A job that hits a scope that
is already open joins it as a waiter instead of opening a second request.

Worker side (routers/agent_worker_help.py, all fenced on the job's lease):
  open_help       open or join (browser_agent_open_help RPC), suppress a link
                  scope given up or skipped in the last 24 h, notify once;
  worker_messages helper and system messages after an id (a secret comes as
                  a secret_ref only, never a value);
  consume         the job read them (help_reads); a holder waiting on this
                  request goes back to running on the same lease;
  agent_reply     the agent's own line in the thread (scrubbed);
  use_secret      the one-time helper secret (an MFA code), holder job only,
                  login scope only, during a login, consumed once;
  contributions   helper files and notes for the RFI (applied by
                  contributions.py once the RFI row exists).

Human side (routers/browser_agents.py): list, detail, messages, post a
message (idempotent on client_msg_id; looks_like_secret guard; a secret is
encrypted before its single insert), upload a file, claim and unclaim, and
the three buttons: Retry now, Skip (the daily sync will catch it), Give up.

Notices (C3): browser_agent.help_needed once per request (compare-and-set
on notified_at), to Estimating Admins, plus IT Admin for login scopes, and
to IT Admin alone when no active Estimating Admin exists. Reminders after
help_reminder_minutes while unclaimed, at most 3; a lapsed claim re-arms
them. Text is a fixed template per reason code: never model text, page
content or screenshots.

Sweeper steps (registered by register_sweep_steps): reminders, claim lapse,
resolve requests whose waiters all finished, expiry (login scopes stay open
while the account is locked or the last login needed a code or a captcha,
and email IT Admin once instead), purge unconsumed helper secrets after 30
min (their HMAC stays 7 days for the leak check), and requeue parked waiters
of an answered request whose answer they never read.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO

from fastapi import HTTPException, status

from app.core import supabase_client
from app.core.agent_auth import AgentLease, agent_error, bytea_literal, bytea_value, rpc
from app.core.config import get_settings
from app.core.redact import redact_text
from app.core.roles import Role
from app.services import notifications
from app.services.browser_agents import artifacts, protocol, queue, redaction, registry, scan
from app.services.browser_agents import secrets as secret_store
from app.services.browser_agents.protocol import ErrorCode

logger = logging.getLogger(__name__)

UTC = timezone.utc
HELP = "browser_agent_help_requests"
WAITERS = "browser_agent_help_waiters"
MESSAGES = "browser_agent_help_messages"
READS = "browser_agent_help_reads"
JOBS = queue.JOBS
ARTIFACTS = "browser_agent_artifacts"
CONTRIBUTIONS = "browser_agent_rfi_contributions"

ACTIVE = ["open", "answered"]
LIVE_JOB = ["queued", "running", "waiting_help"]
TERMINAL = list(protocol.JOB_TERMINAL_STATUSES)

MAX_REMINDERS = 3
SUPPRESS_HOURS = 24
SECRET_PURGE_MINUTES = 30
SECRET_HMAC_KEEP_DAYS = 7
SECRET_VALUE_MAX = 64
HUMAN_BODY_MAX = 8000
UPLOAD_MAX_BYTES = 150 * 1024 * 1024
HELP_PRIORITY = 10
MESSAGE_ARTIFACTS_MAX = 10
LOGIN_SECRET_WINDOW_EXTRA_SECONDS = 600
LOOKS_LIKE_CODE_RE = re.compile(r"\d{4,10}")
HELP_BELL_TYPES = ["browser_agent.help_needed", "browser_agent.help_reminder"]
AGENT_LABEL = "Browser agent"

# Server templates (trusted). The title names the problem; the notice text
# says what a person can do. Neither ever quotes the agent's own words.
REASON_TITLES: dict[str, str] = {
    "login_failed": "Procore login failed",
    "login_locked": "Procore login is locked",
    "captcha": "Procore wants a robot check",
    "mfa": "Procore code needed",
    "sso": "Procore sent the agent to single sign-on",
    "logged_out": "Logged out of Procore",
    "access_denied": "No access to the Procore project",
    "not_found": "RFI not found in Procore",
    "ambiguous": "Which RFI is it?",
    "unexpected_page": "Unexpected page",
    "cap_reached": "Job limit reached",
    "needs_info": "Information needed",
    "provider_config": "AI model problem",
    "other": "Help needed",
}
REASON_TEXT: dict[str, str] = {
    "login_failed": "The browser agents could not log in to Procore. Check the Procore password in "
                    "Settings > Browser agents, then press Retry now.",
    "login_locked": "Procore logins for the browser agents are locked after repeated failures. Check the "
                    "Procore password in Settings > Browser agents.",
    "captcha": "Procore asked the browser agent to prove it is not a robot. It cannot do that alone: retry "
               "later, skip, or give up.",
    "mfa": "Procore asked the browser agent for a verification code. Send the code from the help page "
           "with 'This is a code' ticked.",
    "sso": "Procore sent the browser agent to a single sign-on page it cannot complete.",
    "logged_out": "The browser agent was logged out of Procore.",
    "access_denied": "The browser agent cannot open this project's RFIs in Procore (access denied).",
    "not_found": "The browser agent could not find the RFI in Procore.",
    "ambiguous": "The browser agent found more than one possible RFI and needs a person to pick.",
    "unexpected_page": "The browser agent reached a page it does not understand.",
    "cap_reached": "The browser agent used its step, token or spend limit on this job.",
    "needs_info": "The browser agent needs information to continue.",
    "provider_config": "The browser agent's AI model is not configured correctly.",
    "other": "A browser agent needs help.",
}

# Uploads: extension -> (sniffed kind, mime type, attachable allowed).
UPLOAD_TYPES: dict[str, tuple[str, str, bool]] = {
    ".pdf": ("pdf", "application/pdf", True),
    ".png": ("png", "image/png", True),
    ".jpg": ("jpeg", "image/jpeg", True),
    ".jpeg": ("jpeg", "image/jpeg", True),
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True),
    ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", True),
    ".txt": ("txt", "text/plain", True),
    ".zip": ("zip", "application/zip", False),
    ".dwg": ("dwg", "image/vnd.dwg", False),
}


def _sb():
    return supabase_client.get_supabase()


def _now() -> datetime:
    return queue.now_utc()


def _iso(dt: datetime) -> str:
    return queue.iso(dt)


def _ts(value: Any) -> datetime | None:
    return queue.parse_ts(value)


def _one(data: Any) -> dict[str, Any]:
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "code", None) == "23505" or "23505" in str(exc) or "duplicate key" in str(exc)


def human_error(status_code: int, code: str, detail: str) -> HTTPException:
    """The human API's refusal: FastAPI {detail} plus X-Error-Code, no-store."""
    return HTTPException(status_code=status_code, detail=detail,
                         headers={protocol.HEADER_ERROR: code, "Cache-Control": "no-store"})


# ── People ──────────────────────────────────────────────────────────────────────

ROLE_LABELS: dict[str, str] = {
    Role.ESTIMATING_ADMIN.value: "Estimating Admin",
    Role.IT_ADMIN.value: "IT Admin",
    Role.EXECUTIVE.value: "Executive",
    Role.ACCOUNTANT.value: "Accountant",
    Role.ESTIMATING_ENGINEER_MATERIALS.value: "Estimating Engineer",
    Role.ESTIMATING_ENGINEER_LABOR.value: "Estimating Engineer",
}


def user_label(user: Any) -> str:
    """'Name (Role)' at the time of the action (immutable rows keep it)."""
    name = None
    try:
        rows = _sb().table("profiles").select("full_name, email").eq("id", str(user.id)).limit(1).execute().data or []
        if rows:
            name = rows[0].get("full_name") or rows[0].get("email")
    except Exception:  # noqa: BLE001 - a label is cosmetic
        logger.warning("browser agents: profile label unreadable")
    name = name or getattr(user, "email", None) or "Unknown"
    role = getattr(user, "role", None)
    role_value = getattr(role, "value", role)
    return f"{name} ({ROLE_LABELS.get(str(role_value), str(role_value or 'user'))})"[:200]


def profile_labels(ids: set[str] | list[str]) -> dict[str, str]:
    ids = sorted({str(i) for i in ids if i})
    if not ids:
        return {}
    try:
        rows = _sb().table("profiles").select("id, full_name, email, role").in_("id", ids).execute().data or []
    except Exception:  # noqa: BLE001
        logger.warning("browser agents: profile labels unreadable")
        return {}
    out = {}
    for p in rows:
        name = p.get("full_name") or p.get("email") or "Unknown"
        role = ROLE_LABELS.get(str(p.get("role")), "")
        out[str(p["id"])] = f"{name} ({role})" if role else name
    return out


def project_names(ids: set[str] | list[str]) -> dict[str, str]:
    ids = sorted({str(i) for i in ids if i})
    if not ids:
        return {}
    rows = _sb().table("projects").select("id, name").in_("id", ids).execute().data or []
    return {str(r["id"]): r.get("name") for r in rows}


def rfi_labels(jobs: list[dict[str, Any]]) -> dict[str, str | None]:
    """'RFI Const-198' when the RFI is already in BDR (its Procore number),
    'RFI 27914290' (the Procore id) otherwise, 'Locate RFI #198' for a
    subject-only job, None for a type without an RFI (canary)."""
    wanted: dict[str, set[str]] = {}
    for j in jobs:
        seed = j.get("input") or {}
        if j.get("agent_type") == "rfi_from_email" and seed.get("rfi_id") and j.get("project_id"):
            wanted.setdefault(str(j["project_id"]), set()).add(str(seed["rfi_id"]))
    numbers: dict[tuple[str, str], str] = {}
    for pid, ext_ids in wanted.items():
        try:
            rows = (
                _sb().table("rfis").select("external_id, external_number").eq("project_id", pid)
                .in_("external_id", sorted(ext_ids)).execute()
            ).data or []
        except Exception:  # noqa: BLE001 - a label falls back to the id
            rows = []
        for r in rows:
            if r.get("external_number"):
                numbers[(pid, str(r["external_id"]))] = str(r["external_number"])
    out: dict[str, str | None] = {}
    for j in jobs:
        seed = j.get("input") or {}
        if j.get("agent_type") != "rfi_from_email":
            out[str(j["id"])] = None
        elif seed.get("rfi_id"):
            num = numbers.get((str(j.get("project_id")), str(seed["rfi_id"])))
            out[str(j["id"])] = f"RFI {num}" if num else f"RFI {seed['rfi_id']}"
        elif seed.get("rfi_number_hint"):
            out[str(j["id"])] = f"Locate RFI #{seed['rfi_number_hint']}"
        else:
            out[str(j["id"])] = None
    return out


# ── Templates and notices ────────────────────────────────────────────────────────


def is_login_scope(scope_key: str | None) -> bool:
    return str(scope_key or "").startswith("login:")


def help_title(reason: str, scope_key: str, label: str | None) -> str:
    title = REASON_TITLES.get(reason, REASON_TITLES["other"])
    if label and not is_login_scope(scope_key):
        title = f"{title} ({label})"
    return title[:200]


def _notice_text(help_row: dict[str, Any], *, reminder: bool) -> str:
    title = help_row.get("title") or REASON_TITLES.get(help_row.get("reason_code") or "other", "Help needed")
    text = REASON_TEXT.get(help_row.get("reason_code") or "other", REASON_TEXT["other"])
    if reminder:
        return f"Still waiting for help: {title}. {text}"
    return f"Browser agent needs help: {title}. {text}"


def _notify_role(role: Role, help_row: dict[str, Any], type_: str, message: str, metadata: dict[str, Any], *,
                 email: bool = True) -> int | None:
    try:
        return notifications.notify_role(role, help_row.get("project_id"), type_, message,
                                         mirror_email=email, metadata=metadata)
    except Exception:  # noqa: BLE001 - a notice never breaks the state change
        logger.exception("browser agents: %s notice to %s failed", type_, role)
        return None


def notify_helpers(help_row: dict[str, Any], type_: str, message: str, *, job_id: str | None = None) -> None:
    """O5 audience: Estimating Admins; IT Admin also for login scopes, and
    instead when no active Estimating Admin exists (notify_role created 0)."""
    metadata = {"help_id": str(help_row["id"]), "job_id": str(job_id or help_row.get("opened_by_job_id") or ""),
                "reason": help_row.get("reason_code"), "scope_key": help_row.get("scope_key")}
    if help_row.get("principal_kind", "org") != "org":
        owner = help_row.get("principal_user_id")
        if owner:
            notifications.notify_user(str(owner), None, type_, "Your browser task needs your attention.",
                                      metadata={"help_id": str(help_row["id"])})
        return
    created = _notify_role(Role.ESTIMATING_ADMIN, help_row, type_, message, metadata)
    if is_login_scope(help_row.get("scope_key")) or created == 0:
        _notify_role(Role.IT_ADMIN, help_row, type_, message, metadata)


def notify_help_needed(help_id: str) -> bool:
    """Once per request: compare-and-set on notified_at (a joining job never
    re-notifies). The first reminder is due help_reminder_minutes later."""
    now = _now()
    claimed = (
        _sb().table(HELP).update({"notified_at": _iso(now), "reminder_sent_at": _iso(now)})
        .eq("id", help_id).is_("notified_at", "null").execute()
    ).data or []
    if not claimed:
        return False
    row = claimed[0]
    notify_helpers(row, "browser_agent.help_needed", _notice_text(row, reminder=False))
    return True


def dismiss_help_bells(help_row: dict[str, Any]) -> None:
    """Drop the request's help_needed / help_reminder bells. help_id alone is
    a valid dismissal scope (notifications.dismiss_notifications)."""
    try:
        notifications.dismiss_notifications(
            project_id=str(help_row["project_id"]) if help_row.get("project_id") else None,
            types=HELP_BELL_TYPES,
            metadata_eq={"help_id": str(help_row["id"])},
        )
    except Exception:  # noqa: BLE001 - a stale bell is cosmetic
        logger.exception("browser agents: dismissing help %s bells failed", help_row.get("id"))


# ── Reads ───────────────────────────────────────────────────────────────────────


def get_help(help_id: str) -> dict[str, Any] | None:
    try:
        uuid.UUID(str(help_id))
    except ValueError:
        return None
    rows = _sb().table(HELP).select("*").eq("id", str(help_id)).limit(1).execute().data or []
    return rows[0] if rows else None


def waiter_ids(help_id: str) -> list[str]:
    rows = _sb().table(WAITERS).select("job_id").eq("help_id", help_id).execute().data or []
    return [str(r["job_id"]) for r in rows]


def _jobs(ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    return _sb().table(JOBS).select("*").eq("principal_kind", "org").in_("id", ids).execute().data or []


def _files(artifact_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not artifact_ids:
        return {}
    rows = (
        _sb().table(ARTIFACTS).select("id, filename, size_bytes, mime_type, attachable, kind, help_id")
        .in_("id", sorted(artifact_ids)).execute()
    ).data or []
    return {str(a["id"]): a for a in rows}


def _file_view(a: dict[str, Any]) -> dict[str, Any]:
    return {"artifact_id": str(a["id"]), "filename": a.get("filename") or "file",
            "size_bytes": int(a.get("size_bytes") or 0),
            "mime_type": a.get("mime_type") or "application/octet-stream",
            "attachable": bool(a.get("attachable"))}


MESSAGE_COLUMNS = ("id, help_id, author_kind, author_label, job_id, client_msg_id, action, body, is_secret, "
                   "secret_consumed_at, secret_purged_at, artifact_ids, created_at")


def message_view(m: dict[str, Any], files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """C2 HelpMessage: a secret is is_secret + secret_consumed, never a value."""
    return {
        "id": int(m["id"]),
        "author_kind": m.get("author_kind"),
        "author_label": m.get("author_label"),
        "action": m.get("action") or "message",
        "body": None if m.get("is_secret") else m.get("body"),
        "is_secret": bool(m.get("is_secret")),
        "secret_consumed": m.get("secret_consumed_at") is not None,
        "files": [_file_view(files[str(a)]) for a in (m.get("artifact_ids") or []) if str(a) in files],
        "created_at": m.get("created_at"),
    }


def messages_for(help_id: str, *, after_id: int = 0, kinds: list[str] | None = None) -> list[dict[str, Any]]:
    q = _sb().table(MESSAGES).select(MESSAGE_COLUMNS).eq("help_id", help_id).gt("id", int(after_id or 0))
    if kinds:
        q = q.in_("author_kind", kinds)
    return q.order("id").limit(500).execute().data or []


def human_messages(help_id: str, after_id: int = 0) -> list[dict[str, Any]]:
    rows = messages_for(help_id, after_id=after_id)
    files = _files({str(a) for m in rows for a in (m.get("artifact_ids") or [])})
    return [message_view(m, files) for m in rows]


# ── Waiter state changes (compare-and-set on what was read) ─────────────────────


def _cas_job(job: dict[str, Any], patch: dict[str, Any], statuses: list[str]) -> dict[str, Any] | None:
    if job.get("principal_kind", "org") != "org":
        return None
    rows = (
        _sb().table(JOBS).update(patch).eq("id", str(job["id"])).eq("lease_epoch", int(job.get("lease_epoch") or 0))
        .in_("status", statuses).execute()
    ).data or []
    return rows[0] if rows else None


def _expiry_after_requeue(job: dict[str, Any], now: datetime) -> str | None:
    """A job parked for days may be past its expiry: give it a fresh day."""
    expires = _ts(job.get("expires_at"))
    if expires is None or expires < now + timedelta(hours=1):
        return _iso(now + timedelta(hours=24))
    return None


def requeue_waiter(job: dict[str, Any], now: datetime, *, resume_reason: str = "help_answered",
                   statuses: list[str] | None = None, extra: dict[str, Any] | None = None) -> bool:
    """Back to the queue, uncharged, ahead of the line (priority 10). A held
    lease is released with the epoch bumped, so the old holder's next call
    answers lease_lost."""
    patch = {
        "status": "queued", "resume_reason": resume_reason, "priority": HELP_PRIORITY,
        "waiting_help_id": None, "hold_until": None, "next_attempt_at": None,
        "lease_owner": None, "lease_boot_id": None, "lease_expires_at": None,
        "lease_epoch": int(job.get("lease_epoch") or 0) + 1,
        "cancel_requested_at": None, "cancel_requested_by": None,
        **(extra or {}),
    }
    expires = _expiry_after_requeue(job, now)
    if expires:
        patch["expires_at"] = expires
    return _cas_job(job, patch, statuses or ["waiting_help"]) is not None


def close_waiter(job: dict[str, Any], now: datetime, *, status_: str, error_kind: str, error: str) -> bool:
    patch = {
        "status": status_, "error_kind": error_kind, "error": error[: protocol.ERROR_TEXT_MAX],
        "finished_at": _iso(now), "waiting_help_id": None, "hold_until": None,
        "lease_owner": None, "lease_boot_id": None, "lease_expires_at": None,
        "lease_epoch": int(job.get("lease_epoch") or 0) + 1,
    }
    return _cas_job(job, patch, LIVE_JOB) is not None


def helper_step(job: dict[str, Any], kind: str, summary: str, *, user_id: str | None, label: str | None,
                actor: str = "helper", result: dict[str, Any] | None = None) -> None:
    """A ledger row written by a person (actor helper or admin, with their
    id and label). Never raises: the state change already committed."""
    try:
        _sb().table("browser_agent_steps").insert({
            "job_id": str(job["id"]), "epoch": int(job.get("lease_epoch") or 0), "kind": kind,
            "actor": actor, "actor_user_id": user_id, "actor_label": label,
            "summary": summary[: protocol.STEP_SUMMARY_MAX], "result": result,
        }).execute()
    except Exception:  # noqa: BLE001
        logger.exception("browser agents: step %s for job %s not written", kind, job.get("id"))


def _audit(user_id: str | None, action: str, entity_id: str | None, payload: dict[str, Any]) -> None:
    try:
        notifications.audit(user_id, action, HELP, entity_id, payload)
    except Exception:  # noqa: BLE001 - the change log / ledger rows are the record
        logger.exception("browser agents: audit %s failed", action)


def cap_reset_patch(job: dict[str, Any], now: datetime, label: str | None) -> dict[str, Any]:
    """A helper's cap_reached retry: the job's model counters restart (the
    step count restarts after the current turn, see turns.cap_reset_base);
    what was spent is kept in result.cap_reset for the views."""
    prior = ((job.get("result") or {}) if isinstance(job.get("result"), dict) else {}).get("cap_reset") or {}
    carried = {
        "tokens_in": int(prior.get("tokens_in") or 0) + int(job.get("tokens_in") or 0),
        "tokens_out": int(prior.get("tokens_out") or 0) + int(job.get("tokens_out") or 0),
        "cost_usd": round(float(prior.get("cost_usd") or 0) + float(job.get("cost_usd") or 0), 6),
        "active_seconds": int(prior.get("active_seconds") or 0) + int(job.get("active_seconds") or 0),
    }
    return {
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0, "active_seconds": 0, "helper_retry_used": True,
        "result": {**((job.get("result") or {}) if isinstance(job.get("result"), dict) else {}),
                   "cap_reset": {**carried, "after_turn_no": int(job.get("turn_count") or 0),
                                 "at": _iso(now), "by": label}},
    }


def _resolve(help_row: dict[str, Any], now: datetime, resolution: str, *, user_id: str | None = None,
             new_status: str = "resolved") -> bool:
    patch = {"status": new_status, "resolution": resolution, "resolved_at": _iso(now), "resolved_by": user_id}
    moved = (
        _sb().table(HELP).update(patch).eq("id", str(help_row["id"])).in_("status", ACTIVE).execute()
    ).data or []
    if moved:
        dismiss_help_bells(help_row)
    return bool(moved)


def _set_link_error(project_id: str | None, text: str) -> None:
    if not project_id:
        return
    try:
        _sb().table("pm_external_links").update({"last_error": text[:500]}).eq("project_id", str(project_id)).execute()
    except Exception:  # noqa: BLE001
        logger.exception("browser agents: link last_error not written for %s", project_id)


# ── Worker side ─────────────────────────────────────────────────────────────────


def _fence(lease: AgentLease, statuses: list[str]) -> dict[str, Any]:
    job = _one(rpc("browser_agent_fence", {**lease.params(), "p_statuses": statuses}))
    if not job.get("id"):
        raise agent_error(ErrorCode.LEASE_LOST)
    if job.get("principal_kind", "org") != "org":
        raise agent_error(ErrorCode.IDOR, "user_browser_execution_unavailable")
    return job


def _waited_help(job_id: str, help_id: str) -> dict[str, Any]:
    """IDOR: the help request must be one this job waits (or waited) on."""
    rows = _sb().table(WAITERS).select("help_id").eq("help_id", str(help_id)).eq("job_id", job_id).limit(1).execute().data
    help_row = get_help(str(help_id)) if rows else None
    if not help_row or help_row.get("principal_kind", "org") != "org":
        raise agent_error(ErrorCode.IDOR, "help request does not belong to this job")
    return help_row


def _clean_agent_text(text: str | None, limit: int, job_id: str) -> str | None:
    """Untrusted agent text for the thread: invisible characters out, URL
    credentials out, known secrets out. A text that carried a password,
    helper code or token is dropped whole (the helper sees a note)."""
    if not text:
        return None
    cleaned = redact_text(redaction.strip_invisible(text))
    report = redaction.ScrubReport()
    try:
        cleaned = redaction.scrub(cleaned, redaction.load_secrets(job_id, worker_token=None), report)
    except Exception:  # noqa: BLE001 - never store unscrubbed text when the scrub itself broke
        logger.exception("browser agents: help text scrub failed; text dropped")
        return "[agent text removed: it could not be checked]"
    if report.severe:
        return "[agent text removed: it contained a secret]"
    return str(cleaned)[:limit] or None


def _suppressed(scope_key: str, now: datetime) -> bool:
    since = _iso(now - timedelta(hours=SUPPRESS_HOURS))
    rows = (
        _sb().table(HELP).select("id").eq("scope_key", scope_key).in_("resolution", ["give_up", "skip_daily"])
        .gte("resolved_at", since).limit(1).execute()
    ).data or []
    return bool(rows)


def open_help(lease: AgentLease, req: protocol.HelpOpenRequest) -> protocol.HelpOpenResponse:
    job = _fence(lease, ["running"])
    try:
        spec = registry.get_type(job["agent_type"])
    except registry.UnknownAgentType:
        raise agent_error(ErrorCode.INVALID_REQUEST, "unknown agent type") from None
    if req.reason_code not in spec.help_reasons:
        raise agent_error(ErrorCode.INVALID_REQUEST, f"{spec.key} cannot ask for help with {req.reason_code}")
    sb = _sb()
    if req.screenshot_artifact_id:
        shot = (
            sb.table(ARTIFACTS).select("id, kind").eq("id", str(req.screenshot_artifact_id))
            .eq("job_id", lease.job_id).limit(1).execute()
        ).data or []
        if not shot or shot[0].get("kind") != "screenshot":
            raise agent_error(ErrorCode.IDOR, "screenshot does not belong to this job")
    now = _now()
    scope = protocol.help_scope_key(req.reason_code, identity=spec.identity, project_id=job.get("project_id"),
                                    job_id=lease.job_id)
    if scope.startswith("link:") and _suppressed(scope, now):
        _set_link_error(job.get("project_id"), "Browser agent help for this link was given up or skipped "
                                               "in the last 24 hours; new requests are suppressed.")
        queue.log_step(lease.job_id, lease.epoch, "help.suppressed",
                       f"Help suppressed ({req.reason_code}): the link was given up or skipped in the last 24 h",
                       worker_id=lease.worker.id, actor="bdr", result={"scope": scope})
        return protocol.HelpOpenResponse(suppressed=True)
    settings_row = queue.load_settings()
    label = rfi_labels([job]).get(str(job["id"]))
    message = _clean_agent_text(req.message, protocol.HELP_AGENT_MESSAGE_MAX, lease.job_id)
    data = _one(rpc("browser_agent_open_help", {
        **lease.params(),
        "p_scope": scope,
        "p_reason": req.reason_code,
        "p_title": help_title(req.reason_code, scope, label),
        "p_message": message,
        "p_shot": str(req.screenshot_artifact_id) if req.screenshot_artifact_id else None,
        # A login problem is the account's, not a project's.
        "p_project": None if is_login_scope(scope) else job.get("project_id"),
        "p_hold_seconds": int(settings_row.get("help_hold_seconds") or 900),
        "p_expires_hours": int(settings_row.get("help_expire_hours") or 48),
    }))
    help_id = str(data["help_id"])
    joined = bool(data.get("joined_existing"))
    if joined:
        help_row = get_help(help_id) or {}
        if message:
            _insert_agent_message(help_id, lease.job_id, message)
        if help_row.get("status") == "answered":
            # A waiter asks again after an answer: the request needs a person again.
            reopen_help(help_row, now, settings_row, job_id=lease.job_id)
    else:
        notify_help_needed(help_id)
    return protocol.HelpOpenResponse(
        help_id=help_id, joined_existing=joined, holder=bool(data.get("holder")), suppressed=False,
        hold_until=data.get("hold_until"),
    )


def reopen_help(help_row: dict[str, Any], now: datetime, settings_row: dict[str, Any], *,
                job_id: str | None = None) -> bool:
    """An answered request asked again: back to open, unclaimed, reminders
    re-armed, a fresh expiry, the old bells dropped and a fresh
    help_needed sent. Compare-and-set on status, so one re-ask announces once."""
    hours = int(settings_row.get("help_expire_hours") or 48)
    expires = _ts(help_row.get("expires_at"))
    fresh = now + timedelta(hours=hours)
    moved = (
        _sb().table(HELP).update({
            "status": "open", "claimed_by": None, "claimed_at": None, "reminder_count": 0,
            "reminder_sent_at": _iso(now), "notified_at": _iso(now), "expiry_emailed_at": None,
            "expires_at": _iso(max(fresh, expires) if expires else fresh),
        }).eq("id", str(help_row["id"])).eq("status", "answered").execute()
    ).data or []
    if not moved:
        return False
    dismiss_help_bells(help_row)
    row = moved[0]
    notify_helpers(row, "browser_agent.help_needed", _notice_text(row, reminder=False), job_id=job_id)
    return True


def _insert_agent_message(help_id: str, job_id: str, body: str) -> int:
    row = (
        _sb().table(MESSAGES).insert({
            "help_id": help_id, "author_kind": "agent", "author_label": AGENT_LABEL, "job_id": job_id,
            "action": "message", "body": body,
        }).execute()
    ).data or [{}]
    return int(row[0].get("id") or 0)


def _worker_message(m: dict[str, Any], files: dict[str, dict[str, Any]]) -> protocol.WorkerHelpMessage:
    return protocol.WorkerHelpMessage(
        id=int(m["id"]), author_kind=m["author_kind"], author_label=m.get("author_label"),
        action=m.get("action") or "message",
        body=None if m.get("is_secret") else m.get("body"),
        secret_ref=(protocol.SecretRef(message_id=int(m["id"]), consumed=m.get("secret_consumed_at") is not None)
                    if m.get("is_secret") else None),
        files=[protocol.HelpFile(**_file_view(files[str(a)])) for a in (m.get("artifact_ids") or [])
               if str(a) in files],
        created_at=m["created_at"],
    )


def worker_messages(lease: AgentLease, help_id: str, after: int) -> protocol.HelpMessagesResponse:
    _fence(lease, ["running", "waiting_help"])
    help_row = _waited_help(lease.job_id, help_id)
    rows = messages_for(str(help_row["id"]), after_id=after, kinds=["helper", "system"])
    files = _files({str(a) for m in rows for a in (m.get("artifact_ids") or [])})
    labels = profile_labels({help_row["claimed_by"]}) if help_row.get("claimed_by") else {}
    return protocol.HelpMessagesResponse(
        status=help_row["status"], claimed_by_label=labels.get(str(help_row.get("claimed_by"))),
        messages=[_worker_message(m, files) for m in rows],
    )


def _resume_running(lease: AgentLease, job: dict[str, Any], help_row: dict[str, Any]) -> bool:
    """The job acts on the helper's answer: waiting_help back to running
    (compare-and-set on the lease), so its next open_help is accepted."""
    if job.get("status") != "waiting_help" or str(job.get("waiting_help_id")) != str(help_row["id"]):
        return False
    return bool(
        _sb().table(JOBS).update({"status": "running", "waiting_help_id": None, "hold_until": None})
        .eq("id", lease.job_id).eq("status", "waiting_help").eq("lease_owner", lease.worker.id)
        .eq("lease_epoch", lease.epoch).execute().data
    )


def consume(lease: AgentLease, help_id: str, message_ids: list[int]) -> None:
    job = _fence(lease, ["running", "waiting_help"])
    help_row = _waited_help(lease.job_id, help_id)
    ids = sorted({int(i) for i in message_ids})
    sb = _sb()
    found = sb.table(MESSAGES).select("id").eq("help_id", str(help_row["id"])).in_("id", ids).execute().data or []
    if {int(r["id"]) for r in found} != set(ids):
        raise agent_error(ErrorCode.IDOR, "message does not belong to this help request")
    sb.table(READS).upsert(
        [{"job_id": lease.job_id, "message_id": i, "epoch": lease.epoch} for i in ids],
        on_conflict="job_id,message_id", ignore_duplicates=True,
    ).execute()
    resumed = _resume_running(lease, job, help_row)
    queue.log_step(lease.job_id, lease.epoch, "help.consumed",
                   f"Read {len(ids)} help message(s)" + ("; continuing" if resumed else ""),
                   worker_id=lease.worker.id, actor="bdr",
                   result={"help_id": str(help_row["id"]), "message_ids": ids, "resumed": resumed})


def agent_reply(lease: AgentLease, help_id: str, body: str) -> int:
    _fence(lease, ["running", "waiting_help"])
    help_row = _waited_help(lease.job_id, help_id)
    if help_row["status"] not in ACTIVE:
        raise agent_error(ErrorCode.INVALID_REQUEST, "the help request is closed")
    text = _clean_agent_text(body, protocol.HELP_AGENT_REPLY_MAX, lease.job_id)
    if not text:
        raise agent_error(ErrorCode.INVALID_REQUEST, "empty reply")
    mid = _insert_agent_message(str(help_row["id"]), lease.job_id, text)
    queue.log_step(lease.job_id, lease.epoch, "help.agent_reply", "The agent replied in the help thread",
                   worker_id=lease.worker.id, actor="bdr", result={"help_id": str(help_row["id"]), "message_id": mid})
    return mid


def _decrypt_helper_secret(row: dict[str, Any]) -> str:
    """Help secrets carry no key id: try the current key, then retired ones
    (a rotation inside the 30 min life of a secret)."""
    settings = get_settings()
    ct, nonce = bytea_value(row.get("secret_ciphertext")), bytea_value(row.get("secret_nonce"))
    if not ct or not nonce:
        raise secret_store.SecretDecryptError("no ciphertext")
    aad = secret_store.helper_secret_aad(str(row["secret_id"]))
    key_ids = [settings.browser_agent_secrets_key_id, *settings.browser_agent_old_keys]
    for key_id in key_ids:
        try:
            return secret_store.decrypt_with_aad(ct, nonce, aad, key_id, settings)
        except (secret_store.SecretDecryptError, secret_store.SecretsKeyMissing):
            continue
    raise secret_store.SecretDecryptError("no key opens the helper secret")


def _in_login(job_id: str, now: datetime) -> bool:
    """The job is inside a login it was granted (not reported yet, or
    reported as needing a code / a check) recently enough that its login
    page can still be open: the only time a helper secret may be released."""
    hold = int(queue.load_settings().get("help_hold_seconds") or 900)
    since = _iso(now - timedelta(seconds=hold + LOGIN_SECRET_WINDOW_EXTRA_SECONDS))
    rows = (
        _sb().table("browser_agent_login_attempts").select("outcome, granted, created_at")
        .eq("job_id", job_id).eq("kind", "browser").eq("granted", True).gte("created_at", since)
        .order("created_at", desc=True).limit(1).execute()
    ).data or []
    return bool(rows) and rows[0].get("outcome") in (None, "mfa", "challenge", "captcha", "unexpected", "sso")


def use_secret(lease: AgentLease, help_id: str, message_id: int) -> protocol.HelpSecretUseResponse:
    """One-time release of a helper secret to the login routine of the
    holder job. Compare-and-set on secret_consumed_at; the ciphertext is
    nulled right after (secret_hmac stays for the leak check)."""
    job = _fence(lease, ["running", "waiting_help"])
    help_row = _waited_help(lease.job_id, help_id)
    if not is_login_scope(help_row.get("scope_key")) or str(help_row.get("holder_job_id")) != str(job["id"]):
        raise agent_error(ErrorCode.IDOR, "only the job holding the login page may use a helper secret")
    sb = _sb()
    rows = (
        sb.table(MESSAGES).select("*").eq("id", int(message_id)).eq("help_id", str(help_row["id"])).limit(1).execute()
    ).data or []
    if not rows or not rows[0].get("is_secret"):
        raise agent_error(ErrorCode.IDOR, "not a secret message of this help request")
    msg = rows[0]
    now = _now()
    if msg.get("secret_consumed_at") or msg.get("secret_purged_at") or not msg.get("secret_ciphertext"):
        raise agent_error(ErrorCode.INVALID_REQUEST, "the secret was already used or has expired")
    if not _in_login(lease.job_id, now):
        raise agent_error(ErrorCode.INVALID_REQUEST, "a helper secret is released only during a login")
    try:
        value = _decrypt_helper_secret(msg)
    except (secret_store.SecretDecryptError, secret_store.SecretsKeyMissing):
        logger.error("browser agents: helper secret %s could not be decrypted", message_id)
        raise agent_error(ErrorCode.INVALID_REQUEST, "the secret could not be read") from None
    taken = (
        sb.table(MESSAGES).update({"secret_consumed_at": _iso(now), "secret_consumed_by_job": lease.job_id})
        .eq("id", int(message_id)).is_("secret_consumed_at", "null").execute()
    ).data or []
    if not taken:
        raise agent_error(ErrorCode.INVALID_REQUEST, "the secret was already used or has expired")
    sb.table(MESSAGES).update({"secret_ciphertext": None, "secret_nonce": None}).eq("id", int(message_id)).execute()
    # The worker never /consumes a secret message, so redeeming it is what
    # ends the wait (a wrong code then reopens help from `running`).
    _resume_running(lease, job, help_row)
    queue.log_step(lease.job_id, lease.epoch, "help.secret_used", "Used a helper code in the login (value never stored)",
                   worker_id=lease.worker.id, actor="bdr",
                   result={"help_id": str(help_row["id"]), "message_id": int(message_id)})
    return protocol.HelpSecretUseResponse(value=value)


def job_rfi_ids(job: dict[str, Any]) -> set[str]:
    """The RFI ids a job may hand helper files and notes to, from what BDR
    itself verified (the worker is untrusted): the seeded rfi_id; for a
    locate job (no rfi_id), the target of its accepted captures and the ids
    in its own hash-checked rfi_list_json artifacts."""
    from pydantic import ValidationError

    from app.services.browser_agents.pm_apps import procore as procore_app

    try:
        seed = protocol.RfiFromEmailInput.model_validate(job.get("input") or {})
    except ValidationError:
        return set()
    if seed.rfi_id:
        return {str(seed.rfi_id)}
    sb = _sb()
    ids = {
        str(r["target_external_rfi_id"]) for r in (
            sb.table("pm_rfi_sync_runs").select("target_external_rfi_id").eq("agent_job_id", str(job["id"]))
            .execute().data or []
        ) if r.get("target_external_rfi_id")
    }
    raws: list[bytes] = []
    for a in (
        sb.table(ARTIFACTS).select("id, storage_path, sha256").eq("job_id", str(job["id"]))
        .eq("kind", "rfi_list_json").execute().data or []
    ):
        try:
            blob = artifacts.download_object(a["storage_path"])
        except Exception:  # noqa: BLE001 - a missing list only narrows what can be proved
            continue
        if hashlib.sha256(blob).hexdigest() == a.get("sha256"):
            raws.append(blob)
    return ids | procore_app.list_ids(raws, seed.external_project_id)


def contributions(lease: AgentLease, req: protocol.ContributionsRequest) -> protocol.ContributionsResponse:
    """Helper files and notes the agent hands to the RFI (D15). A file must
    be a helper upload of THIS help request, marked attachable, never zip or
    dwg; a note is the verbatim text of the cited helper messages. Rows
    apply when the RFI exists (now, or when the sweep first imports it)."""
    from app.services.browser_agents import contributions as contrib

    job = _fence(lease, ["running", "waiting_help"])
    if job.get("principal_kind", "org") != "org":
        raise agent_error(ErrorCode.IDOR, "User tasks cannot contribute to BDR records")
    help_row = _waited_help(lease.job_id, str(req.help_id))
    if not job.get("project_id") or not job.get("platform"):
        raise agent_error(ErrorCode.INVALID_REQUEST, "this job has no linked project")
    if req.external_rfi_id not in job_rfi_ids(job):
        raise agent_error(ErrorCode.IDOR, "that RFI is not this job's RFI")
    sb = _sb()
    accepted: list[protocol.ContributionAccepted] = []
    rejected: list[protocol.ContributionRejected] = []
    for index, item in enumerate(req.items):
        row: dict[str, Any] = {
            "project_id": str(job["project_id"]), "platform": job["platform"],
            "external_rfi_id": req.external_rfi_id, "kind": item.kind, "help_id": str(help_row["id"]),
            "job_id": lease.job_id,
        }
        if isinstance(item, protocol.ContributionFileItem):
            files = _files({str(item.artifact_id)})
            a = files.get(str(item.artifact_id))
            if not a or a.get("kind") != "helper_upload" or str(a.get("help_id")) != str(help_row["id"]):
                rejected.append(protocol.ContributionRejected(index=index, reason="not_this_help"))
                continue
            if not a.get("attachable") or str(a.get("filename") or "").lower().endswith(contrib.NEVER_ATTACH_EXTENSIONS):
                rejected.append(protocol.ContributionRejected(index=index, reason="not_attachable"))
                continue
            row["artifact_id"] = str(item.artifact_id)
            existing_q = sb.table(CONTRIBUTIONS).select("id, job_id").eq("artifact_id", str(item.artifact_id))
        else:
            ids = sorted({int(i) for i in item.message_ids})
            msgs = (
                sb.table(MESSAGES).select("id, author_kind, author_label, body, is_secret")
                .eq("help_id", str(help_row["id"])).in_("id", ids).order("id").execute()
            ).data or []
            if len(msgs) != len(ids) or any(m.get("author_kind") != "helper" or m.get("is_secret")
                                            or not m.get("body") for m in msgs):
                rejected.append(protocol.ContributionRejected(index=index, reason="not_helper_message"))
                continue
            text = "\n\n".join(str(m["body"]) for m in msgs)[:HUMAN_BODY_MAX]
            row.update(note_text=text, source_message_ids=ids,
                       note_author_label=", ".join(dict.fromkeys(m.get("author_label") or "Helper" for m in msgs))[:200])
            existing_q = sb.table(CONTRIBUTIONS).select("id, job_id, note_text").eq("kind", "note")
        try:
            inserted = sb.table(CONTRIBUTIONS).insert(row).execute().data or []
        except Exception as exc:  # noqa: BLE001 - the unique index: the same item was given before
            if not _is_unique_violation(exc):
                raise
            same = [
                r for r in (existing_q.eq("project_id", str(job["project_id"]))
                            .eq("external_rfi_id", req.external_rfi_id).execute().data or [])
                if row.get("kind") != "note" or r.get("note_text") == row.get("note_text")
            ]
            if same and str(same[0].get("job_id")) == lease.job_id:
                accepted.append(protocol.ContributionAccepted(index=index, contribution_id=same[0]["id"]))
            else:
                rejected.append(protocol.ContributionRejected(index=index, reason="duplicate"))
            continue
        accepted.append(protocol.ContributionAccepted(index=index, contribution_id=inserted[0]["id"]))
    if accepted:
        queue.log_step(lease.job_id, lease.epoch, "help.contribution",
                       f"{len(accepted)} helper contribution(s) recorded for RFI {req.external_rfi_id}",
                       worker_id=lease.worker.id, actor="bdr",
                       result={"help_id": str(help_row["id"]), "accepted": [str(a.contribution_id) for a in accepted],
                               "rejected": len(rejected)})
        try:
            contrib.apply_pending(sb, str(job["project_id"]))
        except Exception:  # noqa: BLE001 - the sweeper applies them later
            logger.exception("browser agents: applying contributions now failed; the sweeper retries")
    return protocol.ContributionsResponse(accepted=accepted, rejected=rejected)


def requeue_if_answered(job_id: str) -> bool:
    """After a park: an answer that arrived while the page was held but was
    never read must not leave the job parked until expiry."""
    rows = _sb().table(JOBS).select("*").eq("id", job_id).limit(1).execute().data or []
    if not rows:
        return False
    return _requeue_parked_if_unread(rows[0], _now())


def _unread_helper_messages(job_id: str, help_id: str) -> bool:
    msgs = (
        _sb().table(MESSAGES).select("id, is_secret").eq("help_id", help_id).eq("author_kind", "helper").execute()
    ).data or []
    ids = [int(m["id"]) for m in msgs if not m.get("is_secret")]
    if not ids:
        return False
    reads = _sb().table(READS).select("message_id").eq("job_id", job_id).in_("message_id", ids).execute().data or []
    return len({int(r["message_id"]) for r in reads}) < len(ids)


def _requeue_parked_if_unread(job: dict[str, Any], now: datetime) -> bool:
    hid = job.get("waiting_help_id")
    if job.get("status") != "waiting_help" or job.get("lease_owner") or not hid:
        return False
    help_row = get_help(str(hid))
    if not help_row or help_row.get("status") != "answered" or not _unread_helper_messages(str(job["id"]), str(hid)):
        return False
    if requeue_waiter(job, now):
        queue.log_step(str(job["id"]), int(job.get("lease_epoch") or 0), "help.requeued",
                       "Requeued: a helper answered while the job was parked", actor="bdr",
                       result={"help_id": str(hid)})
        return True
    return False


# ── Human side ──────────────────────────────────────────────────────────────────


def _require_active(help_row: dict[str, Any]) -> None:
    if help_row.get("principal_kind", "org") != "org":
        raise human_error(404, "not_found", "No such help request.")
    if help_row.get("status") not in ACTIVE:
        raise human_error(status.HTTP_409_CONFLICT, "help_closed", "This help request is closed.")


def help_rows_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """C2 HelpRow per request (bulk: one query per dimension)."""
    if not rows:
        return []
    ids = [str(r["id"]) for r in rows]
    waiters = _sb().table(WAITERS).select("help_id, job_id").in_("help_id", ids).execute().data or []
    jobs = {str(j["id"]): j for j in _jobs(sorted({str(w["job_id"]) for w in waiters}))}
    waiting: dict[str, int] = {}
    for w in waiters:
        if (jobs.get(str(w["job_id"])) or {}).get("status") == "waiting_help":
            waiting[str(w["help_id"])] = waiting.get(str(w["help_id"]), 0) + 1
    names = project_names({r["project_id"] for r in rows if r.get("project_id")})
    labels = profile_labels({r["claimed_by"] for r in rows if r.get("claimed_by")})
    return [{
        "id": str(r["id"]), "scope_key": r["scope_key"], "reason_code": r["reason_code"], "title": r["title"],
        "status": r["status"], "project_id": r.get("project_id"),
        "project_name": names.get(str(r.get("project_id"))) if r.get("project_id") else None,
        "claimed_by_label": labels.get(str(r.get("claimed_by"))) if r.get("claimed_by") else None,
        "waiting_jobs": waiting.get(str(r["id"]), 0),
        "created_at": r.get("created_at"), "expires_at": r.get("expires_at"),
    } for r in rows]


def help_detail(help_row: dict[str, Any], user: Any) -> dict[str, Any]:
    view = help_rows_view([help_row])[0]
    jobs = _jobs(waiter_ids(str(help_row["id"])))
    labels = rfi_labels(jobs)
    names = project_names({j["project_id"] for j in jobs if j.get("project_id")})
    return {
        **view,
        "agent_message": help_row.get("agent_message"),
        "screenshot_artifact_id": help_row.get("screenshot_artifact_id"),
        "is_login_scope": is_login_scope(help_row.get("scope_key")),
        "claimed_by_me": bool(help_row.get("claimed_by")) and str(help_row.get("claimed_by")) == str(user.id),
        "waiters": [{"job_id": str(j["id"]), "rfi_label": labels.get(str(j["id"])), "status": j["status"],
                     "project_name": names.get(str(j.get("project_id"))) if j.get("project_id") else None}
                    for j in sorted(jobs, key=lambda j: str(j.get("created_at") or ""))],
        "messages": human_messages(str(help_row["id"])),
    }


def _password_fingerprints() -> tuple[set[str], str]:
    """(fingerprints of the Procore password(s), the current password) for
    the looks_like_secret guard. Never raises."""
    fps: set[str] = set()
    try:
        row = secret_store.load_row("procore_password")
        if row and row.get("fingerprint"):
            fps.add(str(row["fingerprint"]))
    except Exception:  # noqa: BLE001
        logger.warning("browser agents: password fingerprint unreadable")
    password = ""
    try:
        from app.services import procore_creds

        password = procore_creds.password(get_settings()) or ""
        if password:
            fps.add(secret_store.fingerprint(password))
    except Exception:  # noqa: BLE001
        logger.warning("browser agents: password unreadable for the help guard")
    return fps, password


def looks_like_secret(body: str, scope_key: str) -> bool:
    """A plaintext message that is (or carries) the Procore password, or, in
    a login scope, is nothing but a 4 to 10 digit code."""
    text = (body or "").strip()
    if not text:
        return False
    if is_login_scope(scope_key) and LOOKS_LIKE_CODE_RE.fullmatch(re.sub(r"[\s-]", "", text)):
        return True
    fps, password = _password_fingerprints()
    if password and len(password) >= 6 and password in text:
        return True
    if fps:
        for candidate in {text, *text.split()}:
            try:
                if secret_store.fingerprint(candidate) in fps:
                    return True
            except Exception:  # noqa: BLE001 - no fingerprint key: the plain check above still ran
                break
    return False


@dataclass(frozen=True)
class HumanMessage:
    client_msg_id: str
    action: str = "message"
    body: str | None = None
    is_secret: bool = False
    secret_id: str | None = None
    secret_value: str | None = None
    artifact_ids: tuple[str, ...] = ()


def _existing_message(help_id: str, client_msg_id: str) -> dict[str, Any] | None:
    rows = (
        _sb().table(MESSAGES).select(MESSAGE_COLUMNS).eq("help_id", help_id).eq("client_msg_id", client_msg_id)
        .limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _view_one(m: dict[str, Any]) -> dict[str, Any]:
    return message_view(m, _files({str(a) for a in (m.get("artifact_ids") or [])}))


def post_message(help_row: dict[str, Any], user: Any, msg: HumanMessage) -> dict[str, Any]:
    """A helper's message or button. Idempotent on client_msg_id: a resend
    returns the first message and does nothing again."""
    if help_row.get("principal_kind", "org") != "org":
        raise human_error(404, "not_found", "No such help request.")
    help_id = str(help_row["id"])
    existing = _existing_message(help_id, msg.client_msg_id)
    if existing:
        return _view_one(existing)
    _require_active(help_row)
    scope = help_row.get("scope_key") or ""
    body = (msg.body or "").strip() or None
    if body and len(body) > HUMAN_BODY_MAX:
        raise human_error(422, "invalid_request", "The message is too long.")
    row: dict[str, Any] = {
        "help_id": help_id, "author_kind": "helper", "author_id": str(user.id), "author_label": user_label(user),
        "client_msg_id": msg.client_msg_id, "action": msg.action,
    }
    if msg.is_secret:
        if not is_login_scope(scope):
            raise human_error(422, "secret_not_allowed", "A code can be sent only on a Procore login request.")
        value = (msg.secret_value or "").strip()
        if (body or msg.artifact_ids or msg.action != "message" or not msg.secret_id or not value
                or len(value) > SECRET_VALUE_MAX or any(c in value for c in "\r\n\x00")):
            raise human_error(422, "invalid_request",
                              "A code needs secret_id and secret_value (1 to 64 characters) and nothing else.")
        try:
            ciphertext, nonce, _key_id = secret_store.encrypt_with_aad(
                value, secret_store.helper_secret_aad(msg.secret_id))
            digest = redaction.secret_hmac(value)
        except Exception:  # noqa: BLE001 - no key configured: refuse, never store plaintext
            logger.error("browser agents: helper secret could not be encrypted")
            raise human_error(409, "secrets_key_missing", "Secrets cannot be stored on this server.") from None
        _require_agent_at_login(help_row)
        row.update(is_secret=True, secret_id=msg.secret_id, secret_ciphertext=bytea_literal(ciphertext),
                   secret_nonce=bytea_literal(nonce), secret_hmac=digest, body=None)
    else:
        if msg.secret_id or msg.secret_value:
            raise human_error(422, "invalid_request", "secret_id and secret_value need is_secret.")
        if msg.action == "message" and not body and not msg.artifact_ids:
            raise human_error(422, "invalid_request", "The message is empty.")
        if body and looks_like_secret(body, scope):
            raise human_error(422, "looks_like_secret",
                              "That looks like a password or a code. Tick 'This is a code' to send it securely.")
        row["body"] = body
    if msg.artifact_ids:
        files = _files(set(msg.artifact_ids))
        if len(files) != len(set(msg.artifact_ids)) or any(
            f.get("kind") != "helper_upload" or str(f.get("help_id")) != help_id for f in files.values()
        ):
            raise human_error(422, "invalid_request", "A file is not an upload of this help request.")
        row["artifact_ids"] = sorted(set(msg.artifact_ids))
    if msg.action == "retry":
        _check_retry_allowed(help_row, user)
    try:
        inserted = (_sb().table(MESSAGES).insert(row).execute()).data or []
    except Exception as exc:  # noqa: BLE001
        if not _is_unique_violation(exc):
            raise
        again = _existing_message(help_id, msg.client_msg_id)
        if again:
            return _view_one(again)
        raise human_error(409, "invalid_request", "That code id was used before; send it again.") from None
    message = inserted[0]
    _after_message(help_row, user, message, row.get("author_label"))
    return _view_one(message)


NO_AGENT_AT_LOGIN_TEXT = ("No browser agent is on the Procore login page right now, so a code would be wasted. "
                          "Press Retry now to start a new login, then send the new code when it is asked for.")


def _require_agent_at_login(help_row: dict[str, Any]) -> None:
    """A code is useful only while the holder job still holds its login page
    (waiting_help with a live lease and hold, inside a granted login). Any
    other time it would be stored, never used and purged, while every other
    Procore job stays parked behind the request: refuse it instead."""
    now = _now()
    holder = _jobs([str(help_row["holder_job_id"])]) if help_row.get("holder_job_id") else []
    job = holder[0] if holder else {}
    hold = _ts(job.get("hold_until"))
    live = (
        job.get("status") == "waiting_help" and bool(job.get("lease_owner"))
        and str(job.get("waiting_help_id")) == str(help_row["id"])
        and hold is not None and hold > now
    )
    if not live or not _in_login(str(job["id"]), now):
        raise human_error(status.HTTP_409_CONFLICT, "no_agent_at_login", NO_AGENT_AT_LOGIN_TEXT)


def _check_retry_allowed(help_row: dict[str, Any], user: Any) -> None:
    """A cap_reached retry resets the job's counters once for a helper;
    after that only IT Admin (or a dev account) may retry it."""
    if help_row.get("reason_code") != "cap_reached" or is_admin(user):
        return
    jobs = _jobs(waiter_ids(str(help_row["id"])))
    if any(j.get("helper_retry_used") and j.get("status") == "waiting_help" for j in jobs):
        raise human_error(403, "retry_admin_only", "This job was already retried once; IT Admin can retry it again.")


def is_admin(user: Any) -> bool:
    role = getattr(getattr(user, "role", None), "value", getattr(user, "role", None))
    return role == Role.IT_ADMIN.value or bool(getattr(user, "is_dev", False))


def _after_message(help_row: dict[str, Any], user: Any, message: dict[str, Any], label: str | None) -> None:
    sb = _sb()
    now = _now()
    help_id = str(help_row["id"])
    action = message.get("action") or "message"
    sb.table(HELP).update({"last_helper_activity_at": _iso(now)}).eq("id", help_id).execute()
    jobs = _jobs(waiter_ids(help_id))
    step_result = {"help_id": help_id, "message_id": int(message["id"]), "action": action}
    affected = 0
    if action == "message":
        if not message.get("is_secret"):
            # A code alone is not an answer: the request stays open (and its
            # reminders keep running) until the code is used and the login works.
            sb.table(HELP).update({"status": "answered"}).eq("id", help_id).eq("status", "open").execute()
        for job in jobs:
            # A code helps only the job still holding the login page; a parked
            # job starts a fresh login on its next attempt.
            parked = job.get("status") == "waiting_help" and not job.get("lease_owner")
            if parked and not message.get("is_secret") and requeue_waiter(job, now):
                affected += 1
                helper_step(job, "help.answered", "A helper answered; requeued", user_id=str(user.id),
                            label=label, result=step_result)
            elif job.get("status") in LIVE_JOB:
                helper_step(job, "help.message", "A helper sent a code" if message.get("is_secret")
                            else "A helper answered", user_id=str(user.id), label=label, result=step_result)
    elif action == "retry":
        _resolve(help_row, now, "retry", user_id=str(user.id))
        for job in jobs:
            if job.get("status") != "waiting_help":
                continue
            extra = cap_reset_patch(job, now, label) if help_row.get("reason_code") == "cap_reached" else None
            if requeue_waiter(job, now, extra=extra):
                affected += 1
                helper_step(job, "help.retry", "Retry now: requeued by a helper", user_id=str(user.id),
                            label=label, result={**step_result, "counters_reset": bool(extra)})
    elif action in ("give_up", "skip_daily"):
        _resolve(help_row, now, action, user_id=str(user.id))
        if str(help_row.get("scope_key") or "").startswith("link:"):
            _set_link_error(help_row.get("project_id"),
                            "A helper gave up on browser agent access for this link."
                            if action == "give_up" else "A helper skipped this link; the daily sync will catch it.")
        for job in jobs:
            if job.get("status") not in LIVE_JOB:
                continue
            if action == "give_up":
                ok = close_waiter(job, now, status_="failed", error_kind="help_declined",
                                  error="A helper gave up on this job")
                kind, summary = "help.give_up", "Give up: failed by a helper"
            else:
                ok = close_waiter(job, now, status_="canceled", error_kind="skipped_by_helper",
                                  error="A helper skipped this job; the daily sync will catch it")
                kind, summary = "help.skip", "Skip: canceled by a helper (the daily sync will catch it)"
            if ok:
                affected += 1
                helper_step(job, kind, summary, user_id=str(user.id), label=label, result=step_result)
    _audit(str(user.id), "browser_agent.help_reply", help_id,
           {"action": action, "message_id": int(message["id"]), "is_secret": bool(message.get("is_secret")),
            "files": len(message.get("artifact_ids") or []), "jobs_affected": affected, "actor_label": label})


def _sniff_upload(ext: str, data: bytes) -> str:
    """The kind the bytes really are; must agree with the extension."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"AC10"):            # AutoCAD drawing: AC1015, AC1018, AC1032 ...
        return "dwg"
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except (zipfile.BadZipFile, ValueError):
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return "zip"
    if ext == ".txt" and b"\x00" not in data:
        try:
            data.decode("utf-8")
            return "txt"
        except UnicodeDecodeError:
            return "unknown"
    return "unknown"


def upload_file(help_row: dict[str, Any], user: Any, filename: str, stream: BinaryIO,
                attachable: bool) -> dict[str, Any]:
    """Store one helper file for this request (not in the thread until a
    message cites it). 150 MB cap, magic-byte sniff agreeing with the
    extension, allowlist; zip and dwg are never attachable."""
    _require_active(help_row)
    safe = artifacts.safe_filename(filename or "file")
    ext = ("." + safe.rsplit(".", 1)[-1].lower()) if "." in safe else ""
    rule = UPLOAD_TYPES.get(ext)
    if rule is None:
        raise human_error(422, "file_not_allowed", "Only pdf, png, jpg, docx, xlsx, txt, zip and dwg files.")
    try:
        data = artifacts.read_capped(stream, UPLOAD_MAX_BYTES)
    except artifacts.ArtifactTooLarge:
        raise human_error(413, "file_too_large", "The file is over 150 MB.") from None
    if not data:
        raise human_error(422, "file_not_allowed", "The file is empty.")
    kind, mime, may_attach = rule
    if _sniff_upload(ext, data) != kind:
        raise human_error(422, "file_not_allowed", "The file's contents do not match its extension.")
    try:
        scan.scan_upload(data, safe, mime)
    except scan.ScanRejected as exc:
        raise human_error(422, "file_not_allowed", f"The file was rejected by the scan: {exc}") from None
    digest = hashlib.sha256(data).hexdigest()
    help_id = str(help_row["id"])
    path = f"help/{help_id}/{digest}{ext}"
    sb = _sb()
    existing = sb.table(ARTIFACTS).select("*").eq("storage_path", path).limit(1).execute().data or []
    if existing:
        a = existing[0]
        if attachable and may_attach and not a.get("attachable"):
            a = (sb.table(ARTIFACTS).update({"attachable": True}).eq("id", a["id"]).execute().data or [a])[0]
        return _file_view(a)
    artifacts.upload_object(path, data, mime)
    row = {
        "help_id": help_id, "kind": "helper_upload", "storage_path": path, "filename": safe,
        "mime_type": mime, "sniffed_mime": mime, "size_bytes": len(data), "sha256": digest,
        "attachable": bool(attachable and may_attach), "uploaded_by_user": str(user.id),
    }
    try:
        a = (sb.table(ARTIFACTS).insert(row).execute()).data[0]
    except Exception as exc:  # noqa: BLE001 - the same file uploaded twice at once
        if not _is_unique_violation(exc):
            raise
        a = (sb.table(ARTIFACTS).select("*").eq("storage_path", path).limit(1).execute().data or [row])[0]
    sb.table(HELP).update({"last_helper_activity_at": _iso(_now())}).eq("id", help_id).execute()
    _audit(str(user.id), "browser_agent.help_file", help_id,
           {"artifact_id": str(a.get("id")), "filename": safe, "size_bytes": len(data),
            "attachable": bool(a.get("attachable")), "actor_label": user_label(user)})
    return _file_view(a)


def claim(help_row: dict[str, Any], user: Any) -> None:
    """'I'm on it'. Lapses after help_claim_lapse_minutes with no helper activity."""
    _require_active(help_row)
    now = _now()
    label = user_label(user)
    _sb().table(HELP).update({"claimed_by": str(user.id), "claimed_at": _iso(now),
                              "last_helper_activity_at": _iso(now)}).eq("id", str(help_row["id"])).execute()
    for job in _jobs(waiter_ids(str(help_row["id"]))):
        if job.get("status") in LIVE_JOB:
            helper_step(job, "help.claimed", "A helper is on it", user_id=str(user.id), label=label,
                        result={"help_id": str(help_row["id"])})
    _audit(str(user.id), "browser_agent.help_claim", str(help_row["id"]), {"claimed": True, "actor_label": label})


def unclaim(help_row: dict[str, Any], user: Any) -> None:
    _require_active(help_row)
    label = user_label(user)
    _sb().table(HELP).update({"claimed_by": None, "claimed_at": None}).eq("id", str(help_row["id"])).execute()
    _audit(str(user.id), "browser_agent.help_claim", str(help_row["id"]), {"claimed": False, "actor_label": label})


# ── Sweeper steps ───────────────────────────────────────────────────────────────


def sweep_reminders(now: datetime, settings_row: dict[str, Any]) -> int:
    """browser_agent.help_reminder after help_reminder_minutes while open and
    unclaimed, at most 3 (compare-and-set on reminder_count)."""
    every = timedelta(minutes=int(settings_row.get("help_reminder_minutes") or 120))
    rows = (
        _sb().table(HELP).select("*").eq("principal_kind", "org").eq("status", "open").is_("claimed_by", "null")
        .lt("reminder_count", MAX_REMINDERS).not_.is_("notified_at", "null")
        .limit(queue.SWEEP_BATCH).execute()
    ).data or []
    sent = 0
    for row in rows:
        last = _ts(row.get("reminder_sent_at")) or _ts(row.get("notified_at"))
        if last is None or last > now - every:
            continue
        count = int(row.get("reminder_count") or 0)
        claimed = (
            _sb().table(HELP).update({"reminder_count": count + 1, "reminder_sent_at": _iso(now)})
            .eq("id", row["id"]).eq("reminder_count", count).eq("status", "open").execute()
        ).data or []
        if claimed:
            notify_helpers(row, "browser_agent.help_reminder", _notice_text(row, reminder=True))
            sent += 1
    return sent


def sweep_claim_lapse(now: datetime, settings_row: dict[str, Any]) -> int:
    """A claim with no helper activity for help_claim_lapse_minutes lapses:
    the request is unclaimed and its reminders re-arm (one goes out now)."""
    lapse = timedelta(minutes=int(settings_row.get("help_claim_lapse_minutes") or 30))
    rows = (
        _sb().table(HELP).select("*").eq("principal_kind", "org").in_("status", ACTIVE).not_.is_("claimed_by", "null")
        .limit(queue.SWEEP_BATCH).execute()
    ).data or []
    done = 0
    for row in rows:
        last = _ts(row.get("last_helper_activity_at")) or _ts(row.get("claimed_at"))
        if last is not None and last > now - lapse:
            continue
        cleared = (
            _sb().table(HELP).update({"claimed_by": None, "claimed_at": None, "reminder_count": 1,
                                      "reminder_sent_at": _iso(now)})
            .eq("id", row["id"]).eq("claimed_by", str(row["claimed_by"])).execute()
        ).data or []
        if cleared:
            notify_helpers(row, "browser_agent.help_reminder", _notice_text(row, reminder=True))
            done += 1
    return done


def sweep_resolve_finished(now: datetime, settings_row: dict[str, Any]) -> int:
    """A job: or link: request whose waiters all left the help (finished,
    applying) is resolved (agent_continued). Login requests resolve on the
    next good login instead (identities.py)."""
    rows = (
        _sb().table(HELP).select("*").eq("principal_kind", "org").in_("status", ACTIVE).limit(queue.SWEEP_BATCH).execute()
    ).data or []
    done = 0
    for row in rows:
        if is_login_scope(row.get("scope_key")):
            continue
        jobs = _jobs(waiter_ids(str(row["id"])))
        if not jobs or any(j.get("status") in LIVE_JOB for j in jobs):
            continue
        if _resolve(row, now, "agent_continued"):
            done += 1
    return done


def _login_blocked(identity: str, now: datetime) -> bool:
    """Whether a login request must stay open past its expiry: the account
    is locked, or the last login attempt needed a code or a robot check."""
    rows = (
        _sb().table("rfp_harvest_sessions").select("locked_until").eq("provider", identity).limit(1).execute()
    ).data or []
    until = _ts((rows[0] if rows else {}).get("locked_until"))
    if until is not None and until > now:
        return True
    last = (
        _sb().table("browser_agent_login_attempts").select("outcome").eq("identity", identity).eq("kind", "browser")
        .not_.is_("outcome", "null").order("created_at", desc=True).limit(1).execute()
    ).data or []
    return bool(last) and last[0].get("outcome") in ("mfa", "captcha", "challenge")


def sweep_expire(now: datetime, settings_row: dict[str, Any]) -> int:
    """At expires_at: job: and link: requests expire and their waiting jobs
    fail help_timeout (Estimating Admin bell). A login: request stays open
    while the account is locked or the last login needed a code or a check:
    IT Admin gets one email instead (compare-and-set on expiry_emailed_at)."""
    rows = (
        _sb().table(HELP).select("*").eq("principal_kind", "org").in_("status", ACTIVE).lt("expires_at", _iso(now))
        .limit(queue.SWEEP_BATCH).execute()
    ).data or []
    done = 0
    for row in rows:
        scope = str(row.get("scope_key") or "")
        if is_login_scope(scope) and _login_blocked(scope.split(":", 1)[1], now):
            if not row.get("expiry_emailed_at"):
                claimed = (
                    _sb().table(HELP).update({"expiry_emailed_at": _iso(now)}).eq("id", row["id"])
                    .is_("expiry_emailed_at", "null").execute()
                ).data or []
                if claimed:
                    queue.notify_it_admin(
                        "browser_agent.help_reminder",
                        f"A Procore login help request is still open past its expiry: {row.get('title')}. "
                        "Browser agent jobs that log in are waiting. Check the Procore account and the "
                        "password in Settings > Browser agents.",
                        {"help_id": str(row["id"]), "job_id": str(row.get("opened_by_job_id") or ""),
                         "reason": row.get("reason_code")},
                        email=True,
                    )
                    done += 1
            continue
        if not _resolve(row, now, "expired", new_status="expired"):
            continue
        done += 1
        first_job = None
        for job in _jobs(waiter_ids(str(row["id"]))):
            if job.get("status") != "waiting_help":
                continue
            if close_waiter(job, now, status_="failed", error_kind="help_timeout",
                            error="Nobody answered the help request in time"):
                first_job = first_job or job
                queue.log_step(str(job["id"]), int(job.get("lease_epoch") or 0), "help.expired",
                               "Failed: the help request expired", actor="system",
                               result={"help_id": str(row["id"])})
        try:
            notifications.notify_role(
                Role.ESTIMATING_ADMIN, row.get("project_id"), "browser_agent.help_expired",
                f"A browser agent help request expired with no answer: {row.get('title')}. "
                "Its waiting jobs failed; they can be retried from the Agents page.",
                metadata={"help_id": str(row["id"]),
                          "job_id": str((first_job or {}).get("id") or row.get("opened_by_job_id") or ""),
                          "reason": row.get("reason_code")},
            )
        except Exception:  # noqa: BLE001
            logger.exception("browser agents: help_expired notice failed")
    return done


def sweep_secret_purge(now: datetime, settings_row: dict[str, Any]) -> int:
    """Unconsumed helper secrets lose their ciphertext after 30 min; every
    secret's HMAC (kept for the leak check) is nulled after 7 days."""
    sb = _sb()
    done = 0
    stale = (
        sb.table(MESSAGES).select("id, help_id").eq("is_secret", True).is_("secret_consumed_at", "null")
        .is_("secret_purged_at", "null").lt("created_at", _iso(now - timedelta(minutes=SECRET_PURGE_MINUTES)))
        .limit(queue.SWEEP_BATCH).execute()
    ).data or []
    for m in stale:
        parent = get_help(str(m.get("help_id") or ""))
        if not parent or parent.get("principal_kind", "org") != "org":
            continue
        moved = (
            sb.table(MESSAGES).update({"secret_ciphertext": None, "secret_nonce": None, "secret_purged_at": _iso(now)})
            .eq("id", m["id"]).is_("secret_consumed_at", "null").is_("secret_purged_at", "null").execute()
        ).data or []
        done += len(moved)
    old = (
        sb.table(MESSAGES).select("id, help_id").eq("is_secret", True).not_.is_("secret_hmac", "null")
        .lt("created_at", _iso(now - timedelta(days=SECRET_HMAC_KEEP_DAYS))).limit(queue.SWEEP_BATCH).execute()
    ).data or []
    for m in old:
        parent = get_help(str(m.get("help_id") or ""))
        if not parent or parent.get("principal_kind", "org") != "org":
            continue
        sb.table(MESSAGES).update({"secret_hmac": None, "secret_ciphertext": None, "secret_nonce": None}).eq(
            "id", m["id"]).execute()
        done += 1
    return done


def sweep_answered_parked(now: datetime, settings_row: dict[str, Any]) -> int:
    """Parked waiters of an answered request whose answer they never read."""
    jobs = (
        _sb().table(JOBS).select("*").eq("principal_kind", "org").eq("status", "waiting_help").is_("lease_owner", "null")
        .not_.is_("waiting_help_id", "null").limit(queue.SWEEP_BATCH).execute()
    ).data or []
    return sum(1 for j in jobs if _requeue_parked_if_unread(j, now))


CLOSED_HELP_GRACE_SECONDS = 120


def sweep_parked_closed_help(now: datetime, settings_row: dict[str, Any]) -> int:
    """A parked waiter (waiting_help, no lease) whose request is no longer
    active, or that names no request at all, would wait forever: every
    other step looks only at active requests. Requeue it uncharged
    (help_answered). A request closed in the last two minutes is left to the
    path that closed it (Retry now requeues its waiters itself)."""
    jobs = (
        _sb().table(JOBS).select("*").eq("principal_kind", "org").eq("status", "waiting_help").is_("lease_owner", "null")
        .limit(queue.SWEEP_BATCH).execute()
    ).data or []
    done = 0
    for job in jobs:
        hid = job.get("waiting_help_id")
        help_row = get_help(str(hid)) if hid else None
        if help_row is not None and help_row.get("status") in ACTIVE:
            continue
        closed = _ts((help_row or {}).get("resolved_at"))
        if closed is not None and closed > now - timedelta(seconds=CLOSED_HELP_GRACE_SECONDS):
            continue
        if requeue_waiter(job, now):
            queue.log_step(str(job["id"]), int(job.get("lease_epoch") or 0), "help.requeued",
                           "Requeued: its help request is closed", actor="system",
                           result={"help_id": str(hid) if hid else None,
                                   "help_status": (help_row or {}).get("status")})
            done += 1
    return done


def sweep_dead_login_holders(now: datetime, settings_row: dict[str, Any]) -> int:
    """An active login request whose holder job ended can never be finished
    by that job (only the holder logs in or redeems a code). Its parked
    waiters are requeued: the first to ask for the session takes the
    request over (identities._blocking_help) and logs in."""
    rows = (
        _sb().table(HELP).select("*").eq("principal_kind", "org").in_("status", ACTIVE).like("scope_key", "login:%")
        .limit(queue.SWEEP_BATCH).execute()
    ).data or []
    done = 0
    for row in rows:
        holder = _jobs([str(row["holder_job_id"])]) if row.get("holder_job_id") else []
        if holder and holder[0].get("status") in LIVE_JOB:
            continue
        for job in _jobs(waiter_ids(str(row["id"]))):
            if job.get("status") != "waiting_help" or job.get("lease_owner"):
                continue
            if requeue_waiter(job, now):
                queue.log_step(str(job["id"]), int(job.get("lease_epoch") or 0), "help.requeued",
                               "Requeued: the job holding the login help request has ended", actor="system",
                               result={"help_id": str(row["id"])})
                done += 1
    return done


def register_sweep_steps() -> None:
    queue.register_sweep_step("help_reminders", sweep_reminders)
    queue.register_sweep_step("help_parked_closed", sweep_parked_closed_help)
    queue.register_sweep_step("help_dead_login_holders", sweep_dead_login_holders)
    queue.register_sweep_step("help_claim_lapse", sweep_claim_lapse)
    queue.register_sweep_step("help_answered_parked", sweep_answered_parked)
    queue.register_sweep_step("help_resolve_finished", sweep_resolve_finished)
    queue.register_sweep_step("help_expire", sweep_expire)
    queue.register_sweep_step("help_secret_purge", sweep_secret_purge)


__all__ = [
    "open_help", "worker_messages", "consume", "agent_reply", "use_secret", "contributions",
    "post_message", "upload_file", "claim", "unclaim", "help_detail", "help_rows_view", "human_messages",
    "register_sweep_steps", "HumanMessage", "looks_like_secret", "notify_help_needed", "rfi_labels",
]

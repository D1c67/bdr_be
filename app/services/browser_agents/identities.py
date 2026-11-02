"""Shared PM-app login for the browser agents (docs/browser_agents/SPEC.md 5.5
as replaced by override O2: the agent logs in itself).

The worker's deterministic `ensure_login` tool (the model never sees any of
this) calls, per job:

  session       the shared stored cookie jar for the job's host
                (rfp_harvest_sessions: the `procore` row for app.procore.com,
                `procore:<host>` otherwise), when it belongs to the configured
                account and is not the jar the worker just found logged out.
                Refused with help_open while a `login:procore` help request is
                open and this job is not its holder, with locked while logins
                are locked and no jar exists, not_configured without an
                account. No Procore call is made here.
  login/begin   the atomic reserve_login on the shared `procore` row (the same
                600 s spacing and 3-strike lock the daily sweep and the RFP
                harvest honor), then the account's email and password, sent
                only in this response with Cache-Control: no-store. The
                password is read fresh from the encrypted store (env fallback),
                never logged, never stored on the attempt row.
  login/result  the worker's classification of what the login page did:
                  ok                 cookies saved to the shared row (stamped
                                     last_login_source 'agent:<worker>'), so
                                     the sweep and the harvest adopt the jar
                                     through _jar_is_newer; failures reset; an
                                     open login help auto-resolves;
                  password_rejected  the atomic failure RPC (shared lockout);
                                     a lock bells IT Admin once ("Check the
                                     Procore password in Settings > Browser
                                     agents."); help reason login_failed, or
                                     login_locked once locked;
                  mfa / captcha / challenge / sso / unexpected / timeout
                                     last_error only (not a password failure);
                                     the worker opens login-scope help.

Every call writes a browser_agent_login_attempts row (kind 'session' for a
jar handout, 'browser' for a released login) and a ledger step. A released
login that never reports within 5 min counts as a failure (sweeper step).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core import supabase_client
from app.core.agent_auth import AgentLease, agent_error, rpc
from app.core.config import get_settings
from app.services import procore_client as pc
from app.services import procore_creds
from app.services.browser_agents import protocol, queue, registry
from app.services.browser_agents.protocol import ErrorCode

logger = logging.getLogger(__name__)

ATTEMPTS = "browser_agent_login_attempts"
HELP = "browser_agent_help_requests"
LIVE_STATUSES = ["running", "waiting_help"]
LIVE_JOB_STATUSES = ("queued", "running", "waiting_help")
OPEN_HELP_STATUSES = ["open", "answered"]
UTC = timezone.utc


def _sb():
    return supabase_client.get_supabase()


def _now() -> datetime:
    return queue.now_utc()


def _ts(value: Any) -> datetime | None:
    return queue.parse_ts(value)


# ── Shared checks ──────────────────────────────────────────────────────────────


def _fenced_job(lease: AgentLease, identity: str, host: str) -> dict:
    """The job row, fenced (owner, epoch, boot; running or waiting_help),
    whose type logs in as `identity` on the seed's `host`."""
    data = rpc("browser_agent_fence", {**lease.params(), "p_statuses": LIVE_STATUSES})
    job = (data[0] if isinstance(data, list) and data else data) or {}
    if not job.get("id"):
        raise agent_error(ErrorCode.LEASE_LOST)
    if job.get("principal_kind", "org") != "org":
        raise agent_error(ErrorCode.IDOR, "Organization identity unavailable")
    try:
        spec = registry.get_type(job["agent_type"])
    except registry.UnknownAgentType:
        raise agent_error(ErrorCode.INVALID_REQUEST, "unknown agent type") from None
    if not spec.identity or spec.identity != identity:
        raise agent_error(ErrorCode.INVALID_REQUEST, "this job does not log in as that identity")
    if (job.get("input") or {}).get("host") != host:
        raise agent_error(ErrorCode.INVALID_REQUEST, "host is not the job's host")
    return job


def open_login_help(identity: str) -> dict | None:
    rows = (
        _sb().table(HELP).select("id, holder_job_id, status, project_id")
        .eq("principal_kind", "org").eq("scope_key", f"login:{identity}").in_("status", OPEN_HELP_STATUSES).limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _holder_alive(job_id: Any) -> bool:
    """The holder of a login request can still act on it: queued (requeued
    by a helper), running, or waiting_help (holding the page or parked)."""
    if not job_id:
        return False
    rows = _sb().table(queue.JOBS).select("status").eq("id", str(job_id)).limit(1).execute().data or []
    return bool(rows) and rows[0].get("status") in LIVE_JOB_STATUSES


def _blocking_help(identity: str, job_id: str, epoch: int = 0) -> dict | None:
    """An open login help request this job does not hold (it waits on it).

    A holder that ended (canceled, failed, expired) can never log in or
    redeem a helper's code, so the asking job takes the request over
    (compare-and-set on the old holder) and goes on to log in itself."""
    help_row = open_login_help(identity)
    if not help_row or str(help_row.get("holder_job_id")) == str(job_id):
        return None
    old = help_row.get("holder_job_id")
    if _holder_alive(old):
        return help_row
    q = _sb().table(HELP).update({"holder_job_id": str(job_id)}).eq("id", str(help_row["id"])).in_(
        "status", OPEN_HELP_STATUSES)
    q = q.eq("holder_job_id", str(old)) if old else q.is_("holder_job_id", "null")
    if q.execute().data:
        queue.log_step(str(job_id), epoch, "help.holder_moved",
                       "Took over the Procore login help request: its previous holder job has ended",
                       actor="bdr", result={"help_id": str(help_row["id"]), "previous_holder": str(old or "")})
        return None
    fresh = open_login_help(identity)
    if fresh and str(fresh.get("holder_job_id")) != str(job_id):
        return fresh
    return None


def _record_attempt(job: dict, lease: AgentLease, identity: str, host: str, kind: str, *, granted: bool,
                    refused_reason: str | None = None, outcome: str | None = None,
                    secret_version: int | None = None) -> dict:
    row = {
        "identity": identity, "host": host, "kind": kind, "job_id": str(job["id"]),
        "worker_id": lease.worker.id, "epoch": lease.epoch, "granted": granted,
        "refused_reason": refused_reason, "outcome": outcome, "secret_version": secret_version,
    }
    if kind == "session":
        row["reported_at"] = queue.iso(_now())
    try:
        inserted = _sb().table(ATTEMPTS).insert(row).execute().data or []
    except Exception:  # noqa: BLE001 - a granted login needs its row; the caller decides
        if kind == "browser" and granted:
            raise
        logger.exception("browser agents: login attempt row for job %s not written", job.get("id"))
        inserted = []
    return inserted[0] if inserted else row


# ── Cookies ────────────────────────────────────────────────────────────────────


def _procore_domain(domain: str | None) -> bool:
    d = (domain or "").lower().lstrip(".")
    return d == "procore.com" or d.endswith(".procore.com")


def stored_to_cookies(stored: list[dict] | None, now: datetime) -> list[protocol.Cookie]:
    """A stored jar (httpx export or an agent's Playwright jar) as the
    Playwright cookies the worker injects. Expired and non-Procore cookies
    are dropped."""
    out: list[protocol.Cookie] = []
    for c in stored or []:
        if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
            continue
        if not _procore_domain(c.get("domain")):
            continue
        expires = c.get("expires")
        expires = float(expires) if isinstance(expires, (int, float)) and expires > 0 else -1
        if expires > 0 and expires < now.timestamp():
            continue
        same_site = c.get("sameSite") if c.get("sameSite") in ("Strict", "Lax", "None") else "Lax"
        try:
            out.append(protocol.Cookie(
                name=str(c["name"]), value=str(c["value"]), domain=str(c["domain"]),
                path=str(c.get("path") or "/"), expires=expires, secure=bool(c.get("secure", True)),
                httpOnly=bool(c.get("httpOnly", c.get("httponly", False))), sameSite=same_site,
            ))
        except Exception:  # noqa: BLE001 - one malformed cookie is skipped
            continue
    return out


def cookies_to_stored(cookies: list[protocol.Cookie]) -> list[dict]:
    """The worker's jar as stored in rfp_harvest_sessions.cookies (readable by
    ProcoreSession._load_store and handed back by `session`)."""
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/",
         "expires": c.expires if c.expires and c.expires > 0 else None, "secure": c.secure,
         "httpOnly": c.httpOnly, "sameSite": c.sameSite}
        for c in cookies if _procore_domain(c.domain)
    ]


# ── POST .../session ──────────────────────────────────────────────────────────


def session(lease: AgentLease, identity: str, req: protocol.SessionRequest) -> protocol.SessionResponse:
    from app.services import pm_rfi_sync

    settings = get_settings()
    job = _fenced_job(lease, identity, req.host)
    now = _now()

    def refuse(status: str, *, retry_at: datetime | None = None, help_id: Any = None) -> protocol.SessionResponse:
        _record_attempt(job, lease, identity, req.host, "session", granted=False, refused_reason=status)
        queue.log_step(str(job["id"]), lease.epoch, "login.session", f"Stored session not given ({status})",
                       worker_id=lease.worker.id, actor="bdr", result={"status": status})
        return protocol.SessionResponse(status=status, retry_at=retry_at, help_id=help_id)

    email = (settings.procore_login_email or "").strip()
    if not email:
        return refuse("not_configured")
    blocking = _blocking_help(identity, job["id"], lease.epoch)
    if blocking:
        return refuse("help_open", help_id=blocking["id"])
    store = pm_rfi_sync.session_store(req.host, settings)
    state = store.load() or {}
    cookies: list[protocol.Cookie] = []
    logged_in_at = _ts(state.get("logged_in_at"))
    rejected = req.reject_jar_logged_in_at
    rejected = rejected if rejected is None or rejected.tzinfo else rejected.replace(tzinfo=UTC)
    if state.get("account") == email and not (rejected and logged_in_at and logged_in_at == rejected):
        cookies = stored_to_cookies(state.get("cookies"), now)
    if cookies:
        _record_attempt(job, lease, identity, req.host, "session", granted=True, outcome="stored_jar")
        queue.log_step(str(job["id"]), lease.epoch, "login.session",
                       f"Stored Procore session handed out ({len(cookies)} cookies)",
                       worker_id=lease.worker.id, actor="bdr", result={"status": "ok"})
        return protocol.SessionResponse(status="ok", cookies=cookies, logged_in_at=logged_in_at, source="stored")
    until = pc.lock_state(state, now)
    if until:
        return refuse("locked", retry_at=until)
    return refuse("no_jar")


# ── POST .../login/begin ──────────────────────────────────────────────────────


def login_begin(lease: AgentLease, identity: str, req: protocol.LoginBeginRequest) -> protocol.LoginBeginResponse:
    from app.services import pm_rfi_sync

    settings = get_settings()
    job = _fenced_job(lease, identity, req.host)

    def refuse(reason: str, *, retry_at: Any = None, help_id: Any = None) -> protocol.LoginBeginResponse:
        _record_attempt(job, lease, identity, req.host, "browser", granted=False, refused_reason=reason)
        queue.log_step(str(job["id"]), lease.epoch, "login.refused", f"Login not released ({reason})",
                       worker_id=lease.worker.id, actor="bdr",
                       result={"reason": reason, "retry_at": queue.iso(retry_at) if isinstance(retry_at, datetime) else retry_at})
        return protocol.LoginBeginResponse(granted=False, reason=reason, retry_at=retry_at, help_id=help_id)

    email = (settings.procore_login_email or "").strip()
    if not email or not procore_creds.configured(settings):
        return refuse("not_configured")
    blocking = _blocking_help(identity, job["id"], lease.epoch)
    if blocking:
        return refuse("help_open", help_id=blocking["id"])
    store = pm_rfi_sync.session_store(req.host, settings)
    reservation = store.reserve_login(settings.procore_login_min_interval_seconds, f"agent:{lease.worker.id}") or {}
    if not reservation.get("granted"):
        reason = "locked" if reservation.get("reason") == "locked" else "spacing"
        return refuse(reason, retry_at=_ts(reservation.get("retry_at")))
    password = procore_creds.password(settings)
    if not password:
        return refuse("not_configured")
    attempt = _record_attempt(job, lease, identity, req.host, "browser", granted=True,
                              secret_version=procore_creds.password_version(settings))
    # The ledger step and the attempt row are the record; no bell per login.
    queue.log_step(str(job["id"]), lease.epoch, "login.begin", "Procore login released to the worker",
                   worker_id=lease.worker.id, actor="bdr", result={"attempt_id": str(attempt.get("id"))})
    return protocol.LoginBeginResponse(granted=True, attempt_id=attempt["id"], email=email, password=password)


# ── POST .../login/result ─────────────────────────────────────────────────────


def _shared_store(settings):
    from app.services import rfp_harvest

    return rfp_harvest._SessionStore(settings, pc.PROVIDER)


def _result_response(state: dict, help_reason: str | None) -> protocol.LoginResultResponse:
    until = pc.lock_state(state, _now())
    return protocol.LoginResultResponse(
        locked_until=until, login_failures=int(state.get("login_failures") or 0), help_reason=help_reason,
    )


def _help_reason_for(outcome: str | None, locked: bool) -> str | None:
    reason = protocol.LOGIN_OUTCOME_HELP_REASON.get(outcome or "")
    if reason and locked and outcome == "password_rejected":
        return "login_locked"
    return reason


def login_result(lease: AgentLease, identity: str, req: protocol.LoginResultRequest) -> protocol.LoginResultResponse:
    from app.services import pm_rfi_sync, rfp_harvest

    settings = get_settings()
    rows = _sb().table(ATTEMPTS).select("*").eq("id", str(req.attempt_id)).limit(1).execute().data or []
    attempt = rows[0] if rows else None
    if attempt is None or str(attempt.get("job_id")) != lease.job_id:
        raise agent_error(ErrorCode.IDOR, "login attempt does not belong to this job")
    if attempt.get("kind") != "browser" or not attempt.get("granted") or attempt.get("identity") != identity:
        raise agent_error(ErrorCode.INVALID_REQUEST, "not a released login attempt")
    job = _fenced_job(lease, identity, attempt["host"])
    host = attempt["host"]
    shared = _shared_store(settings)
    # An MFA login reports `mfa` first (so help opens inside the deadline),
    # then the final outcome once a helper's code went in: `mfa` is the one
    # interim outcome a later, different report may replace.
    after_mfa = bool(attempt.get("reported_at")) and attempt.get("outcome") == "mfa" and req.outcome != "mfa"
    if attempt.get("reported_at") and not after_mfa:
        # A resend (or a report after the sweeper counted it unreported):
        # nothing changes twice.
        state = shared.load() or {}
        return _result_response(state, _help_reason_for(attempt.get("outcome"), bool(pc.lock_state(state, _now()))))
    now = _now()
    stored: list[dict] = []
    if req.outcome == "ok":
        stored = cookies_to_stored(req.cookies or [])
        if not stored:
            raise agent_error(ErrorCode.INVALID_REQUEST, "no Procore cookies in the jar")
    claim_q = (
        _sb().table(ATTEMPTS).update({"reported_at": queue.iso(now), "outcome": req.outcome,
                                      "step": (req.step or "")[:60] or None})
        .eq("id", str(req.attempt_id))
    )
    claim_q = claim_q.eq("outcome", "mfa") if after_mfa else claim_q.is_("reported_at", "null")
    claimed = claim_q.execute().data or []
    if not claimed:
        state = shared.load() or {}
        return _result_response(state, None)
    email = (settings.procore_login_email or "").strip()
    help_reason: str | None = None
    if req.outcome == "ok":
        store = pm_rfi_sync.session_store(host, settings)
        store.save_cookies(email, stored)
        if host != pc.APP_HOST:
            shared.record_login(ok=True, error=None)
        source = f"agent:{lease.worker.id}"
        for provider in {pm_rfi_sync.session_provider(host), pc.PROVIDER}:
            _sb().table("rfp_harvest_sessions").update({"last_login_source": source}).eq("provider", provider).execute()
        _auto_resolve_login_help(identity, job, lease)
        summary = "Procore login succeeded; the session was saved for every Procore client"
    elif req.outcome == "password_rejected":
        error = "password: Procore rejected the password (browser agent login)"
        state = shared.record_login(ok=False, error=error)
        until = pc.lock_state(state, now)
        _sb().table(ATTEMPTS).update({"counted_as_failure": True}).eq("id", str(req.attempt_id)).execute()
        if until:
            try:
                rfp_harvest._notify_lock(until, error, agent=True)
            except Exception:  # noqa: BLE001 - the bell never masks the lock
                logger.exception("browser agents: Procore lock notice failed")
        help_reason = _help_reason_for(req.outcome, bool(until))
        summary = "Procore rejected the password" + (" and logins are now locked" if until else "")
    else:
        step = f" at {req.step}" if req.step else ""
        _sb().table("rfp_harvest_sessions").update({
            "last_error": f"{req.outcome}: the browser agent's login stopped{step}"[:500],
        }).eq("provider", pc.PROVIDER).execute()
        help_reason = _help_reason_for(req.outcome, False)
        summary = f"Procore login needs a person ({req.outcome})"
    queue.log_step(str(job["id"]), lease.epoch, "login.result", summary, worker_id=lease.worker.id, actor="bdr",
                   result={"attempt_id": str(req.attempt_id), "outcome": req.outcome, "step": req.step})
    return _result_response(shared.load() or {}, help_reason)


def _auto_resolve_login_help(identity: str, job: dict, lease: AgentLease) -> int:
    """A successful login resolves the open `login:<identity>` request: it
    becomes resolved (auto_resolved), parked waiters are requeued (uncharged,
    help_answered, ahead of the queue), and every job still holding its own
    page on this request (this one and other slots that joined it) goes back
    to running, so BDR's state matches the worker that carries on."""
    sb = _sb()
    help_row = open_login_help(identity)
    if not help_row:
        return 0
    now_iso = queue.iso(_now())
    moved = (
        sb.table(HELP).update({"status": "resolved", "resolution": "auto_resolved", "resolved_at": now_iso})
        .eq("id", help_row["id"]).in_("status", OPEN_HELP_STATUSES).execute()
    ).data or []
    if not moved:
        return 0
    waiters = sb.table("browser_agent_help_waiters").select("job_id").eq("help_id", help_row["id"]).execute().data or []
    requeued = 0
    for w in waiters:
        jid = str(w["job_id"])
        if jid == str(job["id"]):
            sb.table(queue.JOBS).update({"status": "running", "waiting_help_id": None, "hold_until": None}).eq(
                "id", jid).eq("principal_kind", "org").eq("status", "waiting_help").eq("lease_owner", lease.worker.id).execute()
            continue
        held = (
            sb.table(queue.JOBS).update({"status": "running", "waiting_help_id": None, "hold_until": None})
            .eq("principal_kind", "org").eq("id", jid).eq("status", "waiting_help").eq("waiting_help_id", str(help_row["id"]))
            .not_.is_("lease_owner", "null").execute()
        ).data or []
        if held:
            queue.log_step(jid, int(held[0].get("lease_epoch") or 0), "help.resolved",
                           "The Procore login works again; continuing on the held page", actor="bdr",
                           result={"help_id": str(help_row["id"])})
            continue
        rows = (
            sb.table(queue.JOBS).update({
                "status": "queued", "resume_reason": "help_answered", "priority": 10,
                "waiting_help_id": None, "hold_until": None,
            }).eq("principal_kind", "org").eq("id", jid).eq("status", "waiting_help").is_("lease_owner", "null").execute()
        ).data or []
        if rows:
            requeued += 1
            queue.log_step(jid, int(rows[0].get("lease_epoch") or 0), "help.resolved",
                           "The Procore login works again; requeued", actor="bdr",
                           result={"help_id": str(help_row["id"])})
    queue.log_step(str(job["id"]), lease.epoch, "help.resolved", "Login help resolved by a successful login",
                   worker_id=lease.worker.id, actor="bdr", result={"help_id": str(help_row["id"]), "requeued": requeued})
    # Login help has no project: help_id alone scopes the dismissal.
    from app.services.browser_agents import help as help_flow

    help_flow.dismiss_help_bells(help_row)
    return requeued


# ── Sweeper: unreported browser logins ────────────────────────────────────────


def sweep_unreported_logins(now: datetime, settings_row: dict[str, Any]) -> int:
    """A released login that never reported within LOGIN_RESULT_DEADLINE_SECONDS
    counts as a failure on the shared row (it may have been a rejected
    password from a worker that then died). Compare-and-set on reported_at."""
    from app.services import rfp_harvest

    cutoff = now - timedelta(seconds=protocol.LOGIN_RESULT_DEADLINE_SECONDS)
    sb = _sb()
    rows = (
        sb.table(ATTEMPTS).select("id, job_id, epoch, identity").eq("kind", "browser").eq("granted", True)
        .is_("reported_at", "null").lt("created_at", queue.iso(cutoff)).limit(queue.SWEEP_BATCH).execute()
    ).data or []
    counted = 0
    settings = get_settings()
    for row in rows:
        parents = (sb.table(queue.JOBS).select("principal_kind").eq("id", str(row["job_id"]))
                   .limit(1).execute()).data or []
        if not parents or parents[0].get("principal_kind", "org") != "org":
            continue
        claimed = (
            sb.table(ATTEMPTS).update({"reported_at": queue.iso(now), "outcome": "unreported",
                                       "counted_as_failure": True})
            .eq("id", row["id"]).is_("reported_at", "null").execute()
        ).data or []
        if not claimed:
            continue
        counted += 1
        error = "agent attempt did not report"
        state = _shared_store(settings).record_login(ok=False, error=error)
        until = pc.lock_state(state, now)
        if until:
            try:
                rfp_harvest._notify_lock(until, error, agent=True)
            except Exception:  # noqa: BLE001
                logger.exception("browser agents: Procore lock notice failed")
        queue.log_step(str(row["job_id"]), int(row.get("epoch") or 0), "login.unreported",
                       "A released Procore login never reported; counted as a failure", actor="system")
    return counted

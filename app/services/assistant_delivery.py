"""Phase 7 router external delivery: the send ledger consumer
(docs/ASSISTANT_PHASE7_PLAN.md 8.5).

This module is the router's only sending code: one Teams message per
delivered high or urgent router-owned inbox row and one email per delivered
digest, through the `assistant_external_sends` ledger, reusing
`graph_email.send_mail` and `teams_alerts._deliver`. It never uses BDR's own
queueing or bell fan-out paths (the banned names are listed and pinned by
the source-scan test, plan 11.1), and it is the only router module that
names `graph_email` or `teams_alerts`.

One tick (`send_due`), in order (8.5 steps 1 to 7):
  1. ASSISTANT_PROACTIVE_EXTERNAL_ENABLED off: no ledger row is written (the
     tick passes p_teams and p_email false) and nothing is sent; rows already
     pending are claimed and settled `skipped channel_off`, so turning the
     switch back on sends nothing old. An active RFP test session: nothing is
     claimed (rows wait pending).
  2. Claims, one ledger row per call, up to ASSISTANT_PROACTIVE_SEND_POLL_BATCH
     calls, under ASSISTANT_PROACTIVE_SEND_LEASE_SECONDS. The claim clock is
     `now` plus the time this tick has spent, so a slow row never ages the
     rows behind it past the lease.
  3. Per row, each in its own try: the channel gate (company switch,
     NOTIFICATION_EMAILS_ENABLED with MS_CLIENT_ID, TEAMS_ALERTS_ENABLED
     through teams_alerts.is_configured) settles `skipped channel_off`; the
     recheck settles `skipped offboarded`, `stale` or `opted_out`; the
     send-time gates settle `stale` or defer a non-urgent Teams row to the
     next 06:00 PT; the live-test guard settles `permanent` before any helper
     is called; then the helper call, classified and settled under the fence.
The ledger stores ids, enums and an error kind only: never an address and
never an exception text (that stays in teams_alert_users.last_error and
email_log.error, where the helpers already put it). Plain `def` throughout:
the sync Supabase SDK is touched through repo and the helpers.
"""

from __future__ import annotations

import logging
import math
import re
import time as _time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

import httpx

from app.assistant import access, proactive, repo
from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES, Role
from app.services import email_branding, graph_email, notification_email, rfp_test, teams_alerts

logger = logging.getLogger(__name__)

# Transient backoff by attempt (60 s, 5 min, 30 min, 2 h); the claim that
# makes the fifth attempt dead-letters as failed inside A11.13 (8.5 step 6).
BACKOFF_SECONDS = (60, 300, 1800, 7200)
# A Teams row whose not_before is older than this only ends a backlog: it is
# longer than the whole transient ladder (about 2 h 36 min), 8.5 step 4.
TEAMS_STALE_AFTER = timedelta(hours=3)

INBOX_HEADING_TYPE = "assistant.inbox"
DIGEST_HEADING_TYPE = "assistant.digest"
CTA_LABEL = "Open your inbox"
# The inline logo BDR's own notification emails attach, defined here so the module
# names only the email_branding helpers of 8.5 step 5; a test pins both
# values against email_branding.
LOGO_CONTENT_ID = "g3-logo"
LOGO_FILENAME = "g3-logo.jpg"

# Live-test override (owner question 38, 8.5 step 5): while
# ASSISTANT_PROACTIVE_TEST_EMAIL_TO is set, only these targets pass the guard.
TEST_EMAIL_ALLOWED = "baseballtom33@gmail.com"
TEST_TEAMS_ALLOWED = frozenset({"bdr.e2e.electrical@g3test.dev", "bdr.e2e.admin@g3test.dev"})
TEST_SUBJECT_PREFIX = "[TEST] BDR assistant digest · "
DEV_PROJECT_REF = "bpidntbyvoooqvaispup"

# Hosts the email classifier reads from `exc.request.url.host` (8.5 step 6).
GRAPH_HOST = "graph.microsoft.com"
LOGIN_HOST = "login.microsoftonline.com"

# The fixed texts teams_alerts writes into TeamsAlertError, matched by
# substring or regex, never by prefix (resolve-step texts start with the step
# name). tests/test_assistant_delivery.py pins each against the helper's
# source, so a wording change fails the build instead of misclassifying.
_TEAMS_GRAPH_REFUSED = re.compile(r": Microsoft Graph refused the call \(HTTP (\d{3})")
_TEAMS_GRAPH_UNREACHABLE = ": could not reach Microsoft Graph ("
_TEAMS_CHAT_NOT_OPEN = "has not opened its chat yet"
_TEAMS_BOT_SIGNIN = "The bot could not sign in"
_TEAMS_UNREACHABLE = re.compile(r"Could not reach Teams \((\w+)\)")
_TEAMS_RETURNED = re.compile(r"Teams returned HTTP (\d{3})")
_TEAMS_RESOLVE_4XX = re.compile(r"\(HTTP (4\d\d)")
_TEAMS_PERMANENT_TEXTS = (
    "No Microsoft 365 account found",
    "Teams rejected the bot's sign-in",
    "Teams refused the new BDR Alerts chat",
    "Teams refused the BDR Alerts chat",
    "is not in the Teams app catalog yet",
)

# Tests may pin the elapsed-time source.
_monotonic: Callable[[], float] = _time.monotonic


class _Outcome(Exception):
    """A row's settle decision taken before any helper call."""

    def __init__(self, outcome: str, error_kind: str | None = None, retry_seconds: int = 60):
        super().__init__(outcome)
        self.outcome = outcome
        self.error_kind = error_kind
        self.retry_seconds = retry_seconds


# ── Gates and small readers ──────────────────────────────────────────────────


def external_enabled() -> bool:
    """ASSISTANT_PROACTIVE_EXTERNAL_ENABLED: when false the router writes no
    send-ledger row (the tick passes p_teams and p_email false) and this
    module sends nothing; rows left pending settle `channel_off` (12.1, 8.5
    step 1)."""
    return bool(getattr(get_settings(), "assistant_proactive_external_enabled", False))


def _email_on(settings: Any) -> bool:
    # The global switch is checked only by BDR's own email queue, never
    # inside graph_email.send_mail, so the router checks it itself (8.5 step 1).
    return bool(getattr(settings, "notification_emails_enabled", False) and getattr(settings, "ms_client_id", ""))


def _teams_on() -> bool:
    return bool(teams_alerts.is_configured())


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _slot_times(settings: Any) -> dict[str, str]:
    raw = [p.strip() for p in str(getattr(settings, "assistant_proactive_digest_times", "") or "").split(",")]
    raw = [p for p in raw if p]
    if len(raw) != 2:
        return {"am": "07:00", "pm": "13:00"}
    am, pm = sorted(raw, key=lambda p: tuple(int(x) for x in p.split(":")))
    return {"am": am, "pm": pm}


def _quiet(settings: Any) -> tuple[str, str]:
    return (str(getattr(settings, "assistant_proactive_quiet_start", "") or "18:00"),
            str(getattr(settings, "assistant_proactive_quiet_end", "") or "06:00"))


def _allowlist(settings: Any) -> frozenset[str]:
    raw = str(getattr(settings, "assistant_proactive_user_allowlist", "") or "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _test_email_to(settings: Any) -> str:
    """The live-test override when it is in force: set, and an allowlist set
    (the 12.1 validator refuses it otherwise). Without an allowlist the
    override is inert and every send goes to its account as normal."""
    override = str(getattr(settings, "assistant_proactive_test_email_to", "") or "").strip()
    return override if override and _allowlist(settings) else ""


def _frontend(settings: Any) -> str:
    return str(getattr(settings, "frontend_url", "") or "").rstrip("/")


def _person_ok(user_id: str, settings: Any, settings_row: dict | None) -> dict | None:
    """The person's profiles row when they are active, internal (never an
    estimator) and still have a working assistant (8.5 step 4); else None."""
    person = repo.human_profile(user_id)
    if not person or not person.get("is_active"):
        return None
    try:
        role = Role(person.get("role"))
    except ValueError:
        return None
    if role not in INTERNAL_ROLES or role == Role.ESTIMATOR:
        return None
    user = SimpleNamespace(is_active=True, role=role)
    env = bool(getattr(settings, "assistant_enabled", False))
    if not access.assistant_enabled_for(user, settings_row, repo.profile_row(user_id), env=env):
        return None
    return person


# ── Error classification (8.5 step 6) ───────────────────────────────────────


def _host(exc: BaseException) -> str:
    try:
        return str(exc.request.url.host or "").lower()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an httpx error without a request raises here
        return ""


def _is_requests_connection_error(exc: BaseException) -> bool:
    """requests.exceptions.ConnectionError (ConnectTimeout included), found by
    walking the class's MRO, so this module never imports requests: it is not
    a direct dependency (it arrives through msal). A ReadTimeout is not one."""
    return any(c.__name__ == "ConnectionError" and (c.__module__ + ".").startswith("requests.")
               for c in type(exc).__mro__)


def _msal_host(exc: BaseException) -> str:
    """The host of a requests error. msal signs in through a requests session
    (its default HTTP client), so a login-host outage raises
    requests.exceptions.ConnectionError, not an httpx error."""
    try:
        return str(httpx.URL(exc.request.url).host or "").lower()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - a requests error without a request
        return ""


def classify_email_error(exc: BaseException) -> str:
    """transient, uncertain or permanent for an exception out of
    graph_email.send_mail. send_mail writes email_log through Supabase (also
    httpx) inside the same try as the Graph post, so the host decides: only
    a failure that cannot have reached Graph is retried."""
    host = _host(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        if host != GRAPH_HOST:
            return "uncertain"
        code = exc.response.status_code
        if code in (429, 503):
            return "transient"
        if 400 <= code < 500:
            return "permanent"
        return "uncertain"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)) and host in (GRAPH_HOST, LOGIN_HOST):
        return "transient"
    if isinstance(exc, httpx.HTTPError):
        # The Supabase host, any other timeout or transport error after the
        # request left: delivery is unknown, never replayed.
        return "uncertain"
    if _is_requests_connection_error(exc) and _msal_host(exc) == LOGIN_HOST:
        # msal's sign-in in _acquire_token, before the post (ConnectTimeout
        # is a ConnectionError).
        return "transient"
    if isinstance(exc, rfp_test.RedirectRefused) or (
            isinstance(exc, RuntimeError) and str(exc).startswith("Graph token error")):
        # A test session started mid-tick, or _acquire_token (before the post).
        # Any other RuntimeError (a closed httpx client on the email_log
        # update after Graph accepted) is uncertain.
        return "transient"
    return "uncertain"


def classify_teams_error(exc: BaseException) -> str:
    """transient, uncertain or permanent for an exception out of
    teams_alerts._deliver. Transient only where `_post_card` cannot have run;
    an unknown text or any other exception is uncertain."""
    if _is_requests_connection_error(exc):
        # Only msal speaks requests here (the Graph resolve calls and the card
        # post are httpx), and _bot_token runs before the post: a sign-in
        # outage escapes _graph and _post_card raw, with nothing posted.
        return "transient"
    if not isinstance(exc, teams_alerts.TeamsAlertError):
        return "uncertain"
    text = str(exc)
    m = _TEAMS_GRAPH_REFUSED.search(text)
    if m:
        code = int(m.group(1))
        return "transient" if code == 429 or code >= 500 else "permanent"
    if _TEAMS_GRAPH_UNREACHABLE in text or _TEAMS_CHAT_NOT_OPEN in text or _TEAMS_BOT_SIGNIN in text:
        return "transient"
    m = _TEAMS_UNREACHABLE.search(text)
    if m:
        return "transient" if m.group(1) in ("ConnectError", "ConnectTimeout") else "uncertain"
    m = _TEAMS_RETURNED.search(text)
    if m:
        code = int(m.group(1))
        if code in (429, 503):
            return "transient"
        return "permanent" if 400 <= code < 500 else "uncertain"
    if any(p in text for p in _TEAMS_PERMANENT_TEXTS) or _TEAMS_RESOLVE_4XX.search(text):
        return "permanent"
    return "uncertain"


# ── Per-row preparation (recheck, send-time gates, guard, text) ─────────────


def _prepare_teams(row: dict, clock: datetime, ctx: dict) -> Callable[[], str | None]:
    settings, user_id = ctx["settings"], str(row["user_id"])
    person = _person_ok(user_id, settings, ctx["settings_row"])
    if person is None:
        raise _Outcome("skipped", "offboarded")
    inbox = repo.inbox_row(user_id, str(row["target_id"]))
    if not inbox or inbox.get("delivered_at") is None or inbox.get("dismissed_at") is not None:
        raise _Outcome("skipped", "stale")
    state = teams_alerts.get_state(user_id) or {}
    if state.get("enabled") is False:  # a missing row means on (owner question 35)
        raise _Outcome("skipped", "opted_out")
    urgent = inbox.get("severity") == "urgent"
    if not urgent:
        if inbox.get("read_at") is not None:
            raise _Outcome("skipped", "stale")
        bell = inbox.get("notification_id")
        if bell and (repo.notification_states(user_id, [bell]).get(str(bell)) or {}).get("read_at") is not None:
            raise _Outcome("skipped", "stale")
    payload = inbox.get("payload") or {}
    if inbox.get("event_type") == proactive.APPROVAL_EXPIRING:
        approval_id = payload.get("approval_id")
        card = repo.get_approval(user_id, str(approval_id)) if approval_id else None
        expires = _utc((card or {}).get("expires_at"))
        if not card or card.get("status") != "pending" or (expires is not None and expires <= clock):
            raise _Outcome("skipped", "stale")
    # Send-time gates (8.5 step 4): a backlog first, so a day-old row claimed
    # at night is not deferred into a 06:00 send; then quiet hours.
    not_before = _utc(row.get("not_before")) or clock
    if clock - not_before > TEAMS_STALE_AFTER:
        raise _Outcome("skipped", "stale")
    quiet_start, quiet_end = _quiet(settings)
    if not urgent and proactive.in_quiet_hours(clock, start=quiet_start, end=quiet_end):
        release = proactive.next_quiet_end(clock, end=quiet_end)
        raise _Outcome("deferred", "quiet_hours", max(1, math.ceil((release - clock).total_seconds())))
    email = str(person.get("email") or "").strip()
    if not email:
        raise _Outcome("permanent")
    if _test_email_to(settings) and email.lower() not in TEST_TEAMS_ALLOWED:
        raise _Outcome("permanent")  # the live-test guard: no helper is called
    heading = notification_email.heading_for(INBOX_HEADING_TYPE)
    number = proactive.project_number(payload.get("project_number"))
    card = teams_alerts.build_card(
        heading=heading,
        message=proactive.teams_message(str(inbox.get("title") or ""), str(inbox.get("why") or "")),
        project_label=number,
        cta_label=CTA_LABEL,
        cta_url=f"{_frontend(settings)}/assistant?tab=inbox&item={inbox['id']}",
    )
    summary = proactive.teams_summary(heading, number, _utc(inbox.get("created_at")))

    def send() -> str | None:
        teams_alerts._deliver(user_id, email, card, summary, state)
        return None

    return send


def _banner(account: str, slot_label: str, digest_id: str, count: int) -> str:
    return (
        f"TEST EMAIL from the BDR assistant live test. Intended recipient: {account}. "
        f"Slot: {slot_label}. Digest id: {digest_id}. Items: {count}. "
        f"Env: dev (Supabase project {DEV_PROJECT_REF})."
    )


def _prepare_email(row: dict, clock: datetime, ctx: dict) -> Callable[[], str | None]:
    settings, user_id = ctx["settings"], str(row["user_id"])
    person = _person_ok(user_id, settings, ctx["settings_row"])
    if person is None:
        raise _Outcome("skipped", "offboarded")
    digest = repo.digest(user_id, str(row["target_id"]))
    if not digest or digest.get("notification_id") is None:
        raise _Outcome("skipped", "stale")
    slot_at = _utc(digest.get("slot_at"))
    catchup = int(getattr(settings, "assistant_proactive_digest_catchup_hours", 3) or 3)
    quiet_start, quiet_end = _quiet(settings)
    # The owner's 3-hour catch-up (question 29); the 12.1 validator keeps the
    # window outside quiet hours, and the quiet test holds that at send time.
    if (slot_at is None or clock >= slot_at + timedelta(hours=catchup)
            or proactive.in_quiet_hours(clock, start=quiet_start, end=quiet_end)):
        raise _Outcome("skipped", "stale")
    # Guiding-principle call: rows the person already dismissed are not
    # mailed again; a digest with nothing left to show is stale.
    items = [i for i in repo.inbox_digest_items(user_id, str(digest["id"])) if i.get("dismissed_at") is None]
    if not items:
        raise _Outcome("skipped", "stale")
    account = str(person.get("email") or "").strip()
    if not account:
        raise _Outcome("permanent")
    slot_times = _slot_times(settings)
    slot = str(digest.get("slot") or "am")
    digest_date = date.fromisoformat(str(digest.get("digest_date"))[:10])
    heading = notification_email.heading_for(DIGEST_HEADING_TYPE)
    subject = proactive.digest_subject(heading, slot, digest_date, slot_times)
    # Items read for the digest's own Pacific date (a returning snooze says
    # "overdue", not "due today"), then the auto-downgrade lines this digest
    # carries (9.1): the same text the Inbox digest shows.
    notices = proactive.downgrade_notices(repo.digest_auto_downgrades(user_id, digest))
    lines = proactive.digest_lines(items, int(digest.get("record_count") or 0), _utc(digest.get("since_at")),
                                   day=digest_date, role=person.get("role"), notices=notices)
    override = _test_email_to(settings)
    to = account
    if override:
        allow = _allowlist(settings)
        if user_id.lower() in allow or account.lower() in allow:
            to = override
            subject = TEST_SUBJECT_PREFIX + subject
            label = proactive.digest_slot_label(slot, digest_date, slot_times)
            lines = [_banner(account, label, str(digest["id"]), len(items)), *lines]
        if to.lower() != TEST_EMAIL_ALLOWED:
            raise _Outcome("permanent")  # the live-test guard: no helper is called
    body_html = email_branding.render_notification_email(
        recipient_name=None,
        heading=heading,
        message="\n\n".join(lines),
        cta_label=CTA_LABEL,
        cta_url=f"{_frontend(settings)}/assistant?tab=inbox&digest={digest['id']}",
        project_label=None,
        linkify=False,
    )
    logo = (LOGO_CONTENT_ID, LOGO_FILENAME, email_branding.logo_bytes(), "image/jpeg")

    def send() -> str | None:
        log = graph_email.send_mail(to=[to], subject=subject, body_html=body_html, inline_images=[logo])
        return str(log["id"]) if isinstance(log, dict) and log.get("id") else None

    return send


def _decide(row: dict, clock: datetime, ctx: dict) -> tuple[str, str | None, str | None, int]:
    """(outcome, error_kind, email_log_id, retry_seconds) for one claimed row.
    An unlisted exception before the helper is transient; inside the helper
    it is classified, and unknown there means uncertain (8.5 step 6)."""
    channel = row.get("channel")
    attempts = int(row.get("attempts") or 1)
    retry = BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1]
    if channel not in ("teams", "email") or not ctx["channel_on"].get(channel):
        return "skipped", "channel_off", None, retry
    try:
        send = (_prepare_teams if channel == "teams" else _prepare_email)(row, clock, ctx)
    except _Outcome as decided:
        return decided.outcome, decided.error_kind, None, decided.retry_seconds
    except Exception as exc:  # noqa: BLE001 - per-row isolation
        logger.warning("assistant send %s: prepare failed (%s)", row.get("id"), exc.__class__.__name__)
        return "transient", None, None, retry
    try:
        log_id = send()
    except Exception as exc:  # noqa: BLE001 - classified, never re-raised
        kind = classify_teams_error(exc) if channel == "teams" else classify_email_error(exc)
        logger.warning("assistant send %s: %s %s", row.get("id"), kind, exc.__class__.__name__)
        return kind, None, None, retry
    return "sent", None, log_id, retry


# ── The tick step ────────────────────────────────────────────────────────────


def send_due(now: datetime, worker: str) -> dict[str, Any]:
    """The per-tick send step (8.5): check the gates (external switch, global
    email and Teams switches, RFP test session hold; with the external switch
    off every claim settles `channel_off`), claim ledger rows one at
    a time up to `assistant_proactive_send_poll_batch`, recheck each, apply
    the send-time gates, send through the existing helpers, then classify and
    settle (sent, transient, permanent, uncertain, skipped, deferred). Each
    row is isolated in its own try. Returns counts only; the ledger stores
    error kinds, never exception text or addresses."""
    counts: dict[str, Any] = {"claimed": 0, "sent": 0, "transient": 0, "permanent": 0, "uncertain": 0,
                              "skipped": 0, "deferred": 0, "fence_lost": 0, "settle_errors": 0}
    settings = get_settings()
    if not getattr(settings, "assistant_proactive_external_enabled", False):
        # Like a global switch (8.5 step 1): rows left pending when the switch
        # went off settle `channel_off`, so turning it back on sends nothing
        # old. No row is written and no helper is called.
        counts["external_off"] = True
        ctx = {"settings": settings, "settings_row": None, "channel_on": {"email": False, "teams": False}}
    else:
        try:
            if graph_email._active_test_session() is not None:
                counts["held"] = "test_session"
                logger.info("assistant sends held: an RFP test session is active")
                return counts
            settings_row = repo.settings_row(None)
        except Exception as exc:  # noqa: BLE001 - claim nothing when the gates cannot be read
            logger.warning("assistant send gates unreadable (%s)", exc.__class__.__name__)
            counts["held"] = "gate_error"
            return counts
        switched_on = bool(settings_row and settings_row.get("enabled") and settings_row.get("proactive_enabled"))
        try:
            teams_on = switched_on and _teams_on()
        except Exception as exc:  # noqa: BLE001
            logger.warning("assistant send Teams gate unreadable (%s)", exc.__class__.__name__)
            teams_on = False
        ctx = {"settings": settings, "settings_row": settings_row,
               "channel_on": {"email": switched_on and _email_on(settings), "teams": teams_on}}
    batch = int(getattr(settings, "assistant_proactive_send_poll_batch", 20) or 20)
    lease = int(getattr(settings, "assistant_proactive_send_lease_seconds", 600) or 600)
    base = _utc(now) or datetime.now(timezone.utc)
    started = _monotonic()

    def clock() -> datetime:
        return base + timedelta(seconds=max(0.0, _monotonic() - started))

    for _ in range(max(1, batch)):
        try:
            claimed = repo.external_sends_claim(None, worker=worker, limit=1, lease_seconds=lease, now=clock())
        except Exception as exc:  # noqa: BLE001 - a transport failure on the claim ends the step
            logger.warning("assistant send claim failed (%s)", exc.__class__.__name__)
            counts["claim_error"] = True
            break
        if not claimed:
            break
        for row in claimed:
            counts["claimed"] += 1
            at = clock()
            outcome, kind, log_id, retry = _decide(row, at, ctx)
            try:
                ok = repo.external_send_settle(
                    None, str(row["id"]), worker=worker, outcome=outcome, error_kind=kind,
                    email_log_id=log_id, retry_seconds=retry, now=clock(),
                )
            except Exception as exc:  # noqa: BLE001 - the stale claim retires as uncertain, never re-sent
                logger.warning("assistant send %s: settle failed (%s)", row.get("id"), exc.__class__.__name__)
                counts["settle_errors"] += 1
                continue
            if not ok:
                logger.warning("assistant send %s: settle fence lost (%s)", row.get("id"), outcome)
                counts["fence_lost"] += 1
                continue
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == "skipped":
                counts[f"skipped_{kind}"] = counts.get(f"skipped_{kind}", 0) + 1
    return counts


def channels(user_id: str) -> dict[str, Any]:
    """The read-only channel state for GET /assistant/alerts (10.1):
    `{teams: {available, enabled, connected, last_sent_at, last_error_at},
    email: {available}}`, read from `teams_alerts.status` without the error
    text, so the route never names `teams_alerts`. `available` also needs
    ASSISTANT_PROACTIVE_EXTERNAL_ENABLED (the router sends nothing without it)."""
    settings = get_settings()
    external = bool(getattr(settings, "assistant_proactive_external_enabled", False))
    status = teams_alerts.status(user_id) or {}
    return {
        "teams": {
            "available": external and bool(status.get("available")),
            "enabled": status.get("enabled") is not False,
            "connected": bool(status.get("connected")),
            "last_sent_at": status.get("last_sent_at"),
            "last_error_at": status.get("last_error_at"),
        },
        "email": {"available": external and _email_on(settings)},
    }

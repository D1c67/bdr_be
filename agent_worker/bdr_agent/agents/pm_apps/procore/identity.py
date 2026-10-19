"""`ensure_login` (O2): the job's browser gets a working Procore session.

    1. POST /identities/procore/session: the shared stored jar (same account)
       when BDR has one. Inject it into the job's context and probe
       GET /rest/v1.0/companies. Works: done, no login.
    2. Otherwise POST /identities/procore/login/begin. BDR reserves the login
       atomically (600 s spacing, 3-strike lock, open login help) and only
       then releases {email, password} in that one no-store response.
    3. Fill login.procore.com on the login page object (login.py), classify,
       POST /login/result (cookies on ok, so the daily sweep and RFP harvest
       adopt the jar). A non-ok outcome opens login-scope help with BDR's
       help_reason. For mfa the holder waits for a helper's secret message,
       redeems it once (/secrets/{id}/use), types it, and reports again.

The model only ever sees {ok, outcome}. The password, email, cookie values
and codes are job secrets: scrubbed from every step and observation, masked
in screenshots, never written to disk, and dropped when the job ends.

Terminal results: spacing longer than 120 s -> /requeue login_spacing
(uncharged); not configured / IP not allowed -> finish failed login_failed;
help parked -> handed off (an answer requeues the job).
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from ....browser.executor import ToolError, ToolReturn
from ....protocol import (
    LOGIN_OUTCOME_HELP_REASON,
    Cookie,
    LoginBeginRequest,
    LoginResultRequest,
    SessionRequest,
)
from ....slot import Finish, HandedOff, Requeue
from .login import LoginOutcome, ProcoreLoginPage
from .paths import PROBE_PATH
from .rest import ProcoreRest

log = logging.getLogger("bdr_agent.procore.identity")

IDENTITY = "procore"
SPACING_WAIT_MAX_SECONDS = 120     # wait while holding the slot up to this; longer: requeue
MAX_ROUNDS = 4                     # session/login rounds per ensure_login call (help retries)
_COOKIE_DOMAIN_SUFFIX = "procore.com"
_PROCORE_COOKIE_DOMAIN_RE = re.compile(r"^\.?([a-z0-9-]+\.)*procore\.com$", re.IGNORECASE)
# Cookie names that carry a login session: their values fail a job if they
# reach a model observation. Every other cookie value is scrub-only.
_CREDENTIAL_COOKIE_HINTS = ("session", "token", "auth", "jwt", "remember", "sid")
_MIN_CREDENTIAL_COOKIE_LEN = 16


def is_credential_cookie(name: str | None, value: str | None) -> bool:
    """True for a session/auth cookie whose value is long enough to be a real
    credential. Locale, region, analytics and id cookies are not."""
    n = (name or "").lower()
    v = value or ""
    if len(v) < _MIN_CREDENTIAL_COOKIE_LEN or v.isdigit():
        return False
    return any(h in n for h in _CREDENTIAL_COOKIE_HINTS)

HELP_TEXT = {
    "login_failed": "The Procore login did not work. Check the Procore password in Settings > Browser agents, "
                    "then press Retry.",
    "login_locked": "Procore logins are locked after repeated failures. Check the Procore password in "
                    "Settings > Browser agents, then press Retry.",
    "mfa": "Procore asked for a verification code. Send the code with 'This is a code' ticked.",
    "captcha": "Procore showed a captcha or verification page at login. Retry later, Skip, or Give up.",
    "sso": "Procore sent the login to single sign-on, which the agent cannot complete.",
}


def _now() -> datetime:
    return datetime.now(UTC)


class _Ended(Exception):
    """A help wait or refusal that ends the tool with a terminal result."""

    def __init__(self, ret: ToolReturn):
        super().__init__("ended")
        self.ret = ret


class ProcoreIdentity:
    def __init__(self, ctx: Any, executor: Any, desk: Any, *, host: str,
                 on_session_ready: Any = None, now: Any = _now, sleep: Any = None):
        self.ctx = ctx
        self.ex = executor
        self.desk = desk
        self.host = host
        self._on_ready = on_session_ready
        self._now = now
        self._sleep = sleep or asyncio.sleep
        self.logged_in = False
        self.jar_logged_in_at: datetime | None = None
        self.stale = False
        self.rest = ProcoreRest(executor.jb.request, host, log_step=ctx.steps.log)

    # Helpers ------------------------------------------------------------------------

    def mark_stale(self) -> None:
        """A REST call answered 401 or a login redirect: the jar is dead."""
        self.stale = True
        self.logged_in = False

    async def probe(self) -> bool:
        res = await self.rest.get_json(PROBE_PATH)
        return res.ok and isinstance(res.data, list)

    async def _inject(self, cookies: list[Cookie]) -> None:
        rows = []
        for c in cookies:
            if not c.domain.lstrip(".").lower().endswith(_COOKIE_DOMAIN_SUFFIX):
                continue   # never plant a cookie outside Procore
            self.ex.register_secret(c.value, tripwire=is_credential_cookie(c.name, c.value))
            row: dict[str, Any] = {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/",
                                   "secure": c.secure, "httpOnly": c.httpOnly, "sameSite": c.sameSite}
            if c.expires and c.expires > 0:
                row["expires"] = c.expires
            rows.append(row)
        if rows:
            await self.ex.jb.context.add_cookies(rows)

    async def _clear_procore_cookies(self) -> None:
        # Playwright matches a string domain exactly, so "procore.com" would
        # match neither ".procore.com" nor "app.procore.com"; use a pattern.
        try:
            await self.ex.jb.context.clear_cookies(domain=_PROCORE_COOKIE_DOMAIN_RE)
        except Exception:   # older signature: clear everything (the context is this job's alone)
            await self.ex.jb.context.clear_cookies()

    async def export_cookies(self) -> list[Cookie]:
        out = []
        for c in await self.ex.jb.context.cookies():
            domain = (c.get("domain") or "")
            if not domain.lstrip(".").lower().endswith(_COOKIE_DOMAIN_SUFFIX):
                continue
            self.ex.register_secret(c.get("value"), tripwire=is_credential_cookie(c.get("name"), c.get("value")))
            same = c.get("sameSite") or "Lax"
            out.append(Cookie(name=c["name"], value=c.get("value") or "", domain=domain, path=c.get("path") or "/",
                              expires=float(c.get("expires", -1) or -1), secure=bool(c.get("secure")),
                              httpOnly=bool(c.get("httpOnly")),
                              sameSite=same if same in ("Strict", "Lax", "None") else "Lax"))
        return out

    async def _ready(self, source: str) -> ToolReturn:
        self.logged_in = True
        self.stale = False
        self.ctx.steps.log("login.ready", f"Procore session ready ({source})", ok=True, result={"source": source})
        if self._on_ready is not None:
            await self._on_ready()
        return ToolReturn({"ok": True, "outcome": "ok"}, screenshot=False)

    def _fail(self, outcome: str) -> ToolReturn:
        return ToolReturn({"ok": False, "outcome": outcome}, ok=False, screenshot=False)

    # Help -------------------------------------------------------------------------

    async def _help(self, reason: str, *, page: Any = None, accept: Any = None) -> Any:
        """Open (or join) login-scope help and wait. Returns the WaitResult;
        raises _Ended when the job parked or help is suppressed."""
        shot = await self.ex.screenshot(page, label="login") if page is not None else None
        _resp, thread = await self.desk.open(reason, HELP_TEXT.get(reason, HELP_TEXT["login_failed"]), shot,
                                             skip_history=True)
        if thread is None:
            raise _Ended(ToolReturn({"ok": False, "outcome": reason}, ok=False, terminal=Finish(
                "failed", "Login help is suppressed", error_kind="login_failed", error="login help suppressed")))
        result = await self.desk.wait(thread, accept=accept)
        if result.parked:
            raise _Ended(ToolReturn({"parked": True}, terminal=HandedOff("Parked waiting for login help")))
        # The job acts on these messages now (a secret is redeemed separately).
        await self.desk.consume_seen([m.id for m in result.messages])
        return thread, result

    def _helper_action(self, result: Any) -> str | None:
        for m in result.messages:
            if m.action in ("give_up", "skip_daily"):
                return m.action
        if any(m.action == "retry" for m in result.messages) or result.status in ("resolved",):
            return "retry"
        return None

    # The tool ---------------------------------------------------------------------

    async def ensure(self, host: str) -> ToolReturn:
        if host != self.host:
            raise ToolError("out_of_scope", "use the host from the job seed")
        try:
            return await self._ensure()
        except _Ended as ended:
            return ended.ret

    async def _ensure(self) -> ToolReturn:
        client, lease = self.ctx.client, self.ctx.lease
        if self.logged_in and not self.stale and await self.probe():
            return ToolReturn({"ok": True, "outcome": "ok"}, screenshot=False)
        reject: datetime | None = self.jar_logged_in_at if (self.stale or self.logged_in) else None
        for _round in range(MAX_ROUNDS):
            sess = await client.identity_session(lease, IDENTITY, SessionRequest(
                host=self.host, reject_jar_logged_in_at=reject))
            self.ctx.steps.log("login.session", f"Stored session: {sess.status}", result={"status": sess.status})
            if sess.status == "ok" and sess.cookies:
                await self._inject(sess.cookies)
                if await self.probe():
                    self.jar_logged_in_at = sess.logged_in_at
                    return await self._ready("stored")
                self.ctx.steps.log("login.session", "Stored session is logged out", ok=False)
                reject = sess.logged_in_at
                await self._clear_procore_cookies()
            elif sess.status == "not_configured":
                return self._not_configured("not_configured")
            elif sess.status == "locked":
                action = await self._wait_help("login_locked")
                if action == "retry":
                    continue
                return self._fail("locked")
            elif sess.status == "help_open":
                action = await self._wait_help("login_failed")
                if action == "retry":
                    continue
                return self._fail("help_open")

            begin = await client.login_begin(lease, IDENTITY, LoginBeginRequest(
                host=self.host, rejected_jar_logged_in_at=reject))
            if not begin.granted:
                self.ctx.steps.log("login.begin", f"Login refused: {begin.reason}", ok=False,
                                   result={"reason": begin.reason})
                if begin.reason == "spacing":
                    wait_s = ((begin.retry_at - self._now()).total_seconds() if begin.retry_at else 600.0)
                    if wait_s <= SPACING_WAIT_MAX_SECONDS:
                        await self._sleep(max(1.0, wait_s + 1.0))
                        continue
                    return ToolReturn({"ok": False, "outcome": "spacing"}, ok=False, screenshot=False,
                                      terminal=Requeue("login_spacing", retry_at=begin.retry_at))
                if begin.reason in ("not_configured", "ip_not_allowed"):
                    return self._not_configured(begin.reason)
                reason = "login_locked" if begin.reason == "locked" else "login_failed"
                action = await self._wait_help(reason)
                if action == "retry":
                    continue
                return self._fail(begin.reason or "refused")

            self.ctx.steps.log("login.begin", "Login granted", ok=True, result={"attempt_id": str(begin.attempt_id)})
            outcome = await self._browser_login(begin)
            if outcome is not None:
                return outcome
            # None: a helper asked to retry; go round again.
        return self._fail("unexpected")

    def _not_configured(self, why: str) -> ToolReturn:
        return ToolReturn({"ok": False, "outcome": "not_configured"}, ok=False, screenshot=False,
                          terminal=Finish("failed", "Procore login is not available to this worker",
                                          error_kind="login_failed", error=why))

    async def _wait_help(self, reason: str) -> str | None:
        _thread, result = await self._help(reason)
        return self._helper_action(result)

    async def _report(self, attempt_id: Any, outcome: LoginOutcome, *, cookies: list[Cookie] | None = None,
                      round_no: int = 0) -> Any:
        req = LoginResultRequest(attempt_id=attempt_id, outcome=outcome.outcome, step=outcome.step[:60],
                                 cookies=cookies)
        resp = await self.ctx.client.call(
            "login_result", lease=self.ctx.lease, path_params={"identity": IDENTITY}, body=req,
            idem_key=f"login_result:{attempt_id}:{outcome.outcome}:{round_no}",
            retry=self.ctx.client.in_job_retry)
        self.ctx.steps.log("login.result", f"Login outcome {outcome.outcome} ({outcome.step})",
                           ok=outcome.outcome == "ok", result={"outcome": outcome.outcome, "step": outcome.step})
        return resp

    async def _browser_login(self, begin: Any) -> ToolReturn | None:
        attempt_id = begin.attempt_id
        email = begin.email or ""
        password = begin.password.get_secret_value() if begin.password is not None else ""
        # The email is identifying, not a credential: Procore pages can show it
        # (user menu, distribution). Scrub it from steps, never trip on it.
        self.ex.register_secret(email, tripwire=False)
        self.ex.register_secret(password)
        begin = None   # drop the response that carried the password
        lp = ProcoreLoginPage(self.ex.jb, self.ex, self.host, probe=self.probe)
        try:
            try:
                outcome = await lp.login(email, password)
            finally:
                email = password = ""   # drop our references (a str cannot be zeroed)
            round_no = 0
            while True:
                if outcome.outcome == "ok":
                    cookies = await self.export_cookies()
                    if not cookies:
                        outcome = LoginOutcome("unexpected", "ok_without_cookies")
                        continue
                    await self._report(attempt_id, outcome, cookies=cookies, round_no=round_no)
                    await lp.close()
                    return await self._ready("fresh_login")
                resp = await self._report(attempt_id, outcome, round_no=round_no)
                reason = resp.help_reason or LOGIN_OUTCOME_HELP_REASON.get(outcome.outcome)
                if reason is None:
                    return self._fail(outcome.outcome)
                if outcome.outcome != "mfa":
                    await lp.close()
                    action = await self._wait_help(reason)
                    if action == "retry":
                        return None
                    return self._fail(outcome.outcome)
                code = await self._mfa_code(lp)
                if code is None:
                    return None   # retry / resolved elsewhere: go round again
                if code == "":
                    return self._fail("mfa")
                round_no += 1
                try:
                    outcome = await lp.submit_code(code)
                finally:
                    code = ""
        finally:
            await lp.close()

    async def _mfa_code(self, lp: ProcoreLoginPage) -> str | None:
        """Wait on login help for a helper's code. Returns the code, None to
        retry the whole login, or "" when the helper ended it."""
        def accept(msgs: list) -> bool:
            return any((m.secret_ref is not None and not m.secret_ref.consumed) or m.action != "message"
                       for m in msgs)

        thread, result = await self._help("mfa", page=lp.page, accept=accept)
        action = self._helper_action(result)
        if action in ("give_up", "skip_daily"):
            return ""
        secret = next((m for m in result.messages if m.secret_ref is not None and not m.secret_ref.consumed), None)
        if secret is None:
            return None if action == "retry" else ""
        if not thread.holder:
            # Only the job holding the login page can redeem a code; this one
            # re-runs the session (the holder's login lands in the shared jar).
            return None
        resp = await self.ctx.client.help_secret_use(self.ctx.lease, thread.help_id, secret.id)
        code = resp.value.get_secret_value()
        self.ex.register_secret(code)
        self.ctx.steps.log("login.code", "Used a helper's one-time code", result={"message_id": secret.id})
        return code

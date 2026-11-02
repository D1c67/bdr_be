"""Deterministic personal Procore login through the owner-bound identity API."""
import json
import logging
from urllib.parse import urlsplit

from ...browser.executor import ToolError, _is_browser_dead
from ..pm_apps.procore.identity import ProcoreIdentity
from ..pm_apps.procore.login import ProcoreLoginPage, LoginOutcome, EMAIL_SELECTOR, STEP_TIMEOUT_MS
from ..pm_apps.procore.paths import LOGIN_ENTRY_PATH
from .guard import origin, APP_HOSTS, LOGIN, MAX_RESPONSE_BYTES

log = logging.getLogger(__name__)

HELP_TEXT = {
    "login_failed": "Your personal Procore login failed. Check your Browser accounts settings, then retry.",
    "login_locked": "Your personal Procore login is locked after repeated failures. Replace the saved password.",
    "mfa": "Your personal Procore login needs a verification code. Enter it in this private help request.",
    "captcha": "Procore requires a captcha. This task cannot bypass it; retry later or cancel.",
    "sso": "This task cannot use SSO or Microsoft passwords. Use a personal Procore password account.",
}


class PreparedLoginPage(ProcoreLoginPage):
    prepared = False

    def _assert_login_page(self, step):
        super()._assert_login_page(step)
        if origin(self.page.url) != "https://" + LOGIN:
            raise ToolError("login_refused", "Login origin refused")

    async def open(self):
        if self.prepared and self.page is not None and not self.page.is_closed():
            self._assert_login_page("preflight")
            self.prepared = False
            return None
        self.page = await self.jb.open_login_page()
        try:
            await self.page.goto(f"https://{self.host}{LOGIN_ENTRY_PATH}",
                                 wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS)
            await self.jb.guard.follow_redirects(self.page)
        except Exception:
            return LoginOutcome("unexpected", "entry_refused")
        if await self._await_field(EMAIL_SELECTOR) and self._on_login_path():
            return None
        return await self.classify("entry")

    async def _submit(self, field):
        await super()._submit(field)
        try:
            await self.jb.guard.follow_redirects(self.page)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            # Never the message: it can hold the callback code. classify() then
            # probes whatever session landed and reports a classified outcome.
            log.debug("personal login redirect not followed: %s", type(exc).__name__)


class UserIdentity(ProcoreIdentity):
    def __init__(self, ctx, executor, desk):
        self._prepared = None
        host = urlsplit(origin(ctx.input.url)).hostname
        if host not in APP_HOSTS:
            raise ToolError("login_refused", "Personal identity host refused")
        ctx.client.bind_host(host)
        super().__init__(ctx, executor, desk, host=host, login_factory=self._factory,
                         help_text=HELP_TEXT)
        ctx.client.login_preflight = self._preflight
        ctx.client.login_origin = self._origin

    async def probe(self):
        # APIRequestContext bypasses browser routes. Use one code-fixed GET and
        # disable its independent automatic redirect handling too.
        response = None
        try:
            response = await self.ex.jb.context.request.get(
                f"https://{self.host}/rest/v1.0/companies", max_redirects=0, timeout=15000)
            if response.status != 200:
                return False
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                return False
            body = await response.body()
            return len(body) <= MAX_RESPONSE_BYTES and isinstance(json.loads(body), list)
        except Exception:
            return False
        finally:
            if response is not None:
                await response.dispose()

    async def _help(self, reason, *, page=None, accept=None):
        # Never capture a login page, even with DOM masking.
        return await super()._help(reason, page=None, accept=accept)

    def _factory(self, *args, **kwargs):
        if self._prepared is None:
            raise ToolError("login_refused", "Login page is not ready")
        return self._prepared

    async def _preflight(self):
        if self._prepared is not None:
            await self._prepared.close()
        self._prepared = PreparedLoginPage(self.ex.jb, self.ex, self.host, probe=self.probe)
        outcome = await self._prepared.open()
        if outcome is not None:
            await self._prepared.close()
            reason = outcome.outcome if outcome.outcome in ("captcha", "sso", "mfa") else "login_failed"
            await self._help(reason)
            raise ToolError("login_refused", "Personal login requires owner attention", outcome=outcome.outcome)
        self._prepared._assert_login_page("credential_release")
        self._prepared.prepared = True
        value = self._origin()
        if value != "https://" + LOGIN:
            raise ToolError("login_refused", "Login origin refused")
        return value

    def _origin(self):
        page = self.ex.jb.login_page
        if page is None or page.is_closed():
            raise ToolError("login_refused", "Login page is not ready")
        return origin(page.url)

    async def _inject(self, cookies):
        checked = [c for c in cookies if c.domain.lower() in self.cookie_domains and c.secure]
        if len(checked) != len(cookies):
            raise ToolError("session_refused", "Cookie scope refused")
        await super()._inject(checked)

    async def export_cookies(self):
        return [c for c in await super().export_cookies() if c.domain.lower() in self.cookie_domains and c.secure]

    @property
    def cookie_domains(self):
        return frozenset({self.host, LOGIN, ".procore.com", "procore.com"})

    async def _report(self, attempt_id, outcome, **kwargs):
        if outcome.outcome == "ok":
            page = self.ex.jb.login_page
            if page is None:
                raise ToolError("login_refused", "Login page is not ready")
            # The reporting origin is observed after navigation, not invented
            # from a successful API probe while still on the password page.
            if urlsplit(page.url).hostname != self.host:
                await page.goto(self.ctx.input.url, wait_until="domcontentloaded", timeout=30_000)
                await self.ex.jb.guard.follow_redirects(page)
        return await super()._report(attempt_id, outcome, **kwargs)

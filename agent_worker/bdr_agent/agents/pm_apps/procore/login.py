"""The Procore login routine (O2, spec 4.2). Deterministic; the model never
sees it, the credentials or the login page.

It runs on a dedicated login page object (the only page the guard lets POST,
and only to login-chain paths on login.procore.com or to the challenge
hosts):

  1. GET https://<app host>/auth/procore, which bounces to login.procore.com;
  2. before every fill, asserts the page is on login.procore.com and on a
     vendored login path;
  3. fills the email, submits, then the password, submits;
  4. classifies the result: ok, password_rejected, mfa, captcha, challenge,
     sso, unexpected or timeout;
  5. for mfa, `submit_code` types a helper's one-time code into the code
     field (same assertions) and classifies again.

Password and code fields are registered as secret fields with the executor
(masked in screenshots, hidden in snapshots) and their values as job
secrets (scrubbed from every step). The caller drops its references to the
password once `login` returns; a Python str cannot be zeroed, so the value
lives on in memory until collected, and is never written anywhere.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ....protocol import PROCORE_HOSTS, PROCORE_LOGIN_HOST
from .paths import LOGIN_ENTRY_PATH, LOGIN_LOGIN_PATHS, SESSION_COOKIE

log = logging.getLogger("bdr_agent.procore.login")

EMAIL_SELECTOR = 'input[name="session[email]"], input[type="email"]'
PASSWORD_SELECTOR = 'input[name="session[password]"], input[type="password"]'
CODE_SELECTOR = ('input[autocomplete="one-time-code" i], input[name*="otp" i], input[name*="mfa" i], '
                 'input[name*="code" i], input[id*="otp" i], input[id*="code" i]')
CAPTCHA_SELECTOR = ('.g-recaptcha, iframe[src*="recaptcha"], .h-captcha, iframe[src*="hcaptcha"], '
                    '.cf-turnstile, iframe[src*="challenges.cloudflare.com"]')
CHALLENGE_SELECTOR = '#challenge-form, #challenge-stage, script[src*="/cdn-cgi/challenge-platform"]'
SSO_TEXT = ("single sign-on", "sign in with sso", "continue with sso", "your organization's login")
ERROR_SELECTOR = '[class*="error" i], [class*="alert" i], [class*="flash" i], [role="alert"]'

STEP_TIMEOUT_MS = 30_000
FIELD_TIMEOUT_MS = 15_000


class LoginFlowError(Exception):
    def __init__(self, outcome: str, step: str):
        super().__init__(f"{step}: {outcome}")
        self.outcome = outcome
        self.step = step


@dataclass
class LoginOutcome:
    outcome: str      # protocol.LoginOutcome
    step: str         # classifier step name, no URLs


def _host_path(url: str) -> tuple[str, str]:
    parts = urlsplit(url or "")
    return (parts.hostname or "").lower(), parts.path or "/"


class ProcoreLoginPage:
    """One login attempt on its own page in the job's context."""

    def __init__(self, jb: Any, executor: Any, host: str, *, probe: Any):
        if host not in PROCORE_HOSTS:
            raise ValueError("not a Procore app host")
        self.jb = jb
        self.ex = executor
        self.host = host
        self._probe = probe          # async () -> bool: REST probe with the context's cookies
        self.page: Any = None

    # Page helpers ------------------------------------------------------------------

    def _on_login_path(self) -> bool:
        host, path = _host_path(self.page.url)
        return host == PROCORE_LOGIN_HOST and any(p.match(path) for p in LOGIN_LOGIN_PATHS)

    def _assert_login_page(self, step: str) -> None:
        """Before every fill: the page must be on login.procore.com, on a
        vendored login path. Anything else is never typed into."""
        if self.page is None or self.page.is_closed() or not self._on_login_path():
            raise LoginFlowError("unexpected", f"{step}_not_on_login_page")

    async def _visible(self, selector: str, timeout_ms: int = 0) -> Any | None:
        loc = self.page.locator(selector).first
        try:
            if timeout_ms:
                await loc.wait_for(state="visible", timeout=timeout_ms)
            elif not await loc.is_visible():
                return None
            return loc
        except Exception:
            return None

    async def _blocked(self) -> bool:
        """A page that will never show the field we wait for: off the login
        chain, a Cloudflare page, a captcha, or a code prompt."""
        host, _ = _host_path(self.page.url)
        if host and host != PROCORE_LOGIN_HOST:
            return True
        try:
            if "just a moment" in (await self.page.title()).lower():
                return True
            if await self.page.locator(CHALLENGE_SELECTOR).count() or await self.page.locator(CAPTCHA_SELECTOR).count():
                return True
        except Exception:
            return False
        return bool(await self._visible(CODE_SELECTOR))

    async def _await_field(self, selector: str) -> Any | None:
        """The field once visible; None as soon as a blocker shows (or after
        FIELD_TIMEOUT_MS), so the caller classifies right away."""
        deadline = time.monotonic() + FIELD_TIMEOUT_MS / 1000
        while True:
            loc = await self._visible(selector)
            if loc is not None:
                return loc
            if await self._blocked() or time.monotonic() > deadline:
                return None
            await asyncio.sleep(0.2)

    async def _submit(self, field: Any) -> None:
        """Press Enter in the field and wait for the page to settle. A
        navigation the guard aborted (an SSO hop) is classified afterwards."""
        try:
            async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS):
                await field.press("Enter")
        except Exception as exc:
            log.debug("login submit navigation: %s", exc)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass

    async def _has_app_session(self) -> bool:
        for c in await self.jb.context.cookies():
            if c.get("name") == SESSION_COOKIE and (c.get("domain") or "").lstrip(".").lower() == self.host:
                return True
        return False

    # Classification ------------------------------------------------------------------

    async def classify(self, step: str) -> LoginOutcome:
        page = self.page
        host, _path = _host_path(page.url)
        if host in PROCORE_HOSTS or await self._has_app_session():
            if await self._probe():
                return LoginOutcome("ok", f"{step}_ok")
        for v in self.jb.guard.take_violations("login"):
            vhost, _ = _host_path(v.url)
            if vhost and not vhost.endswith("procore.com"):
                return LoginOutcome("sso", f"{step}_left_procore")
        if host and not host.endswith("procore.com"):
            return LoginOutcome("sso", f"{step}_left_procore")
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        if "just a moment" in title or await self._visible(CHALLENGE_SELECTOR):
            return LoginOutcome("challenge", f"{step}_challenge")
        if await page.locator(CAPTCHA_SELECTOR).count():
            return LoginOutcome("captcha", f"{step}_captcha")
        if await self._visible(CODE_SELECTOR):
            return LoginOutcome("mfa", f"{step}_code_requested")
        if await self._visible(PASSWORD_SELECTOR):
            return LoginOutcome("password_rejected", f"{step}_password_again")
        try:
            body = (await page.inner_text("body", timeout=5_000)).lower()
        except Exception:
            body = ""
        if any(t in body for t in SSO_TEXT):
            return LoginOutcome("sso", f"{step}_sso_page")
        if await self._visible(EMAIL_SELECTOR):
            return LoginOutcome("unexpected", f"{step}_back_to_email")
        return LoginOutcome("unexpected", f"{step}_unknown_page")

    # The routine ------------------------------------------------------------------

    async def open(self) -> LoginOutcome | None:
        """Load the login entry. None when the email form is ready; otherwise
        the outcome (challenge, captcha, ok when the jar was already good...)."""
        self.page = await self.jb.open_login_page()
        try:
            await self.page.goto(f"https://{self.host}{LOGIN_ENTRY_PATH}", wait_until="domcontentloaded",
                                 timeout=STEP_TIMEOUT_MS)
        except Exception as exc:
            if "Timeout" in type(exc).__name__ or "Timeout" in str(exc):
                return LoginOutcome("timeout", "entry_timeout")
            log.debug("login entry navigation: %s", exc)
        if await self._await_field(EMAIL_SELECTOR) and self._on_login_path():
            return None
        return await self.classify("entry")

    async def login(self, email: str, password: str) -> LoginOutcome:
        try:
            early = await self.open()
            if early is not None:
                return early
            self._assert_login_page("email")
            field = await self._await_field(EMAIL_SELECTOR)
            if field is None:
                return await self.classify("email")
            await field.fill(email, timeout=FIELD_TIMEOUT_MS)
            await self._submit(field)

            pw_field = await self._await_field(PASSWORD_SELECTOR)
            if pw_field is None:
                return await self.classify("email")
            self._assert_login_page("password")
            self.ex.mark_secret_field(PASSWORD_SELECTOR)
            await pw_field.fill(password, timeout=FIELD_TIMEOUT_MS)
            await self._submit(pw_field)
            return await self.classify("password")
        except LoginFlowError as exc:
            return LoginOutcome(exc.outcome, exc.step)
        except asyncio.TimeoutError:
            return LoginOutcome("timeout", "login_timeout")
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                return LoginOutcome("timeout", "login_timeout")
            raise

    async def submit_code(self, code: str) -> LoginOutcome:
        """Type a helper's one-time code (MFA) and classify again."""
        try:
            if self.page is None or self.page.is_closed():
                return LoginOutcome("unexpected", "code_page_closed")
            self._assert_login_page("code")
            field = await self._visible(CODE_SELECTOR, FIELD_TIMEOUT_MS)
            if field is None:
                return await self.classify("code")
            self.ex.mark_secret_field(CODE_SELECTOR)
            await field.fill(code, timeout=FIELD_TIMEOUT_MS)
            await self._submit(field)
            return await self.classify("code")
        except LoginFlowError as exc:
            return LoginOutcome(exc.outcome, exc.step)
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                return LoginOutcome("timeout", "code_timeout")
            raise

    async def close(self) -> None:
        await self.jb.close_login_page()
        self.page = None

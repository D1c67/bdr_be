"""Sign-in for user_browser v2: fill_credential, login results, saved sessions
(plan 7.4, 7.5, 8, 10.5, 12.3; WI-10). Signed worker code.

The model picks a field (`fill_credential(ref, field)`); this module decides
whether anything is typed. Before asking BDR for the secret it checks, on the
live page:
  - the page is https and its URL carries no user:pass@;
  - the page's top-level origin is exactly one of the job credential's
    `login_origins` (job policy). Lookalikes such as
    https://procore.com.example.net never equal a login origin;
  - the top-level host is not an identity provider (signed list, 7.5), even
    when the policy says otherwise;
  - the element's frame, and every frame above it, has that same origin (a
    cross-origin iframe is refused);
  - the element is an input of a plausible type (password for the password;
    text, email or tel for the username);
  - the credential is not marked SSO.
BDR's login/begin (USER_V2_ENDPOINTS["user_login_begin_v2"]) checks the
credential and origin again and returns the username and password for one
attempt. The values live only in memory and in the job's secret registry
(scrubbed from every step, the password trips the leak check). They are
written into the checked element itself (SET_VALUE_JS: the native value
setter plus input and change events), never sent as keystrokes, which go to
whatever has focus: a cross-origin frame on the login page could take focus
between two keys. The worker re-checks the top-level origin and the frame
origin right before and after the write; a redirect clears the field.

After each tool, `observe()` follows the open attempt and reports
login/result: `ok` (with only the cookies whose domain is in the credential's
`cookie_domains`, secure only), `mfa` (a code field: the owner's code arrives
through the help secret path and is typed by the worker, never the model),
`captcha` (the task ends: site_challenge), `sso` (an identity provider
appeared: the task ends sso_required), `unexpected` or `timeout` (on an ad hoc
site the task ends login_unclear; a curated site lets the model try again
within BDR's spacing).

Two tasks on one login (12.3): when login/begin answers `spacing` or
`login_busy`, the worker polls the session route every 15 s for a session
saved after this job started, loads it and reloads the page, and only if none
appears falls back to the normal spacing (uncharged requeue). At start every
credential's saved session (if any) is loaded the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets as pysecrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ...browser.executor import ToolError, ToolReturn, _is_browser_dead
from ...client import BdrRejected
from ...protocol import (
    LOGIN_RESULT_DEADLINE_SECONDS,
    USER_SESSION_POLL_SECONDS,
    Cookie,
    UserCredentialFillRequest,
    UserCredentialResultRequest,
    UserPolicyCredential,
    UserSessionPoll,
    is_identity_provider_host,
    url_origin,
)
from ...redact import strip_query
from ...slot import Finish, HandedOff, Requeue

log = logging.getLogger("bdr_agent.user_browser_v2.identity")

RESULT_MARGIN_SECONDS = 30
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
USERNAME_TYPES = ("", "text", "email", "tel")

FIELD_INFO_JS = r"""
(el) => {
  const tag = el && el.tagName ? el.tagName.toLowerCase() : "";
  const type = el && el.getAttribute ? (el.getAttribute("type") || "").toLowerCase() : "";
  return {tag: tag, type: type, disabled: !!(el && el.disabled), readonly: !!(el && el.readOnly)};
}
"""
# A saved value is written into the checked element itself, never sent as
# keystrokes: keystrokes go to whatever has focus, and a cross-origin frame
# on the login page can take focus between two keys (review WI-10). The
# native setter plus input and change events is what frameworks listen to.
SET_VALUE_JS = r"""(el, v) => {
  if (!el || !el.isConnected) return false;
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const d = Object.getOwnPropertyDescriptor(proto, "value");
  try { el.focus({preventScroll: true}); } catch (e) {}
  d.set.call(el, v);
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return d.get.call(el) === v;
}"""
# A field the worker filled from a credential keeps this attribute (the
# snapshot walker strips only its own data-bdr-ref, so the mark survives every
# later read_page): its value reads [hidden], screenshots mask it, and
# type_text refuses it (review WI-10).
SECRET_FIELD_ATTR = "data-bdr-secret"
SECRET_FIELD_SELECTOR = f"[{SECRET_FIELD_ATTR}]"
MARK_SECRET_FIELD_JS = "(el) => { el.setAttribute('data-bdr-secret', '1'); return true; }"
MARK_JS = "(name) => { try { Object.defineProperty(window, name, {value: 1, configurable: false}); } catch (e) {} return true; }"
HAS_MARK_JS = "(name) => Object.prototype.hasOwnProperty.call(window, name)"
CONNECTED_JS = "(el) => !!el && el.isConnected"
PAGE_STATE_JS = r"""
() => {
  const visible = el => {
    if (!el || !el.getClientRects || !el.getClientRects().length) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none";
  };
  const words = /(one.?time|otp|verification|security.?code|passcode|2fa|mfa|auth(entication)?.?code|\bcode\b)/i;
  let password = false, code = false;
  for (const el of document.querySelectorAll("input")) {
    if (!visible(el)) continue;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "password") { password = true; continue; }
    if (type === "hidden" || type === "checkbox" || type === "radio" || type === "submit") continue;
    const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
    let label = "";
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) label = l.textContent || ""; }
    const text = [el.getAttribute("name"), el.id, el.getAttribute("placeholder"), el.getAttribute("aria-label"), label].join(" ");
    if (ac === "one-time-code" || words.test(text)) code = true;
  }
  const captcha = !!document.querySelector(
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], ' +
    'iframe[src*="arkoselabs"], iframe[src*="funcaptcha"], .g-recaptcha, .h-captcha, .cf-turnstile, #challenge-form');
  return {password: password, code: code, captcha: captcha};
}
"""
CODE_FIELD_JS = r"""
() => {
  const visible = el => el && el.getClientRects && el.getClientRects().length;
  const words = /(one.?time|otp|verification|security.?code|passcode|2fa|mfa|auth(entication)?.?code|\bcode\b)/i;
  for (const el of document.querySelectorAll("input")) {
    if (!visible(el)) continue;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (["password", "hidden", "checkbox", "radio", "submit", "button"].includes(type)) continue;
    const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
    const text = [el.getAttribute("name"), el.id, el.getAttribute("placeholder"), el.getAttribute("aria-label")].join(" ");
    if (ac === "one-time-code" || words.test(text)) return el;
  }
  return null;
}
"""


class FillRefused(ToolError):
    """fill_credential typed nothing. `code` is the model-facing reason."""


@dataclass
class LoginState:
    credential: UserPolicyCredential
    attempt_id: UUID
    origin: str
    username: str
    password: str
    page: Any
    started: float
    mark: str = ""
    filled: set[str] = field(default_factory=set)
    handles: dict[str, Any] = field(default_factory=dict)
    mfa_reported: bool = False
    code_rounds: int = 0
    done: bool = False


def cookie_allowed(domain: str, cookie_domains: list[str] | tuple[str, ...]) -> bool:
    """A cookie domain inside the credential's cookie_domains: ".x.com" covers
    x.com and its subdomains, "x.com" covers only x.com."""
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return False
    for cd in cookie_domains:
        c = cd.strip().lower()
        base = c.lstrip(".")
        if d == base or (c.startswith(".") and d.endswith("." + base)):
            return True
    return False


def to_cookie(raw: dict[str, Any]) -> Cookie | None:
    same_site = raw.get("sameSite") if raw.get("sameSite") in ("Strict", "Lax", "None") else "Lax"
    try:
        return Cookie(name=str(raw.get("name") or ""), value=str(raw.get("value") or ""),
                      domain=str(raw.get("domain") or ""), path=str(raw.get("path") or "/"),
                      expires=float(raw.get("expires") if raw.get("expires") is not None else -1),
                      secure=bool(raw.get("secure")), httpOnly=bool(raw.get("httpOnly")), sameSite=same_site)
    except (ValueError, TypeError):
        return None


class UserIdentityV2:
    """Credentials and sessions for one job. `guard` is the job's
    UserGuardV2 (its `policy` is always the current job policy)."""

    def __init__(self, ctx: Any, executor: Any, desk: Any, guard: Any, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 poll_seconds: float = float(USER_SESSION_POLL_SECONDS),
                 pickup_window: float = float(LOGIN_RESULT_DEADLINE_SECONDS),
                 result_deadline: float = float(LOGIN_RESULT_DEADLINE_SECONDS - RESULT_MARGIN_SECONDS)):
        self.ctx = ctx
        self.ex = executor
        self.desk = desk
        self.guard = guard
        self._clock = clock
        self._sleep = sleep
        self.poll_seconds = poll_seconds
        self.pickup_window = pickup_window
        self.result_deadline = result_deadline
        self.started_at = datetime.now(UTC)
        self.attempt: LoginState | None = None
        self.jars: dict[str, datetime | None] = {}
        self._notices: list[str] = []

    # Policy views --------------------------------------------------------------------

    @property
    def policy(self) -> Any:
        return self.guard.policy

    def credential_for_origin(self, origin: str | None) -> UserPolicyCredential | None:
        if origin is None:
            return None
        return next((c for c in self.policy.credentials if origin in c.login_origins), None)

    def _site_kind(self, cred: UserPolicyCredential) -> str:
        site = self.policy.site(cred.site_key)
        return site.kind if site is not None else "ad_hoc"

    def login_origins(self) -> set[str]:
        return {o for c in self.policy.credentials for o in c.login_origins}

    def take_notices(self) -> list[str]:
        out, self._notices = self._notices, []
        return out

    def _notice(self, text: str) -> None:
        if text not in self._notices:
            self._notices.append(text)

    def _step(self, kind: str, summary: str, **fields: Any) -> None:
        try:
            self.ctx.steps.log(kind, summary, **fields)
        except Exception as exc:
            log.debug("identity step not logged: %s", exc)

    # Checks before any secret is asked for ------------------------------------------

    async def check_target(self, page: Any, handle: Any, field_name: str) -> tuple[UserPolicyCredential, str, Any]:
        url = getattr(page, "url", "") or ""
        try:
            parts = urlsplit(url)
        except ValueError:
            raise FillRefused("fill_refused", "not a web page", reason="bad_page") from None
        if parts.scheme != "https":
            raise FillRefused("fill_refused", "sign-in fields are filled only on https pages", reason="not_https")
        if parts.username or parts.password:
            raise FillRefused("fill_refused", "this page address is not a login page", reason="origin_not_allowed")
        origin = url_origin(url)
        host = (parts.hostname or "").lower()
        if origin is None:
            raise FillRefused("fill_refused", "not a web page", reason="bad_page")
        if is_identity_provider_host(host):
            raise FillRefused("fill_refused", "this is a Microsoft, Google or other single sign-on page; "
                              "ask for help with user_request_help reason sso", reason="identity_provider")
        cred = self.credential_for_origin(origin)
        if cred is None:
            raise FillRefused("fill_refused", "this page is not the saved login page of an account in this task",
                              reason="origin_not_allowed")
        if cred.sso_required:
            raise FillRefused("fill_refused", "this account signs in with single sign-on; ask for help with "
                              "user_request_help reason sso", reason="sso_required")
        if handle is None:
            raise FillRefused("fill_refused", "the field is gone; call read_page again", reason="stale_ref")
        frame = await _owner_frame(handle)
        if frame is None or not self._frame_chain_ok(frame, origin):
            raise FillRefused("fill_refused", "the field is inside a frame from another site", reason="cross_origin_frame")
        try:
            info = await handle.evaluate(FIELD_INFO_JS)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise FillRefused("fill_refused", "the field is gone; call read_page again", reason="stale_ref") from None
        tag, typ = str(info.get("tag") or ""), str(info.get("type") or "")
        if tag != "input" or info.get("disabled") or info.get("readonly"):
            raise FillRefused("fill_refused", "that element is not a sign-in input", reason="not_a_field")
        if field_name == "password" and typ != "password":
            raise FillRefused("fill_refused", "the password goes only into a password field", reason="not_a_field")
        if field_name == "username" and typ not in USERNAME_TYPES:
            raise FillRefused("fill_refused", "the username goes only into a text or email field", reason="not_a_field")
        return cred, origin, frame

    @staticmethod
    def _frame_chain_ok(frame: Any, origin: str) -> bool:
        node = frame
        depth = 0
        while node is not None and depth < 20:
            try:
                if url_origin(node.url) != origin:
                    return False
                node = node.parent_frame
            except Exception:
                return False
            depth += 1
        return node is None

    def _still_on(self, page: Any, frame: Any, origin: str) -> bool:
        try:
            if url_origin(page.url) != origin or is_identity_provider_host(urlsplit(page.url).hostname or ""):
                return False
            return self._frame_chain_ok(frame, origin)
        except Exception:
            return False

    # fill_credential -------------------------------------------------------------------

    async def fill(self, ref: str, field_name: str) -> ToolReturn:
        page = await self.ex.page()
        locator = await self.ex._locator(ref)
        try:
            handle = await locator.element_handle(timeout=5_000)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            handle = None
        cred, origin, frame = await self.check_target(page, handle, field_name)
        state = self.attempt
        if state is not None and (state.done or state.page is not page
                                  or str(state.credential.credential_id) != str(cred.credential_id)
                                  or state.origin != origin):
            if not state.done:
                await self._report(state, "unexpected", step="new_attempt")
            state = None
        if state is None:
            begun = await self._begin(page, cred, origin)
            if isinstance(begun, ToolReturn):
                return begun
            state = begun
        value = state.username if field_name == "username" else state.password
        try:
            await self._type(page, frame, handle, origin, value)
        except FillRefused:
            # Something moved between the check and the keystrokes: nothing
            # more is typed and BDR hears that this attempt is over.
            await self._report(state, "unexpected", step="page_changed")
            raise
        selector = self.ex.snapshot.selector(ref) if self.ex.snapshot is not None else None
        if selector:
            self.ex.mark_secret_field(selector)
        if state.mark and await self._navigated(state):
            state.handles.clear()          # a multi-step form: the earlier step's document is gone
        state.filled.add(field_name)
        state.handles[field_name] = handle
        await self._arm(state, page)
        try:
            await self.guard.open_login_attempt(credential_id=cred.credential_id, page=page, element=handle,
                                                markers=tuple(v for v in (state.username, state.password) if v))
        except ValueError:
            await self._report(state, "unexpected", step="exemption_refused")
            raise FillRefused("fill_refused", "the page left the login page", reason="page_changed") from None
        self._step("login.fill", f"Filled the {field_name} field", ok=True,
                   result={"field": field_name, "origin": origin, "account": cred.account_label})
        return ToolReturn({"filled": field_name, "account": cred.account_label,
                           "url": strip_query(page.url) or page.url}, screenshot=False)

    async def _begin(self, page: Any, cred: UserPolicyCredential, origin: str) -> LoginState | ToolReturn:
        client, lease = self.ctx.client, self.ctx.lease
        req = UserCredentialFillRequest(credential_id=cred.credential_id, origin=origin, frame_origin=origin,
                                        rejected_jar_logged_in_at=self.jars.get(str(cred.credential_id)))
        resp = await client.login_begin_v2(lease, req)
        if not resp.granted:
            return await self._refused(resp, cred, page)
        username = resp.username.get_secret_value() if resp.username is not None else ""
        password = resp.password.get_secret_value() if resp.password is not None else ""
        owner = str(self.ctx.job_id)
        self.ctx.secrets.add(password, owner=owner, tripwire=True)
        # The username is identifying but may appear on the page after sign-in.
        self.ctx.secrets.add(username, owner=owner, tripwire=False)
        self._step("login.begin", "Sign-in attempt started", ok=True,
                   result={"origin": origin, "account": cred.account_label})
        state = LoginState(credential=cred, attempt_id=resp.attempt_id, origin=origin, username=username,  # type: ignore[arg-type]
                           password=password, page=page, started=self._clock())
        self.attempt = state
        return state

    async def _refused(self, resp: Any, cred: UserPolicyCredential, page: Any) -> ToolReturn:
        reason = resp.reason or "refused"
        self._step("login.refused", f"Sign-in refused by BDR: {reason}", ok=False, result={"reason": reason})
        if reason in ("spacing", "login_busy"):
            picked = await self.pickup_session(cred, page)
            if picked:
                return ToolReturn({"signed_in": True, "source": "saved_session", "account": cred.account_label,
                                   "url": strip_query(page.url) or page.url})
            return ToolReturn({"error": "login_spacing"}, ok=False, screenshot=False,
                              terminal=Requeue("login_spacing", retry_at=resp.retry_at))
        text = {
            "locked": "the saved sign-in is locked after failed attempts; ask with user_request_help reason needs_credential",
            "daily_cap": "the saved sign-in reached today's attempt limit; ask with user_request_help reason needs_credential",
            "sso_required": "this account signs in with single sign-on; ask with user_request_help reason sso",
            "help_open": "the owner is already helping with this sign-in; wait, then try again",
            "not_configured": "there is no saved sign-in for this site; ask with user_request_help reason needs_credential",
        }.get(reason, "BDR refused the sign-in")
        return ToolReturn({"error": "fill_refused", "reason": reason, "detail": text}, ok=False, screenshot=False)

    async def _type(self, page: Any, frame: Any, handle: Any, origin: str, value: str) -> None:
        """Write `value` into the checked element only (SET_VALUE_JS), after
        the origin and frame checks, so no character can reach another frame."""
        if not value:
            raise FillRefused("fill_refused", "no saved value for that field", reason="no_value")
        if not self._still_on(page, frame, origin):
            raise FillRefused("fill_refused", "the page changed before typing", reason="page_changed")
        try:
            await handle.fill("")
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise FillRefused("fill_refused", "the field is gone; call read_page again", reason="stale_ref") from None
        if not self._still_on(page, frame, origin):
            raise FillRefused("fill_refused", "the page changed while typing", reason="page_changed")
        try:
            written = await handle.evaluate(SET_VALUE_JS, value)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            written = False
        if not written or not self._still_on(page, frame, origin):
            with contextlib.suppress(Exception):
                await handle.fill("")
            raise FillRefused("fill_refused", "the page changed while typing", reason="page_changed")
        with contextlib.suppress(Exception):
            await handle.evaluate(MARK_SECRET_FIELD_JS)
        self.ex.mark_secret_field(SECRET_FIELD_SELECTOR)
        remember = getattr(self.ex, "remember_secret_handle", None)
        if remember is not None:
            remember(handle)

    # After each tool: follow the open attempt ---------------------------------------

    async def observe(self) -> Finish | HandedOff | Requeue | None:
        """Report the open attempt's result once the page shows one. Returns a
        terminal result when the task must end."""
        state = self.attempt
        if state is None or state.done:
            return None
        if self._clock() - state.started > self.result_deadline:
            return await self._result(state, "timeout")
        page = state.page
        if page is None or page.is_closed():
            return await self._result(state, "unexpected", step="page_closed")
        url = page.url or ""
        origin = url_origin(url)
        host = (urlsplit(url).hostname or "").lower() if origin else ""
        if host and is_identity_provider_host(host):
            return await self._result(state, "sso")
        if not state.filled:
            return None
        if not await self._submitted(state):
            return None
        if origin in state.credential.login_origins:
            view = await _page_state(page)
            if view.get("captcha"):
                return await self._result(state, "captcha")
            if view.get("code") and not view.get("password"):
                return await self._mfa(state)
            if view.get("password"):
                if "password" not in state.filled or not await self._navigated(state):
                    return None    # the next step of the sign-in form: fill the password
                return await self._result(state, "unexpected", step="still_on_login_form")
        if origin is None:
            return None            # about:blank between documents
        return await self._result(state, "ok")

    @staticmethod
    async def _arm(state: LoginState, page: Any) -> None:
        """Mark the current document; `_navigated` is true once it is replaced."""
        state.mark = "__bdr_lm_" + pysecrets.token_hex(6)
        with contextlib.suppress(Exception):
            await page.main_frame.evaluate(MARK_JS, state.mark)

    async def _navigated(self, state: LoginState) -> bool:
        try:
            return not bool(await state.page.main_frame.evaluate(HAS_MARK_JS, state.mark))
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            return True

    async def _submitted(self, state: LoginState) -> bool:
        if await self._navigated(state):
            return True
        for handle in state.handles.values():
            try:
                if not await handle.evaluate(CONNECTED_JS):
                    return True
            except Exception:
                return True
        return False

    async def _result(self, state: LoginState, outcome: str, *, step: str | None = None) -> Finish | None:
        cred = state.credential
        cookies: list[Cookie] | None = None
        if outcome == "ok":
            cookies = await self._cookies(cred)
            if not cookies:
                outcome, step = "unexpected", "no_session_cookie"
                cookies = None
        await self._report(state, outcome, step=step, cookies=cookies)
        label = self._label(cred)
        if outcome == "ok":
            self._notice(f"Signed in to {label} as account {cred.account_label}.")
            return None
        if outcome in ("captcha", "challenge"):
            return Finish("failed", f"{label} asked for a captcha or a browser check that the assistant cannot pass.",
                          error_kind="login_failed", error="site_challenge")
        if outcome == "sso":
            return Finish("failed", f"{label} sends this sign-in to Microsoft, Google or another single sign-on page.",
                          error_kind="login_failed", error="sso_required")
        if self._site_kind(cred) == "ad_hoc":
            return Finish("failed", f"The sign-in to {label} did not clearly succeed. Check the saved username "
                          "and password.", error_kind="login_failed", error="login_unclear")
        self._notice(f"The sign-in to {label} did not complete ({outcome}). Check the page before trying again.")
        return None

    def _label(self, cred: UserPolicyCredential) -> str:
        site = self.policy.site(cred.site_key)
        return site.label if site is not None else cred.site_key

    async def _report(self, state: LoginState, outcome: str, *, step: str | None = None,
                      cookies: list[Cookie] | None = None) -> Any:
        if outcome != "mfa":
            state.done = True
            self.guard.close_login_attempt("result")
        req = UserCredentialResultRequest(credential_id=state.credential.credential_id, attempt_id=state.attempt_id,
                                          origin=state.origin, outcome=outcome, step=step, cookies=cookies)  # type: ignore[arg-type]
        self._step("login.result", f"Sign-in result: {outcome}", ok=outcome in ("ok", "mfa"),
                   result={"outcome": outcome, "step": step, "cookies": len(cookies or [])})
        try:
            resp = await self.ctx.client.login_result_v2(self.ctx.lease, req)
        except BdrRejected as exc:
            log.warning("login result refused: %s", exc.code)
            state.done = True
            return None
        if outcome == "ok" and cookies:
            for c in cookies:
                self.ctx.secrets.add(c.value, owner=str(self.ctx.job_id), tripwire=False)
        if not getattr(resp, "attempt_open", False):
            state.done = True
            self.guard.close_login_attempt("result")
        if state.done and self.attempt is state:
            self.attempt = None
        return resp

    async def _cookies(self, cred: UserPolicyCredential) -> list[Cookie]:
        try:
            raw = await self.ex.jb.context.cookies()
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            return []
        out: list[Cookie] = []
        for item in raw:
            if not item.get("secure") or not cookie_allowed(str(item.get("domain") or ""), cred.cookie_domains):
                continue
            cookie = to_cookie(item)
            if cookie is not None and cookie.value:
                out.append(cookie)
        return out

    # One-time codes (8) --------------------------------------------------------------

    async def _mfa(self, state: LoginState) -> Finish | HandedOff | None:
        if state.code_rounds >= 3:
            return await self._result(state, "unexpected", step="code_rounds")
        state.code_rounds += 1
        if not state.mfa_reported:
            state.mfa_reported = True
            resp = await self._report(state, "mfa", step="code_field")
            if state.done or resp is None:
                return None
        label = self._label(state.credential)
        _r, thread = await self.desk.open("mfa", f"{label} asked for a verification code.", None,
                                          login_attempt_id=state.attempt_id,
                                          credential_id=state.credential.credential_id)
        if thread is None:
            return await self._result(state, "unexpected", step="code_help_unavailable")
        waited = await self.desk.wait(thread, accept=lambda msgs: any(m.secret_ref is not None for m in msgs))
        if waited.parked:
            return HandedOff("Parked waiting for a verification code")
        secret = next((m for m in waited.messages if m.secret_ref is not None), None)
        if secret is None:
            return await self._result(state, "unexpected", step="no_code")
        used = await self.ctx.client.help_secret_use(self.ctx.lease, thread.help_id, secret.id,
                                                      login_attempt_id=state.attempt_id)
        code = used.value.get_secret_value()
        self.ctx.secrets.add(code, owner=str(self.ctx.job_id), tripwire=True)
        page = state.page
        try:
            js = await page.main_frame.evaluate_handle(CODE_FIELD_JS)
            element = js.as_element()
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            element = None
        if element is None:
            return await self._result(state, "unexpected", step="code_field_gone")
        frame = await _owner_frame(element)
        origin = url_origin(page.url)
        if (frame is None or origin is None or origin not in state.credential.login_origins
                or not self._still_on(page, frame, origin)):
            return await self._result(state, "unexpected", step="code_page_changed")
        try:
            await self._type(page, frame, element, origin, code)
            await self.guard.open_login_attempt(credential_id=state.credential.credential_id, page=page,
                                                element=element, markers=(code,))
            await self._arm(state, page)
            state.handles = {"code": element}
            await element.press("Enter")
        except (FillRefused, ValueError):
            return await self._result(state, "unexpected", step="code_page_changed")
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        self._step("login.code", "Typed the owner's verification code", ok=True)
        with contextlib.suppress(Exception):
            await self.guard.settle()
        return await self.observe()

    # Saved sessions (10.5, 12.3) ---------------------------------------------------------

    async def load_saved_sessions(self) -> int:
        """At start: load each credential's newest saved session, if any."""
        loaded = 0
        for cred in list(self.policy.credentials):
            if cred.sso_required:
                continue
            try:
                resp = await self.ctx.client.session_poll(self.ctx.lease, UserSessionPoll(
                    credential_id=cred.credential_id, newer_than=LONG_AGO))
            except BdrRejected as exc:
                log.info("no saved session: %s", exc.code)
                continue
            if await self._inject(cred, resp):
                loaded += 1
        return loaded

    async def _inject(self, cred: UserPolicyCredential, resp: Any) -> bool:
        if getattr(resp, "status", None) != "ok" or not resp.cookies:
            return False
        cookies = [c for c in resp.cookies if c.secure and cookie_allowed(c.domain, cred.cookie_domains)]
        if not cookies:
            return False
        for c in cookies:
            self.ctx.secrets.add(c.value, owner=str(self.ctx.job_id), tripwire=False)
        await self.ex.jb.context.add_cookies([c.model_dump() for c in cookies])
        self.jars[str(cred.credential_id)] = resp.logged_in_at
        self._step("login.session", "Loaded a saved sign-in", ok=True,
                   result={"account": cred.account_label, "cookies": len(cookies)})
        return True

    async def pickup_session(self, cred: UserPolicyCredential, page: Any) -> bool:
        """Another task is signing in with this account: wait for the session
        it saves instead of waiting out the spacing."""
        deadline = self._clock() + self.pickup_window
        self.ctx.set_waiting_help(True)
        try:
            while True:
                try:
                    resp = await self.ctx.client.session_poll(self.ctx.lease, UserSessionPoll(
                        credential_id=cred.credential_id, newer_than=self.started_at))
                except BdrRejected as exc:
                    log.info("session poll refused: %s", exc.code)
                    resp = None
                if resp is not None and await self._inject(cred, resp):
                    break
                if self._clock() + self.poll_seconds > deadline:
                    return False
                await self._sleep(self.poll_seconds)
        finally:
            self.ctx.set_waiting_help(False)
        await self._probe(cred, page)
        return True

    async def _probe(self, cred: UserPolicyCredential, page: Any) -> None:
        """Reload the page with the loaded session; with a site
        logged_in_probe, report whether it shows the signed-in marker."""
        site = self.policy.site(cred.site_key)
        probe = site.profile.logged_in_probe if site is not None else None
        try:
            if probe is not None and url_origin(page.url) and self.guard.in_scope(page.url):
                target = url_origin(page.url) + probe.path
                verdict = self.guard.prepare_navigation(page, target, start_url=target)
                if verdict:
                    await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
                    found = await page.locator(probe.selector).count() > 0
                    self._step("login.probe", "Checked the signed-in marker", ok=found)
                    return
            if url_origin(page.url):
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise

    async def abandon(self) -> None:
        """Job end: an attempt that never got a result is reported, so BDR
        does not wait out the result deadline."""
        state = self.attempt
        if state is None or state.done:
            return
        with contextlib.suppress(BdrRejected, Exception):
            await self._report(state, "unexpected", step="job_ended")


async def _owner_frame(handle: Any) -> Any:
    try:
        return await handle.owner_frame()
    except Exception:
        return None


async def _page_state(page: Any) -> dict[str, Any]:
    try:
        data = await page.main_frame.evaluate(PAGE_STATE_JS)
    except Exception as exc:
        if _is_browser_dead(exc):
            raise
        return {}
    return data if isinstance(data, dict) else {}


__all__ = ["FillRefused", "LoginState", "UserIdentityV2", "cookie_allowed", "to_cookie"]

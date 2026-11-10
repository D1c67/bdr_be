"""Model tools for user_browser v2 (plan 15.2, 6.2, 7.4, 10.3; WI-10). Signed worker code.

`V2Executor` is the shared Executor (browser/executor.py, the one choke point)
with the user_browser rules on top. Reused tools: navigate, read_page,
find_text, scroll, go_back, wait_for, click, type_text, select_option and
press_key (protocol.TOOLS_BY_TYPE["user_browser"] is the only type that has
the four acting tools among the personal types). New tools with their own
handlers here: switch_tab, fill_credential, user_request_help, user_finish.

What the user type adds to the shared tools:
  navigate       UserGuardV2.prepare_navigation first (floor, the navigate URL
                 rule with the current page's links and the start URL as the
                 exceptions); a navigation the guard site-holds waits for the
                 owner instead of failing on the goto timeout.
  click          a click-level final-action hold (`check_click`) before the
                 click; refused outside the sign-in form during a login.
  type_text      refuses password and code fields (protocol.USER_SECRET_INPUT_RE,
                 password inputs, one-time-code inputs, fields the worker
                 filled from a credential); `check_interaction` during a
                 login; every typed value is added to the guard's typed
                 strings (exfiltration rule); `submit` presses Enter only after
                 `check_click(action="enter")`.
  select_option  `check_interaction`; the value joins the typed strings.
  press_key      Enter goes through `check_click(action="enter")` on the
                 focused element; other keys through `check_interaction`.
  read_page      also lists the task's tabs (tab ids for switch_tab).
After every tool the executor waits for open holds (the model is not asked
for the next step meanwhile; active time does not count), follows an open
sign-in attempt (identity.observe) and appends the guard's, the sign-in's and
the downloads' notices to the observation.
Screenshots are never taken on a credential login origin or an identity
provider page.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ...agent.help import _CLOSED, HelpDesk, HelpThread, WaitResult
from ...browser.executor import (
    _FIELD_PROBE_JS,
    NAV_TIMEOUT_MS,
    Executor,
    ToolError,
    ToolOutcome,
    ToolReturn,
    _is_browser_dead,
    _json,
    _settle_load,
    _short,
    clip_content,
    secret_input_re,
)
from ...protocol import (
    FINISH_SUMMARY_MAX,
    UserHelpOpenV2Request,
    is_identity_provider_host,
    url_origin,
)
from ...redact import strip_query
from ...slot import Finish, HandedOff
from .exfil import trace_url
from .identity import SECRET_FIELD_ATTR

log = logging.getLogger("bdr_agent.user_browser_v2.tools")

MAX_NAVIGATIONS = 150          # the type row's max_steps default; BDR's step cap is the real limit
PAGE_LINKS_MAX = 2000
SETTLE_POLL_SECONDS = 0.5
FINISH_DRAIN_SECONDS = 600.0
PAGE_LINKS_JS = f"() => Array.from(document.links || []).slice(0, {PAGE_LINKS_MAX}).map(a => String(a.href || ''))"
FOCUSED_ELEMENT_JS = "() => { const e = document.activeElement; return (e && e !== document.body && e !== document.documentElement) ? e : null; }"
FINISH_ERROR_KINDS = {"not_found": "not_found", "access_denied": "access_denied", "unsupported": "unsupported",
                      "declined": "cannot_complete", "other": "other"}
CREDENTIAL_REASONS = ("needs_credential", "mfa_step_up", "sso")


# Help ---------------------------------------------------------------------------------


class V2HelpDesk(HelpDesk):
    """Help for personal v2 jobs: UUID message ids, the user help reason
    vocabulary (UserHelpOpenV2Request) and the credential or login attempt
    the request is about."""

    def absorb(self, help_id: UUID, messages: list[Any]) -> None:
        for m in messages:
            self.messages[m.id] = (help_id, m)
            for f in getattr(m, "files", None) or []:
                self.files[f.artifact_id] = (help_id, f)

    def describe(self, messages: list[Any]) -> list[dict[str, Any]]:
        rows = super().describe(messages)
        for row in rows:
            row["id"] = str(row["id"])
        return rows

    async def open(self, reason_code: str, message: str | None, screenshot_artifact_id: UUID | None,
                   *, skip_history: bool = False, login_attempt_id: UUID | None = None,
                   credential_id: UUID | None = None, hold_id: UUID | None = None) -> tuple[Any, HelpThread | None]:
        try:
            req = UserHelpOpenV2Request(reason_code=reason_code, message=(message or None) and message[:1000],
                                        screenshot_artifact_id=screenshot_artifact_id,
                                        login_attempt_id=login_attempt_id, credential_id=credential_id,
                                        hold_id=hold_id)
        except ValueError:
            raise ToolError("help_unavailable", f"help for {reason_code} is not possible now") from None
        resp = await self.ctx.client.help_open_v2(self.ctx.lease, req)
        if resp.suppressed or resp.help_id is None:
            self.ctx.steps.log("note", f"Help for {reason_code} is suppressed for this scope")
            return resp, None
        known = resp.help_id in self.threads
        thread = self.threads.get(resp.help_id) or HelpThread(resp.help_id, reason=reason_code)
        thread.holder, thread.joined = resp.holder, resp.joined_existing
        thread.hold_until = resp.hold_until or (self._now() + timedelta(seconds=self.default_hold_seconds))
        thread.status = "open"
        self.threads[resp.help_id] = thread
        self.current = thread
        if skip_history and resp.joined_existing and not known:
            history = await self.ctx.client.help_messages(self.ctx.lease, resp.help_id)
            self.absorb(resp.help_id, history.messages)
        self.ctx.steps.log("note", f"{'Joined' if resp.joined_existing else 'Opened'} help ({reason_code})")
        return resp, thread

    async def wait(self, thread: HelpThread, accept: Any = None) -> WaitResult:
        self.ctx.set_waiting_help(True)
        try:
            while True:
                resp = await self.ctx.client.help_messages(self.ctx.lease, thread.help_id)
                fresh = [m for m in resp.messages if m.id not in self.messages]
                self.absorb(thread.help_id, fresh)
                thread.status = resp.status
                owner = [m for m in fresh if m.author_kind != "agent"]
                if owner and (accept is None or accept(owner)):
                    return WaitResult(owner, resp.status)
                if resp.status in _CLOSED:
                    return WaitResult(owner, resp.status)
                remaining = ((thread.hold_until or self._now()) - self._now()).total_seconds()
                if remaining <= 0:
                    await self.ctx.client.park(self.ctx.lease)
                    self.ctx.steps.log("note", "Help hold ran out; parked until the owner answers")
                    return WaitResult([], resp.status, parked=True)
                await self._wake_or_sleep(min(self.poll_seconds, remaining))
        finally:
            self.ctx.set_waiting_help(False)


# Executor ---------------------------------------------------------------------------------


class _GuardView:
    """What the shared Executor.run_tool asks of `self.guard`, served by the
    v2 guard: settle (waits for open holds), no ceiling violations (the v2
    guard reports refusals as notices), the per-job navigation counter."""

    def __init__(self, ex: V2Executor):
        self._ex = ex

    async def settle(self) -> None:
        await self._ex.settle_guard()

    def take_violations(self, _who: str) -> list[Any]:
        return []

    @property
    def nav_count(self) -> int:
        return self._ex.nav_count

    @property
    def max_navigations(self) -> int:
        return self._ex.max_navigations

    @property
    def navigations_left(self) -> int:
        return self._ex.max_navigations - self._ex.nav_count

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ex.v2, name)


class V2Executor(Executor):
    def __init__(self, ctx: Any, jb: Any, guard: Any, *, start_url: str | None = None,
                 sleep: Any = asyncio.sleep, max_navigations: int = MAX_NAVIGATIONS):
        super().__init__(ctx, jb, sleep=sleep)
        self.v2 = guard
        self._view = _GuardView(self)
        self.start_url = start_url
        self.max_navigations = max_navigations
        self.nav_count = 0
        self.identity: Any = None
        self.downloads: Any = None
        self._tab_by_id: dict[int, Any] = {}
        self._id_by_page: dict[int, int] = {}
        # Elements the worker filled from a credential, compared by identity
        # (a page that strips their data-bdr-secret mark is still refused).
        self.secret_handles: list[Any] = []

    def remember_secret_handle(self, handle: Any) -> None:
        if handle is not None and all(h is not handle for h in self.secret_handles):
            self.secret_handles.append(handle)
            del self.secret_handles[:-20]

    @property
    def guard(self) -> Any:
        return self._view

    # Holds and notices -----------------------------------------------------------------

    async def settle_guard(self) -> None:
        """Wait for the guard's in-flight work, including any hold the owner
        is deciding. While a hold is open the job counts as waiting."""
        task = asyncio.ensure_future(self.v2.settle())
        waiting = False
        try:
            while True:
                held = self.v2.holds_open > 0
                if held != waiting:
                    waiting = held
                    self.ctx.set_waiting_help(held)
                if task.done() and not held:
                    break
                if task.done():
                    await self.v2.wait_holds()
                    continue
                await asyncio.wait({task}, timeout=SETTLE_POLL_SECONDS)
            task.result()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if waiting:
                self.ctx.set_waiting_help(False)

    def take_notices(self) -> list[str]:
        out: list[str] = list(self.v2.take_notices())
        for source in (self.identity, self.downloads):
            if source is not None:
                out += source.take_notices()
        seen: set[str] = set()
        return [n for n in out if not (n in seen or seen.add(n))]

    async def run_tool(self, call: Any, *, turn_no: int | None = None, actor: str = "model") -> ToolOutcome:
        outcome = await super().run_tool(call, turn_no=turn_no, actor=actor)
        if outcome.terminal is None and self.identity is not None:
            terminal = await self.identity.observe()
            if terminal is not None:
                outcome.terminal = terminal
        notices = self.take_notices()
        if notices:
            notices = [self.ctx.secrets.scrub_text(n) for n in notices]
            self.step("note", "Notices for the model", result={"notices": notices[:10]}, turn_no=turn_no)
            outcome.content = self._with_notices(outcome.content, notices)
        if self.ctx.secrets.contains(outcome.content):
            self.step("security.secret_scrubbed", f"A secret appeared in the {call.tool} result; job stopped",
                      ok=False, tool_name=call.tool, turn_no=turn_no)
            outcome.content = _json({"error": "secret_leak_blocked"})
            outcome.ok = False
            outcome.terminal = outcome.terminal or Finish("failed", "A secret appeared in a tool result",
                                                          error_kind="secret_leak_blocked",
                                                          error="secret value in an observation")
        return outcome

    @staticmethod
    def _with_notices(content: str, notices: list[str]) -> str:
        try:
            data = json.loads(content)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = {"result": content}
        data["notices"] = notices
        return clip_content(_json(data))

    # Screenshots -----------------------------------------------------------------------

    def _no_screenshot_here(self, page: Any) -> bool:
        url = getattr(page, "url", "") or ""
        origin = url_origin(url)
        if origin is None:
            return False
        if is_identity_provider_host(urlsplit(url).hostname or ""):
            return True
        return any(origin in c.login_origins for c in self.v2.policy.credentials)

    async def screenshot(self, page: Any = None, *, label: str = "shot") -> UUID | None:
        target = page or self.jb.page
        if target is not None and self._no_screenshot_here(target):
            return None
        return await super().screenshot(page, label=label)

    # Tabs ------------------------------------------------------------------------------

    def _tab_id(self, page: Any) -> int:
        key = id(page)
        if key not in self._id_by_page:
            n = len(self._tab_by_id)
            self._id_by_page[key] = n
            self._tab_by_id[n] = page
        return self._id_by_page[key]

    async def tabs(self) -> list[dict[str, Any]]:
        out = []
        for page in self.jb.pages():
            if page.is_closed():
                continue
            n = self._tab_id(page)
            title = ""
            with contextlib.suppress(Exception):
                title = (await page.title())[:120]
            out.append({"tab_id": f"tab_{n}", "url": strip_query(page.url) or page.url, "title": title,
                        "active": page is self.jb.page})
        return out

    async def switch_tab(self, args: Any) -> ToolReturn:
        for page in self.jb.pages():
            self._tab_id(page)
        page = self._tab_by_id.get(int(args.tab_id.removeprefix("tab_")))
        if page is None or page.is_closed():
            raise ToolError("unknown_tab", "that tab is not open; read_page lists the tabs")
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        self.jb.page = page
        self.snapshot = None
        return ToolReturn({**await self._page_state(page), "tab_id": args.tab_id})

    # Shared tools with the user rules ------------------------------------------------

    async def _page_links(self, page: Any) -> list[str]:
        try:
            links = await page.evaluate(PAGE_LINKS_JS)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            return []
        return [str(x) for x in links if isinstance(x, str)] if isinstance(links, list) else []

    async def _navigate(self, args: Any) -> ToolReturn:
        page = await self.page()
        links = await self._page_links(page) if page.url not in ("about:blank", "") else []
        verdict = self.v2.prepare_navigation(page, args.url, page_links=links, start_url=self.start_url)
        if not verdict:
            raise ToolError("navigation_refused", verdict.reason)
        if self.nav_count >= self.max_navigations:
            raise ToolError("navigation_cap", f"at most {self.max_navigations} page loads per task")
        self.nav_count += 1
        status = None
        try:
            resp = await page.goto(args.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = resp.status if resp is not None else None
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            if self.v2.holds_open or getattr(self.v2, "_tasks", None):
                # A site hold: the owner decides while goto timed out. Wait,
                # then report wherever the page is now (the notice says why).
                await self.settle_guard()
                # Playwright 1.64 keeps the held navigation alive past goto's
                # timeout: once approved, the guard delivers it. Let it commit
                # before deciding to open the URL again (a second goto would
                # abort it: net::ERR_ABORTED).
                with contextlib.suppress(Exception):
                    await page.wait_for_url(args.url, wait_until="commit", timeout=5_000)
                await _settle_load(page)
                if (self.v2.in_scope(args.url) and url_origin(page.url) != url_origin(args.url)
                        and self.v2.prepare_navigation(page, args.url, page_links=links,
                                                       start_url=self.start_url)):
                    # Approved after goto gave up waiting: Chromium may have
                    # dropped the held navigation, so open the URL once more
                    # (now in scope, so no second hold).
                    try:
                        await page.goto(args.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    except Exception as again:
                        if _is_browser_dead(again):
                            raise
                        raise ToolError("navigation_failed", _short(again)) from None
                    await _settle_load(page)
                return ToolReturn(await self._page_state(page))
            raise ToolError("navigation_failed", _short(exc)) from None
        await _settle_load(page)
        return ToolReturn({**await self._page_state(page), "status": status})

    async def _go_back(self, args: Any) -> ToolReturn:
        page = await self.page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            if not (self.v2.holds_open or getattr(self.v2, "_tasks", None)):
                raise ToolError("navigation_failed", _short(exc)) from None
            await self.settle_guard()
        return ToolReturn(await self._page_state(page))

    async def _read_page(self, args: Any) -> ToolReturn:
        ret = await super()._read_page(args)
        tabs = await self.tabs()
        if len(tabs) > 1:
            ret.content["tabs"] = tabs
        return ret

    async def _blocked_reason(self, page: Any, element: Any, *, enter: bool = False) -> str | None:
        out = await (self.v2.check_click(page, element, action="enter") if enter
                     else self.v2.check_click(page, element, action="click"))
        if out.proceed:
            return None
        return out.notice or "not performed"

    async def _click(self, args: Any) -> ToolReturn:
        loc = await self._locator(args.ref)
        page = await self.page()
        blocked = await self._blocked_reason(page, loc)
        if blocked is not None:
            return ToolReturn({**await self._page_state(page), "error": "not_clicked", "detail": blocked}, ok=False)
        try:
            await loc.click(timeout=10_000)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("click_failed", _short(exc)) from None
        await _settle_load(page)
        return ToolReturn(await self._page_state(page))

    async def _refuse_secret_field(self, ref: str, loc: Any) -> None:
        # The snapshot's own `secret` flag uses the org rule (SECRET_INPUT_RE,
        # which also matches "Zip code"); the user type decides with its own
        # rule below, plus the fields the worker filled from a credential.
        selector = self.snapshot.selector(ref) if self.snapshot else None
        if selector and selector in self.secret_fields:
            raise ToolError("refused", "this looks like a password or code field; use fill_credential")
        for handle in list(self.secret_handles):
            try:
                same = await loc.evaluate("(el, other) => el === other", handle)
            except Exception:
                continue           # another frame or a gone document: not this element
            if same:
                raise ToolError("refused", "this field was filled from a saved sign-in; use fill_credential")
        if await loc.evaluate(f"(el) => el.hasAttribute('{SECRET_FIELD_ATTR}')"):
            raise ToolError("refused", "this field was filled from a saved sign-in; use fill_credential")
        probe = await loc.evaluate(_FIELD_PROBE_JS)
        if (probe.get("type") == "password" or probe.get("autocomplete") == "one-time-code"
                or secret_input_re(self.agent_type).search(probe.get("words") or "")):
            raise ToolError("refused", "this looks like a password or code field; use fill_credential")

    async def _interaction_blocked(self, page: Any, element: Any) -> str | None:
        out = await self.v2.check_interaction(page, element)
        return None if out.proceed else (out.notice or "not allowed now")

    async def _type_text(self, args: Any) -> ToolReturn:
        page = await self.page()
        loc = await self._locator(args.ref)
        await self._refuse_secret_field(args.ref, loc)
        blocked = await self._interaction_blocked(page, loc)
        if blocked is not None:
            raise ToolError("refused", blocked)
        if args.text:
            self.v2.typed.add(args.text)
        try:
            await loc.fill(args.text, timeout=10_000)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("type_failed", _short(exc)) from None
        content: dict[str, Any] = {"typed": len(args.text)}
        if args.submit:
            blocked = await self._blocked_reason(page, loc, enter=True)
            if blocked is not None:
                content.update(error="not_submitted", detail=blocked)
                return ToolReturn({**await self._page_state(page), **content}, ok=False)
            try:
                await loc.press("Enter", timeout=10_000)
            except Exception as exc:
                if _is_browser_dead(exc):
                    raise
                raise ToolError("type_failed", _short(exc)) from None
            await _settle_load(page)
        return ToolReturn({**await self._page_state(page), **content})

    async def _select_option(self, args: Any) -> ToolReturn:
        page = await self.page()
        loc = await self._locator(args.ref)
        blocked = await self._interaction_blocked(page, loc)
        if blocked is not None:
            raise ToolError("refused", blocked)
        if args.value:
            self.v2.typed.add(args.value)
        try:
            try:
                await loc.select_option(value=args.value, timeout=10_000)
            except Exception:
                await loc.select_option(label=args.value, timeout=10_000)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("select_failed", _short(exc)) from None
        await _settle_load(page)
        return ToolReturn(await self._page_state(page))

    async def _press_key(self, args: Any) -> ToolReturn:
        page = await self.page()
        element = None
        try:
            handle = await page.evaluate_handle(FOCUSED_ELEMENT_JS)
            element = handle.as_element()
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
        if element is not None:
            if args.key == "Enter":
                blocked = await self._blocked_reason(page, element, enter=True)
            else:
                blocked = await self._interaction_blocked(page, element)
            if blocked is not None:
                return ToolReturn({**await self._page_state(page), "error": "not_pressed", "detail": blocked},
                                  ok=False)
        elif self.v2.login_open and args.key == "Enter":
            return ToolReturn({**await self._page_state(page), "error": "not_pressed",
                               "detail": "A sign-in is in progress: only the sign-in form can be used now."}, ok=False)
        await page.keyboard.press(args.key)
        await _settle_load(page)
        return ToolReturn(await self._page_state(page))


# New tools ----------------------------------------------------------------------------------


class V2Tools:
    """switch_tab, fill_credential, user_request_help and user_finish."""

    def __init__(self, executor: V2Executor, desk: V2HelpDesk, identity: Any, downloads: Any, *,
                 finish_drain_seconds: float = FINISH_DRAIN_SECONDS):
        self.ex = executor
        self.desk = desk
        self.identity = identity
        self.downloads = downloads
        self.finish_drain_seconds = finish_drain_seconds

    def handlers(self) -> dict[str, Any]:
        return {"switch_tab": self.ex.switch_tab, "fill_credential": self.fill_credential,
                "user_request_help": self.user_request_help, "user_finish": self.user_finish}

    async def fill_credential(self, args: Any) -> ToolReturn:
        return await self.identity.fill(args.ref, args.field)

    def _credential(self) -> Any:
        policy = self.ex.v2.policy
        page = self.ex.jb.page
        url = getattr(page, "url", "") or ""
        cred = self.identity.credential_for_origin(url_origin(url)) if url else None
        if cred is None and url:
            site = self.ex.v2.site_for(url)
            if site is not None:
                cred = next((c for c in policy.credentials if c.site_key == site.site_key), None)
        if cred is None and len(policy.credentials) == 1:
            cred = policy.credentials[0]
        return cred

    async def user_request_help(self, args: Any) -> ToolReturn:
        reason = args.reason_code
        cred = self._credential() if reason in CREDENTIAL_REASONS else None
        if reason == "mfa_step_up" and cred is None:
            raise ToolError("help_unavailable", "no saved sign-in in this task to ask a code for")
        attempt = self.identity.attempt if self.identity is not None else None
        attempt_id = attempt.attempt_id if attempt is not None and not attempt.done else None
        shot = await self.ex.screenshot(label="help")
        _resp, thread = await self.desk.open(reason, args.message, shot,
                                             credential_id=cred.credential_id if cred is not None else None,
                                             login_attempt_id=attempt_id)
        if thread is None:
            return ToolReturn({"suppressed": True}, ok=False, screenshot=False, terminal=Finish(
                "failed", "Help for this task was declined recently", error_kind="access_denied_suppressed",
                error="help suppressed for this scope"))
        result = await self.desk.wait(thread)
        if result.parked:
            return ToolReturn({"parked": True}, screenshot=False, terminal=HandedOff("Parked waiting for help"))
        ids = [m.id for m in result.messages]
        await self.desk.consume_seen(ids)
        content: dict[str, Any] = {"help_id": str(thread.help_id), "status": result.status,
                                   "messages": self.desk.describe(result.messages)}
        if reason == "mfa_step_up" and any(m.secret_ref is not None for m in result.messages):
            content["note"] = ("The owner sent a code. This worker types codes only inside a sign-in; "
                               "sign in again with fill_credential if the site asks.")
        return ToolReturn(content, ledger={"help_id": str(thread.help_id), "status": result.status,
                                           "message_ids": [str(i) for i in ids]},
                          helper_message_ids=ids)

    async def user_finish(self, args: Any) -> ToolReturn:
        downloads: list[dict[str, Any]] = []
        if self.downloads is not None:
            await self.downloads.drain(self.finish_drain_seconds)
            downloads = self.downloads.summary()
        await self.ex.settle_guard()
        if self.identity is not None:
            await self.identity.abandon()
        kept: list[dict[str, Any]] = []
        dropped = 0
        for fact in args.facts or []:
            if self.ex.v2.in_scope(fact.source_url) and url_origin(fact.source_url):
                kept.append({"text": fact.text, "source": trace_url(fact.source_url)})
            else:
                dropped += 1
        ledger = {"outcome": args.outcome, "error_kind": args.error_kind, "facts": kept, "facts_dropped": dropped,
                  "downloads": downloads, "side_effects": bool(getattr(self.ex.v2, "side_effects", False))}
        summary = args.result[:FINISH_SUMMARY_MAX]
        if args.outcome == "completed":
            terminal = Finish("succeeded", summary, outcome="nothing_to_do")
        else:
            kind = FINISH_ERROR_KINDS.get(args.error_kind or "other", "other")
            terminal = Finish("failed", summary, error_kind=kind,
                              error="declined" if args.error_kind == "declined" else None)
        return ToolReturn({"finished": True, "facts_kept": len(kept), "facts_dropped": dropped},
                          ledger=ledger, terminal=terminal, screenshot=False)


__all__ = ["V2Executor", "V2HelpDesk", "V2Tools"]

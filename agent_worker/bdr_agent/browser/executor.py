"""The single choke point for every tool call (spec 4.1, 4.2, D12, D13).

`Executor.run_tool` is the only way a tool runs, whoever asked for it (the
model through /turns; a helper-actor call in the future live-view seam). For
each call it:

  1. re-validates the call against the contract (protocol.validate_tool_call:
     per-type tool set and args schema), the second policy check after BDR's;
  2. runs the handler: the browser tools below, or a site/help/finish tool a
     type registers (`tools=`);
  3. re-checks the model page against the ceiling (a redirect that left it
     sends it to about:blank) and the per-job navigation cap;
  4. takes a masked JPEG screenshot (q70) of the model page and uploads it;
  5. scrubs live secrets from the observation; a hit fails the job with
     `secret_leak_blocked` after a `security.secret_scrubbed` step;
  6. writes one ledger step `tool.<name>`: redacted args, compact result, URL
     before and after (query stripped), duration, screenshot id, turn number.

The model never gets JavaScript evaluation, uploads, downloads, new tabs,
console or network reads; `type_text` refuses secret-looking fields and any
page on a login host; secrets are only ever typed by the login routine,
which marks those fields (`mark_secret_field`) so they are masked in
screenshots and hidden in snapshots.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel

from ..client import BdrApiError, BdrUnreachable, LeaseLost, StaleBoot, WorkerRevoked, WorkerUnauthorized
from ..protocol import (
    ARTIFACT_RULES,
    SECRET_INPUT_RE,
    TOOL_RESULT_MAX_CHARS,
    USER_BROWSER_TYPE,
    USER_SECRET_INPUT_RE,
    ToolCall,
    ToolCallInvalid,
    validate_tool_call,
)
from ..redact import strip_query
from ..slot import Finish, JobResult
from .guard import check_navigation, host_in
from .snapshot import DEFAULT_MAX_CHARS, Snapshot, take_snapshot

log = logging.getLogger("bdr_agent.browser.executor")

SCREENSHOT_QUALITY = 70
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
_SECRET_WORDS = ("otp", "passcode", "mfa", "verification", "2fa", "code", "password")
# Masked in every screenshot (spec 4.2): password inputs, one-time-code inputs
# and anything whose name or id looks like a secret.
MASK_SELECTORS: tuple[str, ...] = (
    'input[type="password" i]',
    'input[autocomplete="one-time-code" i]',
    *(f'input[name*="{w}" i]' for w in _SECRET_WORDS),
    *(f'input[id*="{w}" i]' for w in _SECRET_WORDS),
)

_FIELD_PROBE_JS = """
(el) => {
  const label = (() => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l.textContent; }
    const w = el.closest('label'); return w ? w.textContent : '';
  })();
  return {tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
          autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
          words: [el.getAttribute('name') || '', el.id || '', el.getAttribute('placeholder') || '', label || ''].join(' ')};
}
"""


class ToolError(Exception):
    """A tool failed in a way the model should hear about (it is an
    observation, not a crash)."""

    def __init__(self, code: str, detail: str | None = None, **extra: Any):
        super().__init__(f"{code}: {detail or ''}")
        self.code = code
        self.detail = detail
        self.extra = extra

    def content(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code}
        if self.detail:
            out["detail"] = self.detail
        out.update(self.extra)
        return out


@dataclass
class ToolReturn:
    """What a handler hands back to the executor."""
    content: dict[str, Any]
    ok: bool = True
    terminal: JobResult | None = None
    ledger: dict[str, Any] | None = None          # compact result for the step row (default: content)
    screenshot: bool = True
    helper_message_ids: list[int] = field(default_factory=list)


@dataclass
class ToolOutcome:
    """What the agent loop gets back for one call."""
    call_id: str
    tool: str
    ok: bool
    content: str                                   # JSON text, <= TOOL_RESULT_MAX_CHARS
    terminal: JobResult | None = None
    screenshot_artifact_id: UUID | None = None
    helper_message_ids: list[int] = field(default_factory=list)


Handler = Callable[[BaseModel], Awaitable[ToolReturn]]

# Exceptions that end the job's processing and must propagate to the slot.
_FATAL = (LeaseLost, StaleBoot, WorkerRevoked, WorkerUnauthorized)


def secret_input_re(agent_type: str) -> Any:
    """The secret-field rule type_text applies. user_browser (v2) uses its own
    narrower rule (ordinary "Zip code" and "Cost code" fields are typable); every
    other type keeps SECRET_INPUT_RE unchanged."""
    return USER_SECRET_INPUT_RE if agent_type == USER_BROWSER_TYPE else SECRET_INPUT_RE


def _json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def clip_content(text: str, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 15] + "...[truncated]"


class Executor:
    def __init__(self, ctx: Any, jb: Any, *, tools: dict[str, Handler] | None = None,
                 screenshots: bool = True, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.ctx = ctx
        self.jb = jb
        self.agent_type: str = ctx.agent_type
        self.extra_tools: dict[str, Handler] = dict(tools or {})
        self.screenshots = screenshots
        self._sleep = sleep
        self.snapshot: Snapshot | None = None
        self.secret_fields: list[str] = []       # selectors filled from a secret (login page)
        self._shot_n = 0
        self._builtin: dict[str, Handler] = {
            "navigate": self._navigate, "read_page": self._read_page, "find_text": self._find_text,
            "click": self._click, "type_text": self._type_text, "select_option": self._select_option,
            "press_key": self._press_key, "scroll": self._scroll, "go_back": self._go_back,
            "wait_for": self._wait_for,
        }

    # Helpers available to tool handlers -------------------------------------------

    @property
    def guard(self) -> Any:
        return self.jb.guard

    async def page(self) -> Any:
        return await self.jb.model_page()

    def mark_secret_field(self, selector: str) -> None:
        if selector not in self.secret_fields:
            self.secret_fields.append(selector)

    def register_secret(self, value: str | None, *, tripwire: bool = True) -> None:
        """A job secret, scrubbed everywhere. `tripwire=False` for values that
        are identifying but not credentials (they may legitimately appear in
        page text): scrubbed from steps, but never fail the job."""
        self.ctx.secrets.add(value, owner=str(self.ctx.job_id), tripwire=tripwire)

    def step(self, kind: str, summary: str, **fields: Any) -> str:
        return self.ctx.steps.log(kind, summary, **fields)

    async def screenshot(self, page: Any = None, *, label: str = "shot") -> UUID | None:
        """Masked JPEG of `page` (default: the model page), uploaded as a
        screenshot artifact. None for a blank page or when it cannot be taken."""
        if not self.screenshots:
            return None
        page = page or self.jb.page
        if page is None or page.is_closed() or page.url in ("about:blank", ""):
            return None
        masks = [page.locator(s) for s in (*MASK_SELECTORS, *self.secret_fields)]
        data = None
        for quality in (SCREENSHOT_QUALITY, 40):
            try:
                data = await page.screenshot(type="jpeg", quality=quality, mask=masks, full_page=False,
                                             animations="disabled", caret="hide", timeout=15_000)
            except Exception as exc:
                log.debug("screenshot failed: %s", exc)
                return None
            if len(data) <= ARTIFACT_RULES["screenshot"]["max_bytes"]:
                break
            data = None
        if data is None:
            return None
        self._shot_n += 1
        try:
            resp = await self.ctx.client.upload_artifact(self.ctx.lease, "screenshot",
                                                          f"{label}-{self._shot_n}.jpg", data, "image/jpeg")
        except _FATAL:
            raise
        except (BdrApiError, BdrUnreachable, ValueError) as exc:
            log.warning("screenshot upload failed: %s", exc)
            return None
        return resp.artifact_id

    # The choke point ------------------------------------------------------------

    async def run_tool(self, call: ToolCall, *, turn_no: int | None = None, actor: str = "model") -> ToolOutcome:
        started = time.monotonic()
        page = self.jb.page
        url_before = page.url if page is not None and not page.is_closed() else None
        ret: ToolReturn
        try:
            args = validate_tool_call(self.agent_type, call.tool, call.args)
        except ToolCallInvalid as exc:
            ret = ToolReturn({"error": "invalid_call", "detail": exc.reason}, ok=False, screenshot=False)
            args = None
        if args is not None:
            handler = self._builtin.get(call.tool) or self.extra_tools.get(call.tool)
            if handler is None:
                ret = ToolReturn({"error": "tool_unavailable", "detail": call.tool}, ok=False, screenshot=False)
            else:
                try:
                    ret = await handler(args)
                except ToolError as exc:
                    ret = ToolReturn(exc.content(), ok=False)
                except ToolCallInvalid as exc:
                    ret = ToolReturn({"error": "invalid_call", "detail": exc.reason}, ok=False, screenshot=False)
                except _FATAL:
                    raise
                except BdrUnreachable:
                    raise
                except BdrApiError as exc:
                    ret = ToolReturn({"error": "bdr_refused", "detail": exc.code or str(exc.status)}, ok=False)
                except Exception as exc:
                    if _is_browser_dead(exc):
                        raise
                    ret = ToolReturn({"error": "tool_failed", "detail": _short(exc)}, ok=False)

        await self.guard.settle()
        violations = self.guard.take_violations("model")
        if violations and ret.terminal is None:
            ret.ok = False
            ret.content = {**ret.content, "error": ret.content.get("error") or "navigation_blocked",
                           "blocked": [{"host": urlsplit(v.url).hostname, "reason": v.reason} for v in violations[-3:]]}
        if self.guard.nav_count > self.guard.max_navigations and ret.terminal is None:
            await self._blank()
            ret.ok = False
            ret.content = {"error": "navigation_cap", "detail": f"more than {self.guard.max_navigations} page loads"}

        shot_id = None
        if ret.screenshot and ret.terminal is None:
            shot_id = await self.screenshot(label=f"t{turn_no or 0}")

        content_text = _json(ret.content)
        if self.ctx.secrets.contains(content_text):
            # A live secret reached an observation: never send it, fail the job.
            self.step("security.secret_scrubbed", f"A secret appeared in the {call.tool} result; job stopped",
                      ok=False, tool_name=call.tool, turn_no=turn_no)
            content_text = _json({"error": "secret_leak_blocked"})
            ret.terminal = ret.terminal or Finish("failed", "A secret appeared in a tool result",
                                                  error_kind="secret_leak_blocked",
                                                  error="secret value in an observation")
            ret.ok = False
        content_text = clip_content(content_text)

        page = self.jb.page
        url_after = page.url if page is not None and not page.is_closed() else None
        ledger = ret.ledger if ret.ledger is not None else ret.content
        self.step(f"tool.{call.tool}", f"{call.tool}: {'ok' if ret.ok else ret.content.get('error', 'failed')}",
                  actor=actor, tool_name=call.tool, args=_redacted_args(call.tool, call.args),
                  result=ledger, url_before=_step_url(url_before), url_after=_step_url(url_after),
                  screenshot_artifact_id=shot_id, turn_no=turn_no, ok=ret.ok,
                  duration_ms=int((time.monotonic() - started) * 1000))
        return ToolOutcome(call_id=call.call_id, tool=call.tool, ok=ret.ok, content=content_text,
                           terminal=ret.terminal, screenshot_artifact_id=shot_id,
                           helper_message_ids=list(ret.helper_message_ids))

    async def _blank(self) -> None:
        page = self.jb.page
        if page is not None and not page.is_closed():
            try:
                await page.goto("about:blank")
            except Exception as exc:
                log.debug("about:blank failed: %s", exc)

    async def _page_state(self, page: Any) -> dict[str, Any]:
        try:
            title = (await page.title())[:300]
        except Exception:
            title = ""
        return {"url": strip_query(page.url) or page.url, "title": title}

    # Browser tools ----------------------------------------------------------------

    async def _navigate(self, args: Any) -> ToolReturn:
        page = await self.page()
        verdict = check_navigation(self.guard.ceiling, args.url)
        if not verdict:
            self.guard._record_block("NAVIGATE", args.url, verdict.reason or "refused")
            await self._blank()
            raise ToolError("navigation_refused", verdict.reason)
        if self.guard.navigations_left <= 0:
            raise ToolError("navigation_cap", f"at most {self.guard.max_navigations} page loads per job")
        status = None
        try:
            resp = await page.goto(args.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = resp.status if resp is not None else None
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("navigation_failed", _short(exc)) from None
        await _settle_load(page)
        return ToolReturn({**await self._page_state(page), "status": status})

    async def _snapshot(self) -> Snapshot:
        page = await self.page()
        if page.url in ("about:blank", ""):
            raise ToolError("blank_page", "nothing is open; use navigate or a site tool first")
        self.snapshot = await take_snapshot(page, secret_selectors=self.secret_fields)
        return self.snapshot

    async def _read_page(self, args: Any) -> ToolReturn:
        snap = await self._snapshot()
        text, cut = snap.text(args.max_chars or DEFAULT_MAX_CHARS)
        return ToolReturn({"url": strip_query(snap.url), "title": snap.title, "text": text, "truncated": cut,
                           "refs": len(snap.refs)},
                          ledger={"url": strip_query(snap.url), "chars": len(text), "refs": len(snap.refs),
                                  "truncated": cut})

    async def _find_text(self, args: Any) -> ToolReturn:
        snap = await self._snapshot()
        matches = snap.find(args.query)
        return ToolReturn({"query": args.query, "count": len(matches), "matches": matches},
                          ledger={"query": args.query, "count": len(matches)})

    async def _locator(self, ref: str) -> Any:
        if self.snapshot is None:
            raise ToolError("stale_ref", "call read_page first")
        sel = self.snapshot.selector(ref)
        page = await self.page()
        if sel is None or page.url != self.snapshot.url:
            raise ToolError("stale_ref", "the page changed; call read_page again")
        loc = page.locator(sel)
        if await loc.count() != 1:
            raise ToolError("stale_ref", "the page changed; call read_page again")
        return loc

    async def _click(self, args: Any) -> ToolReturn:
        loc = await self._locator(args.ref)
        page = await self.page()
        try:
            await loc.click(timeout=ACTION_TIMEOUT_MS)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("click_failed", _short(exc)) from None
        await _settle_load(page)
        return ToolReturn(await self._page_state(page))

    async def _type_text(self, args: Any) -> ToolReturn:
        page = await self.page()
        host = (urlsplit(page.url).hostname or "").lower()
        login_page = getattr(self.jb, "login_page", None)
        if login_page is not None and page is login_page:
            raise ToolError("refused", "typing on the login page is not allowed")
        if host_in(host, self.guard.ceiling.login_hosts):
            raise ToolError("refused", "typing on a login host is not allowed")
        info = self.snapshot.refs.get(args.ref) if self.snapshot else None
        if info is not None and info.secret:
            raise ToolError("refused", "this looks like a password or code field")
        loc = await self._locator(args.ref)
        probe = await loc.evaluate(_FIELD_PROBE_JS)
        if (probe.get("type") == "password" or probe.get("autocomplete") == "one-time-code"
                or secret_input_re(self.agent_type).search(probe.get("words") or "")):
            raise ToolError("refused", "this looks like a password or code field")
        try:
            await loc.fill(args.text, timeout=ACTION_TIMEOUT_MS)
            if args.submit:
                await loc.press("Enter", timeout=ACTION_TIMEOUT_MS)
                await _settle_load(page)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("type_failed", _short(exc)) from None
        return ToolReturn({**await self._page_state(page), "typed": len(args.text)})

    async def _select_option(self, args: Any) -> ToolReturn:
        loc = await self._locator(args.ref)
        page = await self.page()
        try:
            try:
                await loc.select_option(value=args.value, timeout=ACTION_TIMEOUT_MS)
            except Exception:
                await loc.select_option(label=args.value, timeout=ACTION_TIMEOUT_MS)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("select_failed", _short(exc)) from None
        await _settle_load(page)
        return ToolReturn(await self._page_state(page))

    async def _press_key(self, args: Any) -> ToolReturn:
        page = await self.page()
        await page.keyboard.press(args.key)
        await _settle_load(page)
        return ToolReturn(await self._page_state(page))

    async def _scroll(self, args: Any) -> ToolReturn:
        page = await self.page()
        dy = 400 * args.amount * (1 if args.direction == "down" else -1)
        await page.mouse.wheel(0, dy)
        await self._sleep(0.2)
        return ToolReturn(await self._page_state(page))

    async def _go_back(self, args: Any) -> ToolReturn:
        page = await self.page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("navigation_failed", _short(exc)) from None
        return ToolReturn(await self._page_state(page))

    async def _wait_for(self, args: Any) -> ToolReturn:
        page = await self.page()
        seconds = args.seconds or (10 if args.text else 2)
        if not args.text:
            await self._sleep(seconds)
            return ToolReturn({**await self._page_state(page), "waited_s": seconds})
        try:
            await page.get_by_text(args.text).first.wait_for(state="visible", timeout=seconds * 1000)
            found = True
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            found = False
        return ToolReturn({**await self._page_state(page), "found": found}, ok=found)


# Small helpers ---------------------------------------------------------------------


async def _settle_load(page: Any, timeout_ms: int = 5_000) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass


def _short(exc: BaseException, limit: int = 300) -> str:
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:limit]


def _is_browser_dead(exc: BaseException) -> bool:
    """Target or browser closed: a context crash, which the slot requeues."""
    name = type(exc).__name__
    text = str(exc)
    return name == "TargetClosedError" or "Target page, context or browser has been closed" in text \
        or "Browser has been closed" in text


def _step_url(url: str | None) -> str | None:
    if not url or url == "about:blank":
        return url
    return strip_query(url)


def _redacted_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Args as the ledger stores them: URL queries stripped; the StepLog
    scrubs live secrets on top of this."""
    out = dict(args or {})
    if tool == "navigate" and isinstance(out.get("url"), str):
        out["url"] = strip_query(out["url"])
    return out

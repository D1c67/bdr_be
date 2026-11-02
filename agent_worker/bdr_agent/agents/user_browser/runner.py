"""Personal GET-only reader. No site uploads, record captures, forms or organization identity."""
import asyncio
import inspect
from dataclasses import replace

from ...agent.help import HelpTools
from ...agent.loop import run_loop
from ...browser.executor import _FATAL, Executor, ToolError, ToolReturn, _is_browser_dead
from ...browser.snapshot import take_snapshot
from ...client import BdrApiError, BdrUnreachable
from ...protocol import ToolCallInvalid
from ...redact import strip_query
from ...slot import Finish
from .help import UserHelpDesk
from .identity import UserIdentity
from .progress import (
    BLANK_PAGE_LIMIT,
    DIAGNOSTIC_TIMEOUT_S,
    NOT_RENDERING_DETAIL,
    RENDER_WAIT_SECONDS,
    RENDER_WAITS,
    SAME_PAGE_RELOAD_LIMIT,
    SNAPSHOT_TIMEOUT_S,
    SUMMARY,
    LoginGate,
    Observation,
    ProgressTracker,
    Reading,
    UserAgentLoop,
    read_snapshot,
)

_SNAPSHOT = object()


def _personal(handler):
    """A personal tool handler: an unexpected exception becomes a fixed
    tool_failed code (its text can hold a URL with its query). The page title
    is dropped from the step ledger by UserExecutor.step."""

    async def run(args):
        try:
            ret = await handler(args)
        except (ToolError, ToolCallInvalid, BdrApiError, BdrUnreachable, *_FATAL):
            raise
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            raise ToolError("tool_failed", type(exc).__name__) from None
        return ret

    return run


class _Handlers(dict):
    def get(self, key, default=None):
        handler = super().get(key)
        return default if handler is None else _personal(handler)


class UserExecutor(Executor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._builtin = _Handlers(self._builtin)
        self.extra_tools = _Handlers(self.extra_tools)
        self.progress = ProgressTracker()
        self._obs: Observation | None = None
        self._turn_no: int | None = None
        self._pending: str | None = None

    def step(self, kind, summary, **fields):
        # The title stays out of every tool.* step. Done here, after run_tool's
        # guard-violation and navigation-cap rewrites, so the recorded result
        # keeps their error and blocked fields. The model still sees the title.
        result = fields.get("result")
        if kind.startswith("tool.") and isinstance(result, dict) and "title" in result:
            fields["result"] = {k: v for k, v in result.items() if k != "title"}
        return super().step(kind, summary, **fields)

    # No-progress bookkeeping ------------------------------------------------------

    def observe(self, obs: Observation) -> None:
        self._obs = obs

    def progress_terminal(self, reason: str) -> Finish:
        return self.progress.terminal(reason, self.step, turn_no=self._turn_no)

    def defer_terminal(self, reason: str) -> None:
        """A handler's no-progress stop, applied by run_tool after the base checks."""
        self._pending = reason

    async def run_tool(self, call, *, turn_no=None, actor="model"):
        self._obs, self._turn_no, self._pending = None, turn_no, None
        outcome = await super().run_tool(call, turn_no=turn_no, actor=actor)
        # Secret leak, finish, login, MFA and help terminals keep precedence.
        if outcome.terminal is not None:
            return outcome
        if self._pending is not None:
            return replace(outcome, terminal=self.progress_terminal(self._pending))
        if call.tool in ("request_help", "reply_help"):
            self.progress.no_progress = 0
            return outcome
        obs = self._obs if outcome.ok and self._obs is not None else Observation()
        reason = self.progress.note(obs)
        if reason is None:
            return outcome
        return replace(outcome, terminal=self.progress_terminal(reason))

    # Render checks ------------------------------------------------------------------

    @property
    def diagnostics(self):
        return getattr(self.guard, "diagnostics", None)

    async def _diagnostic(self, name, *args):
        """Call an optional RenderDiagnostics method. Diagnostics never fail a tool."""
        method = getattr(self.diagnostics, name, None)
        if not callable(method):
            return None
        try:
            value = method(*args)
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, DIAGNOSTIC_TIMEOUT_S)
            return value
        except Exception:
            return None

    async def emit_diagnostics(self, trigger, page=None):
        return await self._diagnostic("emit", page if page is not None else self.jb.page, trigger)

    async def page_code_blocked(self) -> bool:
        """The guard refused part of the page's own code (a remote entry
        script) and the page shows Procore's not-found screen. A fresh probe
        when the page is open, otherwise the last probe's marker."""
        if self.diagnostics is None:
            return False
        if await self._diagnostic("remote_entry_refused_here") is False:
            return False   # nothing refused on this document: no extra probe
        probe = await self._diagnostic("probe", getattr(self.jb, "page", None))
        return await self._diagnostic("page_code_blocked", probe) is True

    async def _measure(self, page, snap=_SNAPSHOT) -> Reading:
        if snap is _SNAPSHOT:
            snap = None
            try:
                snap = await asyncio.wait_for(take_snapshot(page, secret_selectors=self.secret_fields),
                                              SNAPSHOT_TIMEOUT_S)
            except Exception as exc:
                if _is_browser_dead(exc):
                    raise
        reading = read_snapshot(snap)
        if reading.blank and self.diagnostics is not None:
            probe = await self._diagnostic("probe", page)
            if isinstance(probe, dict):
                reading = read_snapshot(snap, probe, self.diagnostics)
        return reading

    async def _left_blank(self, page) -> bool:
        """The guard sent the page to about:blank (a redirect or route it refused)."""
        await self.guard.settle()
        return page.url in ("about:blank", "")

    async def _stabilize(self, page, reading: Reading) -> Reading:
        for n in range(1, RENDER_WAITS + 1):
            if not reading.blank or await self._left_blank(page):
                break
            self.step("browser.render_wait", "Waiting for the page to render", ok=True,
                      result={"wait_s": RENDER_WAIT_SECONDS, "n": n}, turn_no=self._turn_no)
            await self._sleep(RENDER_WAIT_SECONDS)
            reading = await self._measure(page)
        self.progress.stabilized_gen = self.progress.gen
        return reading

    def _blank_load(self, content) -> ToolReturn:
        self.progress.blank_loads += 1
        content = {**content, "error": "page_not_rendering"}
        if self.progress.blank_loads >= BLANK_PAGE_LIMIT:
            self.defer_terminal("page_not_rendering")
            return ToolReturn(content, ok=False, screenshot=False)
        return ToolReturn({**content, "detail": NOT_RENDERING_DETAIL}, ok=False)

    async def _blocked_load(self, page) -> ToolReturn:
        """Not a blank load: run_tool reports the guard's block to the model."""
        await self.emit_diagnostics("navigate", page)
        return ToolReturn(await self._page_state(page))

    async def _after_load(self, page) -> ToolReturn:
        await self.guard.follow_redirects(page)
        if await self._left_blank(page):
            return await self._blocked_load(page)
        tracker = self.progress
        tracker.gen += 1
        reading = await self._stabilize(page, await self._measure(page))
        if await self._left_blank(page):
            return await self._blocked_load(page)
        await self.emit_diagnostics("navigate", page)
        state = {**await self._page_state(page), "readable_chars": reading.chars, "refs": reading.refs}
        if reading.blank:
            return self._blank_load(state)
        if state["url"] == tracker.last_url and reading.fp == tracker.last_fp:
            tracker.same_reload += 1
            if tracker.same_reload >= SAME_PAGE_RELOAD_LIMIT:
                self.defer_terminal("same_page_reload")
                return ToolReturn(state, screenshot=False)
            state["unchanged"] = True
        else:
            tracker.same_reload = 0
        self.observe(Observation(url=state["url"], fp=reading.fp))
        return ToolReturn(state)

    async def _checked_read(self, handler, args, new_info) -> ToolReturn:
        """Run read_page or find_text; an empty result gets the render waits
        once per page load, then a cheap page_not_rendering error."""
        ret = await handler(args)
        page = await self.page()
        reading = await self._measure(page, self.snapshot)
        tracker = self.progress
        if reading.blank and tracker.stabilized_gen != tracker.gen:
            # The page arrived without a model load (for example through login).
            reading = await self._stabilize(page, reading)
            if await self._left_blank(page):
                raise ToolError("blank_page", "nothing is open; use navigate or a site tool first")
            if not reading.blank:
                ret = await handler(args)
                reading = await self._measure(page, self.snapshot)
            else:
                await self.emit_diagnostics("empty_read", page)
                return self._blank_load({"url": strip_query(page.url) or page.url,
                                         "readable_chars": reading.chars, "refs": reading.refs})
        if reading.blank:
            await self.emit_diagnostics("empty_read", page)
            return ToolReturn({"url": strip_query(page.url) or page.url, "error": "page_not_rendering",
                               "detail": NOT_RENDERING_DETAIL, "readable_chars": reading.chars,
                               "refs": reading.refs}, ok=False, screenshot=False)
        self.observe(Observation(url=strip_query(page.url) or page.url, fp=reading.fp, new_info=new_info(ret)))
        return ret

    # Personal tool overrides ----------------------------------------------------------

    async def _navigate(self, args):
        await super()._navigate(args)
        return await self._after_load(await self.page())

    async def _go_back(self, args):
        await super()._go_back(args)
        return await self._after_load(await self.page())

    async def _read_page(self, args):
        return await self._checked_read(super()._read_page, args, lambda ret: False)

    async def _find_text(self, args):
        return await self._checked_read(super()._find_text, args,
                                        lambda ret: int(ret.content.get("count") or 0) > 0)

    async def _wait_for(self, args):
        ret = await super()._wait_for(args)
        if args.text and ret.content.get("found") is True:
            self.observe(Observation(new_info=True))
        return ret


class FinishTool:
    """`finish` for personal jobs. A cannot_complete finish on a page whose
    own code the guard refused (page_code_blocked) keeps error_kind
    cannot_complete but gets the fixed reason and owner-safe summary."""

    def __init__(self, page_code_blocked):
        self.page_code_blocked = page_code_blocked

    async def __call__(self, args):
        if args.outcome == "captured":
            raise ToolError("unsupported", "Personal tasks do not capture BDR records")
        if args.outcome == "cannot_complete":
            if await self._blocked():
                terminal = Finish("failed", SUMMARY["page_code_blocked"], error_kind="cannot_complete",
                                  error="page_code_blocked")
            else:
                terminal = Finish("failed", args.summary, error_kind=args.error_kind or "cannot_complete")
        else:
            terminal = Finish("succeeded", args.summary, outcome="nothing_to_do")
        return ToolReturn({"finished": True}, terminal=terminal, screenshot=False)

    async def _blocked(self) -> bool:
        try:
            return bool(await self.page_code_blocked())
        except (ToolError, ToolCallInvalid, BdrApiError, BdrUnreachable, *_FATAL):
            raise
        except Exception as exc:
            if _is_browser_dead(exc):
                raise
            return False


class UserBrowserRunner:
    version = "1.0.0"
    needs_browser = True

    async def run(self, ctx):
        if ctx.job.principal_kind != "user" or not ctx.job.principal_user_id:
            return Finish("failed", "Personal principal required", error_kind="unsupported")
        jb = ctx.browser_context
        jb.attach_steps(ctx.steps.log)
        jb.apply_narrowing(ctx.type_config.host_narrowing)
        desk = UserHelpDesk(ctx)
        executor = UserExecutor(ctx, jb)
        identity = UserIdentity(ctx, executor, desk)
        executor.extra_tools["ensure_login"] = LoginGate(executor, identity)
        helpers = HelpTools(desk, executor, rfi_id=lambda: None).handlers()
        executor.extra_tools.update({key: helpers[key] for key in ("request_help", "reply_help")})

        executor.extra_tools["finish"] = FinishTool(executor.page_code_blocked)
        try:
            return await run_loop(UserAgentLoop(ctx, executor, desk))
        finally:
            # One final cumulative census, flushed by the slot before the terminal.
            await executor.emit_diagnostics("final")

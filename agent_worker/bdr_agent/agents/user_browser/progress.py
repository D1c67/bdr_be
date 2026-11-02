"""Personal no-progress limits: blank renders, redundant logins, reloads and caps.

A personal job ends early, with a fixed owner-safe summary, instead of
spending its step cap on a page that never renders or on calls that change
nothing. Every limit ends the job with Finish("failed", error_kind=
"cannot_complete") and the reason code in `error`; no new error kind exists.

Organization jobs never use this module: the org AgentLoop and Executor are
unchanged and only the personal runner builds these classes.

Nothing raw reaches a step from here: results hold counters and reason codes,
never a URL, title, page text or fingerprint.
"""
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ...agent.loop import AgentLoop
from ...browser.executor import ToolReturn
from ...browser.guard import host_in
from ...slot import Finish, JobResult

RENDER_WAIT_SECONDS = 5
RENDER_WAITS = 2                 # at most 10 s of render waits per page load
SNAPSHOT_TIMEOUT_S = 5
DIAGNOSTIC_TIMEOUT_S = 5
BLANK_TEXT_CHARS = 40
BLANK_PAGE_LIMIT = 2             # blank page loads in one attempt
REDUNDANT_LOGIN_LIMIT = 3
SAME_PAGE_RELOAD_LIMIT = 2
NO_PROGRESS_LIMIT = 6            # consecutive actions without new content
MAX_SEEN_FINGERPRINTS = 64

SUMMARY = {
    "page_not_rendering": "The approved Procore page opened but showed no readable content after 2 loads of up "
                          "to 10 seconds each, so the task stopped. Nothing was changed in Procore.",
    "redundant_login": "The task kept re-checking a working Procore sign-in without making progress, so it was "
                       "stopped. Nothing was changed in Procore.",
    "same_page_reload": "The task reloaded the same Procore page without any change, so it was stopped. "
                        "Nothing was changed in Procore.",
    "no_progress": f"The task took {NO_PROGRESS_LIMIT} actions in a row without reaching new page content, so it "
                   "was stopped. Nothing was changed in Procore.",
    "step_cap": "This task already used its full step limit, so retrying cannot continue it. "
                "Start a new request.",
    "cap_reached": "The task reached its step, token, cost or time limit and stopped. "
                   "Start a new request to try again.",
    "daily_cap_reached": "The daily browser spend limit is reached. Try again tomorrow.",
    # Not a no-progress stop: the finish tool's fixed reason (runner.FinishTool).
    "page_code_blocked": "The Procore page showed its page not found screen because the worker does not yet "
                         "allow a part of that page's own code, so the task stopped. Nothing was changed "
                         "in Procore.",
}

NOT_RENDERING_DETAIL = ("The page loaded but shows no readable content after waiting 10 seconds. Do not reload "
                        "it, do not call ensure_login and do not wait again. You may open one different approved "
                        "page or finish with outcome cannot_complete.")
ALREADY_SIGNED_IN_DETAIL = "Already signed in. Do not call ensure_login again; use navigate or read_page."


@dataclass(frozen=True)
class Reading:
    """What one render check saw: sizes and a content fingerprint only."""
    chars: int = 0
    refs: int = 0
    fp: str | None = None        # None when blank
    blank: bool = True


@dataclass(frozen=True)
class Observation:
    """What a tool call showed, for the no-progress counter."""
    url: str | None = None
    fp: str | None = None
    new_info: bool = False


def read_snapshot(snap: Any, probe: dict[str, Any] | None = None, diagnostics: Any = None) -> Reading:
    """Size a snapshot. Blank means no refs and under BLANK_TEXT_CHARS of text.
    A render probe that finds readable content overrides a blank snapshot, so
    a walker gap never ends a job on its own."""
    if snap is None:
        lines, refs = [], 0
    else:
        lines, refs = list(snap.lines), len(snap.refs)
    joined = "\n".join(lines)
    chars = len("".join(joined.split()))
    blank = refs == 0 and chars < BLANK_TEXT_CHARS
    if blank and probe is not None and probe_readable(diagnostics, probe):
        blank = False
    fp = None if blank else hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()[:16]
    return Reading(chars=chars, refs=refs, fp=fp, blank=blank)


def probe_readable(diagnostics: Any, probe: dict[str, Any]) -> bool:
    check = getattr(diagnostics, "readable", None)
    try:
        if callable(check):
            return bool(check(probe))
        return int(probe.get("text_len") or 0) > 0
    except Exception:
        return False


def on_login_host(page: Any, ceiling: Any) -> bool:
    if page is None or page.is_closed():
        return False
    try:
        host = (urlsplit(page.url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and host_in(host, ceiling.login_hosts)


@dataclass
class ProgressTracker:
    gen: int = 0                         # page-load generation, bumped by each model navigate or go_back
    stabilized_gen: int = -1
    blank_loads: int = 0
    last_url: str | None = None
    last_fp: str | None = None
    seen_fps: OrderedDict = field(default_factory=OrderedDict)
    no_progress: int = 0
    same_reload: int = 0
    login_ok: bool = False
    redundant_logins: int = 0

    def note(self, obs: Observation) -> str | None:
        """Count one finished tool call. Returns a reason code at the limit."""
        progress = obs.new_info or (obs.url is not None and obs.url != self.last_url) or (
            obs.fp is not None and obs.fp not in self.seen_fps)
        if obs.url is not None:
            self.last_url = obs.url
        if obs.fp is not None:
            self.last_fp = obs.fp
            self.seen_fps[obs.fp] = None
            self.seen_fps.move_to_end(obs.fp)
            while len(self.seen_fps) > MAX_SEEN_FINGERPRINTS:
                self.seen_fps.popitem(last=False)
        if progress:
            self.no_progress = 0
            self.same_reload = 0
            return None
        self.no_progress += 1
        return "no_progress" if self.no_progress >= NO_PROGRESS_LIMIT else None

    def terminal(self, reason: str, log: Any, *, turn_no: int | None = None) -> Finish:
        log("error.no_progress", f"Personal task stopped: {reason}", ok=False, turn_no=turn_no,
            result={"reason": reason, "no_progress": self.no_progress, "blank_loads": self.blank_loads,
                    "redundant_logins": self.redundant_logins, "same_reloads": self.same_reload})
        return Finish("failed", SUMMARY[reason], error_kind="cannot_complete", error=reason)


class LoginGate:
    """`ensure_login` for personal jobs: the real login only when it can change
    something; otherwise one cheap session probe and an already-signed-in
    answer. A failed probe marks the session stale and runs the real login,
    so a mid-job logout is recovered rather than counted as redundant."""

    def __init__(self, executor: Any, identity: Any):
        self.ex = executor
        self.identity = identity

    async def __call__(self, args: Any) -> ToolReturn:
        ex, identity, tracker = self.ex, self.identity, self.ex.progress
        if (args.host != identity.host or not tracker.login_ok or identity.stale or not identity.logged_in
                or on_login_host(ex.jb.page, ex.guard.ceiling) or not await self._still_signed_in()):
            ret = await identity.ensure(args.host)
            # Login, MFA and help terminals are returned untouched.
            if ret.terminal is None:
                tracker.login_ok = bool(ret.ok and ret.content.get("outcome") == "ok")
                ex.observe(Observation(new_info=tracker.login_ok))
            return ret
        tracker.redundant_logins += 1
        ex.observe(Observation())
        content = {"ok": True, "outcome": "ok", "already": True}
        if tracker.redundant_logins >= REDUNDANT_LOGIN_LIMIT:
            ex.defer_terminal("redundant_login")
            return ToolReturn(content, screenshot=False)
        return ToolReturn({**content, "detail": ALREADY_SIGNED_IN_DETAIL}, screenshot=False)

    async def _still_signed_in(self) -> bool:
        try:
            ok = bool(await self.identity.probe())
        except Exception:
            ok = False
        if not ok:
            self.identity.mark_stale()
        return ok


class UserAgentLoop(AgentLoop):
    """The org loop, except that a personal job never parks on a cap: a resume
    past the step cap and a cap stop both finish failed, with no help."""

    async def run(self) -> JobResult:
        ctx = self.ctx
        used, cap = int(ctx.job.turn_count or 0), int(ctx.type_config.max_steps)
        if used >= cap:
            ctx.steps.log("error.step_cap", "The step limit is already used; not resuming", ok=False,
                          result={"turn_count": used, "max_steps": cap})
            return Finish("failed", SUMMARY["step_cap"], error_kind="cannot_complete", error="step_cap")
        return await super().run()

    async def _on_stop(self, stop: Any) -> JobResult:
        if stop.reason in ("cap_reached", "daily_cap_reached"):
            self.ctx.steps.log("note", f"BDR stopped the model loop: {stop.reason}")
            return Finish("failed", SUMMARY[stop.reason], error_kind="cannot_complete", error=stop.reason)
        return await super()._on_stop(stop)

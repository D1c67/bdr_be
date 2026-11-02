"""Personal no-progress limits with fake pages (no browser): render waits,
the ensure_login gate, same-page reloads, the general counter and caps."""
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from bdr_agent import protocol as P
from bdr_agent.agents import ceiling_for
from bdr_agent.agents.user_browser import progress as progress_mod
from bdr_agent.agents.user_browser.guard import UserGuard
from bdr_agent.agents.user_browser.progress import (
    MAX_SEEN_FINGERPRINTS,
    SUMMARY,
    LoginGate,
    Observation,
    ProgressTracker,
    UserAgentLoop,
)
from bdr_agent.agents.user_browser.runner import UserBrowserRunner, UserExecutor
from bdr_agent.browser.executor import Executor, ToolReturn
from bdr_agent.client import Lease
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Finish, HandedOff, Requeue

SEED = "https://app.procore.com/123/project/rfi/list"
HOME = "https://app.procore.com/123/project/home"
SHOW = "https://app.procore.com/123/project/rfi/show/{}"
EM_DASH = chr(0x2014)
DECOYS = ("DECOY-PAGE-TEXT", "DECOY-TITLE", "decoy_query=1", "decoy.example")

BLANK = {"lines": [], "refs": {}}


def content(n=1):
    return {"lines": [f'heading(1) "RFI list {n} DECOY-PAGE-TEXT"', f'link "RFI {n}" [ref_1]'],
            "refs": {"ref_1": {"tag": "a", "role": "link"}}}


# Fakes ---------------------------------------------------------------------------------


class FakeSteps:
    def __init__(self):
        self.rows = []

    def log(self, kind, summary, **fields):
        assert P.worker_step_kind_allowed(kind), kind
        self.rows.append({"kind": kind, "summary": summary, **fields})
        return str(len(self.rows))

    def kinds(self):
        return [r["kind"] for r in self.rows]

    def of(self, kind):
        return [r for r in self.rows if r["kind"] == kind]


class FakePage:
    def __init__(self, views=None, url="about:blank"):
        self.url = url
        self.views = views or {}       # url -> list of walker results; the last one sticks
        self.evaluations = 0
        self.history = []
        self.closed = False

    def is_closed(self):
        return self.closed

    async def goto(self, url, wait_until=None, timeout=None):
        self.history.append(self.url)
        self.url = url
        return NS(status=200)

    async def go_back(self, wait_until=None, timeout=None):
        self.url = self.history.pop() if self.history else "about:blank"

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def title(self):
        return "DECOY-TITLE"

    async def evaluate(self, _js, _args):
        self.evaluations += 1
        queue = self.views.get(self.url.split("?")[0]) or [BLANK]
        view = queue.pop(0) if len(queue) > 1 else queue[0]
        return {**view, "title": "DECOY-TITLE", "url": self.url}

    def get_by_text(self, text):
        page = self

        class _Loc:
            first = None

            async def wait_for(self, state=None, timeout=None):
                if text not in json.dumps(page.views.get(page.url, [BLANK])[0]):
                    raise RuntimeError("not visible")

        loc = _Loc()
        loc.first = loc
        return loc


class FakeDiagnostics:
    def __init__(self, readable=False, fail=False):
        self.emits = []
        self.probes = 0
        self._readable = readable
        self.fail = fail

    async def emit(self, page, trigger):
        self.emits.append(trigger)
        if self.fail:
            raise RuntimeError("diagnostics broke")
        return {"trigger": trigger}

    async def probe(self, page):
        self.probes += 1
        if self.fail:
            raise RuntimeError("probe broke")
        return {"text_len": 500 if self._readable else 0, "element_count": 3}

    def readable(self, probe):
        return probe["text_len"] > 0


class FakeGuard:
    def __init__(self, diagnostics=None):
        real = UserGuard(ceiling_for("user_browser_read"))
        real.bind_seed(SEED)
        self.ceiling = real.ceiling
        self.nav_count = 0
        self.max_navigations = 40
        self.navigations_left = 40
        if diagnostics is not None:
            self.diagnostics = diagnostics

    async def settle(self):
        return None

    def take_violations(self, _who):
        return []

    async def follow_redirects(self, page):
        return None

    def _record_block(self, *a):
        return None


class FakeJb:
    def __init__(self, page, guard):
        self.page = page
        self.guard = guard
        self.login_page = None
        self.request = object()
        self.context = NS(request=None)

    async def model_page(self):
        return self.page

    def attach_steps(self, _log):
        return None

    def apply_narrowing(self, _value):
        return None


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


class FakeIdentity:
    host = "app.procore.com"

    def __init__(self, results=None):
        self.calls = 0
        self.probes = 0
        self.probe_ok = True
        self.logged_in = False
        self.stale = False
        self.results = list(results or [])

    async def probe(self):
        self.probes += 1
        return self.probe_ok

    async def ensure(self, host):
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        self.logged_in, self.stale = True, False
        return ToolReturn({"ok": True, "outcome": "ok"}, screenshot=False)

    def mark_stale(self):
        self.stale, self.logged_in = True, False


def make_ctx(*, turn_count=0, max_steps=40):
    job = NS(job_id=uuid4(), turn_count=turn_count, unconsumed_help=[], principal_kind="user",
             principal_user_id=uuid4())
    return NS(agent_type="user_browser_read", job=job, job_id=job.job_id, lease=Lease(job.job_id, 1),
              steps=FakeSteps(), secrets=LiveSecrets(), type_config=NS(max_steps=max_steps, host_narrowing=None),
              client=NS(), input=NS(url=SEED))


def make_executor(views=None, *, diagnostics=None, url="about:blank", identity=None):
    ctx = make_ctx()
    sleeps = Sleeps()
    page = FakePage(views, url=url)
    jb = FakeJb(page, FakeGuard(diagnostics))
    ex = UserExecutor(ctx, jb, screenshots=False, sleep=sleeps)
    identity = identity or FakeIdentity()
    ex.extra_tools["ensure_login"] = LoginGate(ex, identity)

    async def finish(args):
        return ToolReturn({"finished": True}, screenshot=False,
                          terminal=Finish("succeeded", args.summary, outcome="nothing_to_do"))

    async def help_tool(args):
        return ToolReturn({"status": "answered"}, screenshot=False)

    ex.extra_tools.update(finish=finish, request_help=help_tool, reply_help=help_tool)
    return ex, ctx, page, sleeps, identity


_n = 0


async def call(ex, tool, **args):
    global _n
    _n += 1
    return await ex.run_tool(P.ToolCall(call_id=f"c{_n}", tool=tool, args=args), turn_no=_n)


def assert_valid_finish(result):
    assert isinstance(result, Finish)
    P.FinishRequest(status=result.status, outcome=result.outcome, error_kind=result.error_kind,
                    error=result.error, summary=result.summary)
    assert result.error_kind == "cannot_complete" and result.status == "failed"
    assert EM_DASH not in result.summary and "http" not in result.summary


def assert_private(ctx):
    """New personal steps never carry a URL, title or page text."""
    rows = [r for r in ctx.steps.rows if not r["kind"].startswith("tool.")]
    text = json.dumps(rows, default=str)
    for decoy in DECOYS + ("procore.com/",):
        assert decoy not in text


# 1-3: render stabilization ------------------------------------------------------------


async def test_blank_navigate_waits_twice_then_reports_not_rendering():
    diag = FakeDiagnostics()
    ex, ctx, _page, sleeps, _ = make_executor({SEED: [BLANK]}, diagnostics=diag)
    out = await call(ex, "navigate", url=SEED)
    assert sleeps.calls == [5, 5]
    assert len(ctx.steps.of("browser.render_wait")) == 2
    assert diag.emits == ["navigate"]
    assert out.terminal is None and out.ok is False
    body = json.loads(out.content)
    assert body["error"] == "page_not_rendering" and "ensure_login" in body["detail"]
    assert body["readable_chars"] == 0 and body["refs"] == 0
    assert_private(ctx)


async def test_content_after_first_wait_is_ok_without_more_waits():
    diag = FakeDiagnostics()
    ex, _ctx, _page, sleeps, _ = make_executor({SEED: [BLANK, content()]}, diagnostics=diag)
    out = await call(ex, "navigate", url=SEED)
    assert sleeps.calls == [5]
    assert out.ok and out.terminal is None
    body = json.loads(out.content)
    assert body["readable_chars"] > 0 and body["refs"] == 1
    assert diag.emits == ["navigate"]


async def test_second_blank_load_is_terminal_after_a_diagnostic():
    diag = FakeDiagnostics()
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [BLANK], HOME: [BLANK]}, diagnostics=diag)
    first = await call(ex, "navigate", url=SEED)
    assert first.terminal is None
    second = await call(ex, "navigate", url=HOME)
    assert_valid_finish(second.terminal)
    assert second.terminal.error == "page_not_rendering"
    assert second.terminal.summary == SUMMARY["page_not_rendering"]
    assert diag.emits == ["navigate", "navigate"]
    kinds = ctx.steps.kinds()
    assert kinds.index("tool.navigate") < kinds.index("error.no_progress")
    assert_private(ctx)


async def test_no_diagnostics_falls_back_to_the_snapshot():
    ex, _ctx, _page, sleeps, _ = make_executor({SEED: [BLANK]})
    out = await call(ex, "navigate", url=SEED)
    assert json.loads(out.content)["error"] == "page_not_rendering" and sleeps.calls == [5, 5]


async def test_readable_probe_overrides_a_blank_snapshot_and_broken_diagnostics_are_ignored():
    ex, _ctx, _page, sleeps, _ = make_executor({SEED: [BLANK]}, diagnostics=FakeDiagnostics(readable=True))
    out = await call(ex, "navigate", url=SEED)
    assert out.ok and sleeps.calls == []
    diag = FakeDiagnostics(fail=True)
    ex, _ctx, _page, sleeps, _ = make_executor({SEED: [content()]}, diagnostics=diag)
    out = await call(ex, "navigate", url=SEED)
    assert out.ok and diag.emits == ["navigate"]


# 4: cheap reads on a stabilized blank page ----------------------------------------------


async def test_read_on_stabilized_blank_page_is_cheap_and_counts_as_no_progress():
    diag = FakeDiagnostics()
    ex, _ctx, _page, sleeps, _ = make_executor({SEED: [BLANK]}, diagnostics=diag)
    await call(ex, "navigate", url=SEED)
    sleeps.calls.clear()
    ret = await ex._read_page(P.ReadPageArgs())
    assert ret.screenshot is False and ret.ok is False and ret.content["error"] == "page_not_rendering"
    assert sleeps.calls == []
    before = ex.progress.no_progress
    out = await call(ex, "find_text", query="RFI")
    assert json.loads(out.content)["error"] == "page_not_rendering" and sleeps.calls == []
    assert ex.progress.no_progress == before + 1
    assert diag.emits.count("empty_read") == 2


async def test_read_on_unstabilized_page_waits_once():
    ex, _ctx, _page, sleeps, _ = make_executor({SEED: [BLANK, content()]}, url=SEED)
    out = await call(ex, "read_page")
    assert out.ok and sleeps.calls == [5]
    assert "RFI list" in json.loads(out.content)["text"]
    out = await call(ex, "read_page")
    assert out.ok and sleeps.calls == [5]


async def test_read_page_with_content_is_unchanged():
    ex, _ctx, _page, _sleeps, _ = make_executor({SEED: [content()]})
    await call(ex, "navigate", url=SEED)
    out = await call(ex, "read_page")
    body = json.loads(out.content)
    assert out.ok and body["refs"] == 1 and "RFI list" in body["text"]


# 5-7: the ensure_login gate ------------------------------------------------------------


async def test_redundant_logins_only_probe_and_the_third_is_terminal():
    ex, _ctx, _page, _sleeps, identity = make_executor({SEED: [content()]})
    first = await call(ex, "ensure_login", host="app.procore.com")
    assert first.ok and identity.calls == 1 and identity.probes == 0
    second = await call(ex, "ensure_login", host="app.procore.com")
    assert json.loads(second.content)["already"] is True and second.terminal is None
    await call(ex, "ensure_login", host="app.procore.com")
    third = await call(ex, "ensure_login", host="app.procore.com")
    assert identity.calls == 1 and identity.probes == 3
    assert_valid_finish(third.terminal)
    assert third.terminal.error == "redundant_login"


async def test_a_mid_job_logout_runs_the_real_login_and_is_not_redundant():
    ex, _ctx, _page, _sleeps, identity = make_executor({SEED: [content()]})
    await call(ex, "ensure_login", host="app.procore.com")
    identity.probe_ok = False                       # the session died after the first login
    for _ in range(3):
        out = await call(ex, "ensure_login", host="app.procore.com")
        assert out.ok and out.terminal is None and "already" not in json.loads(out.content)
    assert identity.calls == 4 and identity.probes == 3
    assert ex.progress.redundant_logins == 0


@pytest.mark.parametrize("how", ["stale", "not_ok", "login_host", "other_host"])
async def test_gate_passes_through_when_login_can_change_something(how):
    results = [ToolReturn({"ok": False, "outcome": "help_open"}, ok=False, screenshot=False)] \
        if how == "not_ok" else []
    identity = FakeIdentity(results)
    ex, _ctx, page, _sleeps, _ = make_executor({SEED: [content()]}, identity=identity)
    await call(ex, "ensure_login", host="app.procore.com")
    if how == "stale":
        identity.mark_stale()
    elif how == "login_host":
        page.url = "https://login.procore.com/sessions/new"
    host = "us02.procore.com" if how == "other_host" else "app.procore.com"
    out = await call(ex, "ensure_login", host=host)
    assert identity.calls == 2
    assert ex.progress.redundant_logins == 0 and out.terminal is None


@pytest.mark.parametrize("terminal", [
    HandedOff("Parked waiting for login help"),
    Finish("failed", "Procore login is not available to this worker", error_kind="login_failed",
           error="not_configured"),
    Requeue("login_spacing"),
])
async def test_login_and_help_terminals_pass_through_untouched(terminal):
    identity = FakeIdentity([ToolReturn({"ok": False, "outcome": "mfa"}, ok=False, screenshot=False,
                                        terminal=terminal)])
    ex, ctx, _page, _sleeps, _ = make_executor(identity=identity)
    ex.progress.no_progress = 5
    out = await call(ex, "ensure_login", host="app.procore.com")
    assert out.terminal is terminal
    assert not ctx.steps.of("error.no_progress")


# 8-10: reloads and the general counter --------------------------------------------------


async def test_two_unchanged_reloads_end_the_job():
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [content(1)]})
    assert (await call(ex, "navigate", url=SEED)).terminal is None
    reload1 = await call(ex, "navigate", url=SEED)
    assert reload1.terminal is None and json.loads(reload1.content)["unchanged"] is True
    reload2 = await call(ex, "navigate", url=SEED)
    assert_valid_finish(reload2.terminal)
    assert reload2.terminal.error == "same_page_reload"
    assert_private(ctx)


async def test_changed_content_on_reload_resets_the_counter():
    ex, _ctx, _page, _sleeps, _ = make_executor({SEED: [content(1), content(1), content(2), content(2)]})
    await call(ex, "navigate", url=SEED)
    await call(ex, "navigate", url=SEED)
    assert ex.progress.same_reload == 1
    out = await call(ex, "navigate", url=SEED)
    assert out.terminal is None and ex.progress.same_reload == 0


async def test_six_no_progress_actions_end_the_job():
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [content()]})
    await call(ex, "navigate", url=SEED)
    outs = [await call(ex, "scroll", direction="down", amount=1) for _ in range(6)]
    assert all(o.terminal is None for o in outs[:5])
    assert_valid_finish(outs[5].terminal)
    assert outs[5].terminal.error == "no_progress"
    [row] = ctx.steps.of("error.no_progress")
    assert row["result"]["reason"] == "no_progress" and row["result"]["no_progress"] == 6
    assert_private(ctx)


async def test_progress_resets_the_counter():
    ex, _ctx, page, _sleeps, _ = make_executor({SEED: [content(1)], HOME: [content(2)]})
    page.views[SEED] = [content(1)]
    await call(ex, "navigate", url=SEED)
    for _ in range(5):
        await call(ex, "scroll", direction="down", amount=1)
    assert ex.progress.no_progress == 5
    await call(ex, "find_text", query="RFI")          # a positive match is new information
    assert ex.progress.no_progress == 0
    for _ in range(5):
        await call(ex, "wait_for", seconds=1)
    await call(ex, "navigate", url=HOME)               # a URL change
    assert ex.progress.no_progress == 0
    for _ in range(5):
        await call(ex, "read_page")                     # same content: no progress
    assert ex.progress.no_progress == 5
    await call(ex, "request_help", reason_code="other", message="stuck")
    assert ex.progress.no_progress == 0


def test_seen_fingerprints_are_bounded():
    tracker = ProgressTracker()
    for i in range(MAX_SEEN_FINGERPRINTS + 10):
        tracker.note(Observation(fp=f"{i:016x}"))
    assert len(tracker.seen_fps) == MAX_SEEN_FINGERPRINTS
    assert tracker.no_progress == 0


async def test_secret_leak_and_finish_terminals_are_never_replaced():
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [content()]})
    await call(ex, "navigate", url=SEED)
    ctx.secrets.add("DECOY-PAGE-TEXT", owner=str(ctx.job_id))
    ex.progress.no_progress = 5
    out = await call(ex, "read_page")
    assert out.terminal.error_kind == "secret_leak_blocked"
    ex.progress.no_progress = 5
    out = await call(ex, "finish", outcome="nothing_to_do", summary="done")
    assert out.terminal.status == "succeeded"
    assert not ctx.steps.of("error.no_progress")


# 11-13: the personal loop ----------------------------------------------------------------


class FakeLoopClient:
    def __init__(self, script):
        self.script = list(script)
        self.turns = []
        self.parks = []

    async def turn(self, lease, req):
        self.turns.append(req)
        item = self.script.pop(0) if self.script else {"stop": "cap_reached"}
        if "stop" in item:
            return P.TurnStopResponse(stop=True, reason=item["stop"], retry_at=item.get("retry_at"))
        n = req.prev_turn_no + 1
        return P.TurnActionResponse(turn_no=n, action=P.ToolCall(call_id=f"t{n}", tool=item["tool"],
                                                                   args=item.get("args", {})),
                                    caps=P.TurnCaps(steps_left=10, tokens_left=1000, cost_left_usd=1.0))

    async def park(self, lease):
        self.parks.append(lease)


class FakeDesk:
    def __init__(self):
        self.opened = []

    def absorb_claim(self, unconsumed):
        return []

    async def consume_seen(self, ids):
        return None

    async def open(self, reason, message, shot, **kw):
        self.opened.append(reason)
        return None, NS(help_id=uuid4())


async def _no_sleep(_s):
    return None


def loop_for(script, *, turn_count=0, max_steps=40, ex=None):
    ex = ex or make_executor()[0]
    ex.ctx.job.turn_count = turn_count
    ex.ctx.type_config.max_steps = max_steps
    ex.ctx.client = FakeLoopClient(script)
    desk = FakeDesk()
    return UserAgentLoop(ex.ctx, ex, desk, sleep=_no_sleep), ex.ctx, desk


@pytest.mark.parametrize("turn_count", [40, 41])
async def test_resume_at_the_step_cap_finishes_without_a_turn(turn_count):
    loop, ctx, desk = loop_for([{"tool": "read_page"}], turn_count=turn_count)
    result = await loop.run()
    assert_valid_finish(result)
    assert result.error == "step_cap" and result.summary == SUMMARY["step_cap"]
    assert ctx.client.turns == [] and desk.opened == [] and ctx.client.parks == []
    [row] = ctx.steps.of("error.step_cap")
    assert row["result"] == {"turn_count": turn_count, "max_steps": 40}


async def test_resume_below_the_cap_proceeds():
    loop, ctx, _desk = loop_for([{"tool": "finish", "args": {"outcome": "nothing_to_do", "summary": "x"}}],
                               turn_count=39)
    result = await loop.run()
    assert result.status == "succeeded" and len(ctx.client.turns) == 1


@pytest.mark.parametrize("reason", ["cap_reached", "daily_cap_reached"])
async def test_cap_stops_finish_without_help_or_park(reason):
    loop, ctx, desk = loop_for([{"stop": reason}])
    result = await loop.run()
    assert_valid_finish(result)
    assert result.error == reason and result.summary == SUMMARY[reason]
    assert desk.opened == [] and ctx.client.parks == []


async def test_other_stops_keep_the_org_behaviour():
    loop, ctx, desk = loop_for([{"stop": "provider_config"}])
    assert isinstance(await loop.run(), HandedOff)
    assert desk.opened == ["provider_config"] and len(ctx.client.parks) == 1
    loop, ctx, desk = loop_for([{"stop": "provider_outage"}])
    assert await loop.run() == Requeue("provider_outage")
    loop, ctx, desk = loop_for([{"stop": "canceled"}])
    assert (await loop.run()).status == "canceled"


async def test_second_blank_load_sends_no_further_turn():
    ex = make_executor({SEED: [BLANK], HOME: [BLANK]})[0]
    loop, ctx, _desk = loop_for([{"tool": "navigate", "args": {"url": SEED}},
                                {"tool": "ensure_login", "args": {"host": "app.procore.com"}},
                                {"tool": "navigate", "args": {"url": HOME}},
                                {"tool": "read_page"}], ex=ex)
    result = await loop.run()
    assert_valid_finish(result)
    assert result.error == "page_not_rendering"
    assert len(ctx.client.turns) == 3


async def test_runner_step_cap_resume_emits_final_diagnostics():
    diag = FakeDiagnostics()
    page = FakePage()
    jb = FakeJb(page, FakeGuard(diag))
    ctx = make_ctx(turn_count=40)
    ctx.browser_context = jb
    ctx.client = FakeLoopClient([])
    ctx.client.bind_host = lambda host: None
    result = await UserBrowserRunner().run(ctx)
    assert_valid_finish(result)
    assert result.error == "step_cap" and ctx.client.turns == []
    assert diag.emits == ["final"]


def test_every_summary_is_owner_safe():
    for reason, text in SUMMARY.items():
        assert EM_DASH not in text and "http" not in text and len(text) <= P.FINISH_SUMMARY_MAX
        P.FinishRequest(status="failed", error_kind="cannot_complete", error=reason, summary=text)
    for text in (progress_mod.NOT_RENDERING_DETAIL, progress_mod.ALREADY_SIGNED_IN_DETAIL):
        assert EM_DASH not in text


# 14: org regression ---------------------------------------------------------------------


async def test_org_executor_navigate_never_waits_for_render():
    ctx = make_ctx()
    ctx.agent_type = "rfi_from_email"
    sleeps = Sleeps()
    page = FakePage({SEED: [BLANK]})
    ex = Executor(ctx, FakeJb(page, FakeGuard(FakeDiagnostics())), screenshots=False, sleep=sleeps)
    ret = await ex._navigate(P.NavigateArgs(url=SEED))
    assert sleeps.calls == [] and page.evaluations == 0
    assert set(ret.content) == {"url", "title", "status"}


def test_diagnostics_triggers_match_the_render_diagnostics_contract():
    diagnostics = pytest.importorskip("bdr_agent.agents.user_browser.diagnostics")
    assert {"navigate", "empty_read", "final"} <= set(diagnostics.TRIGGERS)


# Review fixes ------------------------------------------------------------------------------


async def test_navigate_ledger_never_holds_the_title():
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [content()], HOME: [BLANK]})
    out = await call(ex, "navigate", url=SEED)
    assert json.loads(out.content)["title"] == "DECOY-TITLE"       # the model still sees it
    await call(ex, "navigate", url=HOME)                              # page_not_rendering result
    await call(ex, "go_back")
    rows = ctx.steps.of("tool.navigate") + ctx.steps.of("tool.go_back")
    assert len(rows) == 3 and "DECOY-TITLE" not in json.dumps(rows, default=str)
    assert rows[0]["result"]["refs"] == 1


async def test_a_blocked_navigate_step_keeps_the_block_but_not_the_title():
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [content()]})
    pending = [[NS(url="https://app.procore.com/webclients/x?decoy_query=1", reason="path_not_allowed")]]
    ex.guard.take_violations = lambda _who: pending.pop() if pending else []
    out = await call(ex, "navigate", url=SEED)
    seen = json.loads(out.content)
    assert seen["title"] == "DECOY-TITLE" and seen["error"] == "navigation_blocked"
    row = ctx.steps.of("tool.navigate")[0]["result"]
    assert "title" not in row and row["error"] == "navigation_blocked"
    assert row["blocked"] == [{"host": "app.procore.com", "reason": "path_not_allowed"}]


async def test_a_navigation_cap_step_records_the_cap_error():
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [content()]})
    ex.guard.nav_count = ex.guard.max_navigations + 1
    await call(ex, "navigate", url=SEED)
    row = ctx.steps.of("tool.navigate")[0]["result"]
    assert row["error"] == "navigation_cap" and "refs" not in row


async def test_unexpected_handler_errors_become_a_fixed_code():
    callback = "https://app.procore.com/auth/procore/callback?code=decoy_query=1&state=DECOY-PAGE-TEXT"
    ex, ctx, _page, _sleeps, identity = make_executor({SEED: [content()]})

    async def refuse(page):
        raise ValueError(f"Page.goto: net::ERR_BLOCKED_BY_CLIENT at {callback}")

    ex.guard.follow_redirects = refuse
    nav = await call(ex, "navigate", url=SEED)

    async def ensure(host):
        raise RuntimeError(f"Page.goto: net::ERR_ABORTED at {callback}")

    identity.ensure = ensure
    login = await call(ex, "ensure_login", host="app.procore.com")
    assert json.loads(nav.content) == {"error": "tool_failed", "detail": "ValueError"}
    assert json.loads(login.content) == {"error": "tool_failed", "detail": "RuntimeError"}
    text = json.dumps(ctx.steps.rows, default=str) + nav.content + login.content
    assert "callback" not in text and "decoy_query" not in text and "DECOY-PAGE-TEXT" not in text


async def test_an_old_ref_stays_stale_after_a_navigate():
    ex, _ctx, _page, _sleeps, _ = make_executor({SEED: [content(1)], HOME: [content(2)]})
    await call(ex, "navigate", url=SEED)
    await call(ex, "read_page")
    seen = ex.snapshot
    await call(ex, "navigate", url=HOME)
    assert ex.snapshot is seen
    with pytest.raises(Exception) as err:
        await ex._locator("ref_1")
    assert getattr(err.value, "code", None) == "stale_ref"


async def test_secret_leak_wins_over_a_progress_terminal():
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [BLANK], HOME: [BLANK]})
    await call(ex, "navigate", url=SEED)
    ctx.secrets.add("DECOY-TITLE", owner=str(ctx.job_id))
    out = await call(ex, "navigate", url=HOME)
    assert out.terminal.error_kind == "secret_leak_blocked"
    assert not ctx.steps.of("error.no_progress")
    ex, ctx, _page, _sleeps, _ = make_executor({SEED: [content()]})
    await call(ex, "navigate", url=SEED)
    await call(ex, "navigate", url=SEED)
    ctx.secrets.add("DECOY-TITLE", owner=str(ctx.job_id))
    out = await call(ex, "navigate", url=SEED)                         # would be same_page_reload
    assert out.terminal.error_kind == "secret_leak_blocked"
    assert not ctx.steps.of("error.no_progress")


class BlankingGuard(FakeGuard):
    """The guard refuses a redirect or route and sends the model page to about:blank."""

    def __init__(self, page, *, during_waits=False):
        super().__init__(FakeDiagnostics())
        self.page, self.during_waits, self.reported = page, during_waits, []

    def blank(self):
        self.page.url = "about:blank"
        self.reported.append(NS(url=SEED, reason="path_not_allowed", page="model"))

    async def follow_redirects(self, page):
        if not self.during_waits:
            self.blank()

    def take_violations(self, _who):
        out, self.reported = self.reported, []
        return out


@pytest.mark.parametrize("during_waits", [False, True])
async def test_a_guard_blank_is_a_block_not_a_blank_load(during_waits):
    ex, ctx, page, sleeps, _ = make_executor({SEED: [BLANK]})
    ex.jb.guard = guard = BlankingGuard(page, during_waits=during_waits)
    if during_waits:
        async def sleep(seconds):
            sleeps.calls.append(seconds)
            guard.blank()
        ex._sleep = sleep
    for _ in range(2):
        out = await call(ex, "navigate", url=SEED)
        body = json.loads(out.content)
        assert out.terminal is None and out.ok is False
        assert body["error"] == "navigation_blocked" and body["blocked"][0]["reason"] == "path_not_allowed"
    assert ex.progress.blank_loads == 0
    assert sleeps.calls == ([5, 5] if during_waits else [])   # one wait per load, then the blank is seen
    assert guard.diagnostics.emits == ["navigate", "navigate"]
    assert not ctx.steps.of("error.no_progress")


def test_not_rendering_detail_leaves_a_second_chance():
    detail = progress_mod.NOT_RENDERING_DETAIL
    assert "error_kind" not in detail and "different approved page" in detail


# 1.0.6: page_code_blocked finish -------------------------------------------------------------


class BlockedDiagnostics(FakeDiagnostics):
    def __init__(self, blocked):
        super().__init__(readable=True)
        self.blocked = blocked

    def page_code_blocked(self, probe):
        return self.blocked and probe is not None


@pytest.mark.parametrize("blocked", [True, False])
async def test_runner_finish_maps_a_blocked_page_code_to_a_fixed_reason(blocked):
    page = FakePage()
    jb = FakeJb(page, FakeGuard(BlockedDiagnostics(blocked)))
    ctx = make_ctx()
    ctx.browser_context = jb
    ctx.client = FakeLoopClient([{"tool": "finish", "args": {"outcome": "cannot_complete", "error_kind": "not_found",
                                                             "summary": "404 DECOY-PAGE-TEXT"}}])
    ctx.client.bind_host = lambda host: None
    result = await UserBrowserRunner().run(ctx)
    if blocked:
        assert_valid_finish(result)
        assert result.error == "page_code_blocked" and result.summary == SUMMARY["page_code_blocked"]
    else:
        assert result.error_kind == "not_found" and result.error is None
    assert len(ctx.client.turns) == 1

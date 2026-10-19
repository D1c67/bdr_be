"""The canary type end to end: claim -> fresh context -> scripted model turns
-> guarded browser -> finish (spec 12.4 M1)."""

import pytest

from .agent_bdr import AgentBdr
from .agent_harness import make_provider, needs_browser, run_job
from .procore_site import ProcoreSite

pytestmark = needs_browser

CANARY_URL = "https://bdr.test/agent-worker/v1/canary/page"


@pytest.fixture
def site():
    s = ProcoreSite().start()
    yield s
    s.stop()


@pytest.fixture
async def provider(site):
    p = await make_provider(site)
    yield p
    await p.stop()


async def test_canary_self_test_ok(site, provider, paths):
    bdr = AgentBdr()
    bdr.script = [("navigate", {"url": CANARY_URL}), ("read_page", {}), ("find_text", {"query": "canary ok"}),
                  ("finish", {"outcome": "nothing_to_do", "summary": "Page read"})]
    run = await run_job(bdr, provider, paths, "canary")
    assert run.outcome.terminal == "finish:succeeded"
    assert run.job.finish["outcome"] == "self_test_ok"
    page = bdr.last_result("call_2")
    assert "BDR canary ok" in page["text"]
    assert bdr.last_result("call_3")["count"] >= 1
    # The first turn is prev 0; each later turn carries the previous result and a screenshot.
    assert [r["prev_turn_no"] for r in bdr.turn_requests] == [0, 1, 2, 3]
    assert bdr.turn_requests[1]["screenshot_artifact_id"]
    assert bdr.turn_requests[1]["url"] == CANARY_URL
    kinds = [s["kind"] for s in run.steps(bdr)]
    assert {"tool.navigate", "tool.read_page", "tool.find_text", "tool.finish"} <= set(kinds)
    shots = bdr.artifacts_of("screenshot")
    assert shots and all(a["data"][:3] == b"\xff\xd8\xff" for a in shots)   # JPEG


async def test_canary_cannot_leave_its_page(site, provider, paths):
    bdr = AgentBdr()
    bdr.script = [("navigate", {"url": "https://app.procore.com/3167322/project/home"}),
                  ("navigate", {"url": "https://bdr.test/agent-worker/v1/other"}),
                  ("finish", {"outcome": "nothing_to_do", "summary": "done"})]
    run = await run_job(bdr, provider, paths, "canary")
    assert bdr.last_result("call_1")["error"] == "navigation_refused"
    assert bdr.last_result("call_2")["error"] == "navigation_refused"
    assert not site.seen(host="app.procore.com")
    # The page never showed the canary text: the self-test fails.
    assert run.outcome.terminal == "finish:failed"
    assert run.job.finish["error_kind"] == "cannot_complete"


async def test_canary_rejects_tools_outside_its_set(site, provider, paths):
    bdr = AgentBdr()
    bdr.script = [("ensure_login", {"host": "app.procore.com"}), ("click", {"ref": "ref_1"}),
                  ("finish", {"outcome": "captured", "summary": "x"}),
                  ("finish", {"outcome": "cannot_complete", "summary": "gave up", "error_kind": "other"})]
    run = await run_job(bdr, provider, paths, "canary")
    assert bdr.last_result("call_1") == {"error": "invalid_call", "detail": "tool_not_allowed"}
    assert bdr.last_result("call_2")["error"] == "invalid_call"
    assert bdr.last_result("call_3") == {"error": "invalid_call", "detail": "captured_not_supported_for_type"}
    assert run.outcome.terminal == "finish:failed"
    assert run.job.finish["error_kind"] == "other"

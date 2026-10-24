"""scripts/plans_reader_loop_replay.py: stored tool-loop rounds replayed at
$0 (docs/AI_ESTIMATING_BUILD_SPEC.md section 6 WS2 step 13).

The stored rows come from the real loop (plans_reader_toolloop.run_loop
over FakeLoopStore with the scripted OpenAI transport of the loop suite),
so the replay is pinned against the exact row shapes the loop writes.

Pinned here:
  match        re-running every read tool against unchanged services
               matches every stored call (text, typed result, citations,
               views), the pre-call stage included, with zero model calls;
  mismatch     a service that now answers differently is reported per call
               with the differing fields and a unified text diff;
  skips        write tools are never re-run (their handler is not called)
               but still count toward their call limit; loop refusals,
               pending rows and unknown tools are classified, not re-run;
  offline      a call whose service is unavailable in the replay is
               `unavailable_now`, not a mismatch;
  pricing      a pricing tool mismatch withholds its text diff;
  bundle       rows written as <bundle>/loops/*.json replay the same, and
               the CLI refuses a bundle without them; the db mode refuses a
               database that is not dev.
No network, no database, synthetic data only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.services import plans_reader_tools as tools
from app.services.plans_reader_loop_store import FakeLoopStore
from app.services.plans_reader_toolloop import PreCall
from tests.test_plans_reader_toolloop import FakeOpenAI, _run_openai, quiet  # noqa: F401

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plans_reader_loop_replay.py"


def _load():
    if "plans_reader_loop_replay" in sys.modules:
        return sys.modules["plans_reader_loop_replay"]
    spec = importlib.util.spec_from_file_location("plans_reader_loop_replay", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plans_reader_loop_replay"] = mod
    spec.loader.exec_module(mod)
    return mod


rp = _load()

GROUP = "77777777-7777-4777-8777-777777777777"
KEY = f"group:{GROUP}"


def _schema(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


class World:
    """The 'documents' the read tools look at: a mutable table so a test can
    change what a tool would now answer."""

    def __init__(self) -> None:
        self.counts = {"a": 3, "b": 5, "c": 8}
        self.unavailable = False
        self.writes: list[dict] = []
        self.price = 0.5

    def probe(self, ctx: tools.ToolContext, args: dict) -> tools.ToolResult:
        if self.unavailable:
            return tools.ToolResult.ok(tools.unavailable_result("census", "offline"))
        q = args["q"]
        return tools.ToolResult.ok({"q": q, "count": self.counts[q]}, citations=[f"c:1a2b3c4dp1h{q}0000000"])

    def snap(self, ctx: tools.ToolContext, args: dict) -> tools.ToolResult:
        view = {"view_id": f"v{args['page']}", "ref": f"v:v{args['page']}", "bbox": [0, 0, 0.1, 0.1],
                "w_px": 10, "h_px": 10, "image": b"png-bytes"}
        return tools.ToolResult.ok({"views": [{k: v for k, v in view.items() if k != "image"}]},
                                   citations=[view["ref"]], views=[view])

    def mark(self, ctx: tools.ToolContext, args: dict) -> tools.ToolResult:
        self.writes.append({"key": ctx.write_key, **args})
        return tools.ToolResult.ok({"ok": True})

    def price_lookup(self, ctx: tools.ToolContext, args: dict) -> tools.ToolResult:
        return tools.ToolResult.ok({"status": "ok", "labor_hours": {"median": self.price}})


@pytest.fixture
def world():
    saved = dict(tools.REGISTRY)
    tools.REGISTRY.clear()
    w = World()
    kinds = tools.kinds_from_codes("RC, RV")
    specs = [
        tools.register(tools.ToolSpec("probe", "Probe a tag count.", _schema({"q": {"type": "string"}}), {}, "read", kinds, w.probe)),
        tools.register(tools.ToolSpec("snap", "Snap a view.", _schema({"page": {"type": "integer"}}), {}, "read", kinds, w.snap, cost_class="image")),
        tools.register(tools.ToolSpec("mark", "Write a mark.", _schema({"n": {"type": "integer"}}), {}, "write", tools.kinds_from_codes("RV"), w.mark, max_calls_per_group=1)),
        tools.register(tools.ToolSpec("price_lookup", "Unit price.", _schema({"d": {"type": "string"}}), {}, "read", kinds, w.price_lookup, internal_pricing=True)),
    ]
    w.specs = specs
    yield w
    tools.REGISTRY.clear()
    tools.REGISTRY.update(saved)
    tools.reset_services()


POLICY = {
    1: [("probe", {"q": "a"}), ("mark", {"n": 1})],
    2: [("snap", {"page": 8}), ("probe", {"q": "b"}), ("price_lookup", {"d": "duplex"})],
    3: [("probe", {"q": "nope_bad_arg_is_fine"}), ("mark", {"n": 2})],
    4: "final",
}


def _stored(monkeypatch, world) -> FakeLoopStore:
    store = FakeLoopStore()
    world.counts["nope_bad_arg_is_fine"] = 13
    result, _client = _run_openai(
        monkeypatch, store, world.specs, POLICY, kind="review",
        precalls=[PreCall("probe", {"q": "c"}, label="pre")],
        tool_ctx=tools.ToolContext(run_id="run", kind="review", services=tools.fake_services()),
    )
    assert result.status == "done"
    return store


def _replay(store, **kw):
    rows = rp.rows_from_store(store, key=KEY, run_id="run", kind="review")
    return rp.replay_rows(rows, services=tools.fake_services(), **kw)


def test_unchanged_services_match_every_read_call_at_zero_cost(quiet, monkeypatch, world):  # noqa: F811
    store = _stored(monkeypatch, world)
    writes_before = len(world.writes)
    report = _replay(store)
    c = report["counts"]
    assert report["model_calls_made"] == 0 and report["schema"] == rp.REPORT_SCHEMA
    # pre-call probe + 3 probes + snap + price_lookup re-run; mark twice (the
    # second refused by its own limit of 1) is never re-run.
    assert (c["match"], c["mismatch"], c["skipped_write"]) == (6, 0, 2)
    assert report["rerun"] == 6 and report["match_ratio"] == 1.0 and report["precall_calls"] == 1
    assert len(world.writes) == writes_before  # no write handler ran
    assert [r["round_no"] for r in report["rounds"]] == [1, 2, 3, 4]
    assert report["rounds"][-1]["answered"] is True and report["rounds"][-1]["tool_calls"] == 0
    assert report["provider"] == "openai"
    assert all(r["status"] == "done" for r in report["rounds"])
    assert report["spent_usd"] > 0  # the dev round costs, read from the rows


def test_a_changed_service_is_reported_with_fields_and_a_text_diff(quiet, monkeypatch, world):  # noqa: F811
    store = _stored(monkeypatch, world)
    world.counts["b"] = 6
    world.price = 0.75
    report = _replay(store)
    bad = [e for e in report["calls"] if e["outcome"] == "mismatch"]
    assert sorted(e["tool"] for e in bad) == ["price_lookup", "probe"]
    probe = next(e for e in bad if e["tool"] == "probe")
    assert probe["round_no"] == 2 and probe["fields"] == ["result_text", "result_data"]
    assert any(line.startswith("-") and '"count":5' in line for line in probe["text_diff"])
    assert any(line.startswith("+") and '"count":6' in line for line in probe["text_diff"])
    price = next(e for e in bad if e["tool"] == "price_lookup")
    assert price["text_diff"] == ["[pricing tool: text diff withheld; rerun with --show-pricing under EVAL_ROOT]"]
    shown = _replay(store, show_pricing=True)
    price = next(e for e in shown["calls"] if e["tool"] == "price_lookup")
    assert any("0.75" in line for line in price["text_diff"])
    assert report["match_ratio"] == round(4 / 6, 4)
    lines = rp.summary_lines(report)
    assert any("MISMATCH round 2" in line and "probe" in line for line in lines)


def test_an_unavailable_service_is_not_a_mismatch(quiet, monkeypatch, world):  # noqa: F811
    store = _stored(monkeypatch, world)
    world.unavailable = True
    report = _replay(store)
    assert report["counts"]["unavailable_now"] == 4 and report["counts"]["mismatch"] == 0
    assert {e["service"] for e in report["calls"] if e["outcome"] == "unavailable_now"} == {"census"}


def test_loop_refusals_pending_rows_unknown_tools_and_missing_rows_are_classified(world):
    rows = rp.LoopRows(
        key=KEY, run_id="run", kind="review",
        rounds=[{"round_no": 1, "status": "done", "cost_usd": 0.01, "tool_calls": [
            {"call_id": "k1", "name": "probe"}, {"call_id": "k2", "name": "probe"}, {"call_id": "k3", "name": "gone"},
            {"call_id": "k4", "name": "probe"}, {"call_id": "k5", "name": "probe"}, {"call_id": "k6", "name": "probe"}]}],
        tool_calls=[
            {"round_no": 1, "call_id": "k1", "tool_name": "probe", "args": {"q": "a"}, "refused": True,
             "result_text": "x", "result_data": {"refused": True, "code": "max_calls", "detail": {"scope": "loop"}}},
            {"round_no": 1, "call_id": "k2", "tool_name": "probe", "args": {"q": "a"}, "result_text": None,
             "result_data": {"pending": True}},
            {"round_no": 1, "call_id": "k3", "tool_name": "gone", "args": {}, "result_text": "{}", "result_data": {}},
            {"round_no": 1, "call_id": "k4", "tool_name": "probe", "args": {"q": "a"}, "refused": True,
             "result_text": "x", "result_data": {"refused": True, "code": "final_round"}},
            {"round_no": 1, "call_id": "k5", "tool_name": "probe", "args": {"q": 7}, "refused": True,
             "result_text": "", "result_data": {}},
        ],
    )
    report = rp.replay_rows(rows, services=tools.fake_services())
    by_id = {e["call_id"]: e["outcome"] for e in report["calls"]}
    assert by_id == {"k1": "loop_refusal", "k2": "pending", "k3": "unknown_tool", "k4": "loop_refusal",
                     "k5": "mismatch", "k6": "missing_row"}
    assert report["rounds"][0]["missing_call_ids"] == ["k6"]
    assert rp.is_loop_refusal(rows.tool_calls[0]) and not rp.is_loop_refusal(
        {"refused": True, "result_data": {"code": "max_calls", "detail": {"calls_made": 2}}}
    )


def test_bundle_rows_replay_the_same_and_the_cli_reports(quiet, monkeypatch, world, tmp_path, capsys):  # noqa: F811
    store = _stored(monkeypatch, world)
    bundle = tmp_path / "bundle"
    rounds_path, calls_path = rp.bundle_loop_paths(bundle)
    rounds_path.parent.mkdir(parents=True)
    other = {"round_no": 1, "status": "done", "group_id": "other", "tool_calls": []}
    rounds_path.write_text(json.dumps(store.rounds(KEY) + [other], default=str))
    calls_path.write_text(json.dumps(store.tool_calls(KEY), default=str))
    rows = rp.rows_from_bundle(bundle, key=KEY, run_id="run", kind="review")
    assert len(rows.rounds) == 4  # the other key's round is filtered out
    report = rp.replay_rows(rows, services=tools.fake_services())
    assert report["counts"]["match"] == 6 and report["counts"]["mismatch"] == 0

    for name in ("run.json", "files.json", "documents.json", "groups.json"):
        (bundle / name).write_text(json.dumps({"id": "run"} if name == "run.json" else []))
    out = tmp_path / "report.json"
    monkeypatch.setattr(rp, "register_registry_tools", lambda: None)
    assert rp.main(["bundle", "--bundle", str(bundle), "--group", GROUP, "--kind", "review", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["counts"]["match"] == 6
    world.counts["a"] = 99
    assert rp.main(["bundle", "--bundle", str(bundle), "--group", GROUP, "--kind", "review", "--strict"]) == 1
    printed = capsys.readouterr().out
    assert "MISMATCH" in printed and "match ratio" in printed


def test_the_cli_refuses_missing_rows_two_keys_and_a_non_dev_database(tmp_path, monkeypatch, capsys):
    bundle = tmp_path / "b"
    for name in ("run.json", "files.json", "documents.json", "groups.json"):
        bundle.mkdir(exist_ok=True)
        (bundle / name).write_text(json.dumps({"id": "run"} if name == "run.json" else []))
    monkeypatch.setattr(rp, "register_registry_tools", lambda: None)
    assert rp.main(["bundle", "--bundle", str(bundle), "--group", GROUP]) == 2
    assert "export command" in capsys.readouterr().err
    assert rp.main(["bundle", "--bundle", str(bundle), "--group", GROUP, "--message", "m"]) == 2
    from types import SimpleNamespace

    from app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(supabase_url="https://zxqkqotlcgbalwoqeeca.supabase.co"))
    assert rp.main(["db", "--group", GROUP]) == 2
    assert "dev project" in capsys.readouterr().err


def test_export_writes_both_files_without_image_bytes(tmp_path):
    class Q:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *_a):
            return self

        def eq(self, *_a):
            return self

        def order(self, *_a):
            return self

        def range(self, start, end):
            self._slice = (start, end)
            return self

        def execute(self):
            from types import SimpleNamespace

            s, e = self._slice
            return SimpleNamespace(data=self.rows[s : e + 1])

    tables = {
        "plans_reader_loop_rounds": [{"round_no": 1, "group_id": GROUP, "assistant_raw": {"raw": [{"image": "AAAA"}]}}],
        "plans_reader_tool_calls": [{"round_no": 1, "call_id": "c1", "views": [{"view_id": "v1", "image": "AAAA"}]}],
    }
    sb = type("SB", (), {"table": lambda self, name: Q(tables[name])})()
    rounds_path, calls_path, n_rounds, n_calls = rp.export_loop_rows(sb, "run", tmp_path / "bundle")
    assert (n_rounds, n_calls) == (1, 1)
    assert "image" not in rounds_path.read_text() and "image" not in calls_path.read_text()
    assert json.loads(calls_path.read_text())[0]["views"] == [{"view_id": "v1"}]

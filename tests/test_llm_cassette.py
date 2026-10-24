"""services/llm_cassette.py through llm._guarded (spec 0.4 rule 6, WS0
step 11).

Pinned here:
  hit          a recorded call costs 0 in both logs: no llm_call_log row,
               result cost_usd 0 and last_cost_usd() 0 (what callers write
               into plans_reader_calls), usage tagged cassette, and the
               provider is never called; the eval ledger is not consulted.
  modes        record_missing records on a miss and replays after; record
               always calls live; replay raises CassetteMiss on a miss
               (permanent) and never calls the provider; off reads nothing.
  fingerprint  bytes and data URLs are hashed, never stored; volatile top
               level keys do not change the key; nested schema keys do.
  turns        complete_turn ModelTurns (tool calls, response ids) replay.
  prod         any mode but off refuses in production.

No network, no model call, no database.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import eval_ledger, llm, llm_cassette, llm_errors, llm_gate, llm_queue
from app.services import llm_parts as lp


class FakeOpenAI:
    def __init__(self, text='{"ok": true}', output=()):
        self.calls: list[dict] = []
        self.text = text
        self.output = list(output)
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            input_tokens=2000, input_tokens_details=SimpleNamespace(cached_tokens=500),
            output_tokens=300, output_tokens_details=SimpleNamespace(reasoning_tokens=100),
        )
        return SimpleNamespace(
            id=f"resp_{len(self.calls)}", status="completed", incomplete_details=None,
            output_text=self.text, output=self.output, usage=usage,
        )

    def with_options(self, **kw):
        return self


@pytest.fixture
def env(monkeypatch, tmp_path):
    llm_gate.reset_gates()
    rows: list[dict] = []
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: rows.append(kw))
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    fake = FakeOpenAI()
    monkeypatch.setattr(llm, "_client_for", lambda route, s: fake)
    yield SimpleNamespace(rows=rows, fake=fake, dir=tmp_path)
    llm_gate.reset_gates()


def _s(mode: str, directory, **over) -> Settings:
    base = dict(
        _env_file=None, openai_api_key="o-key", plans_reader_enabled=True,
        llm_cassette_mode=mode, llm_cassette_dir=str(directory),
    )
    base.update(over)
    return Settings(**base)


def _parts_call(s, text="count the panels"):
    return llm.complete_parts(
        "plans_sub", system="sys", parts=[{"type": "input_text", "text": text}],
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                "additionalProperties": False},
        settings=s,
    )


def _cassettes(directory):
    return sorted(directory.glob("*.json"))


def test_record_missing_records_then_a_hit_costs_zero_in_both_logs(env):
    s = _s("record_missing", env.dir)
    live = _parts_call(s)
    assert len(env.fake.calls) == 1 and len(env.rows) == 1
    assert live.cost_usd is not None and live.cost_usd > 0
    assert llm.last_call_replayed() is False
    assert len(_cassettes(env.dir)) == 1

    hit = _parts_call(s)
    assert len(env.fake.calls) == 1, "a hit never reaches the provider"
    assert len(env.rows) == 1, "a hit writes no llm_call_log row"
    assert hit.cost_usd == Decimal("0") and llm.last_cost_usd() == Decimal("0")
    assert isinstance(llm.last_usage(), llm.CassetteUsage) and llm.last_usage().source == "cassette"
    assert llm.last_call_replayed() is True
    assert hit.usage.input_tokens == live.usage.input_tokens == 2000
    assert hit.parsed == live.parsed == {"ok": True}
    assert hit.response_id == live.response_id == "resp_1"
    index = (env.dir / "index" / "no_sweep.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in index] == ["miss", "record", "hit"]

    # A different prompt misses and goes live.
    _parts_call(s, text="count the switches")
    assert len(env.fake.calls) == 2 and len(_cassettes(env.dir)) == 2


def test_replay_raises_cassette_miss_and_never_calls_the_provider(env):
    with pytest.raises(llm_errors.CassetteMiss) as e:
        _parts_call(_s("replay", env.dir))
    assert env.fake.calls == [] and env.rows == []
    assert e.value.key and len(e.value.key) == 64
    assert llm_errors.classify(e.value) == llm_errors.KIND_NOT_CONFIGURED
    assert not llm_errors.is_transient(e.value)
    # Once recorded, replay serves it at $0.
    _parts_call(_s("record", env.dir))
    got = _parts_call(_s("replay", env.dir))
    assert got.cost_usd == Decimal("0") and len(env.fake.calls) == 1


def test_record_mode_always_calls_live(env):
    s = _s("record", env.dir)
    _parts_call(s)
    _parts_call(s)
    assert len(env.fake.calls) == 2 and len(env.rows) == 2
    assert len(_cassettes(env.dir)) == 1
    # LLM_CASSETTE_DIR is the cassettes directory itself ($EVAL_ROOT/cassettes)
    assert not (env.dir / "cassettes").exists() and (env.dir / "index").is_dir()
    assert llm_cassette.cassette_path(env.dir, "ab" * 32) == env.dir / f"{'ab' * 32}.json"


def test_off_mode_touches_nothing(env):
    _parts_call(_s("off", ""))
    assert not _cassettes(env.dir) and not (env.dir / "index").exists()
    assert llm_cassette.begin("plans_sub", SimpleNamespace(provider="openai", model="m"),
                              SimpleNamespace(llm_cassette_mode="off"), {"x": 1}) is llm_cassette.OFF_TAPE


def test_a_hit_needs_no_open_sweep(env):
    """Replay is $0, so the ledger is never consulted on a hit."""
    _parts_call(_s("record", env.dir))
    store = eval_ledger.MemoryLedgerStore()
    eval_ledger.use_store(store)
    try:
        enforced = _s("replay", env.dir, ai_eval_ledger_enforce=True)
        assert _parts_call(enforced).cost_usd == Decimal("0")
        # A live call in the same process would be refused (no open sweep).
        with pytest.raises(llm_errors.EvalBudgetRefused):
            _parts_call(_s("off", "", ai_eval_ledger_enforce=True), text="new prompt")
    finally:
        eval_ledger.use_store(None)


def test_image_bytes_are_hashed_never_stored(env):
    img = b"\x89PNG\r\n\x1a\n" + b"IMAGEBYTES" * 50
    s = _s("record_missing", env.dir)
    llm.complete_images_json(
        "bid_split", prompt="classify", images=[("Page 1", img)], schema={"type": "object"},
        schema_name="pages", settings=s.model_copy(update={"bid_split_llm_provider": "openai"}),
    )
    text = _cassettes(env.dir)[0].read_text()
    assert base64.b64encode(img).decode()[:40] not in text
    assert "IMAGEBYTES" not in text


def test_fingerprint_drops_volatile_keys_but_keeps_nested_ones():
    a = llm_cassette.fingerprint("plans_sub", "openai", "m", {"op": "parts", "input": "x", "prompt_cache_key": "run-1", "timeout": 30})
    b = llm_cassette.fingerprint("plans_sub", "openai", "m", {"op": "parts", "input": "x", "prompt_cache_key": "run-2"})
    assert llm_cassette.key(a) == llm_cassette.key(b)
    schema1 = {"type": "object", "properties": {"timeout": {"type": "number"}}}
    schema2 = {"type": "object", "properties": {"user": {"type": "number"}}}
    k1 = llm_cassette.key(llm_cassette.fingerprint("f", "openai", "m", {"schema": schema1}))
    k2 = llm_cassette.key(llm_cassette.fingerprint("f", "openai", "m", {"schema": schema2}))
    assert k1 != k2
    url1 = "data:image/png;base64," + base64.b64encode(b"one").decode()
    url2 = "data:image/png;base64," + base64.b64encode(b"two").decode()
    c1 = llm_cassette.canonical({"image_url": url1})
    assert c1["image_url"].startswith("data:image/png;base64;sha256=")
    assert c1 != llm_cassette.canonical({"image_url": url2})
    # Evidence refs on parts never change the key; the pixels do.
    p1 = lp.ImagePart(png=b"abc", ref={"document_id": "d1"}, view_id="v1")
    p2 = lp.ImagePart(png=b"abc", ref={"document_id": "d2"}, view_id="v9")
    assert llm_cassette.canonical(p1) == llm_cassette.canonical(p2)
    assert llm_cassette.canonical(p1) != llm_cassette.canonical(lp.ImagePart(png=b"abd"))


def test_complete_turn_model_turn_replays_with_tool_calls(env):
    env.fake.text = ""
    env.fake.output = [{"type": "function_call", "call_id": "call_1", "name": "sheet_index", "arguments": "{}"}]
    req = lp.ModelRequest(
        feature="plans_recount", system="sys",
        messages=(lp.UserTurn(parts=(lp.TextPart(text="recount E1"),)),),
        tools=(lp.ToolDef(name="sheet_index", description="d", schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False}),),
        max_tokens=4000,
    )
    s = _s("record_missing", env.dir)
    live = llm.complete_turn("plans_recount", req, settings=s)
    assert live.stop_reason == "tool_use" and live.cost_usd > 0
    hit = llm.complete_turn("plans_recount", req, settings=s)
    assert len(env.fake.calls) == 1 and len(env.rows) == 1
    assert hit.cost_usd == Decimal("0") and hit.stop_reason == "tool_use"
    assert hit.tool_calls == live.tool_calls and hit.response_id == live.response_id
    assert isinstance(hit.usage, llm.CassetteUsage)
    assert hit.assistant_turn().tool_calls[0].name == "sheet_index"
    # A timeout difference does not change the key; the effort does.
    llm.complete_turn("plans_recount", lp.ModelRequest(**{**req.__dict__, "timeout": 99.0}), settings=s)
    assert len(env.fake.calls) == 1
    llm.complete_turn("plans_recount", lp.ModelRequest(**{**req.__dict__, "effort": "high"}), settings=s)
    assert len(env.fake.calls) == 2


def test_text_and_json_results_round_trip(env):
    env.fake.text = "plain answer"
    s = _s("record_missing", env.dir, openai_proposal_model="gpt-6-astra")
    msgs = [{"role": "user", "content": "hi"}]
    assert llm.complete_text("proposal", system="s", messages=msgs, settings=s) == "plain answer"
    assert llm.complete_text("proposal", system="s", messages=msgs, settings=s) == "plain answer"
    assert len(env.fake.calls) == 1
    env.fake.text = '{"a": [1, 2]}'
    assert llm.complete_json("proposal", system="s", messages=msgs, settings=s) == {"a": [1, 2]}
    assert llm.complete_json("proposal", system="s", messages=msgs, settings=s) == {"a": [1, 2]}
    assert len(env.fake.calls) == 2


def test_cassettes_refuse_production(tmp_path):
    route = SimpleNamespace(provider="openai", model="m")
    prod = SimpleNamespace(llm_cassette_mode="replay", llm_cassette_dir=str(tmp_path), is_production=True)
    with pytest.raises(llm_errors.CassetteMiss):
        llm_cassette.begin("plans_sub", route, prod, {"x": 1})
    off = SimpleNamespace(llm_cassette_mode="off", is_production=True)
    assert llm_cassette.begin("plans_sub", route, off, {"x": 1}) is llm_cassette.OFF_TAPE


def test_corrupt_cassette_is_a_miss(env):
    s = _s("record_missing", env.dir)
    _parts_call(s)
    path = _cassettes(env.dir)[0]
    path.write_text("{not json")
    with pytest.raises(llm_errors.CassetteMiss):
        _parts_call(_s("replay", env.dir))

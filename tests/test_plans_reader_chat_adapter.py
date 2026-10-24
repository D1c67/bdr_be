"""Registry tools offered to the existing chat loop through
services/plans_reader_chat_adapter.py (docs/AI_ESTIMATING_BUILD_SPEC.md
section 6 WS2 step 12, 5.2 kind C and Pro Max, 5.5 chat visibility).

Pinned here:
  flags off   with every parent flag off nothing is offered, no profile is
              read, and the chat loop's tool list is exactly the legacy one;
  pricing     pricing tools are hidden from the Accountant, from an
              engineer while AI_PRICING_STATE=test, and whenever
              AI_PRICING_CHAT_TOOLS_ENABLED is off; shown to Executive (test)
              and to an engineer (live); serving one marks the message
              pricing_restricted;
  Pro Max     needs_pro_max tools only with Pro Max on; their views ride
              along as input images and count against the page cap;
  writes      write tools only for a writer role whose message asks for a
              change; review tools never;
  gates       a tool without a parent entry is never offered (fail closed),
              a legacy name is never shadowed, the chat route's tools flag
              off offers nothing;
  serving     dispatch through the registry: strict argument refusals,
              per-message call limits across rounds, citations in the
              summary;
  chat loop   with the WS8 hook simulated (chat_tools and serve_tool
              wrapped), plans_reader_chat.run_message serves a registry
              tool and still answers.
No network, no database, no model, synthetic data only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import features
from app.core.config import Settings
from app.services import llm
from app.services import plans_reader_chat as chat
from app.services import plans_reader_chat_adapter as adapter
from app.services import plans_reader_measure as measure  # registers its tools before `registry` saves REGISTRY
from app.services import plans_reader_tools as tools
from app.services.plans_reader_schemas import CHAT_TOOLS
from tests.test_plans_reader_chat import FakeLLM, _clock, _db, _msg, _pending, _result, spend  # noqa: F401

RUN = "run-1"


def _schema(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def _ok(data: dict, *refs: str) -> tools.ToolResult:
    return tools.ToolResult.ok(data, citations=list(refs))


SPECS = {
    "census": tools.ToolSpec(
        "census", "Count a tag on a page.", _schema({"tag": {"type": "string"}}), {}, "read",
        tools.kinds_from_codes("RC, M, RV, C"),
        lambda ctx, a: _ok({"count": 7, "tag": a["tag"]}, "c:abc12345p3h000000000"), max_calls_per_group=2,
    ),
    "price_lookup": tools.ToolSpec(
        "price_lookup", "Unit price of an item.", _schema({"description": {"type": "string"}}), {}, "read",
        tools.kinds_from_codes("M, RV, RS, C"),
        lambda ctx, a: _ok({"status": "ok", "labor_hours": {"median": 0.5}}), internal_pricing=True,
    ),
    "zoom_region": tools.ToolSpec(
        "zoom_region", "Zoom into a page region.", _schema({"page": {"type": "integer"}}), {}, "read",
        tools.kinds_from_codes("RC, RV, C"),
        lambda ctx, a: tools.ToolResult.ok(
            {"views": [{"view_id": "v1", "bbox": [0, 0, 0.5, 0.5]}]}, citations=["v:v1"],
            views=[{"view_id": "v1", "document_id": "doc-e", "page": a["page"], "bbox": [0, 0, 0.5, 0.5],
                    "w_px": 64, "h_px": 48, "ppi": 150, "image": b"\x89PNG-synthetic"}],
        ),
        cost_class="image", needs_pro_max=True,
    ),
    "set_linear_item": tools.ToolSpec(
        "set_linear_item", "Override a linear item length.", _schema({"tag": {"type": "string"}}), {}, "write",
        tools.kinds_from_codes("D, C"), lambda ctx, a: _ok({"ok": True, "key": ctx.write_key}),
    ),
    "record_finding": tools.ToolSpec(
        "record_finding", "Record a review finding.", _schema({"claim": {"type": "string"}}), {}, "review",
        frozenset({"review", "chat"}), lambda ctx, a: _ok({"finding_id": "f1"}),
    ),
    "mystery_tool": tools.ToolSpec(
        "mystery_tool", "A tool nobody named a parent flag for.", _schema({}), {}, "read",
        tools.kinds_from_codes("C"), lambda ctx, a: _ok({"ok": True}),
    ),
    "get_output": tools.ToolSpec(
        "get_output", "A registry tool shadowing a legacy chat tool name.", _schema({}), {}, "read",
        tools.kinds_from_codes("C"), lambda ctx, a: _ok({"shadow": True}),
    ),
}

ALL_ON = dict(
    plans_reader_enabled=True,
    ai_estimating_state="live",
    plans_reader_page_facts_enabled=True,
    plans_reader_derive_enabled=True,
    ai_pricing_enabled=True,
    ai_pricing_chat_tools_enabled=True,
    ai_pricing_state="live",
)


@pytest.fixture
def registry():
    saved = dict(tools.REGISTRY)
    tools.REGISTRY.clear()
    for spec in SPECS.values():
        tools.register(spec)
    adapter.set_services_factory(lambda ctx: tools.fake_services())
    yield
    tools.REGISTRY.clear()
    tools.REGISTRY.update(saved)
    adapter.set_services_factory(None)
    adapter.TOOL_PARENTS.pop("mystery_tool", None)


def _settings(monkeypatch, **values) -> Settings:
    s = Settings(_env_file=None, openai_api_key="test-key", **values)
    monkeypatch.setattr(features, "get_settings", lambda: s)
    return s


class _NoDB:
    """A client that must not be touched."""

    def table(self, name):  # pragma: no cover - reaching it fails the test
        raise AssertionError(f"the adapter read {name} with every flag off")


class _Profiles:
    def __init__(self, role: str | None):
        self.role = role
        self.reads = 0

    def table(self, name):
        assert name == "profiles"
        self.reads += 1
        rows = [{"role": self.role}] if self.role else []
        q = SimpleNamespace()
        q.select = lambda *a, **k: q
        q.eq = lambda *a, **k: q
        q.limit = lambda *a, **k: q
        q.execute = lambda: SimpleNamespace(data=rows)
        return q


def _ctx(s, *, role="executive", sender_role=None, pro_max=False, modify_allowed=False, sb=None):
    return SimpleNamespace(
        sb=sb if sb is not None else _Profiles(role),
        s=s,
        run={"id": RUN},
        message={"id": "m-1", "created_by": "u-1", "sender_role": sender_role},
        pro_max=pro_max,
        modify_allowed=modify_allowed,
        page_cap=2,
        pages_used=0,
        pending_parts=[],
        pending_image_bytes=0,
        pending_image_tokens=0,
        images_sent=[],
        documents=lambda: [],
    )


def _names(ctx) -> list[str]:
    return [t["name"] for t in adapter.tool_definitions(ctx)]


# -- flags off ---------------------------------------------------------------------


def test_every_parent_flag_off_offers_nothing_and_reads_nothing(monkeypatch, registry):
    s = _settings(monkeypatch, plans_reader_enabled=True)
    ctx = _ctx(s, sb=_NoDB())
    assert adapter.tool_definitions(ctx) == []
    assert adapter.handles(ctx, "census") is False and adapter.pricing_restricted(ctx) is False


def test_the_chat_route_tools_flag_off_offers_nothing(monkeypatch, registry):
    s = _settings(monkeypatch, **ALL_ON, plans_reader_routes='{"chat": {"tools": false}}')
    assert _names(_ctx(s)) == []


# -- pricing visibility ------------------------------------------------------------


def test_pricing_tools_are_hidden_from_the_accountant(monkeypatch, registry):
    s = _settings(monkeypatch, **ALL_ON)
    names = _names(_ctx(s, role="accountant"))
    assert "price_lookup" not in names and "census" in names
    assert adapter.pricing_allowed_for_message(_ctx(s, role="accountant")) is False
    assert "price_lookup" in _names(_ctx(s, role="estimating_engineer_labor"))  # live: engineers see prices


def test_pricing_tools_are_hidden_from_an_engineer_in_pricing_state_test(monkeypatch, registry):
    s = _settings(monkeypatch, **{**ALL_ON, "ai_pricing_state": "test"})
    for role in ("estimating_engineer_materials", "estimating_engineer_labor", "estimating_admin", "accountant"):
        assert "price_lookup" not in _names(_ctx(s, role=role)), role
    assert "price_lookup" in _names(_ctx(s, role="executive"))
    assert "price_lookup" in _names(_ctx(s, role="it_admin"))


def test_pricing_tools_need_the_chat_tools_flag_and_never_reach_the_estimator(monkeypatch, registry):
    s = _settings(monkeypatch, **{**ALL_ON, "ai_pricing_chat_tools_enabled": False})
    assert "price_lookup" not in _names(_ctx(s, role="executive"))
    s = _settings(monkeypatch, **ALL_ON)
    assert "price_lookup" not in _names(_ctx(s, role="estimator"))
    assert "price_lookup" not in _names(_ctx(s, role=None))  # unknown sender fails closed


def test_the_persisted_sender_role_wins_over_the_profile(monkeypatch, registry):
    s = _settings(monkeypatch, **{**ALL_ON, "ai_pricing_state": "test"})
    sb = _Profiles("executive")
    ctx = _ctx(s, sb=sb, sender_role="accountant")
    assert "price_lookup" not in _names(ctx) and sb.reads == 0
    assert adapter.sender_role_value(ctx) == "accountant"
    ctx = _ctx(s, sb=sb)
    assert "price_lookup" in _names(ctx) and sb.reads == 1
    adapter.offered_specs(ctx)
    assert sb.reads == 1  # resolved once per message


def test_serving_a_pricing_tool_marks_the_message_pricing_restricted(monkeypatch, registry):
    s = _settings(monkeypatch, **ALL_ON)
    ctx = _ctx(s, role="executive")
    assert adapter.handles(ctx, "price_lookup")
    text, summary = adapter.serve(ctx, "price_lookup", {"description": "duplex receptacle"})
    assert '"status":"ok"' in text and summary["pricing_restricted"] is True and summary["registry"] is True
    assert adapter.pricing_restricted(ctx) is True


# -- Pro Max ---------------------------------------------------------------------------


def test_pro_max_tools_need_pro_max_and_attach_their_views(monkeypatch, registry):
    s = _settings(monkeypatch, **ALL_ON)
    off = _ctx(s, pro_max=False)
    assert "zoom_region" not in _names(off) and adapter.handles(off, "zoom_region") is False
    on = _ctx(s, pro_max=True)
    assert "zoom_region" in _names(on)
    text, summary = adapter.serve(on, "zoom_region", {"page": 3})
    assert summary["result_summary"] == "ok, 1 views attached" and summary["citations"] == ["v:v1"]
    assert "1 view image(s) follow" in text
    kinds = [p["type"] for p in on.pending_parts]
    assert kinds == ["input_text", "input_image"]
    assert on.pages_used == 1 and on.pending_image_bytes > 0 and on.images_sent[0]["view_id"] == "v1"
    adapter.serve(on, "zoom_region", {"page": 4})
    _text, summary3 = adapter.serve(on, "zoom_region", {"page": 5})
    assert on.pages_used == 2  # the page cap of 2 holds across calls
    assert summary3["result_summary"] == "ok"


# -- writes, review, gates ---------------------------------------------------------


def test_write_tools_need_a_writer_role_and_a_change_request(monkeypatch, registry):
    s = _settings(monkeypatch, **ALL_ON)
    assert "set_linear_item" not in _names(_ctx(s, role="executive", modify_allowed=False))
    assert "set_linear_item" not in _names(_ctx(s, role="accountant", modify_allowed=True))
    ctx = _ctx(s, role="estimating_admin", modify_allowed=True)
    assert "set_linear_item" in _names(ctx)
    _text, summary = adapter.serve(ctx, "set_linear_item", {"tag": "L5"})
    assert summary["result_summary"] == "ok"
    assert adapter.state_for(ctx).tool_ctx.call_counts == {"set_linear_item": 1}


def test_review_tools_unparented_tools_and_legacy_names_are_never_offered(monkeypatch, registry):
    s = _settings(monkeypatch, **ALL_ON)
    names = _names(_ctx(s))
    assert "record_finding" not in names and "mystery_tool" not in names and "get_output" not in names
    assert names == sorted(names)
    adapter.set_tool_parents("mystery_tool", ["plans_reader_page_facts_enabled"])
    assert "mystery_tool" in _names(_ctx(s))
    with pytest.raises(ValueError):
        adapter.set_tool_parents("mystery_tool", [])


def test_parent_flags_gate_each_family(monkeypatch, registry):
    s = _settings(monkeypatch, plans_reader_enabled=True, ai_estimating_state="live", plans_reader_derive_enabled=True)
    names = _names(_ctx(s, role="estimating_admin", modify_allowed=True, pro_max=True))
    assert names == ["set_linear_item"]  # page facts off: census and zoom hidden; pricing off


# -- serving ---------------------------------------------------------------------------


def test_serve_refuses_bad_args_and_holds_the_per_message_call_limit(monkeypatch, registry):
    s = _settings(monkeypatch, **ALL_ON)
    ctx = _ctx(s)
    text, summary = adapter.serve(ctx, "census", {"tag": 3})
    assert summary["result_summary"] == "refused: invalid_args" and "invalid arguments" in text
    for _ in range(2):
        _t, summary = adapter.serve(ctx, "census", {"tag": "(X)"})
        assert summary["result_summary"] == "ok"
        assert summary["citations"] == ["c:abc12345p3h000000000"]
    _t, summary = adapter.serve(ctx, "census", {"tag": "(X)"})
    assert summary["result_summary"] == "refused: max_calls"
    assert "c:abc12345p3h000000000" in adapter.state_for(ctx).tool_ctx.ledger_refs
    assert adapter.state_for(ctx).tool_ctx.message_id == "m-1" and adapter.state_for(ctx).tool_ctx.kind == "chat"
    _t, summary = adapter.serve(ctx, "not_a_tool", {})
    assert summary["result_summary"] == "unknown tool"


# -- the chat loop with the WS8 hook simulated ------------------------------------------


def _hook(monkeypatch):
    """The two hook lines WS8 adds to plans_reader_chat (chat_tools appends
    the adapter's definitions; serve_tool hands registry names over)."""
    orig_tools, orig_serve = chat.chat_tools, chat.serve_tool
    monkeypatch.setattr(chat, "chat_tools", lambda ctx: orig_tools(ctx) + adapter.tool_definitions(ctx))

    def serve_tool(ctx, name, args):
        if adapter.handles(ctx, name):
            return adapter.serve(ctx, name, args)
        return orig_serve(ctx, name, args)

    monkeypatch.setattr(chat, "serve_tool", serve_tool)


def test_with_flags_off_the_hooked_chat_loop_sends_exactly_the_legacy_tools(monkeypatch, registry, spend):  # noqa: F811
    s = Settings(_env_file=None, openai_api_key="test-key", plans_reader_enabled=True, llm_queue_enabled=True)
    monkeypatch.setattr(chat, "get_settings", lambda: s)
    _hook(monkeypatch)
    db = _db()
    _t, _u, answer = _pending(db)
    fake = FakeLLM(_result(text="42 receptacles.", rid="r1"))
    monkeypatch.setattr(llm, "complete_parts", fake)
    chat.run_message(db, answer["id"])
    sent = [t["name"] for t in fake.calls[0]["tools"]]
    assert sent == [t["name"] for t in CHAT_TOOLS if t["name"] != chat.MODIFY_TOOL]
    assert _msg(db, answer["id"])["status"] == "done"


def test_the_hooked_chat_loop_serves_a_registry_tool(monkeypatch, registry, spend):  # noqa: F811
    s = Settings(_env_file=None, openai_api_key="test-key", llm_queue_enabled=True, **ALL_ON)
    monkeypatch.setattr(chat, "get_settings", lambda: s)
    monkeypatch.setattr(features, "get_settings", lambda: s)
    _hook(monkeypatch)
    db = _db()
    _t, _u, answer = _pending(db, content="How many (X) tags on ED3.01?")
    fake = FakeLLM(
        _result(tool_calls=[{"call_id": "c1", "name": "census", "arguments": {"tag": "(X)"}}], rid="r1"),
        _result(text="The census finds 7 (X) tags.", rid="r2"),
    )
    monkeypatch.setattr(llm, "complete_parts", fake)
    chat.run_message(db, answer["id"])
    first_tools = [t["name"] for t in fake.calls[0]["tools"]]
    assert "census" in first_tools and "price_lookup" in first_tools  # u1 is estimating_admin, pricing live
    assert first_tools[: len(CHAT_TOOLS) - 1] == [t["name"] for t in CHAT_TOOLS if t["name"] != chat.MODIFY_TOOL]
    (out,) = fake.calls[1]["tool_outputs"]
    assert out["call_id"] == "c1" and '"count":7' in out["output"]
    msg = _msg(db, answer["id"])
    assert msg["status"] == "done" and msg["content"] == "The census finds 7 (X) tags."
    (summary,) = msg["tool_calls"]
    assert summary["name"] == "census" and summary["registry"] is True and summary["result_summary"] == "ok"


# -- writer inputs on the chat ToolContext (wave 3 fix pass D2, spec 5.2) ----------


def test_the_chat_tool_context_carries_the_writer_inputs_and_write_key(monkeypatch, registry):
    s = _settings(monkeypatch, **ALL_ON)
    seen = {}

    def handler(ctx, a):
        seen.update(key=ctx.write_key, user_id=ctx.user_id, text=ctx.message_text, change=ctx.change_words, role=ctx.role)
        return _ok({"ok": True})

    tools.register(tools.ToolSpec("set_linear_item", "Override a linear item length.", _schema({"tag": {"type": "string"}}),
                                  {}, "write", tools.kinds_from_codes("D, C"), handler), replace=True)
    ctx = _ctx(s, role="estimating_admin", modify_allowed=True)
    ctx.question = "Set L5 to the 12 FT candidates"
    adapter.serve(ctx, "set_linear_item", {"tag": "L5"})
    assert seen == {"key": "m-1:c1", "user_id": "u-1", "text": "Set L5 to the 12 FT candidates", "change": True,
                    "role": "estimating_admin"}
    adapter.serve(ctx, "set_linear_item", {"tag": "L6"})
    assert seen["key"] == "m-1:c2"  # one key per call of the message


def test_the_measure_write_gate_passes_for_a_writer_with_change_words_only(monkeypatch, registry):
    """plans_reader_measure._writer_ok (the chat write gate) reads the
    attributes the adapter sets: a writer whose own message asks for a
    change passes; no change words, or a read-only role, is refused."""
    s = _settings(monkeypatch, **ALL_ON)
    ctx = _ctx(s, role="estimating_admin", modify_allowed=True)
    ctx.question = "Move panel LP-1 to the east wall"
    adapter.serve(ctx, "set_linear_item", {"tag": "L5"})
    tool_ctx = adapter.state_for(ctx).tool_ctx
    assert measure._writer_ok(tool_ctx) is None
    tool_ctx.message_text = "Where is panel LP-1?"
    assert measure._writer_ok(tool_ctx).code == "change_words_required"
    acct = _ctx(s, role="accountant", modify_allowed=True)
    acct.question = "Move panel LP-1 to the east wall"
    adapter.serve(acct, "census", {"tag": "(X)"})
    assert measure._writer_ok(adapter.state_for(acct).tool_ctx).code == "writer_role_required"
    # Before this fix the context had neither attribute and every chat write was refused.
    bare = tools.ToolContext(run_id=RUN, kind="chat", role="estimating_admin", message_id="m-1")
    assert measure._writer_ok(bare).code == "chat_write_unavailable"

"""AI Estimating chat visibility (plans_reader_chat, WS8 wave 3; spec 5.5):

- the pricing clause of the system prompt follows the SENDER's role
  (executive in `live` sees the allowed clause; the accountant, an engineer
  under `ai_pricing_state=test`, and an unknown sender get the restricted
  sentence);
- the sender's role is persisted on the message rows (send_message from the
  user's role; run_message from the profile of created_by);
- an answer that used an internal_pricing tool, or explain_row's price
  block, is tagged pricing_restricted and its content and tool calls are
  redacted on thread reads for viewers who fail ai_pricing_allowed;
- the WS2 adapter's registry tools are appended to CHAT_TOOLS and served
  through plans_reader_chat.serve_tool.

Everything is faked (the Responses transport, the registry services, the
spend cap); synthetic numbers only; no network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core import deps, features
from app.core.config import Settings
from app.core.roles import Role
from app.services import ai_spend, llm
from app.services import pricing_tools  # noqa: F401 - registers price_lookup and get_price_summary (WS6)
from app.services import plans_reader_chat as chat
from app.services import plans_reader_chat_adapter as adapter
from app.services import plans_reader_chat_tools as chat_tools
from app.services import plans_reader_tools as tools
from tests.test_plans_reader_chat import NOW, FakeLLM, RUN_ID, _db, _pending, _result

RUN_ID_ = RUN_ID


def _settings(**over) -> Settings:
    base = {
        "plans_reader_enabled": True, "ai_estimating_state": "live", "ai_pricing_enabled": True,
        "ai_pricing_state": "live", "ai_pricing_chat_tools_enabled": True, "llm_queue_enabled": False,
    }
    base.update(over)
    return Settings(_env_file=None, **base)


@pytest.fixture
def live(monkeypatch):
    """Pricing on and live for every module that reads settings."""
    s = _settings()
    for mod in (features, deps, chat, adapter, chat_tools):
        monkeypatch.setattr(mod, "get_settings", lambda: s, raising=False)
    from app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: s)
    monkeypatch.setattr(adapter, "chat_route_allows_tools", lambda settings=None: True)
    monkeypatch.setattr(ai_spend, "check_cap", lambda sb, estimated_usd: None)
    monkeypatch.setattr(ai_spend, "after_call", lambda sb: None)
    monkeypatch.setattr(chat, "_utcnow", lambda: NOW)
    adapter.set_services_factory(lambda ctx: tools.fake_services(
        price=tools.FakePriceService({"duplex receptacle": {"uom": "EA", "labor_hours": {"median": 0.4, "n_obs": 9}}})))
    chat_tools.set_sources_factory(None)
    yield s
    adapter.set_services_factory(None)
    chat_tools.set_sources_factory(None)


def _ctx(db, role: str | None = None, *, pro_max=False):
    thread, _user, answer = _pending(db, pro_max=pro_max)
    if role:
        answer["sender_role"] = role
        db.table("plans_reader_chat_messages").update({"sender_role": role}).eq("id", answer["id"]).execute()
    ctx = chat._Context(db, chat.get_settings(), db.rows("plans_reader_runs")[0], thread, answer)
    ctx.question = "How much?"
    return ctx


def _profiles(db, role: Role):
    db.tables["profiles"] = [{"id": "u1", "email": "e@g3.com", "role": role.value, "is_active": True}]


# -- The sender's clause --------------------------------------------------------


def test_pricing_clause_follows_the_senders_role(live):
    db = _db()
    exec_prompt = chat.build_system_prompt(_ctx(db, "executive"))
    assert "may see G3's internal AI pricing" in exec_prompt and "Never give prices" not in exec_prompt
    assert exec_prompt.startswith("You are G3 Electrical")
    for role in ("accountant", "estimator"):
        prompt = chat.build_system_prompt(_ctx(db, role))
        assert "Never give prices" in prompt and "internal AI pricing" not in prompt
    # No stored role and no profile for the sender: fails closed.
    ctx = _ctx(db, None)
    ctx.message["created_by"] = "nobody"
    assert "Never give prices" in chat.build_system_prompt(ctx)
    # The fixture profile of u1 (an Estimating Admin, a pricing viewer in live) decides otherwise.
    assert "internal AI pricing" in chat.build_system_prompt(_ctx(db, None))


def test_engineer_sees_the_restricted_clause_under_pricing_state_test(live, monkeypatch):
    s = _settings(ai_pricing_state="test")
    for mod in (features, deps, chat, adapter, chat_tools):
        monkeypatch.setattr(mod, "get_settings", lambda: s, raising=False)
    from app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: s)
    db = _db()
    assert "Never give prices" in chat.build_system_prompt(_ctx(db, "estimating_engineer_labor"))
    assert "internal AI pricing" in chat.build_system_prompt(_ctx(db, "it_admin"))


def test_pricing_clause_is_restricted_with_pricing_off(monkeypatch):
    db = _db()
    assert "Never give prices" in chat.build_system_prompt(_ctx(db, "executive"))


# -- sender_role persistence ----------------------------------------------------


def test_send_message_stores_the_senders_role_on_both_rows(live, monkeypatch):
    db = _db()
    run = db.rows("plans_reader_runs")[0]
    thread = chat.create_thread(db, run, title=None, user=SimpleNamespace(id="u1", role=Role.EXECUTIVE))
    monkeypatch.setattr(chat, "_dispatch", lambda sb, mid, uid: None)
    out = chat.send_message(db, run, thread, content="price?", pro_max=False, user=SimpleNamespace(id="u1", role=Role.EXECUTIVE))
    assert out["user_message"]["sender_role"] == "executive" and out["assistant_message"]["sender_role"] == "executive"
    rows = db.rows("plans_reader_chat_messages")
    assert all(r.get("sender_role") == "executive" for r in rows)
    # A user without a role stores none (the profile resolves it at answer time).
    thread2 = chat.create_thread(db, run, title=None, user=SimpleNamespace(id="u2"))
    out2 = chat.send_message(db, run, thread2, content="q", pro_max=False, user=SimpleNamespace(id="u2"))
    assert "sender_role" not in out2["user_message"]


def test_run_message_resolves_the_role_from_the_profile_and_tags_pricing_answers(live, monkeypatch):
    db = _db()
    _profiles(db, Role.EXECUTIVE)
    _thread, _user, answer = _pending(db, content="What is the price summary?")
    fake = FakeLLM(
        _result(tool_calls=[{"call_id": "c1", "name": "get_price_summary", "arguments": {}}]),
        _result(text="The draft total is unverified."),
    )
    monkeypatch.setattr(llm, "complete_parts", fake)
    chat.run_message(db, answer["id"])
    row = next(r for r in db.rows("plans_reader_chat_messages") if r["id"] == answer["id"])
    assert row["status"] == "done" and row["sender_role"] == "executive"
    assert row["pricing_restricted"] is True
    assert row["tool_calls"][0]["name"] == "get_price_summary" and row["tool_calls"][0]["pricing_restricted"] is True
    assert row["tool_calls"][0]["registry"] is True
    # The first call offered the registry pricing tools after the legacy ones.
    names = [t["name"] for t in fake.calls[0]["tools"]]
    legacy = [t["name"] for t in chat.CHAT_TOOLS if t["name"] != chat.MODIFY_TOOL]
    assert names[: len(legacy)] == legacy
    assert "get_price_summary" in names and "price_lookup" in names
    assert "internal AI pricing" in fake.calls[0]["system"]


def test_an_accountants_answer_offers_no_pricing_tool_and_is_not_tagged(live, monkeypatch):
    db = _db()
    _profiles(db, Role.ACCOUNTANT)
    _thread, _user, answer = _pending(db)
    fake = FakeLLM(_result(text="Quantities only."))
    monkeypatch.setattr(llm, "complete_parts", fake)
    chat.run_message(db, answer["id"])
    row = next(r for r in db.rows("plans_reader_chat_messages") if r["id"] == answer["id"])
    assert row["sender_role"] == "accountant" and not row.get("pricing_restricted")
    names = [t["name"] for t in fake.calls[0]["tools"]]
    assert "get_price_summary" not in names and "price_lookup" not in names
    assert "Never give prices" in fake.calls[0]["system"]


# -- Redaction on reads ---------------------------------------------------------


def _restricted_thread(db):
    thread, _user, answer = _pending(db, content="price?")
    db.table("plans_reader_chat_messages").update({
        "status": "done", "content": "Labor 12.5 hours at the policy rate, material $1,234.",
        "pricing_restricted": True, "sender_role": "executive",
        "tool_calls": [
            {"name": "get_price_summary", "arguments": {}, "result_summary": "ok", "pricing_restricted": True, "registry": True},
            {"name": "get_brief", "arguments": {}, "result_summary": "ok"},
        ],
    }).eq("id", answer["id"]).execute()
    return thread, answer


def test_thread_reads_redact_restricted_answers_for_non_pricing_viewers(live):
    db = _db()
    thread, answer = _restricted_thread(db)
    for viewer in (Role.ACCOUNTANT, "accountant", None, Role.ESTIMATOR):
        got = chat.get_thread(db, thread["id"], viewer_role=viewer)
        msgs = {m["id"]: m for m in got["messages"]}
        assert msgs[answer["id"]]["content"] == chat.PRICING_REDACTED_MESSAGE
        assert "$" not in json.dumps(got) and "12.5" not in json.dumps(got)
        calls = msgs[answer["id"]]["tool_calls"]
        assert calls[0] == {"name": "get_price_summary", "pricing_restricted": True, "result_summary": "redacted"}
        assert calls[1]["name"] == "get_brief" and calls[1]["result_summary"] == "ok"
        assert got["thread"]["pricing_restricted"] is True
        # The user's own question is never redacted.
        user_rows = [m for m in got["messages"] if m["role"] == "user"]
        assert user_rows and user_rows[0]["content"] == "price?"
    full = chat.get_thread(db, thread["id"], viewer_role=Role.EXECUTIVE)
    assert "12.5 hours" in {m["id"]: m for m in full["messages"]}[answer["id"]]["content"]
    threads = chat.list_threads(db, RUN_ID_, viewer_role=Role.ACCOUNTANT)
    assert [t["pricing_restricted"] for t in threads] == [True]


def test_engineer_under_test_state_is_redacted_but_live_is_not(live, monkeypatch):
    db = _db()
    thread, answer = _restricted_thread(db)
    assert "12.5" in chat.get_thread(db, thread["id"], viewer_role=Role.ESTIMATING_ENGINEER_LABOR)["messages"][-1]["content"]
    s = _settings(ai_pricing_state="test")
    for mod in (features, deps, chat, adapter, chat_tools):
        monkeypatch.setattr(mod, "get_settings", lambda: s, raising=False)
    from app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: s)
    got = chat.get_thread(db, thread["id"], viewer_role=Role.ESTIMATING_ENGINEER_LABOR)
    assert got["messages"][-1]["content"] == chat.PRICING_REDACTED_MESSAGE


def test_redaction_survives_a_database_without_the_0165_columns(live):
    db = _db()
    thread, _user, _answer = _pending(db)
    db.fail_tables.add("plans_reader_chat_messages")
    # list_threads degrades to "nothing restricted" instead of failing.
    threads = chat.list_threads(db, RUN_ID_, viewer_role=Role.ACCOUNTANT)
    assert threads and threads[0]["pricing_restricted"] is False


# -- explain_row through chat ---------------------------------------------------


def _evidence():
    return {
        "lines": [{"line_id": "B1", "description": "DUPLEX RECEPTACLE", "unit": "EA", "quantity": 42,
                   "qty_chip": {"kind": "counted", "tone": "ok", "text": "Counted 42, marked 40, 2 not located"},
                   "source_refs": [{"ref": "C4", "multiplier": 1}], "marks_by_method": {"anchor": 40},
                   "not_located": 2, "sheets": ["D1P1"]}],
        "sheets": [{"sheet_key": "D1P1", "sheet_number": "E-101", "marks": [
            {"kind": "dot", "method": "anchor", "line_id": "B1", "points": [[0.1, 0.2]], "count": 1}]}],
    }


def _priced():
    return {"B1": {"line_id": "B1", "labor": {"value": "0.4", "quality": "strong", "n_obs": 9, "method": "exact"},
                   "material": {"value": "10", "quality": "strong", "n_obs": 9, "method": "exact"},
                   "material_source": "library", "labor_hours": "16.8", "material_cost": "420", "flags": []}}


def test_explain_row_price_block_only_for_pricing_actors_and_tags_the_message(live, monkeypatch):
    chat_tools.set_sources_factory(lambda ctx: chat_tools.FakeExplainSources(
        evidence=_evidence(), priced=_priced(),
        gate_findings=[{"gate_id": "census_floor", "severity": "warn", "status": "open", "message": "floor 44",
                        "target": {"line_id": "B1"}}]))
    monkeypatch.setattr(adapter, "TOOL_PARENTS", {**adapter.TOOL_PARENTS, "explain_row": ("ai_pricing_enabled",)})
    db = _db()
    exec_ctx = _ctx(db, "executive")
    assert "explain_row" in [t["name"] for t in chat.chat_tools(exec_ctx)]
    out, summary = chat.serve_tool(exec_ctx, "explain_row", {"line": "duplex receptacle", "include_marks": True})
    data = json.loads(out)
    assert data["line_id"] == "B1" and data["derivation"].startswith("42 EA counted from C4")
    assert data["price"]["chip"]["kind"] == "library" and data["price"]["labor_hours"] == "16.8"
    assert data["gate_findings"][0]["gate_id"] == "census_floor" and data["marks"][0]["sheet_key"] == "D1P1"
    assert summary["pricing_restricted"] is True and adapter.pricing_restricted(exec_ctx) is True
    # A second, non-pricing call in the same message does not re-tag the summary.
    _out, summary2 = chat.serve_tool(exec_ctx, "get_brief", {})
    assert "pricing_restricted" not in summary2

    acct_ctx = _ctx(db, "accountant")
    out, summary = chat.serve_tool(acct_ctx, "explain_row", {"line": "B1", "include_marks": False})
    data = json.loads(out)
    assert "price" not in data and "pricing_restricted" not in data and "marks" not in data
    assert "pricing_restricted" not in summary and adapter.pricing_restricted(acct_ctx) is False
    assert not chat_tools.humanize("", {}) and chat_tools.humanize("see D2", {}) == "see detail D2"


def test_explain_row_refusals(live):
    chat_tools.set_sources_factory(lambda ctx: chat_tools.FakeExplainSources(evidence=_evidence()))
    ctx = tools.ToolContext(run_id=RUN_ID_, kind="chat", role="accountant", services=tools.fake_services())
    assert chat_tools.explain_row(ctx, {"line": "zzz", "include_marks": False}).code == "unknown_line"
    chat_tools.set_sources_factory(lambda ctx: chat_tools.FakeExplainSources(evidence=None))
    assert chat_tools.explain_row(ctx, {"line": "B1", "include_marks": False}).code == "no_evidence"
    spec = tools.get_spec("explain_row")
    assert spec is not None and spec.kinds == frozenset({"review", "chat"}) and not spec.internal_pricing


# -- Price service for chat (wave 3 fix pass D2, hook [16]) ---------------------


def _live_with(monkeypatch, **over):
    s = _settings(**over)
    for mod in (features, deps, chat, adapter, chat_tools):
        monkeypatch.setattr(mod, "get_settings", lambda: s, raising=False)
    from app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: s)
    return s


def test_the_price_slot_is_bound_per_message_only_for_pricing_actors(live, monkeypatch):
    db = _db()
    exec_ctx = _ctx(db, "executive")
    assert adapter.handles(exec_ctx, "price_lookup")
    services = tools.ToolServices()
    adapter._bind_price(exec_ctx, services)
    assert isinstance(services.price, adapter.RunPriceService) and services.price.run_id == RUN_ID_
    # A fake or registered price service is never replaced.
    fake = tools.FakePriceService({})
    kept = tools.ToolServices(price=fake)
    adapter._bind_price(exec_ctx, kept)
    assert kept.price is fake
    acct_ctx = _ctx(db, "accountant")
    adapter.offered_specs(acct_ctx)
    services = tools.ToolServices()
    adapter._bind_price(acct_ctx, services)
    assert isinstance(services.price, tools.Unavailable)


def test_chat_serves_the_price_summary_through_the_bound_run_service(live, monkeypatch):
    """The default services path (no factory) binds RunPriceService, so
    get_price_summary answers from the run's latest pricing version."""
    from app.services import plans_reader_evidence_inputs as evidence_inputs
    from app.services import plans_reader_tool_handlers as tool_handlers
    from app.services import pricing_run

    adapter.set_services_factory(None)
    monkeypatch.setattr(evidence_inputs, "inputs_for", lambda sb: None)
    monkeypatch.setattr(tool_handlers, "services_for_run", lambda sb, run_id, **kw: tools.ToolServices())
    version = {"id": "p1", "run_id": RUN_ID_, "version": 3, "status": "priced", "verify_draft": {"labor_rate": 95, "bid_total": 1000},
               "quality_summary": {}, "range_low": 900, "range_high": 1100}
    monkeypatch.setattr(pricing_run, "latest_version", lambda sb, run_id: version if run_id == RUN_ID_ else None)
    monkeypatch.setattr(pricing_run, "version_lines", lambda sb, pid: [
        {"line": {"line_id": "B1", "labor_hours": 2, "material_cost": 10, "uom": "EA", "row": {"description": "DUPLEX", "quantity": 4}}}])
    db = _db()
    ctx = _ctx(db, "executive")
    out, summary = chat.serve_tool(ctx, "get_price_summary", {})
    data = json.loads(out)
    assert data["status"] == "priced" and data["version"] == 3 and data["top_lines"][0]["line_id"] == "B1"
    assert summary["pricing_restricted"] is True and summary["result_summary"] == "ok"
    assert isinstance(adapter.state_for(ctx).tool_ctx.services.price, adapter.RunPriceService)


def test_run_price_service_lookup_loads_the_library_once_and_degrades(live, monkeypatch):
    from app.services import pricing_run
    from app.services.cost_library import CostLibrary

    loads = []
    monkeypatch.setattr(pricing_run, "load_library", lambda sb, run, *, warnings: loads.append(run["id"]) or CostLibrary(()))

    def no_policy(sb):
        raise RuntimeError("policy table missing")

    monkeypatch.setattr(pricing_run, "load_policy", no_policy)
    svc = adapter.RunPriceService(object(), {"id": RUN_ID_, "project_id": "p-9"})
    query = {"description": "duplex receptacle", "uom": "EA", "quantity": 4, "family_hint": None, "spec_facts": []}
    assert svc.lookup(query)["status"] == "no_match"
    svc.lookup(query)
    assert loads == [RUN_ID_]
    # Another run's summary is refused; a library that cannot load is unavailable, never raised.
    assert svc.summary("other-run")["error"] == "unavailable"

    def no_library(sb, run, *, warnings):
        raise RuntimeError("db down")

    monkeypatch.setattr(pricing_run, "load_library", no_library)
    broken = adapter.RunPriceService(object(), {"id": RUN_ID_})
    assert tools.is_unavailable(broken.lookup(query))


# -- explain_row parents and visibility (J5) ------------------------------------


def test_explain_row_has_a_parent_entry_and_reaches_every_internal_role(live, monkeypatch):
    from app.core.roles import INTERNAL_ROLES

    assert adapter.TOOL_PARENTS["explain_row"] == ("plans_reader_gates_blocking", "plans_reader_code_regions", "plans_reader_derive_enabled")
    assert all(name in adapter.TOOL_PARENTS for name in ("price_lookup", "get_price_summary"))
    db = _db()
    # Rows and measure parents off: explain_row is not offered (fail closed on its own parents).
    assert "explain_row" not in [t["name"] for t in chat.chat_tools(_ctx(db, "executive"))]
    _live_with(monkeypatch, plans_reader_derive_enabled=True)
    chat_tools.set_sources_factory(lambda ctx: chat_tools.FakeExplainSources(evidence=_evidence(), priced=_priced()))
    for role in sorted(r.value for r in INTERNAL_ROLES):
        ctx = _ctx(db, role)
        assert "explain_row" in [t["name"] for t in chat.chat_tools(ctx)], role
        out, summary = chat.serve_tool(ctx, "explain_row", {"line": "B1", "include_marks": False})
        data = json.loads(out)
        sees = features.ai_pricing_allowed(Role(role))
        assert ("price" in data) is sees, role
        assert bool(summary.get("pricing_restricted")) is sees, role
    assert features.ai_pricing_allowed(Role.ACCOUNTANT) is False

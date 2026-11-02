"""Approval cards inside a turn (app/assistant/loop.py;
docs/ASSISTANT_PHASE1_PLAN.md 3.5), with a fake model and the probe write
tools from tests/assistant_fakes.py.

Invariants pinned here:

  cards     a confirm call becomes one pending approval and the turn ends as
            waiting_approval with the fixed sentence, one action_card block
            per card and no further model call; the model's own text in that
            round is discarded; reads in the same round still run;
  cap       a sixth confirm call in one turn gets the cap error, unproposed;
  failure   a failed proposal goes back to the model and the turn goes on;
  history   waiting messages are history, followed by one status line per
            card; the binding texts are the current and the previous user
            message;
  review    a begun-without-completed step parks the message as
            waiting_review (review_card never leaves the server);
  ceilings  a tool's max_cards_per_turn refuses a second card of it with
            the fixed text and no row; max_proposals_per_turn refuses an
            attempt past the limit before any preview read (phase 2, 3.9.4).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.assistant import access, approvals, llm_messages, loop, principal, registry
from app.assistant import executor as executor_mod
from app.assistant.executor import WriteMayHavePartiallyRun
from app.core import supabase_client
from app.services import llm

from tests.assistant_fakes import (
    CONV, MSG_ASSISTANT, NOW, PROJECT, SECRET, USER,
    FakeDB, approval, conversation, delegation, install_probes, message, pending_answer, profile, settings_row,
)
from tests.test_assistant_spend import install_spend_rpcs

EM_DASH = chr(0x2014)
DUE_ROWS = [
    {"project": {"id": PROJECT, "name": "Alpha Clinic", "number": "26.10.7267",
                 "internal_bid_at": NOW.isoformat(), "current_stage": "material_numbers"},
     "checks": {"has_quotes": False, "has_labor": True, "has_markups": True}, "severity": "red"},
]
DUE_ARGS = {"scope": "today", "response_format": "concise", "limit": None}


def _usage():
    return llm.LlmUsage(input_tokens=1000, cached_tokens=200, output_tokens=100, reasoning_tokens=40)


def _result(text="", tool_calls=()):
    return llm_messages.MessagesResult(
        text=text, tool_calls=list(tool_calls), output_items=[], usage=_usage(),
        response_id="resp_1", model="gpt-5.4", cost_usd=Decimal("0.01"),
    )


def _call(call_id, name, **arguments):
    return {"call_id": call_id, "name": name, "arguments": arguments}


def _set(call_id, amount="250.00", notes=None):
    return _call(call_id, "probe_set", project_id=PROJECT, amount=amount, notes=notes, url=None)


@pytest.fixture
def world(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_delegations=[delegation()], assistant_settings=[settings_row()],
        assistant_profiles=[], assistant_conversations=[conversation()],
        assistant_messages=[message(content="Set the labor to 250 please"), pending_answer()],
        assistant_tool_calls=[], assistant_tool_results=[], assistant_approvals=[], llm_call_log=[],
    )
    install_spend_rpcs(db)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    s = type("S", (), {"assistant_token_secret": SECRET, "assistant_enabled": True})()
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    bdr, _specs = install_probes(monkeypatch)
    bdr.responses[("GET", "/projects/due-checks")] = (200, DUE_ROWS)
    bdr.responses[("GET", "/projects/today")] = (200, [])

    script: list = []
    calls: list[dict] = []

    def fake_model(**kwargs):
        calls.append(kwargs)
        return script.pop(0)

    monkeypatch.setattr(llm_messages, "complete_messages", fake_model)
    return type("W", (), {"db": db, "script": script, "calls": calls, "bdr": bdr})()


def _answer(db):
    return next(r for r in db.tables["assistant_messages"] if r["id"] == MSG_ASSISTANT)


def _run():
    loop.run_turn(MSG_ASSISTANT, user_id=USER, lease_owner="worker:j:1", sleep=lambda s: None)


def _outputs(call):
    return [m for m in call["messages"] if m.get("type") == "function_call_output"]


# ── one card ─────────────────────────────────────────────────────────────────


def test_a_confirm_call_becomes_a_card_and_ends_the_turn(world):
    world.script[:] = [
        _result(text="I set it.", tool_calls=[_call("c1", "whats_due", **DUE_ARGS), _set("c2")]),
        _result(text="never reached"),
    ]
    _run()
    assert len(world.calls) == 1   # no further model call
    rows = world.db.tables["assistant_approvals"]
    assert len(rows) == 1
    card = rows[0]
    assert card["status"] == "pending" and card["action_kind"] == "probe_set"
    assert card["message_id"] == MSG_ASSISTANT and card["conversation_id"] == CONV
    row = _answer(world.db)
    assert row["status"] == "waiting_approval"
    # The model's text of that round is discarded: the fixed sentence only.
    assert row["content"] == loop.CARDS_TEXT_ONE and "I set it" not in json.dumps(loop.ui_blocks(row["blocks"]))
    served = loop.ui_blocks(row["blocks"])
    assert served == [{"type": "text", "text": loop.CARDS_TEXT_ONE},
                      {"type": "action_card", "approval_id": card["id"]}]
    assert any(b["type"] == "transcript" for b in row["blocks"])
    # The read of the same round ran first; the write was never sent.
    sent = [s["route"] for s in world.bdr.sent]
    assert ("GET", "/projects/due-checks") in sent
    assert ("PUT", "/projects/{project_id}/labor") not in sent
    assert [p["tool_name"] for p in row["tool_calls"]] == ["whats_due", "probe_set"]
    assert row["tool_calls"][1]["ok"] is True and row["tool_calls"][1]["seq"] == 2
    assert row["tool_calls"][1]["progress_label"] == "Setting probe labor"
    # No ledger row for the proposal: only the read's begun and completed.
    assert [r["tool_name"] for r in world.db.tables["assistant_tool_calls"]] == ["whats_due", "whats_due"]
    assert world.db.tables["assistant_conversations"][0]["last_message_at"] is not None
    assert EM_DASH not in row["content"]


def test_two_cards_get_the_plural_sentence_and_two_blocks(world):
    world.script[:] = [_result(tool_calls=[_set("c1", "250.00"), _set("c2", "300.00")])]
    _run()
    row = _answer(world.db)
    ids = [r["id"] for r in world.db.tables["assistant_approvals"]]
    assert row["content"] == loop.CARDS_TEXT_MANY.format(n=2) and "2 changes" in row["content"]
    assert [b["approval_id"] for b in loop.ui_blocks(row["blocks"]) if b["type"] == "action_card"] == ids


def test_a_sixth_confirm_call_gets_the_cap_error(world, monkeypatch):
    outputs: list[str] = []
    real = loop._Turn._propose

    def spy(self, name, args):
        out = real(self, name, args)
        outputs.append(out)
        return out

    monkeypatch.setattr(loop._Turn, "_propose", spy)
    world.script[:] = [_result(tool_calls=[_set(f"c{i}", f"{200 + i}.00") for i in range(6)])]
    _run()
    assert len(world.db.tables["assistant_approvals"]) == loop.MAX_CARDS_PER_TURN == 5
    assert json.loads(outputs[-1]) == {"error": loop.CARD_CAP_TEXT}
    assert all(json.loads(o)["status"] == "waiting_for_approval" for o in outputs[:5])
    row = _answer(world.db)
    assert row["status"] == "waiting_approval" and row["content"] == loop.CARDS_TEXT_MANY.format(n=5)


def _limited_probe(monkeypatch, **limits):
    """probe_set as an expensive tool with per-turn ceilings."""
    spec = registry.validate(dataclasses.replace(registry._REGISTRY["probe_set"], effect="expensive", **limits))
    monkeypatch.setitem(registry._REGISTRY, "probe_set", spec)
    return spec


def _spy_outputs(monkeypatch) -> list[str]:
    outputs: list[str] = []
    real = loop._Turn._propose

    def spy(self, name, args):
        out = real(self, name, args)
        outputs.append(out)
        return out

    monkeypatch.setattr(loop._Turn, "_propose", spy)
    return outputs


def test_a_second_card_of_a_one_card_tool_is_refused(world, monkeypatch):
    _limited_probe(monkeypatch, max_cards_per_turn=1, max_proposals_per_turn=3)
    outputs = _spy_outputs(monkeypatch)
    world.script[:] = [_result(tool_calls=[_set("c1", "250.00"), _set("c2", "300.00")])]
    _run()
    rows = world.db.tables["assistant_approvals"]
    assert len(rows) == 1
    assert json.loads(outputs[1]) == {"error": loop.CARDS_PER_TOOL_TEXT}
    assert EM_DASH not in loop.CARDS_PER_TOOL_TEXT + loop.PROPOSALS_PER_TOOL_TEXT
    row = _answer(world.db)
    assert row["status"] == "waiting_approval" and row["content"] == loop.CARDS_TEXT_ONE
    # An expensive card waits 4 hours (TTL_BY_EFFECT).
    created = datetime.fromisoformat(rows[0]["created_at"])
    expires = datetime.fromisoformat(rows[0]["expires_at"])
    assert timedelta(hours=3, minutes=59) < expires - created < timedelta(hours=4, minutes=1)


def test_proposal_attempts_past_the_limit_are_refused_before_any_read(world, monkeypatch):
    _limited_probe(monkeypatch, max_cards_per_turn=1, max_proposals_per_turn=3)
    outputs = _spy_outputs(monkeypatch)
    world.script[:] = [
        _result(tool_calls=[_set(f"c{i}", amount=None, notes=None) for i in range(4)]),
        _result(text="Nothing to change on 26.10.7267."),
    ]
    _run()
    assert [json.loads(o) for o in outputs[:3]] == [{"error": "Nothing to change."}] * 3
    assert json.loads(outputs[3]) == {"error": loop.PROPOSALS_PER_TOOL_TEXT}
    labor_reads = [s for s in world.bdr.sent if s["route"] == ("GET", "/projects/{project_id}/labor")]
    assert len(labor_reads) == 3
    assert world.db.tables["assistant_approvals"] == []
    assert _answer(world.db)["status"] == "done"


def test_tools_without_ceilings_keep_the_phase_1_behavior(world, monkeypatch):
    outputs = _spy_outputs(monkeypatch)
    world.script[:] = [_result(tool_calls=[_set("c1", "250.00"), _set("c2", "300.00")])]
    _run()
    assert len(world.db.tables["assistant_approvals"]) == 2
    assert all(json.loads(o)["status"] == "waiting_for_approval" for o in outputs)
    card = world.db.tables["assistant_approvals"][0]
    expires = datetime.fromisoformat(card["expires_at"]) - datetime.fromisoformat(card["created_at"])
    assert timedelta(minutes=59) < expires < timedelta(hours=1, minutes=1)


def test_a_failed_proposal_goes_back_to_the_model_and_the_turn_goes_on(world):
    world.script[:] = [
        _result(tool_calls=[_set("c1", amount=None, notes=None)]),
        _result(text="What amount should I put in?"),
    ]
    _run()
    assert len(world.calls) == 2
    out = json.loads(_outputs(world.calls[1])[0]["output"])
    assert out == {"error": "Nothing to change."}
    row = _answer(world.db)
    assert row["status"] == "done" and row["content"] == "What amount should I put in?"
    assert row["tool_calls"][0]["ok"] is False and row["tool_calls"][0]["tool_name"] == "probe_set"
    assert world.db.tables["assistant_approvals"] == []


def test_an_unexpected_proposal_error_is_an_answer_not_a_dead_turn(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(approvals, "propose", boom)
    world.script[:] = [_result(tool_calls=[_set("c1")]), _result(text="That did not work.")]
    _run()
    assert json.loads(_outputs(world.calls[1])[0]["output"]) == {"error": loop.CARD_FAILED_TEXT}
    assert _answer(world.db)["status"] == "done"


def test_binding_texts_are_the_current_and_previous_user_messages(world, monkeypatch):
    world.db.tables["assistant_messages"] = [
        message(id="u0", content="older question", created_at=(NOW - timedelta(seconds=40)).isoformat()),
        message(id="a0", role="assistant", content="older answer", created_at=(NOW - timedelta(seconds=39)).isoformat()),
        message(id="u1", content="Labor note: phase one", created_at=(NOW - timedelta(seconds=30)).isoformat()),
        message(id="a1", role="assistant", content="Noted.", created_at=(NOW - timedelta(seconds=29)).isoformat()),
        message(content="Now set it to 250"),
        pending_answer(),
    ]
    seen: dict = {}
    real = approvals.propose

    def spy(ex, name, args, **kw):
        seen.update(kw)
        return real(ex, name, args, **kw)

    monkeypatch.setattr(approvals, "propose", spy)
    world.script[:] = [_result(tool_calls=[_set("c1")])]
    _run()
    assert seen["user_texts"] == ("Now set it to 250", "Labor note: phase one")
    assert seen["conversation_id"] == CONV and seen["message_id"] == MSG_ASSISTANT


# ── history ──────────────────────────────────────────────────────────────────


def test_history_replays_waiting_messages_with_their_card_lines(world):
    earlier = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    replaced = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    world.db.tables["assistant_messages"] = [
        message(id="u0", content="Set the labor to 250", created_at=(NOW - timedelta(seconds=40)).isoformat()),
        message(id="a0", role="assistant", status="waiting_approval", content=loop.CARDS_TEXT_MANY.format(n=2),
                blocks=[{"type": "text", "text": "x"}, {"type": "action_card", "approval_id": earlier},
                        {"type": "action_card", "approval_id": replaced}],
                created_at=(NOW - timedelta(seconds=39)).isoformat()),
        message(id="a1", role="assistant", status="waiting_review", content=loop.REVIEW_TEXT,
                created_at=(NOW - timedelta(seconds=38)).isoformat()),
        message(content="Did it go through?"),
        pending_answer(),
    ]
    world.db.tables["assistant_approvals"] = [
        approval(id=earlier, message_id="a0", status="executed",
                 preview={"v": 1, "title": "Set probe on 26.9.7130", "rows": []}),
        approval(id=replaced, message_id="a0", status="expired", expired_reason="changed_since_preview",
                 superseded_by=earlier, preview={"v": 1, "title": "Set probe on 26.9.7131", "rows": []}),
    ]
    world.script[:] = [_result(text="Yes, it was executed.")]
    _run()
    msgs = world.calls[0]["messages"]
    texts = [m["content"][0]["text"] for m in msgs if m.get("role") in ("user", "assistant")]
    assert texts[0] == "Set the labor to 250"
    assert texts[1] == (loop.CARDS_TEXT_MANY.format(n=2)
                        + "\n[Approval card: Set probe on 26.9.7130: executed]"
                        + "\n[Approval card: Set probe on 26.9.7131: superseded, changed since preview]")
    assert texts[2] == loop.REVIEW_TEXT
    assert texts[-1] == "Did it go through?"
    assert _answer(world.db)["status"] == "done"


def test_a_card_line_read_failure_keeps_the_history(world, monkeypatch):
    from app.assistant import repo

    world.db.tables["assistant_messages"] = [
        message(id="u0", content="Set it", created_at=(NOW - timedelta(seconds=40)).isoformat()),
        message(id="a0", role="assistant", status="waiting_approval", content=loop.CARDS_TEXT_ONE,
                blocks=[{"type": "action_card", "approval_id": "x"}],
                created_at=(NOW - timedelta(seconds=39)).isoformat()),
        message(content="And now?"),
        pending_answer(),
    ]

    def boom(user_id, message_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "list_approvals_for_message", boom)
    world.script[:] = [_result(text="Checking.")]
    _run()
    texts = [m["content"][0]["text"] for m in world.calls[0]["messages"] if m.get("role") == "assistant"]
    assert texts == [loop.CARDS_TEXT_ONE]


def test_approval_line_shapes():
    row = approval(status="pending", preview={"title": "Set labor numbers on 26.9.7130"},
                   expires_at=(NOW - timedelta(minutes=1)).isoformat())
    assert loop.approval_line(row, NOW) == "[Approval card: Set labor numbers on 26.9.7130: expired]"
    row = approval(status="expired", expired_reason="project_moved", preview={"title": "T"})
    assert loop.approval_line(row, NOW) == "[Approval card: T: expired, project moved]"
    row = approval(status="failed", result={"ok": False, "may_have_partially_run": True}, preview={"title": "T"})
    assert loop.approval_line(row, NOW) == "[Approval card: T: partial]"
    # A dash in a title never reaches the model as a dash.
    row = approval(status="executed", preview={"title": f"A {EM_DASH} B"})
    assert EM_DASH not in loop.approval_line(row, NOW)


# ── review and blocks ────────────────────────────────────────────────────────


def test_a_partially_run_step_parks_the_message_for_review(world, monkeypatch):
    def collide(self, name, args):
        self.calls += 1
        raise WriteMayHavePartiallyRun("begun row exists")

    monkeypatch.setattr(executor_mod.Executor, "call", collide)
    world.script[:] = [_result(tool_calls=[_call("c1", "whats_due", **DUE_ARGS)])]
    _run()
    row = _answer(world.db)
    assert row["status"] == "waiting_review" and row["content"] == loop.REVIEW_TEXT
    assert {"type": "review_card", "tool_name": "whats_due", "step": 1} in row["blocks"]
    assert loop.ui_blocks(row["blocks"]) == [{"type": "text", "text": loop.REVIEW_TEXT}]


def test_ui_blocks_serve_action_cards_only_with_an_id():
    blocks = [
        {"type": "text", "text": "x"},
        {"type": "action_card", "approval_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        {"type": "action_card"},
        {"type": "action_card", "approval_id": 3},
        {"type": "review_card", "tool_name": "t", "step": 1},
        {"type": "transcript", "items": []},
    ]
    assert loop.ui_blocks(blocks) == blocks[:2]
    assert loop.action_card_ids(blocks) == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "3"]
    assert "action_card" in loop.UI_BLOCK_TYPES


def _sensitive_probe(monkeypatch):
    """probe_set as a sensitive_read tool (a read-only evidence card)."""
    # Not registry.validate: it demands the real evidence POST route, which
    # the loop never inspects; only spec.effect matters here.
    spec = dataclasses.replace(registry._REGISTRY["probe_set"], effect="sensitive_read")
    monkeypatch.setitem(registry._REGISTRY, "probe_set", spec)
    return spec


def test_a_sensitive_read_card_gets_its_own_sentence(world, monkeypatch):
    _sensitive_probe(monkeypatch)
    world.script[:] = [_result(tool_calls=[_set("c1", "250.00")])]
    _run()
    row = _answer(world.db)
    assert row["status"] == "waiting_approval"
    assert row["content"] == loop.CARDS_TEXT_READ_ONE
    assert "access log" in row["content"] and "reason" in row["content"]
    # A read prepares no change: the write-card sentence must not appear.
    assert "prepared" not in row["content"] and "Nothing changes" not in row["content"]
    assert [b["text"] for b in loop.ui_blocks(row["blocks"]) if b["type"] == "text"] == [loop.CARDS_TEXT_READ_ONE]


def test_two_sensitive_read_cards_get_the_plural_read_sentence(world, monkeypatch):
    _sensitive_probe(monkeypatch)
    world.script[:] = [_result(tool_calls=[_set("c1", "250.00"), _set("c2", "300.00")])]
    _run()
    row = _answer(world.db)
    assert row["content"] == loop.CARDS_TEXT_READ_MANY.format(n=2) and "prepared" not in row["content"]


def test_cards_text_mixes_and_defaults():
    turn = loop._Turn.__new__(loop._Turn)
    turn.sensitive_cards = 0
    assert turn._cards_text(1) == loop.CARDS_TEXT_ONE
    assert turn._cards_text(3) == loop.CARDS_TEXT_MANY.format(n=3)
    turn.sensitive_cards = 1
    assert turn._cards_text(1) == loop.CARDS_TEXT_READ_ONE
    assert turn._cards_text(2) == loop.CARDS_TEXT_MIXED.format(n=2)
    turn.sensitive_cards = 2
    assert turn._cards_text(2) == loop.CARDS_TEXT_READ_MANY.format(n=2)


def test_card_texts_have_no_em_dash():
    for text in (loop.CARDS_TEXT_ONE, loop.CARDS_TEXT_MANY, loop.REVIEW_TEXT, loop.CARD_CAP_TEXT,
                 loop.CARD_FAILED_TEXT, loop.CARDS_TEXT_READ_ONE, loop.CARDS_TEXT_READ_MANY,
                 loop.CARDS_TEXT_MIXED):
        assert EM_DASH not in text

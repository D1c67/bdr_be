"""The housekeeping tier in the executor (app/assistant/executor.py;
docs/ASSISTANT_PHASE3_PLAN.md 3.7.2).

Invariants pinned here:

  inline     a housekeeping call runs in the turn (never ConfirmRequired),
             under a begun ledger row and one terminal row, and every
             request carries X-Assistant-Idempotency-Key set to the ledger
             key over anything the builder set;
  ledger     without a begun row (the begin RPC raises or returns no id)
             the call fails ledger_unavailable and nothing is sent; a begun
             row from an earlier attempt is re-run under a fresh key, never
             WriteMayHavePartiallyRun;
  redaction  the begun, completed and failed rows store the spec's
             redact_args form, never the title or body, through a success,
             a route 422 that echoes the arguments and a trimmer failure;
             payload_sha256 stays the hash of the full arguments; a
             redact_args that raises stores {"redacted": true};
  precheck   a refusal (or a precheck that raises) is a failed step with no
             ledger row, no stored result and no request;
  memory_ids the stored result of a memory tool names the full uuids behind
             the short ids under `id` and `memory_id` at any depth, taken
             before truncation; other namespaces store none;
  notes      a memory housekeeping result with show_note true adds its
             memory's full id to memory_notes once; a read result or a
             schedule result never does.

The specs are test doubles (the real memory and schedule tools arrive with
WI-7); requests go to a recording sender, not the shim.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from app.assistant import executor as executor_mod
from app.assistant import principal, registry, repo
from app.assistant.executor import Executor
from app.assistant.registry import EFFECT_HOUSEKEEPING, ToolRequest, ToolSpec, schema
from app.core import supabase_client
from app.core.roles import INTERNAL_ROLES

from tests.assistant_fakes import CONV, MSG_ASSISTANT, RUN, SECRET, USER, FakeDB, delegation, profile

SAVE = ("POST", "/assistant/tools/memory/save")
MEMORY_LIST = ("GET", "/assistant/tools/memory")
PAUSE = ("POST", "/assistant/tools/schedules/{schedule_id}/pause")
MEMORY = "abcdef01-0000-4000-8000-000000000001"
MEMORY2 = "abcdef02-0000-4000-8000-000000000002"
SCHEDULE = "abcdef03-0000-4000-8000-000000000003"

TITLE = "Gate code for the Monument trailer"
BODY = "Prefers the due list sorted by internal bid time, Graybar first"
ARGS = {"kind": "preference", "title": TITLE, "body": BODY, "project_id": None, "memory_id": None}
SENSITIVE = ("memory_sensitive", "That looks like a password, ID number, bank or pay detail, so it was not saved.")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact(args):
    """The memory specs' ledger form (3.7.2): hashes and lengths only."""
    title, body = str(args.get("title") or ""), str(args.get("body") or "")
    return {"kind": args.get("kind"), "project_id": args.get("project_id"), "memory_id": args.get("memory_id"),
            "title_len": len(title), "title_sha256": _sha(title), "body_len": len(body), "body_sha256": _sha(body)}


def _save_output(payloads, args, ctx):
    data = payloads[0]
    return {"memory_id": ctx.short(data["memory_id"]), "status": data["status"],
            "similar": [{"id": ctx.short(s["id"]), "similarity": s["similarity"]} for s in data["similar"]],
            "show_note": data["show_note"], "message": data["message"]}


def _memory_spec(name="memory_save_probe", trimmer=None, **over) -> ToolSpec:
    base = dict(
        name=name, namespace="memory", sub_app=None, route=SAVE, effect=EFFECT_HOUSEKEEPING, confirm=False,
        schema=schema({k: {"type": ["string", "null"]} for k in ARGS}),
        response_trimmer=trimmer or _save_output, progress_label="Saving a note", roles=INTERNAL_ROLES,
        description="probe memory save",
        requests=lambda a: [ToolRequest(route=SAVE, path=SAVE[1], json=dict(a))],
        redact_args=_redact,
    )
    base.update(over)
    return registry.validate(ToolSpec(**base))


def _pause_spec(**over) -> ToolSpec:
    base = dict(
        name="schedule_pause_probe", namespace="schedule", sub_app="bidding", route=PAUSE,
        redact_args=None, schema=schema({"schedule_id": {"type": "string"}}),
        requests=lambda a: [ToolRequest(route=PAUSE, path=f"/assistant/tools/schedules/{a['schedule_id']}/pause",
                                        headers={"x-assistant-idempotency-key": "forged-by-the-builder"})],
        trimmer=lambda p, a, c: {"id": c.short(p[0]["id"]), "status": p[0]["status"], "show_note": True,
                                 "memory_id": MEMORY},
    )
    base.update(over)
    return _memory_spec(**base)


def _list_spec(**over) -> ToolSpec:
    base = dict(
        name="memory_list_probe", route=MEMORY_LIST, effect="read", redact_args=None,
        schema=schema({"limit": registry.LIMIT_PROP}),
        requests=lambda a: [ToolRequest(route=MEMORY_LIST, path=MEMORY_LIST[1])],
        trimmer=lambda p, a, c: {"items": [{"id": c.short(m["id"]), "title": m["title"]} for m in p[0]["items"]],
                                 "show_note": True, "memory_id": c.short(p[0]["items"][0]["id"])},
    )
    base.update(over)
    return _memory_spec(**base)


def _saved(**over) -> dict:
    base = {"memory_id": MEMORY, "status": "active", "source": "user_stated", "origin_tainted": False,
            "similar": [{"id": MEMORY2, "title": "Short answers", "similarity": 0.4}],
            "show_note": True, "message": "Saved."}
    base.update(over)
    return base


@pytest.fixture
def settings(monkeypatch):
    s = SimpleNamespace(assistant_token_secret=SECRET, assistant_enabled=True)
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    return s


@pytest.fixture
def db(monkeypatch, settings):
    fake = FakeDB(assistant_delegations=[delegation()], profiles=[profile()],
                  assistant_tool_calls=[], assistant_tool_results=[])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


class Sender:
    """Records every request; answers from a queue of (status, payload)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent: list[ToolRequest] = []

    def __call__(self, req, token):
        assert token.startswith("bdra_")
        self.sent.append(req)
        return self.answers.pop(0) if self.answers else (200, _saved())


def _executor(specs, sender, **over) -> Executor:
    base = dict(user_id=USER, user_label="Tom Moore", run_id=RUN, specs={s.name: s for s in specs},
                lease_owner="worker:job-1:1", conversation_id=CONV, message_id=MSG_ASSISTANT, sender=sender)
    base.update(over)
    return Executor(**base)


def _ledger(db) -> list[dict]:
    return db.tables["assistant_tool_calls"]


def _no_text(rows) -> bool:
    blob = json.dumps(rows, default=str)
    return TITLE not in blob and BODY not in blob and "Monument" not in blob and "Graybar" not in blob


# ── inline under the ledger ──────────────────────────────────────────────────


def test_housekeeping_runs_inline_with_the_ledger_key_header(db):
    sender = Sender((200, {"id": SCHEDULE, "status": "paused"}))
    ex = _executor([_pause_spec()], sender)
    outcome = ex.call("schedule_pause_probe", {"schedule_id": SCHEDULE})
    assert outcome.ok and outcome.tool_name == "schedule_pause_probe"
    begun, completed = _ledger(db)
    assert begun["phase"] == "begun" and completed["phase"] == "completed"
    assert begun["effect"] == completed["effect"] == "housekeeping"
    assert begun["method"] == "POST" and begun["path_format"] == PAUSE[1]
    (req,) = sender.sent
    # The executor's header wins over the builder's, whatever its spelling.
    assert dict(req.headers) == {executor_mod.IDEMPOTENCY_HEADER: begun["idempotency_key"]}
    assert begun["idempotency_key"].startswith(f"{MSG_ASSISTANT}:1:schedule_pause_probe:")
    assert outcome.ledger_id == begun["id"]
    (result,) = db.tables["assistant_tool_results"]
    assert result["tool_call_id"] == begun["id"] and "memory_ids" not in result


def test_the_header_name_is_the_write_tools_header():
    from app.assistant.tools.writes import ASSISTANT_IDEMPOTENCY_HEADER

    assert executor_mod.IDEMPOTENCY_HEADER == ASSISTANT_IDEMPOTENCY_HEADER


def test_no_begun_row_means_nothing_is_sent(db, monkeypatch):
    sender = Sender()
    ex = _executor([_memory_spec()], sender)
    monkeypatch.setattr(repo, "tool_call_begin", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    outcome = ex.call("memory_save_probe", ARGS)
    assert not outcome.ok and outcome.error_kind == "ledger_unavailable"
    assert outcome.output == {"error": executor_mod.LEDGER_UNAVAILABLE_TEXT}
    assert sender.sent == [] and _ledger(db) == [] and db.tables["assistant_tool_results"] == []
    # A begin answer without a row id is the same refusal.
    monkeypatch.setattr(repo, "tool_call_begin", lambda *a, **k: {"inserted": True, "rows": [{"phase": "begun"}]})
    outcome = ex.call("memory_save_probe", ARGS)
    assert outcome.error_kind == "ledger_unavailable" and sender.sent == []


def test_an_earlier_begun_row_reruns_under_a_fresh_key(db):
    sender = Sender()
    ex = _executor([_memory_spec()], sender)
    spec = ex.specs["memory_save_probe"]
    first = ex._idempotency_key(spec, 1, ARGS, 0)
    _ledger(db).append({"id": "old", "idempotency_key": first, "phase": "begun", "lease_owner": "other:1:1",
                        "user_id": USER, "run_id": RUN, "seq": 1, "tool_name": spec.name,
                        "effect": "housekeeping"})
    outcome = ex.call("memory_save_probe", ARGS)   # no WriteMayHavePartiallyRun, no ConfirmRequired
    assert outcome.ok
    assert sender.sent[0].headers[executor_mod.IDEMPOTENCY_HEADER] == first + ":r1"
    assert [r["phase"] for r in _ledger(db) if r["idempotency_key"] == first + ":r1"] == ["begun", "completed"]


def test_confirm_tiers_still_raise_and_housekeeping_never_does(db):
    write = registry.validate(ToolSpec(
        name="write_probe", namespace="bidding", sub_app="bidding", route=("PUT", "/projects/{project_id}/labor"),
        effect="write_internal", confirm=True, schema=schema({}), response_trimmer=lambda p, a, c: p,
        progress_label="Writing", roles=INTERNAL_ROLES, description="probe",
        requests=lambda a: [],
        write=registry.WriteSpec(preview_requests=lambda a: [], build=lambda a, r, c: None,
                                 snapshot=lambda a, r: ({}, {})),
    ))
    ex = _executor([write, _memory_spec()], Sender())
    with pytest.raises(executor_mod.ConfirmRequired):
        ex.call("write_probe", {})
    assert ex.call("memory_save_probe", ARGS).ok


def test_a_tainted_turn_marks_the_housekeeping_begun_row(db):
    ex = _executor([_memory_spec()], Sender(), tainted_context=True)
    ex.call("memory_save_probe", ARGS)
    assert all(r["tainted_context"] is True for r in _ledger(db))


# ── redaction on every ledger row ────────────────────────────────────────────


def test_success_rows_store_the_redacted_form(db):
    ex = _executor([_memory_spec()], Sender())
    assert ex.call("memory_save_probe", ARGS).ok
    begun, completed = _ledger(db)
    assert begun["args_redacted"] == completed["args_redacted"] == _redact(ARGS)
    full = executor_mod._dumps(ARGS)
    assert begun["payload_sha256"] == completed["payload_sha256"] == _sha(full)
    assert _no_text(_ledger(db))


@pytest.mark.parametrize("answer", [
    (422, {"detail": [{"type": "string_too_long", "loc": ["body", "title"], "input": TITLE}]}),
    (422, {"detail": f"Title {TITLE!r} is too long"}),
    (409, {"detail": {"code": "memory_stale", "message": BODY}}),
])
def test_a_route_refusal_leaves_no_argument_text_in_the_ledger(db, answer):
    ex = _executor([_memory_spec()], Sender(answer))
    outcome = ex.call("memory_save_probe", ARGS)
    assert not outcome.ok and outcome.error_kind == f"http_{answer[0]}"
    begun, failed = _ledger(db)
    assert failed["phase"] == "failed" and failed["ok"] is False
    assert begun["args_redacted"] == failed["args_redacted"] == _redact(ARGS)
    assert failed["result_summary"] == {"error": outcome.error_kind, "redacted": True}
    assert _no_text(_ledger(db))
    assert db.tables["assistant_tool_results"] == []


def test_a_trimmer_failure_leaves_no_argument_text_in_the_ledger(db):
    def broken(payloads, args, ctx):
        raise KeyError(args["title"])

    ex = _executor([_memory_spec(trimmer=broken)], Sender())
    outcome = ex.call("memory_save_probe", ARGS)
    assert not outcome.ok and outcome.error_kind == "tool_error"
    assert [r["phase"] for r in _ledger(db)] == ["begun", "failed"]
    assert _no_text(_ledger(db))


def test_a_redact_args_that_raises_stores_nothing_of_the_args(db):
    def bad(args):
        raise ValueError(args["title"])

    ex = _executor([_memory_spec(redact_args=bad)], Sender())
    assert ex.call("memory_save_probe", ARGS).ok
    assert all(r["args_redacted"] == {"redacted": True} for r in _ledger(db))
    assert _no_text(_ledger(db))


def test_a_spec_without_redact_args_keeps_the_size_cap_form(db):
    ex = _executor([_pause_spec()], Sender((200, {"id": SCHEDULE, "status": "paused"})))
    ex.call("schedule_pause_probe", {"schedule_id": SCHEDULE})
    assert all(r["args_redacted"] == {"schedule_id": SCHEDULE} for r in _ledger(db))


# ── precheck ─────────────────────────────────────────────────────────────────


def test_a_precheck_refusal_records_and_sends_nothing(db):
    seen = []

    def precheck(args):
        seen.append(dict(args))
        return SENSITIVE if "code" in str(args.get("title")).lower() else None

    sender = Sender()
    ex = _executor([_memory_spec(precheck=precheck)], sender)
    outcome = ex.call("memory_save_probe", ARGS)
    assert not outcome.ok and outcome.error_kind == "memory_sensitive"
    assert outcome.error == SENSITIVE[1] and outcome.output == {"error": SENSITIVE[1]}
    assert outcome.ledger_id is None and outcome.seq == 1
    assert sender.sent == [] and _ledger(db) == [] and db.tables["assistant_tool_results"] == []
    # A clean call passes the precheck and runs.
    clean = {**ARGS, "title": "Short answers"}
    assert ex.call("memory_save_probe", clean).ok
    assert seen[-1]["title"] == "Short answers" and len(sender.sent) == 1


def test_a_precheck_that_raises_refuses_too(db):
    def precheck(args):
        raise RuntimeError("regex bug")

    sender = Sender()
    outcome = _executor([_memory_spec(precheck=precheck)], sender).call("memory_save_probe", ARGS)
    assert not outcome.ok and outcome.error_kind == "tool_error"
    assert sender.sent == [] and _ledger(db) == []


def test_a_precheck_sees_expanded_ids(db):
    seen = []
    ex = _executor([_memory_spec(precheck=lambda a: seen.append(a["memory_id"]))], Sender())
    short = ex.short(MEMORY)
    ex.call("memory_save_probe", {**ARGS, "memory_id": short})
    assert seen == [MEMORY]


# ── memory_ids and memory_notes ──────────────────────────────────────────────


def test_a_memory_result_stores_the_full_ids_behind_the_short_ones(db):
    ex = _executor([_memory_spec()], Sender())
    outcome = ex.call("memory_save_probe", ARGS)
    # The model saw short ids only.
    assert outcome.output["memory_id"] == MEMORY[:8] and outcome.output["similar"][0]["id"] == MEMORY2[:8]
    (result,) = db.tables["assistant_tool_results"]
    assert result["memory_ids"] == [MEMORY, MEMORY2]


def test_a_memory_read_stores_ids_before_truncation(db):
    items = [{"id": f"abcdef{i:02d}-0000-4000-8000-0000000000{i:02d}", "title": "x" * 900} for i in range(1, 30)]
    ex = _executor([_list_spec()], Sender((200, {"items": items})))
    outcome = ex.call("memory_list_probe", {"limit": None})
    assert outcome.ok and outcome.truncated
    (result,) = db.tables["assistant_tool_results"]
    assert result["memory_ids"] == [m["id"] for m in items]
    # A read never feeds memory_notes, show_note or not.
    assert ex.memory_notes == []


def test_other_namespaces_store_no_memory_ids(db):
    ex = _executor([_pause_spec()], Sender((200, {"id": SCHEDULE, "status": "paused"})))
    ex.call("schedule_pause_probe", {"schedule_id": SCHEDULE})
    (result,) = db.tables["assistant_tool_results"]
    assert "memory_ids" not in result
    # show_note belongs to the memory tools: a schedule result adds no note.
    assert ex.memory_notes == []


def test_a_memory_result_without_ids_stores_none(db):
    trimmer = lambda p, a, c: {"status": "ok", "items": [{"id": "not-a-uuid"}]}  # noqa: E731
    ex = _executor([_memory_spec(trimmer=trimmer)], Sender())
    ex.call("memory_save_probe", ARGS)
    assert "memory_ids" not in db.tables["assistant_tool_results"][0]


def test_show_note_collects_each_memory_once(db):
    sender = Sender((200, _saved()), (200, _saved()), (200, _saved(memory_id=MEMORY2, show_note=False)))
    ex = _executor([_memory_spec()], sender)
    ex.call("memory_save_probe", ARGS)
    ex.call("memory_save_probe", {**ARGS, "title": "Again"})
    ex.call("memory_save_probe", {**ARGS, "title": "Quiet"})
    assert ex.memory_notes == [MEMORY]


def test_a_failed_housekeeping_call_adds_no_note(db):
    ex = _executor([_memory_spec()], Sender((500, {"detail": "boom"})))
    assert not ex.call("memory_save_probe", ARGS).ok
    assert ex.memory_notes == []

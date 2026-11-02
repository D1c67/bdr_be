"""advance_lane (app/assistant/tools/workflow.py;
docs/ASSISTANT_PHASE4_PLAN.md 3.12).

  literals  the route templates, lane tasks, panel-owned heads, stage and
            lane labels, head owners and drawing categories equal the
            service side's;
  spec      write_internal, WRITER_ROLES only, deferred, no Send Out lane,
            the note is a bound text field, the Accountant gets no card;
  build     the card, the forced request (gono_action review and the
            previewed head, whatever the arguments say), the precondition,
            every refusal of 3.12 and the warnings;
  route     the built request against the real /advance route with an
            assistant principal: it passes the guard, parks the project for
            review, writes no Go/No-Go decision, and a moved head answers
            409 lane_moved;
  e2e       a card through the approvals engine: executed, and a lane a
            teammate moved supersedes the card.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

import app.main
from app.assistant import access, approvals, approvals_job, binding, executor, jobs, principal, registry, repo
from app.assistant.approvals import canonical_json, precondition_for
from app.assistant.registry import BuildContext, ProposalRefused
from app.assistant.tools import workflow as tool
from app.assistant.tools._common import ToolArgumentError
from app.core import features, supabase_client
from app.core.file_categories import DRAWING_CATEGORIES
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role
from app.routers import workflow as route_side
from app.services import gono, notifications
from app.services import workflow as service
from tests.assistant_fakes import (
    CONV,
    MSG_ASSISTANT,
    PROJECT,
    SECRET,
    USER,
    FakeDB,
    delegation,
    pending_answer,
    profile,
    settings_row,
)
from tests.assistant_fakes import RUN as TURN_RUN
from tests.test_workflow_expected_task import PID, Api

EM_DASH = chr(0x2014)
APPROVAL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NUMBER = "26.10.7140"
FLAGS = {"bidding": True, "pm": True, "certified_payroll": True, "ai_estimating": True}


def _spec():
    return registry.all_tools()["advance_lane"]


def _ctx(**over) -> BuildContext:
    base = dict(approval_id=APPROVAL, role=Role.EXECUTIVE, short=lambda v: str(v)[:8], tainted=False,
                stripped=(), now=datetime.now(timezone.utc), is_revert=False)
    base.update(over)
    return BuildContext(**base)


def _refused(*a, **k) -> str:
    with pytest.raises(ProposalRefused) as exc:
        _spec().write.build(*a, **k)
    return str(exc.value)


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


def _state(head="intake", status="active", category="intake") -> dict:
    return {
        "intake": {"category": "intake", "current_task": "intake", "status": "locked"},
        "material_numbers": {"category": "material_numbers", "current_task": "estimate_received", "status": "locked"},
        "labor_numbers": {"category": "labor_numbers", "current_task": "labor_numbers", "status": "locked"},
        "select_vendors": {"category": "select_vendors", "current_task": "select_vendors", "status": "locked"},
        "markup": {"category": "markup", "current_task": "markup", "status": "locked"},
        "send_out": {"category": "send_out", "current_task": "gc_pricing", "status": "locked"},
        category: {"category": category, "current_task": head, "status": status},
    }


def _project(head="intake", status="active", category="intake", **over) -> dict:
    base = {"id": PROJECT, "number": NUMBER, "name": "ZZ TEST Lane", "current_stage": head,
            "abandoned_at": None, "failed_to_bid_at": None, "category_state": _state(head, status, category)}
    base.update(over)
    return base


def _args(**over) -> dict:
    base = {"project_id": PROJECT, "category": "intake", "note": None}
    base.update(over)
    return base


def _file(category="electrical_drawing") -> dict:
    return {"id": "f1f1f1f1-f1f1-4f1f-8f1f-f1f1f1f1f1f1", "category": category, "filename": "E-1.pdf"}


def _build(project=None, args=None, extra=None, **ctx_over):
    args = args or _args()
    reads = [project or _project(), *(extra or [])]
    proposal = _spec().write.build(args, reads, _ctx(**ctx_over))
    _check(proposal, args, reads)
    return proposal


def _check(proposal, args, reads):
    spec = _spec()
    assert proposal.request.route == spec.route
    canonical_json(proposal.request.json)
    canonical_json(dict(proposal.request.headers))
    target, gate = spec.write.snapshot(args, reads)
    assert (proposal.target, proposal.gate) == (target, gate)
    precondition_for(target, gate)
    for text in _walk_strings(proposal.preview):
        assert EM_DASH not in text
    assert EM_DASH not in proposal.summary
    for req in spec.write.preview_requests(args):
        assert req.route in spec.routes


# ── literals ─────────────────────────────────────────────────────────────────


def _route_table() -> set[tuple[str, str]]:
    return {(m, r.path_format) for r in app.main.app.routes if isinstance(r, APIRoute) for m in (r.methods or ())}


def test_route_literals_are_live_and_equal_the_route_side():
    table = _route_table()
    for route in _spec().routes:
        assert route in table, route
    assert tool.ADVANCE_POST == ("POST", "/projects/{project_id}/advance")
    assert route_side.router.prefix == "/projects/{project_id}"


def test_lane_literals_equal_the_service():
    lanes = {c: tuple(t) for c, t in service.CATEGORY_TASKS.items() if c != "send_out"}
    assert tool.CATEGORY_TASKS == lanes
    assert set(tool.CATEGORIES) == set(lanes) and "send_out" not in tool.CATEGORIES
    assert tool.PANEL_OWNED_HEADS == service.PANEL_OWNED_HEADS
    assert set(tool.PANEL_TEXTS) == set(service.PANEL_OWNED_HEADS)
    for key, label in tool.STAGE_LABELS.items():
        assert service.STAGES[key].label == label, key
    assert tool.LANE_LABELS == service.CATEGORY_LABELS
    for tasks in tool.CATEGORY_TASKS.values():
        for head in tasks:
            if head in tool.HEAD_OWNERS:
                assert service.internal_owner_role_for(head).value == tool.HEAD_OWNERS[head], head
    assert set(tool.ROLE_LABELS) >= set(tool.HEAD_OWNERS.values())
    assert tool.DRAWING_CATEGORIES == DRAWING_CATEGORIES


def test_optional_read_routes_are_internal_routes_of_the_served_app():
    for name in ("ASSIGNMENTS_GET", "FILES_GET", "RFQ_SENDS_GET"):
        assert getattr(tool, name) in _route_table(), name


# ── spec ─────────────────────────────────────────────────────────────────────


def test_spec_shape():
    spec = _spec()
    assert spec.effect == "write_internal" and spec.confirm and spec.write is not None
    assert spec.roles == WRITER_ROLES and Role.ACCOUNTANT not in spec.roles
    assert spec.namespace == "bidding" and spec.sub_app == "bidding"
    assert spec.deferred and spec.keywords
    assert spec.max_cards_per_turn == 3
    assert spec.write.bound_text_fields == ("note",)
    assert spec.write.revert_tool is None and spec.write.creates is None
    assert spec.write.writes_tables == ("project_category_state", "stage_events", "projects", "notifications",
                                        "email_log")
    props = spec.schema["properties"]
    assert set(props) == {"project_id", "category", "note"}
    assert props["category"]["enum"] == ["intake", "material_numbers", "labor_numbers", "select_vendors", "markup"]
    assert "gono_action" not in props and "to_stage" not in props and "expected_task" not in props
    assert EM_DASH not in json.dumps(spec.definition())


def test_only_the_preview_reads_are_reachable_inside_a_turn():
    allow = registry.turn_allowlist([_spec()])
    assert tool.ADVANCE_POST not in allow
    assert tool.PROJECT_GET in allow


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES))
def test_toolset_has_the_card_for_writers_only(role):
    names = {s.name for s in registry.toolset_for(SimpleNamespace(id="u", role=role, is_active=True), FLAGS)}
    assert ("advance_lane" in names) == (role in WRITER_ROLES)
    off = {s.name for s in registry.toolset_for(SimpleNamespace(id="u", role=role, is_active=True),
                                                {**FLAGS, "bidding": False})}
    assert "advance_lane" not in off


# ── requests ─────────────────────────────────────────────────────────────────


def test_preview_requests_by_lane():
    spec = _spec()
    intake = spec.write.preview_requests(_args())
    assert [r.route for r in intake] == [tool.PROJECT_GET, tool.ASSIGNMENTS_GET, tool.FILES_GET]
    assert intake[0].path == f"/projects/{PROJECT}" and not intake[0].optional
    assert all(r.optional for r in intake[1:])
    material = spec.write.preview_requests(_args(category="material_numbers"))
    assert [r.route for r in material] == [tool.PROJECT_GET, tool.RFQ_SENDS_GET]
    assert material[1].path == f"/projects/{PROJECT}/rfqs/sends" and material[1].optional
    for category in ("labor_numbers", "select_vendors", "markup"):
        assert [r.route for r in spec.write.preview_requests(_args(category=category))] == [tool.PROJECT_GET]


@pytest.mark.parametrize("bad", [{"category": "send_out"}, {"category": "nope"}, {"category": None},
                                 {"project_id": "x"}, {"project_id": None}])
def test_argument_errors(bad):
    with pytest.raises(ToolArgumentError):
        _spec().write.preview_requests(_args(**bad))


def test_note_is_normalized_and_capped():
    assert _build(args=_args(note="  move   it  ")).request.json["note"] == "move it"
    assert _build(args=_args(note="   ")).request.json["note"] is None
    with pytest.raises(ToolArgumentError, match="at most 300"):
        _build(args=_args(note="n" * 301))


# ── the card and the forced request ──────────────────────────────────────────


def test_intake_card_names_the_park_and_the_handoff():
    p = _build()
    assert p.project_id == PROJECT
    assert p.request.path == f"/projects/{PROJECT}/advance"
    assert p.request.json == {"category": "intake", "gono_action": "review", "expected_task": "intake", "note": None}
    assert p.request.headers == {tool.ASSISTANT_IDEMPOTENCY_HEADER: APPROVAL}
    card = p.preview
    assert card["title"] == f"Intake: Intake to Go / No-Go on {NUMBER}"
    assert card["project"]["number"] == NUMBER and card["items"] == []
    fields = {r["field"]: r["after"] for r in card["rows"]}
    assert fields["lane"] == "Intake" and fields["from_task"] == "Intake" and fields["to_task"] == "Go / No-Go"
    assert fields["go_no_go"] == "Parks at Go/No-Go for a manual decision, no score applied"
    assert fields["handoff"] == "Notifies the Executive by bell and email"
    assert [w["code"] for w in card["warnings"]] == ["no_undo"]
    assert card["warnings"][-1]["text"] == "Cannot be undone from here."
    assert p.target == {"category": "intake", "current_task": "intake", "status": "active"}
    assert p.gate == {"closed": False, "phase": "open"}
    assert "Go / No-Go" in p.summary


def test_the_request_always_forces_review_and_the_previewed_head_whatever_the_arguments_say():
    args = _args(note="ok", gono_action="go", expected_task="markup", to_stage="declined")
    p = _build(args=args)
    assert p.request.json == {"category": "intake", "gono_action": "review", "expected_task": "intake", "note": "ok"}
    for category, head in (("material_numbers", "rfqs"), ("labor_numbers", "labor_numbers"),
                           ("select_vendors", "select_vendors"), ("markup", "markup")):
        extra = [[]] if category == "material_numbers" else []
        p = _build(_project(head, category=category), _args(category=category, gono_action="no_go"), extra)
        assert p.request.json["gono_action"] == "review" and p.request.json["expected_task"] == head


@pytest.mark.parametrize(("category", "head", "to_label", "handoff"), [
    ("intake", "to_estimator", "Intake complete", "other lanes"),
    ("material_numbers", "estimate_received", "RFQs", "Materials Engineer"),
    ("material_numbers", "rfqs", "Receive Quotes", "Materials Engineer"),
    ("material_numbers", "receive_quotes", "Material Numbers complete", "other lanes"),
    ("labor_numbers", "labor_numbers", "Labor Numbers complete", "other lanes"),
    ("markup", "markup", "Markup complete", "other lanes"),
])
def test_to_label_and_handoff_by_head(category, head, to_label, handoff):
    extra = [[_file()]] if head == "to_estimator" else [[]] if category == "material_numbers" else []
    if head == "to_estimator":
        extra = [[{"id": "a", "revoked_at": None}], [_file()]]
    p = _build(_project(head, category=category), _args(category=category), extra)
    fields = {r["field"]: r["after"] for r in p.preview["rows"]}
    assert fields["to_task"] == to_label and handoff in fields["handoff"]


def test_note_is_shown_on_the_card():
    fields = {r["field"]: r["after"] for r in _build(args=_args(note="per the GC call")).preview["rows"]}
    assert fields["note"] == "per the GC call"


# ── refusals ─────────────────────────────────────────────────────────────────


def test_closed_projects_are_refused():
    for over in ({"abandoned_at": "2026-10-01T00:00:00+00:00"}, {"failed_to_bid_at": "2026-10-01T00:00:00+00:00"},
                 {"current_stage": "declined"}):
        assert "abandoned, declined" in _refused(_args(), [_project(**over)], _ctx())
    for stage in ("pm_only", "cp_only"):
        assert "not in the bidding flow" in _refused(_args(), [_project(current_stage=stage)], _ctx())


def test_lane_state_refusals():
    assert "already complete" in _refused(_args(), [_project("intake", "complete")], _ctx())
    assert "not active yet" in _refused(_args(), [_project("intake", "locked")], _ctx())
    assert "not active yet" in _refused(_args(category="markup"), [_project("intake")], _ctx())   # markup locked
    assert "not active yet" in _refused(_args(), [_project("gc_pricing")], _ctx())               # not this lane's task
    assert "not active yet" in _refused(_args(), [{"id": PROJECT, "number": NUMBER}], _ctx())    # no category state


def test_panel_owned_heads_are_refused_with_the_page_that_completes_them():
    text = _refused(_args(), [_project("go_no_go")], _ctx())
    assert text == tool.PANEL_TEXTS["go_no_go"] and "decided on its own page" in text
    for head, message in tool.PANEL_TEXTS.items():
        assert EM_DASH not in message and head.replace("_", " ").split()[0].lower() in message.lower().replace("/", " ")


def test_to_estimator_needs_a_drawing_and_an_unreadable_list_does_not_block():
    head = _project("to_estimator")
    assert _refused(_args(), [head, [], [_file("proposal"), _file("other")]], _ctx()) == tool.NO_DRAWING_TEXT
    assert _refused(_args(), [head, [], []], _ctx()) == tool.NO_DRAWING_TEXT
    for category in sorted(DRAWING_CATEGORIES):
        _build(head, extra=[[], [_file(category)]])
    _build(head, extra=[None, None])        # both optional reads 404: the route's own check stands
    _build(head, extra=[[{"id": "a"}], [_file()]])


def test_warnings_for_a_missing_estimator_and_no_sent_rfq():
    head = _project("to_estimator")
    none = _build(head, extra=[[], [_file()]])
    assert [w["code"] for w in none.preview["warnings"]] == ["no_estimator", "no_undo"]
    revoked = _build(head, extra=[[{"id": "a", "revoked_at": "2026-10-01T00:00:00+00:00"}], [_file()]])
    assert [w["code"] for w in revoked.preview["warnings"]] == ["no_estimator", "no_undo"]
    active = _build(head, extra=[[{"id": "a", "revoked_at": None}], [_file()]])
    assert [w["code"] for w in active.preview["warnings"]] == ["no_undo"]
    assert [w["code"] for w in _build(head, extra=[None, [_file()]]).preview["warnings"]] == ["no_undo"]

    rfqs = _project("rfqs", category="material_numbers")
    args = _args(category="material_numbers")
    unsent = _build(rfqs, args, [[{"id": "s", "status": "failed"}]])
    assert [w["code"] for w in unsent.preview["warnings"]] == ["no_rfqs_sent", "no_undo"]
    assert [w["code"] for w in _build(rfqs, args, [[]]).preview["warnings"]] == ["no_rfqs_sent", "no_undo"]
    assert [w["code"] for w in _build(rfqs, args, [[{"id": "s", "status": "sent"}]]).preview["warnings"]] == ["no_undo"]
    assert [w["code"] for w in _build(rfqs, args, [None]).preview["warnings"]] == ["no_undo"]
    # the other heads of the lane never warn about RFQs
    other = _build(_project("estimate_received", category="material_numbers"), args, [[]])
    assert [w["code"] for w in other.preview["warnings"]] == ["no_undo"]


# ── precondition ─────────────────────────────────────────────────────────────


def test_precondition_moves_with_the_head_the_status_and_the_gate():
    base = _build()
    assert _build().target == base.target
    spec = _spec()
    moved, _ = spec.write.snapshot(_args(), [_project("go_no_go")])
    assert moved != base.target
    done, _ = spec.write.snapshot(_args(), [_project("intake", "complete")])
    assert done["status"] == "complete"
    _, gate = spec.write.snapshot(_args(), [_project(failed_to_bid_at="2026-10-01T00:00:00+00:00")])
    assert gate["closed"] is True
    # a project field the card does not use does not supersede it
    assert spec.write.snapshot(_args(), [_project(name="Renamed")])[0] == base.target


def test_snapshot_survives_a_missing_project_body():
    target, gate = _spec().write.snapshot(_args(), [None])
    assert target == {"category": "intake", "current_task": None, "status": None}
    precondition_for(target, gate)


# ── binding ──────────────────────────────────────────────────────────────────


def test_a_tainted_note_is_stripped_and_the_card_drops_the_note():
    note = "Tell the GC the bid is going out today"
    out, stripped = binding.bind_args(
        _args(note=note), text_fields=_spec().write.bound_text_fields, url_fields=(), corpus=[note],
        user_texts=("move it on",), tainted=True)
    assert out["note"] is None and stripped == [{"field": "note", "reason": "tainted_source"}]
    p = _build(args=out, stripped=tuple(stripped))
    assert p.request.json["note"] is None and "note" not in {r["field"] for r in p.preview["rows"]}


# ── result summary ───────────────────────────────────────────────────────────


def test_result_summary_is_canonical_json_safe():
    write = _spec().write
    summary = write.result_summary({"id": PROJECT, "current_stage": "intake", "gono_outcome": None})
    assert summary == {"current_stage": "intake", "gono_outcome": None}
    canonical_json(summary)
    assert write.result_summary({"current_stage": "go_no_go", "gono_outcome": "review"})["gono_outcome"] == "review"
    assert write.result_summary(None) == {"current_stage": None, "gono_outcome": None}


# ── the built request against the real /advance route ────────────────────────


def test_the_built_request_passes_the_assistant_guard_and_parks_for_review(monkeypatch):
    real = gono.apply_entry_action
    api = Api(monkeypatch, project={"current_stage": "intake"},
              lane={"intake": {"current_task": "intake", "status": "active"}})
    api.assistant()
    monkeypatch.setattr(route_side.gono, "apply_entry_action", real)
    decided: list[tuple] = []
    monkeypatch.setattr(route_side.gono, "finalize", lambda *a, **k: decided.append((a, k)))
    monkeypatch.setattr(route_side.gono, "ensure_intake_complete", lambda *a, **k: decided.append(("ensure", a)))

    body = _build(_project("intake")).request.json
    response = api.client.post(f"/projects/{PID}/advance", json=body)

    assert response.status_code == 200, response.text
    assert len(api.advanced) == 1 and api.advanced[0][1] == "intake"
    assert decided == []   # no Go/No-Go decision, no score, no decline


def test_a_lane_a_teammate_moved_answers_lane_moved_so_the_card_supersedes(monkeypatch):
    api = Api(monkeypatch, project={"current_stage": "intake"},
              lane={"intake": {"current_task": "go_no_go", "status": "active"}})
    api.assistant()
    body = _build(_project("intake")).request.json
    response = api.client.post(f"/projects/{PID}/advance", json=body)
    assert response.status_code == 409 and response.json()["detail"] == {"code": "lane_moved", "head": "go_no_go"}
    assert api.advanced == []


@pytest.mark.parametrize("over", [
    {"abandoned_at": "2026-10-01T00:00:00+00:00"}, {"current_stage": "declined"},
    {"failed_to_bid_at": "2026-10-01T00:00:00+00:00"}, {"current_stage": "pm_only"}, {"current_stage": "cp_only"},
])
def test_the_route_refuses_the_same_closed_projects_the_tool_does(monkeypatch, over):
    api = Api(monkeypatch, project=over, lane={"intake": {"current_task": "intake", "status": "active"}})
    api.assistant()
    body = _build(_project("intake")).request.json
    response = api.client.post(f"/projects/{PID}/advance", json=body)
    assert response.status_code == 409 and api.advanced == []


# ── end to end through the approvals engine ──────────────────────────────────


class FakeBDR:
    def __init__(self):
        self.project = _project("intake")
        self.sent: list[dict] = []
        self.advance_status = 200

    def send(self, req, token):
        self.sent.append({"route": req.route, "path": req.path, "json": copy.deepcopy(req.json),
                          "headers": dict(req.headers or {})})
        if req.route == tool.PROJECT_GET:
            return 200, copy.deepcopy(self.project)
        if req.route == tool.ASSIGNMENTS_GET:
            return 200, []
        if req.route == tool.FILES_GET:
            return 200, [_file()]
        if req.route == tool.ADVANCE_POST:
            if self.advance_status == 409:
                return 409, {"detail": {"code": "lane_moved", "head": "go_no_go"}}
            self.project = _project("go_no_go")
            return 200, {"id": PROJECT, "current_stage": "go_no_go"}
        return 404, {"detail": "Not Found"}


@pytest.fixture
def env(monkeypatch):
    s = SimpleNamespace(assistant_token_secret=SECRET, assistant_enabled=True)
    monkeypatch.setattr(principal, "get_settings", lambda: s)
    monkeypatch.setattr(access, "get_settings", lambda: s)
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[delegation()], assistant_approvals=[], assistant_tool_calls=[],
        assistant_tool_results=[], assistant_messages=[pending_answer(status="waiting_approval")],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    # The approvals job bells through notifications, which binds the real
    # get_supabase by name.
    bells: list[dict] = []
    monkeypatch.setattr(notifications, "notify_user", lambda user_id, project_id, type_, message, **kw:
                        bells.append({"user_id": user_id, "type": type_, "message": message, **kw}))
    monkeypatch.setattr(features, "enabled_map", lambda role=None: dict(FLAGS))
    monkeypatch.setattr(jobs, "_renew_lease", lambda: True)
    bdr = FakeBDR()
    monkeypatch.setattr(executor.Executor, "_send_via_shim", lambda self, req, token: bdr.send(req, token))
    return SimpleNamespace(db=db, bdr=bdr, bells=bells)


def _turn() -> executor.Executor:
    user = SimpleNamespace(id="u", role=Role.EXECUTIVE, is_active=True)
    specs = {s.name: s for s in registry.toolset_for(user, FLAGS)}
    return executor.Executor(user_id=USER, user_label="Tom Moore", run_id=TURN_RUN, specs=specs,
                             lease_owner="worker:turn:1", conversation_id=CONV, message_id=MSG_ASSISTANT,
                             role=Role.EXECUTIVE)


def _propose(args):
    res = approvals.propose(_turn(), "advance_lane", args, user_texts=("move it on",), conversation_id=CONV,
                            message_id=MSG_ASSISTANT)
    assert res.ok, res.model_output
    return res.approval


def _approve_and_run(card: dict) -> dict:
    decided = repo.decide_approval(USER, card["id"], decision="approve", payload_sha256=card["payload_sha256"],
                                   precondition=card["precondition"], session_aal="aal2")
    assert decided["ok"], decided
    approvals_job.run_approval_execute({"approval_id": card["id"], "user_id": USER})
    return repo.get_approval(USER, card["id"])


def _advances(env):
    return [s for s in env.bdr.sent if s["route"] == tool.ADVANCE_POST]


def test_e2e_the_card_executes_one_forced_request(env):
    card = _propose(_args(note="per the GC call"))
    assert card["project_id"] == PROJECT and card["preview"]["warnings"][-1]["code"] == "no_undo"
    row = _approve_and_run(card)
    assert row["status"] == "executed", row
    (post,) = _advances(env)
    assert post["json"] == {"category": "intake", "gono_action": "review", "expected_task": "intake",
                            "note": "per the GC call"}
    assert post["headers"] == {tool.ASSISTANT_IDEMPOTENCY_HEADER: card["id"]}
    assert row["result"]["summary"] == {"current_stage": "go_no_go", "gono_outcome": None}
    out = approvals.out_for(USER, row)
    assert out["revertible"] is False


def test_e2e_a_lane_moved_by_a_teammate_supersedes_the_card(env):
    card = _propose(_args())
    env.bdr.advance_status = 409
    env.bdr.project = _project("intake")
    row = _approve_and_run(card)
    assert approvals.display_status(row) == "superseded"
    fresh = repo.get_approval(USER, row["superseded_by"])
    assert fresh["status"] == "pending" and fresh["preview"]["rows"][2]["after"] == "Intake"


def test_e2e_a_lane_that_reached_a_panel_head_before_approval_expires_without_posting(env):
    card = _propose(_args())
    env.bdr.project = _project("go_no_go")
    row = _approve_and_run(card)
    # The changed record is Go/No-Go, which only a person decides: no new card is built.
    assert row["status"] == "expired" and row["expired_reason"] == "changed_since_preview"
    assert _advances(env) == []

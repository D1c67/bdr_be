"""The schedule tools (app/assistant/tools/schedules.py) against
docs/ASSISTANT_PHASE3_PLAN.md 3.6.

Invariants pinned here:

  literals   the route literals, the idempotency header, the delivery and
             status sets and the active cap equal the route side's;
  schemas    strict, every property required, only the keyword set phase 0
             proved live (no em dash anywhere);
  create     the draft is the one preview read (flat params); a draft that
             refuses, or 10 active routines, is ProposalRefused; build's
             request is the spec route with the approval id header; target
             and gate equal snapshot's; the next three runs ride in the
             preview, never in the gate; created_ref links to Routines;
             the recover read is a declared route and finds an inserted
             routine after a 5xx (the stranded-card path);
  tiers      schedule_list and schedule_preview are reads (only the preview
             taints, through project names); pause, resume, end and Run now
             are housekeeping with the 3.7.1 ceilings (3 calls, 1 tainted);
  served     through the real shim into app.main.app (firewall, principal,
             the routines router, repo over the fake): the list, the
             pause, the end and its tainted pause, the tainted refusals,
             and a card proposed, approved and executed end to end.

`Served` is the harness the memory tool and injection gate tests reuse.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant import access, approvals, approvals_job, jobs, registry, repo
from app.assistant import executor as executor_mod
from app.assistant.approvals import canonical_json, precondition_for
from app.assistant.executor import Executor
from app.assistant.registry import BuildContext, ProposalRefused
from app.assistant.tools import schedules
from app.assistant.tools._common import ToolArgumentError
from app.core import deps, supabase_client
from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES, Role
from app.services import notifications

from tests.assistant_fakes import (
    CONV, MSG_ASSISTANT, NOW, PROJECT, RUN, SCHEDULE, SCHEDULE2, SECRET, USER, FakeDB, conversation,
    delegation, message, pending_answer, profile, schedule, settings_row,
)

EM_DASH = chr(0x2014)
FLAGS = {"bidding": True, "pm": True, "certified_payroll": True}
APPROVAL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"
ALLOWED_SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum",
                                 "description"})
SCHEDULE_TOOLS = ("schedule_create", "schedule_list", "schedule_preview", "schedule_pause", "schedule_resume",
                  "schedule_end", "schedule_run_now")
HOUSEKEEPING = ("schedule_pause", "schedule_resume", "schedule_end", "schedule_run_now")
WEEKDAYS_7 = {"frequency": "weekdays", "hour": 7, "minute": 0, "days": [], "day_of_month": None,
              "interval": None, "window_start_hour": None, "window_end_hour": None}
REAL_SEND = executor_mod.Executor._send_via_shim


def create_args(**over) -> dict:
    base = {"recipe": "due_today", "cadence": dict(WEEKDAYS_7), "delivery": "chat_bell", "only_if_changed": False,
            "horizon_days": None, "project_id": None, "period": None, "week": None,
            "response_format": "concise", "limit": None}
    base.update(over)
    return base


# ── the served harness (reused by the memory and gate tests) ─────────────────


class RecipeData:
    """app/services/assistant_recipes as the routines router sees it: the
    rows a recipe returns, the watch state, the digest log."""

    def __init__(self):
        self.rows: list[dict] = []
        self.watch = "open"
        self.digest = False

    def rows_for(self, sb, recipe, *, at_utc, horizon_days=None, project_id=None):
        from app.assistant import recipes

        recipes.validate_params(recipe, horizon_days, project_id)
        watch = {"project_id": project_id, "state": self.watch} if project_id else None
        return {"recipe": recipe, "window": {"start": "s", "end": "e", "label": "Friday, October 23"},
                "rows": copy.deepcopy(self.rows), "watch": watch}

    def watch_state(self, sb, project_id):
        return self.watch

    def digest_sent(self, sb, user_id, local_date):
        return self.digest


def served_db(**tables) -> FakeDB:
    """The owner, the delegation, one conversation with its user message and
    the running answer, and empty phase 3 tables."""
    base = dict(
        profiles=[profile()], assistant_delegations=[delegation()], assistant_settings=[settings_row()],
        assistant_profiles=[], assistant_conversations=[conversation()],
        assistant_messages=[message(content="Stop sending that report."), pending_answer(status="running")],
        assistant_tool_calls=[], assistant_tool_results=[], assistant_approvals=[], assistant_schedules=[],
        assistant_schedule_runs=[], assistant_memories=[], assistant_memory_events=[],
        assistant_memory_revisions=[], projects=[{"id": PROJECT, "number": "26.10.7267", "name": "Alpha Clinic"}],
        audit_log=[], llm_call_log=[],
    )
    base.update(tables)
    return FakeDB(**base)


class Served:
    """Every /assistant request an executor sends goes through the real shim
    into app.main.app: the firewall, the allowlist, the bdra_ principal, the
    provenance scope and the phase 3 routers, over the fake database. Any
    other path is answered from `payloads` ({path: (status, body)}), so a
    test can plant text in a BDR read. `log` holds every request sent."""

    def __init__(self, monkeypatch, db: FakeDB, payloads: dict | None = None):
        from app.routers import assistant_routines

        self.db = db
        self.payloads = dict(payloads or {})
        self.log: list[dict] = []
        self.data = RecipeData()
        for module in (supabase_client, deps, notifications):
            monkeypatch.setattr(module, "get_supabase", lambda: db)
        s = get_settings()
        for key, value in dict(assistant_enabled=True, assistant_token_secret=SECRET, bidding_enabled=True,
                               assistant_scheduler_enabled=True, rate_limit_enabled=False).items():
            monkeypatch.setattr(s, key, value)
        monkeypatch.setattr(access, "env_enabled", lambda: True)
        monkeypatch.setattr(assistant_routines, "_data", lambda: self.data)
        monkeypatch.setattr(jobs, "_renew_lease", lambda: True)
        self.bells: list[tuple] = []
        monkeypatch.setattr(notifications, "notify_user", lambda *a, **kw: self.bells.append((a, kw)))
        monkeypatch.setattr(executor_mod.Executor, "_send_via_shim",
                            lambda ex, req, token: self.send(ex, req, token))

    def send(self, ex: Executor, req, token):
        if req.path.startswith("/assistant/"):
            status, payload = REAL_SEND(ex, req, token)
        else:
            answer = self.payloads.get(req.path, (404, {"detail": "Not Found"}))
            status, payload = answer(req) if callable(answer) else answer
            payload = copy.deepcopy(payload)
        self.log.append({"method": req.method, "route": req.route, "path": req.path, "status": status,
                         "headers": dict(req.headers or {}), "approval_id": ex.approval_id})
        return status, payload

    def executor(self, *, role: Role = Role.EXECUTIVE, tainted: bool = False, run_id: str = RUN) -> Executor:
        user = SimpleNamespace(id=USER, role=role, is_active=True)
        specs = {s.name: s for s in registry.toolset_for(user, FLAGS)}
        return Executor(user_id=USER, user_label="Tom Moore", run_id=run_id, specs=specs,
                        lease_owner="worker:turn:1", conversation_id=CONV, message_id=MSG_ASSISTANT,
                        role=role, tainted_context=tainted)

    def sent(self, method: str | None = None) -> list[dict]:
        return [r for r in self.log if method is None or r["method"] == method]


@pytest.fixture
def served(monkeypatch):
    return Served(monkeypatch, served_db(assistant_schedules=[schedule()]))


def _spec(name):
    return registry.all_tools()[name]


def _ctx(**over) -> BuildContext:
    base = dict(approval_id=APPROVAL, role=Role.EXECUTIVE, short=lambda v: str(v)[:8], tainted=False,
                stripped=(), now=datetime.now(timezone.utc), is_revert=False)
    base.update(over)
    return BuildContext(**base)


def _draft(**over) -> dict:
    """GET /assistant/schedules/draft as the route answers a good routine."""
    base = {
        "ok": True, "code": None, "message": None,
        "body": {"recipe": "due_today", "cadence": {**WEEKDAYS_7, "interval": 1}, "delivery": "chat_bell",
                 "only_if_changed": False, "horizon_days": None, "project_id": None},
        "title": "Bids due today, weekdays at 7:00 AM", "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0",
        "cadence_text": "Weekdays at 7:00 AM Pacific, holidays skipped",
        "next_runs": ["2026-10-26T14:00:00+00:00", "2026-10-27T14:00:00+00:00", "2026-10-28T14:00:00+00:00"],
        "warnings": [{"code": "digest_overlap", "text": "The 6:03 AM daily digest email also lists these bids."}],
        "active_count": 2, "active_cap": 10, "project": None,
    }
    base.update(over)
    return base


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


# ── literals ─────────────────────────────────────────────────────────────────


def test_route_literals_equal_the_routes():
    from app.assistant import recipes, schemas
    from app.routers import assistant_routines as r
    from app.routers import projects

    assert schedules.SCHEDULES_GET == ("GET", r.SCHEDULES_ROUTE)
    assert schedules.SCHEDULES_POST == ("POST", r.SCHEDULES_ROUTE)
    assert schedules.DRAFT_GET == ("GET", r.SCHEDULE_DRAFT_ROUTE)
    assert schedules.PREVIEW_GET == ("GET", r.SCHEDULE_PREVIEW_ROUTE)
    assert schedules.BY_APPROVAL_GET == ("GET", r.SCHEDULE_BY_APPROVAL_ROUTE)
    assert schedules.SCHEDULE_PREVIEW_GET == ("GET", r.SCHEDULE_ID_PREVIEW_ROUTE)
    assert schedules.PAUSE_POST == ("POST", r.TOOL_PAUSE_ROUTE)
    assert schedules.RESUME_POST == ("POST", r.TOOL_RESUME_ROUTE)
    assert schedules.END_POST == ("POST", r.TOOL_END_ROUTE)
    assert schedules.RUN_NOW_POST == ("POST", r.TOOL_RUN_NOW_ROUTE)
    assert recipes.RECIPE_ROWS_ROUTE == ("GET", r.RECIPE_ROWS_ROUTE)
    assert schedules.ASSISTANT_IDEMPOTENCY_HEADER == projects.ASSISTANT_IDEMPOTENCY_HEADER
    assert schedules.ASSISTANT_IDEMPOTENCY_HEADER == executor_mod.IDEMPOTENCY_HEADER
    assert schedules.DELIVERIES == r.DELIVERIES
    assert set(schedules.STATUSES) == set(r.STATUS_FILTERS)
    assert schedules.ACTIVE_CAP == schemas.ACTIVE_SCHEDULE_CAP
    assert schedules.CAP_TEXT == r._MESSAGES[r.SCHEDULE_CAP_REACHED]
    # Every route the tools name is one the routines router serves.
    served_routes = {(m, route.path_format) for route in r.router.routes for m in route.methods}
    for name in SCHEDULE_TOOLS:
        for route in _spec(name).routes:
            assert route in served_routes, (name, route)


# ── schemas and tiers ────────────────────────────────────────────────────────


def _schema_keys(node, path="$"):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties":
                for prop, sub in value.items():
                    yield from _schema_keys(sub, f"{path}.{prop}")
            elif key == "items":
                yield from _schema_keys(value, f"{path}[]")
            yield (path, key)


@pytest.mark.parametrize("name", SCHEDULE_TOOLS)
def test_schemas_are_strict_with_the_phase_one_keywords(name):
    spec = _spec(name)
    assert [k for _p, k in _schema_keys(spec.schema) if k not in ALLOWED_SCHEMA_KEYS] == []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") in ("object", ["object", "null"]):
                assert node.get("additionalProperties") is False, name
                assert set(node.get("required") or ()) == set(node.get("properties") or {}), name
            for value in node.values():
                walk(value)
    walk(spec.schema)
    for text in (spec.description, spec.progress_label, json.dumps(spec.schema)):
        assert EM_DASH not in text


def test_tiers_namespaces_and_ceilings():
    tools = registry.all_tools()
    create = tools["schedule_create"]
    assert create.effect == "write_internal" and create.confirm and create.write is not None
    assert create.max_cards_per_turn == 1 and create.sub_app == "bidding" and create.roles == INTERNAL_ROLES
    assert create.write.creates == "schedule" and create.write.revert_tool is None
    assert create.write.writes_tables == ("assistant_schedules",)
    assert create.write.bound_text_fields == () and create.write.bound_url_fields == ()
    assert tools["schedule_list"].effect == "read" and tools["schedule_list"].sub_app is None
    assert tools["schedule_list"].taint_fields == ()
    assert tools["schedule_preview"].effect == "read" and tools["schedule_preview"].taint_fields == ("name",)
    for name in HOUSEKEEPING:
        spec = tools[name]
        assert spec.effect == registry.EFFECT_HOUSEKEEPING and spec.namespace == "schedule", name
        assert spec.sub_app == "bidding" and spec.roles == INTERNAL_ROLES, name
        assert spec.max_proposals_per_turn == 3 and spec.max_tainted_calls_per_turn == 1, name
    for name in SCHEDULE_TOOLS:
        assert tools[name].namespace == "schedule"


def test_accountant_may_keep_routines_and_review_mode_drops_them():
    def names(role, **kw):
        return {s.name for s in registry.toolset_for(SimpleNamespace(role=role), FLAGS, **kw)}

    assert set(SCHEDULE_TOOLS) <= names(Role.ACCOUNTANT)
    assert not set(SCHEDULE_TOOLS) & names(Role.EXECUTIVE, mode=registry.MODE_MAILBOX_REVIEW)
    # Incognito drops only memory.
    assert set(SCHEDULE_TOOLS) <= names(Role.EXECUTIVE, incognito=True)
    off = {s.name for s in registry.toolset_for(SimpleNamespace(role=Role.EXECUTIVE), {**FLAGS, "bidding": False})}
    assert "schedule_list" in off and not (set(SCHEDULE_TOOLS) - {"schedule_list"}) & off


# ── schedule_create: the draft, the card, the gate ───────────────────────────


def test_the_draft_is_the_one_preview_read_with_flat_params():
    spec = _spec("schedule_create")
    (req,) = spec.write.preview_requests(create_args())
    assert req.route == schedules.DRAFT_GET and req.route in spec.routes
    assert req.params == {"recipe": "due_today", "frequency": "weekdays", "hour": 7, "minute": 0,
                          "delivery": "chat_bell", "only_if_changed": "false"}
    weekly = {**WEEKDAYS_7, "frequency": "weekly", "days": ["MO", "TH"], "interval": 2, "minute": 30, "hour": 6}
    (req,) = spec.write.preview_requests(create_args(recipe="missing_quotes", cadence=weekly, horizon_days=14,
                                                     delivery="chat_bell_email", only_if_changed=True,
                                                     project_id=PROJECT))
    assert req.params == {"recipe": "missing_quotes", "frequency": "weekly", "hour": 6, "minute": 30,
                          "days": "MO,TH", "interval": 2, "delivery": "chat_bell_email",
                          "only_if_changed": "true", "horizon_days": 14, "project_id": PROJECT}
    with pytest.raises(ToolArgumentError):
        spec.write.preview_requests(create_args(cadence="every day"))
    with pytest.raises(ToolArgumentError):
        spec.write.preview_requests(create_args(project_id="not-an-id"))


def test_build_is_the_spec_route_with_the_approval_header_and_target_equals_snapshot():
    spec = _spec("schedule_create")
    args, reads = create_args(), [_draft()]
    proposal = spec.write.build(args, reads, _ctx())
    assert proposal.request.route == spec.route == schedules.SCHEDULES_POST
    assert proposal.request.path == "/assistant/schedules"
    assert proposal.request.headers == {schedules.ASSISTANT_IDEMPOTENCY_HEADER: APPROVAL}
    assert proposal.request.json == _draft()["body"]
    canonical_json(proposal.request.json)
    target, gate = spec.write.snapshot(args, reads)
    assert (proposal.target, proposal.gate) == (target, gate)
    assert gate == {"active_ok": True, "watch": None}
    precondition_for(target, gate)
    # The run times are display only: in the preview, never in the gate.
    assert proposal.preview["runs"] == _draft()["next_runs"]
    assert "runs" not in json.dumps(gate)
    later = _draft(next_runs=["2026-10-27T14:00:00+00:00", "2026-10-28T14:00:00+00:00",
                              "2026-10-29T14:00:00+00:00"])
    assert spec.write.snapshot(args, [later]) == (target, gate)
    rows = {r["field"]: r for r in proposal.preview["rows"]}
    assert rows["recipe"]["after"] == "Bids due today"
    assert rows["cadence"]["after"] == "Weekdays at 7:00 AM Pacific, holidays skipped"
    assert rows["delivery"]["after"] == "Chat and the bell"
    assert rows["only_if_changed"]["after"] == "No"
    # Nothing watched: the row is omitted, never shown as "Empty".
    assert "watch" not in rows
    assert proposal.preview["title"] == "Bids due today, weekdays at 7:00 AM"
    assert proposal.preview["tool_label"] == "New routine"
    assert proposal.preview["warnings"] == [{"code": "digest_overlap",
                                             "text": "The 6:03 AM daily digest email also lists these bids."}]
    assert proposal.summary == "New routine: Bids due today, weekdays at 7:00 AM. First run 2026-10-26 7:00 AM PT."
    for text in [*_walk_strings(proposal.preview), proposal.summary]:
        assert EM_DASH not in text


def test_a_watch_card_names_the_bid_and_gates_on_it():
    spec = _spec("schedule_create")
    body = {**_draft()["body"], "recipe": "labor_not_entered", "project_id": PROJECT}
    draft = _draft(body=body, title="Labor not entered on 26.10.7267, weekdays at 7:00 AM",
                   project={"id": PROJECT, "number": "26.10.7267", "name": "Alpha Clinic"}, warnings=[])
    args = create_args(recipe="labor_not_entered", project_id=PROJECT)
    proposal = spec.write.build(args, [draft], _ctx())
    assert proposal.project_id == PROJECT and proposal.gate == {"active_ok": True, "watch": "open"}
    rows = {r["field"]: r["after"] for r in proposal.preview["rows"]}
    assert rows["watch"] == "26.10.7267" and "horizon_days" not in rows
    assert proposal.preview["project"] == {"id": PROJECT, "number": "26.10.7267", "name": "Alpha Clinic"}
    # A window recipe without a watch shows its days ahead.
    window = _draft(body={**body, "recipe": "missing_quotes", "project_id": None, "horizon_days": 14},
                    title="Bids missing vendor quotes, weekdays at 7:00 AM")
    rows = {r["field"]: r["after"] for r in spec.write.build(create_args(recipe="missing_quotes"), [window],
                                                             _ctx()).preview["rows"]}
    assert rows["horizon_days"] == "14"


def test_period_and_week_are_forwarded_and_shown_on_the_card():
    """Phase 4 (3.10.1): the month of estimator_vs_bid and the week of
    quote_readiness ride in the draft read and show as card rows."""
    spec = _spec("schedule_create")
    props = spec.schema["properties"]
    assert props["period"]["type"] == ["string", "null"] and props["week"]["type"] == ["string", "null"]
    assert set(spec.schema["required"]) == set(props)
    assert {"estimator_vs_bid", "quote_readiness"} <= set(props["recipe"]["enum"]) and len(props["recipe"]["enum"]) == 6

    (req,) = spec.write.preview_requests(create_args(recipe="estimator_vs_bid", period="2026-09"))
    assert req.params["period"] == "2026-09" and "week" not in req.params
    (req,) = spec.write.preview_requests(create_args(recipe="quote_readiness", week=" next_week "))
    assert req.params["week"] == "next_week" and "period" not in req.params
    (req,) = spec.write.preview_requests(create_args())
    assert "period" not in req.params and "week" not in req.params

    month = _draft(body={**_draft()["body"], "recipe": "estimator_vs_bid", "period": "2026-09", "week": None},
                   title="Estimator vs our price, weekdays at 7:00 AM", warnings=[])
    proposal = spec.write.build(create_args(recipe="estimator_vs_bid", period="2026-09"), [month], _ctx())
    rows = {r["field"]: r["after"] for r in proposal.preview["rows"]}
    assert rows["period"] == "September 2026" and "week" not in rows and "horizon_days" not in rows
    assert proposal.request.json["period"] == "2026-09"          # the create body carries it too
    canonical_json(proposal.request.json)

    last = _draft(body={**_draft()["body"], "recipe": "estimator_vs_bid", "period": None, "week": None}, warnings=[])
    assert {r["field"]: r["after"] for r in spec.write.build(create_args(recipe="estimator_vs_bid"), [last],
                                                             _ctx()).preview["rows"]}["period"] == "last month"
    week = _draft(body={**_draft()["body"], "recipe": "quote_readiness", "week": "next_week"}, warnings=[])
    rows = {r["field"]: r["after"] for r in spec.write.build(create_args(recipe="quote_readiness"), [week],
                                                             _ctx()).preview["rows"]}
    assert rows["week"] == "next week" and "period" not in rows
    monday = _draft(body={**_draft()["body"], "recipe": "quote_readiness", "week": "2026-10-26"}, warnings=[])
    rows = {r["field"]: r["after"] for r in spec.write.build(create_args(recipe="quote_readiness"), [monday],
                                                             _ctx()).preview["rows"]}
    assert rows["week"] == "week of 10/26/2026"
    # A recipe that watches nothing shows no Watching row, and no row reads "Empty".
    assert "watch" not in {r["field"] for r in proposal.preview["rows"]}
    assert all(r["after"] != "Empty" for r in proposal.preview["rows"])
    # A phase 3 recipe shows neither row.
    plain = {r["field"] for r in spec.write.build(create_args(), [_draft()], _ctx()).preview["rows"]}
    assert not plain & {"period", "week"}
    for text in _walk_strings(proposal.preview):
        assert EM_DASH not in text


def test_the_description_names_the_six_recipes():
    description = _spec("schedule_create").description
    assert "Six recipes" in description and "estimator vs our price" in description
    assert "key quotes for the bid week" in description and EM_DASH not in description


def test_draft_refusals_and_the_cap_become_proposal_refused():
    spec = _spec("schedule_create")
    refused = {"ok": False, "code": "bad_cadence", "message": "The hour is required (0 to 23, Pacific time)."}
    with pytest.raises(ProposalRefused, match="The hour is required"):
        spec.write.build(create_args(), [refused], _ctx())
    with pytest.raises(ProposalRefused, match="10 active routines"):
        spec.write.build(create_args(), [_draft(active_count=10)], _ctx())
    with pytest.raises(ProposalRefused):
        spec.write.build(create_args(), [{}], _ctx())
    # A refusing draft at execute time gives a target that matches no card.
    target, gate = spec.write.snapshot(create_args(), [{"ok": False, "code": "watch_project_closed"}])
    assert target == {"refused": "watch_project_closed"} and gate == {"active_ok": False, "watch": "closed"}
    assert spec.write.snapshot(create_args(), [_draft(active_count=10)])[1]["active_ok"] is False


def test_created_ref_recover_and_result_summary():
    write = _spec("schedule_create").write
    row = {"id": SCHEDULE, "title": "Bids due today, weekdays at 7:00 AM", "status": "active"}
    assert write.created_ref(create_args(), row) == {
        "kind": "schedule", "id": SCHEDULE, "label": "Bids due today, weekdays at 7:00 AM",
        "href": f"/assistant?tab=routines&schedule={SCHEDULE}",
    }
    assert write.created_ref(create_args(), {}) is None
    req = write.recover_request(create_args(), APPROVAL)
    assert req.route in _spec("schedule_create").routes and req.route == schedules.BY_APPROVAL_GET
    assert req.path == f"/assistant/schedules/by-approval/{APPROVAL}"
    assert write.result_summary(row) == row


def test_the_recover_read_finds_an_inserted_routine_through_the_shim(monkeypatch):
    """approvals_job._recover over the declared by-approval GET: a row is
    "found", a 404 is "missing"."""
    spec = _spec("schedule_create")
    answers = {f"/assistant/schedules/by-approval/{APPROVAL}": (200, {"id": SCHEDULE, "title": "t"})}
    sent = []

    def send(self, req, token):
        sent.append(req.route)
        return answers.get(req.path, (404, {"detail": "Not Found"}))

    monkeypatch.setattr(executor_mod.Executor, "_send_via_shim", send)
    monkeypatch.setattr(get_settings(), "assistant_token_secret", SECRET)
    ex = Executor(user_id=USER, user_label="Tom Moore", run_id=RUN, specs={spec.name: spec},
                  lease_owner="worker:job:1", approval_id=APPROVAL)
    assert approvals_job._recover(ex, spec, create_args(), APPROVAL) == ("found", {"id": SCHEDULE, "title": "t"})
    assert approvals_job._recover(ex, spec, create_args(), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb") == ("missing", None)
    assert sent == [schedules.BY_APPROVAL_GET, schedules.BY_APPROVAL_GET]


# ── the reads ────────────────────────────────────────────────────────────────


def test_list_and_preview_requests():
    (req,) = _spec("schedule_list").requests({"status": None, "response_format": "concise", "limit": None})
    assert req.route == schedules.SCHEDULES_GET and req.params == {"status": "all"}
    (req,) = _spec("schedule_list").requests({"status": "paused"})
    assert req.params == {"status": "paused"}
    preview = _spec("schedule_preview")
    (req,) = preview.requests({"schedule_id": SCHEDULE, "recipe": None, "horizon_days": None, "project_id": None})
    assert req.route == schedules.SCHEDULE_PREVIEW_GET and req.path == f"/assistant/schedules/{SCHEDULE}/preview"
    (req,) = preview.requests({"schedule_id": None, "recipe": "missing_quotes", "horizon_days": 30, "project_id": None})
    assert req.route == schedules.PREVIEW_GET and req.params == {"recipe": "missing_quotes", "horizon_days": 30}
    with pytest.raises(ToolArgumentError):
        preview.requests({"schedule_id": None, "recipe": None})
    with pytest.raises(ToolArgumentError):
        _spec("schedule_end").requests({"schedule_id": "nope"})


def test_list_through_the_shim_is_server_built_and_never_taints(served):
    served.db.tables["assistant_schedules"].append(
        schedule(id=SCHEDULE2, status="paused", paused_reason="user", paused_at=NOW.isoformat()))
    ex = served.executor()
    out = ex.call("schedule_list", {"status": None, "response_format": "concise", "limit": None})
    assert out.ok and not out.tainted and not ex.tainted
    assert out.output["active"] == 1 and out.output["active_cap"] == 10
    by_status = {r["status"]: r for r in out.output["routines"]}
    active = by_status["active"]
    assert ex.ids[active["id"]] == SCHEDULE and active["title"] == "Bids due today, weekdays at 7:00 AM"
    assert active["repeats"] == "Weekdays at 7:00 AM Pacific, holidays skipped"
    assert len(active["next_runs"]) == 1 and active["next_runs"][0].endswith("7:00 AM PT")
    assert by_status["paused"]["paused_reason"] == "user" and "next_runs" not in by_status["paused"]
    (ledger,) = [r for r in served.db.tables["assistant_tool_calls"] if r["phase"] == "begun"]
    assert ledger["effect"] == "read" and ledger["tool_name"] == "schedule_list"


def test_preview_through_the_shim_taints_with_project_names(served):
    served.data.rows = [{
        "project": {"id": PROJECT, "name": "Alpha Clinic", "number": "26.10.7267",
                    "internal_bid_at": NOW.isoformat()},
        "checks": {"has_quotes": False, "has_labor": True, "has_markups": True}, "severity": "red",
        "missing_categories": None,
    }]
    ex = served.executor()
    out = ex.call("schedule_preview", {"schedule_id": SCHEDULE, "recipe": None, "horizon_days": None,
                                       "project_id": None, "response_format": "concise", "limit": None})
    assert out.ok, out.error
    assert out.output["content"] == "Bids due today: 1. Missing vendor quotes: 1. Missing labor: 0. Missing markups: 0."
    assert out.output["bids"][0]["name"] == "Alpha Clinic (#26.10.7267)" and out.output["bids"][0]["id"] == PROJECT[:8]
    assert out.output["note"] == "Preview only. Nothing was sent or saved."
    assert out.tainted and ex.tainted and ("name", "Alpha Clinic (#26.10.7267)") in ex.tainted_texts
    assert served.db.tables["assistant_schedule_runs"] == []


# ── housekeeping through the shim ────────────────────────────────────────────


def _hk(ex, name, sid=SCHEDULE):
    return ex.call(name, {"schedule_id": sid})


def test_pause_then_end_in_an_untainted_turn(served):
    ex = served.executor()
    listing = ex.call("schedule_list", {"status": None, "response_format": "concise", "limit": None})
    # The model passes the short id it was shown.
    (short,) = [r["id"] for r in listing.output["routines"]]
    out = _hk(ex, "schedule_pause", short)
    assert out.ok and out.output["status"] == "paused" and out.output["paused_reason"] == "user"
    out = _hk(ex, "schedule_end")
    assert out.ok and out.output["status"] == "ended" and out.output["ended_reason"] == "user"
    assert "note" not in out.output
    assert served.db.tables["assistant_schedules"][0]["status"] == "ended"
    posts = served.sent("POST")
    assert [p["path"] for p in posts] == [f"/assistant/tools/schedules/{SCHEDULE}/pause",
                                          f"/assistant/tools/schedules/{SCHEDULE}/end"]
    begun = [r for r in served.db.tables["assistant_tool_calls"] if r["phase"] == "begun" and r["effect"] == "housekeeping"]
    # Every housekeeping request carries its own ledger key.
    assert [p["headers"][schedules.ASSISTANT_IDEMPOTENCY_HEADER] for p in posts] == [b["idempotency_key"] for b in begun]


def test_end_in_a_tainted_turn_pauses_and_says_so(served):
    ex = served.executor(tainted=True)
    out = _hk(ex, "schedule_end", SCHEDULE)
    assert out.ok and out.output["status"] == "paused" and out.output["paused_reason"] == "user"
    assert out.output["note"] == "Paused. End it from Routines to stop it for good."
    row = served.db.tables["assistant_schedules"][0]
    assert row["status"] == "paused" and row["paused_reason"] == "user" and row["ended_at"] is None


def test_resume_and_run_now_are_refused_in_a_tainted_turn(served):
    served.db.tables["assistant_schedules"][0].update(status="paused", paused_reason="user",
                                                      paused_at=NOW.isoformat())
    ex = served.executor(tainted=True)
    for name in ("schedule_resume", "schedule_run_now"):
        out = _hk(ex, name, SCHEDULE)
        assert not out.ok and out.error_kind == "http_409", name
    assert served.db.tables["assistant_schedules"][0]["status"] == "paused"
    assert served.db.tables["assistant_schedule_runs"] == []


def test_resume_and_run_now_in_an_untainted_turn(served):
    served.db.tables["assistant_schedules"][0].update(status="paused", paused_reason="user",
                                                      paused_at=NOW.isoformat())
    ex = served.executor()
    out = _hk(ex, "schedule_resume", SCHEDULE)
    assert out.ok and out.output["status"] == "active" and out.output["next_run"].endswith("PT")
    out = _hk(ex, "schedule_run_now", SCHEDULE)
    assert out.ok, out.error
    assert out.output["status"] == "claimed" and out.output["message"] == schedules.RUN_QUEUED_TEXT
    (run,) = served.db.tables["assistant_schedule_runs"]
    assert run["kind"] == "run_now" and run["claimed_by"] is None


def test_a_human_never_reaches_a_tool_route(served):
    """The tool routes trust only a turn principal (a bare 404 for a
    human); a routine that is not the caller's is not found."""
    from fastapi.testclient import TestClient

    import app.main

    resp = TestClient(app.main.app).post(f"/assistant/tools/schedules/{SCHEDULE}/end")
    assert resp.status_code in (401, 403, 404)
    ex = served.executor()
    out = ex.call("schedule_end", {"schedule_id": "ffffffff-ffff-4fff-8fff-000000000009"})
    assert not out.ok and out.error_kind == "http_404"


# ── end to end: propose, approve, execute ────────────────────────────────────


def _approve_and_run(card: dict) -> dict:
    decided = repo.decide_approval(USER, card["id"], decision="approve", payload_sha256=card["payload_sha256"],
                                   precondition=card["precondition"], session_aal="aal2")
    assert decided["ok"], decided
    approvals_job.run_approval_execute({"approval_id": card["id"], "user_id": USER})
    return repo.get_approval(USER, card["id"])


def _propose(served, args, *, tainted=False):
    ex = served.executor(tainted=tainted)
    res = approvals.propose(ex, "schedule_create", args, user_texts=("Every weekday at 7, send me what is due",),
                            conversation_id=CONV, message_id=MSG_ASSISTANT)
    return res


def test_e2e_a_card_proposed_approved_and_executed(served):
    served.db.tables["assistant_schedules"] = []
    res = _propose(served, create_args())
    assert res.ok, res.model_output
    card = res.approval
    preview = card["preview"]
    assert preview["tool"] == "schedule_create" and len(preview["runs"]) == 3
    assert all(r.endswith("+00:00") for r in preview["runs"])
    assert preview["taint_level"] == "none" and served.db.tables["assistant_schedules"] == []
    assert [r["path"] for r in served.sent()] == ["/assistant/schedules/draft"]

    row = _approve_and_run(card)
    assert row["status"] == "executed", row
    (created,) = served.db.tables["assistant_schedules"]
    assert created["approval_id"] == card["id"] and created["status"] == "active"
    assert created["title"] == "Bids due today, weekdays at 7:00 AM"
    assert created["next_run_at"] and created["after_next_run_at"]
    assert row["result"]["created"] == {"kind": "schedule", "id": created["id"],
                                        "label": "Bids due today, weekdays at 7:00 AM",
                                        "href": f"/assistant?tab=routines&schedule={created['id']}"}
    (post,) = [r for r in served.sent("POST")]
    assert post["path"] == "/assistant/schedules" and post["approval_id"] == card["id"]
    assert post["headers"][schedules.ASSISTANT_IDEMPOTENCY_HEADER] == card["id"]


def test_e2e_a_tainted_card_is_high_and_still_writes_only_on_approve(served):
    served.db.tables["assistant_schedules"] = []
    res = _propose(served, create_args(), tainted=True)
    assert res.ok and res.approval["preview"]["taint_level"] == "high"
    assert any(w["code"] == "tainted_conversation" for w in res.approval["preview"]["warnings"])
    assert served.db.tables["assistant_schedules"] == [] and served.sent("POST") == []


def test_e2e_the_stranded_card_recovers_the_inserted_routine(served, monkeypatch):
    """The create commits, then the answer is lost (a 5xx): the execute job
    finds the row through the by-approval read and records it executed."""
    served.db.tables["assistant_schedules"] = []
    base = served.send

    def lossy(ex, req, token):
        status, payload = base(ex, req, token)
        if req.route == schedules.SCHEDULES_POST and status < 300:
            return 502, {"detail": "Bad Gateway"}
        return status, payload

    monkeypatch.setattr(executor_mod.Executor, "_send_via_shim", lambda ex, req, token: lossy(ex, req, token))
    res = _propose(served, create_args())
    assert res.ok, res.model_output
    row = _approve_and_run(res.approval)
    (created,) = served.db.tables["assistant_schedules"]
    assert row["status"] == "executed" and row["result"]["recovered"] is True
    assert row["result"]["created"]["id"] == created["id"]
    assert served.sent()[-1]["path"] == f"/assistant/schedules/by-approval/{res.approval['id']}"


def test_e2e_a_draft_refusal_makes_no_card(served):
    served.db.tables["assistant_schedules"] = [
        schedule(id=f"ffffffff-ffff-4fff-8fff-{i:012d}", approval_id=None) for i in range(10)]
    res = _propose(served, create_args())
    assert not res.ok and res.error_kind == "refused"
    assert "10 active routines" in res.model_output["error"]
    res = _propose(served, create_args(cadence={**WEEKDAYS_7, "hour": None}))
    assert not res.ok and res.error_kind == "refused" and "hour" in res.model_output["error"]
    assert served.db.tables["assistant_approvals"] == []


def test_schedule_preview_points_one_off_reports_at_coded_report():
    """Live gate 2026-10-24: three one-off report prompts ("compare the
    estimator's price with ours last month") called schedule_preview with a
    recipe instead of coded_report; the description now says it is for
    routines only and names the one-off tools."""
    text = registry.get("schedule_preview").description
    assert "only when the user asks about a routine" in text
    assert "coded_report" in text and "whats_due" in text
    assert chr(0x2014) not in text

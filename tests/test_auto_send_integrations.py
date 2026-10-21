"""Auto send wiring into the existing code (docs/AUTO_SEND.md 6.1-6.9).

Covers the estimator-round hook (system scope-lines draft, estimate warm-up),
the start_generation refactor behind POST /proposal-lines, the lines-review
bell and its dismissal, the abandoned 409 on the line routes, the
auto_send_pending flag on the project reads, the per-GC Revised Proposal
subject (and the reclaim matching it), the notification meta and deep link,
the notification log classifier, the file delete protection, and the
main.py lifespan gate. No database, no Graph: FakeDB plus monkeypatch.
"""

import inspect
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException
from postgrest.exceptions import APIError

import app.services as services_pkg
from app.core.deps import CurrentUser
from app.core.ratelimit import ai_rate_limit
from app.core.roles import Role
from app.models.schemas import (
    BidsTodayProjectOut,
    ProjectOut,
    ProposalGenerateIn,
    ProposalLinesIn,
    ProposalManualIn,
)
from app.routers import estimator as estimator_router
from app.routers import files as files_router
from app.routers import projects as projects_router
from app.routers import proposals as proposals_router
from app.services import auto_send_hooks as hooks
from app.services import estimator_rounds, llm, llm_queue, notification_email, notification_log
from app.services import proposal_scope
from app.services import proposal_send as psend
from tests.test_late_gc_pricing_approval import FakeDB, _Query

PID = "p1"
NOW_ISO = datetime.now(timezone.utc).isoformat()


def _user(role=Role.ESTIMATING_ENGINEER_LABOR, uid="u1"):
    return CurrentUser(id=uid, email="e@g3.com", role=role, is_active=True)


def _settings(**over):
    base = {"llm_queue_enabled": False}
    base.update(over)
    return types.SimpleNamespace(**base)


@pytest.fixture
def fake_auto_send(monkeypatch):
    """Install a stand-in `app.services.auto_send` (another agent owns the
    real one) so `from app.services import auto_send` resolves to it."""

    def _install(**funcs):
        mod = types.ModuleType("app.services.auto_send")
        for name, fn in funcs.items():
            setattr(mod, name, fn)
        monkeypatch.setitem(sys.modules, "app.services.auto_send", mod)
        monkeypatch.setattr(services_pkg, "auto_send", mod, raising=False)
        return mod

    return _install


@pytest.fixture
def scope_env(monkeypatch):
    """One FakeDB behind the hook, the scope service and the proposals router,
    with dispatch and audit recorded."""
    db = FakeDB({"projects": [{"id": PID, "name": "Sunset", "number": "26.10.7300",
                               "abandoned_at": None}]})
    audits: list[tuple] = []
    for mod in (hooks, proposal_scope, proposals_router):
        monkeypatch.setattr(mod, "get_supabase", lambda: db)
    monkeypatch.setattr(proposal_scope, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(proposals_router, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(proposal_scope, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        estimator_rounds, "resolve_boq_file_id", lambda pid, explicit: explicit or "boq-1"
    )
    return types.SimpleNamespace(db=db, audits=audits)


# ── 6.1 the estimator round hook ─────────────────────────────────────────────


def test_hook_starts_a_system_draft_when_none_exists(scope_env):
    bg = BackgroundTasks()
    hooks.on_estimator_round(PID, {"boq": 1}, bg)
    drafts = scope_env.db.tables["proposal_drafts"]
    assert len(drafts) == 1
    assert drafts[0]["created_by"] is None and drafts[0]["status"] == "pending"
    assert drafts[0]["boq_file_id"] == "boq-1"
    assert [t.func for t in bg.tasks] == [proposal_scope.run_generation]
    assert scope_env.audits == [
        (None, "proposal.lines_generate", "proposal_draft", drafts[0]["id"],
         {"boq_file_id": "boq-1", "source": "auto"})
    ]


def test_hook_starts_over_only_failed_drafts(scope_env):
    scope_env.db.tables["proposal_drafts"] = [
        {"id": "old", "project_id": PID, "status": "failed", "created_by": "u9"},
    ]
    hooks.on_estimator_round(PID, {"boq": 1, "estimate": 1}, BackgroundTasks())
    assert len(scope_env.db.tables["proposal_drafts"]) == 2


@pytest.mark.parametrize("status", ["done", "pending", "running"])
def test_hook_never_starts_over_an_existing_draft(scope_env, status):
    scope_env.db.tables["proposal_drafts"] = [
        {"id": "human", "project_id": PID, "status": status, "created_by": "u9",
         "lines_json": ["Edited by a person."]},
    ]
    assert hooks.maybe_start_scope_lines(PID, BackgroundTasks()) is None
    assert [d["id"] for d in scope_env.db.tables["proposal_drafts"]] == ["human"]
    assert scope_env.audits == []


@pytest.mark.parametrize("status", ["sent", "sending"])
def test_hook_skips_when_a_proposal_went_out(scope_env, status):
    scope_env.db.tables["proposal_sends"] = [{"id": "s1", "project_id": PID, "status": status}]
    assert hooks.maybe_start_scope_lines(PID) is None
    assert not scope_env.db.tables.get("proposal_drafts")


def test_hook_skips_an_abandoned_project(scope_env):
    scope_env.db.tables["projects"][0]["abandoned_at"] = NOW_ISO
    assert hooks.maybe_start_scope_lines(PID) is None
    assert not scope_env.db.tables.get("proposal_drafts")


def test_hook_skips_quietly_without_a_usable_boq(scope_env, monkeypatch):
    def _no_boq(pid, explicit):
        raise HTTPException(400, "No BOQ file uploaded for this project")

    monkeypatch.setattr(estimator_rounds, "resolve_boq_file_id", _no_boq)
    assert hooks.maybe_start_scope_lines(PID) is None
    assert not scope_env.db.tables.get("proposal_drafts")


def test_hook_without_a_boq_in_the_round_does_not_draft(scope_env):
    hooks.on_estimator_round(PID, {"estimate": 1}, BackgroundTasks())
    assert not scope_env.db.tables.get("proposal_drafts")


def test_hook_warms_the_estimate_in_the_background_not_inline(monkeypatch, fake_auto_send):
    calls = []
    est = types.ModuleType("app.services.auto_send_estimate")
    est.load_estimate = lambda pid, **kw: calls.append(pid) or {"ok": True}
    monkeypatch.setitem(sys.modules, "app.services.auto_send_estimate", est)
    monkeypatch.setattr(services_pkg, "auto_send_estimate", est, raising=False)
    bg = BackgroundTasks()
    hooks.on_estimator_round(PID, {"estimate": 1}, bg)
    assert calls == []  # not inline
    assert [(t.func, t.args) for t in bg.tasks] == [(hooks.warm_estimate, (PID,))]
    bg.tasks[0].func(*bg.tasks[0].args)
    assert calls == [PID]


def test_warm_estimate_swallows_failures(monkeypatch):
    est = types.ModuleType("app.services.auto_send_estimate")

    def _boom(pid, **kw):
        raise RuntimeError("parse exploded")

    est.load_estimate = _boom
    monkeypatch.setitem(sys.modules, "app.services.auto_send_estimate", est)
    monkeypatch.setattr(services_pkg, "auto_send_estimate", est, raising=False)
    hooks.warm_estimate(PID)  # no raise


def test_hook_isolates_a_draft_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(hooks, "maybe_start_scope_lines", _boom)
    hooks.on_estimator_round(PID, {"boq": 1}, BackgroundTasks())  # no raise


def _submit_env(monkeypatch, round_no, counts):
    db = FakeDB({
        "projects": [{"id": PID, "name": "Sunset", "number": "26.10.7300",
                      "current_stage": "estimating"}],
        "estimator_assignments": [],
    })
    monkeypatch.setattr(estimator_router, "get_supabase", lambda: db)
    seen_rounds = []

    def _create(pid, uid, impersonated_by=None):
        seen_rounds.append((pid, uid, impersonated_by))
        return {"round": round_no, "summary": counts}, []

    monkeypatch.setattr(estimator_router.estimator_rounds, "create_submission_round", _create)
    monkeypatch.setattr(estimator_router, "audit", lambda *a, **k: None)
    monkeypatch.setattr(estimator_router, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(estimator_router, "_queue_general_material", lambda *a, **k: None)
    monkeypatch.setattr(estimator_router, "_before_receive_quotes", lambda pid: True)
    monkeypatch.setattr(
        estimator_router.revision_email, "queue_revision_alert", lambda *a, **k: None
    )
    return seen_rounds


@pytest.mark.parametrize("round_no", [1, 2])
def test_submit_calls_the_hook_in_both_branches(monkeypatch, round_no):
    counts = {"estimate": 1, "boq": 1}
    _submit_env(monkeypatch, round_no, counts)
    seen = []
    monkeypatch.setattr(
        estimator_router.auto_send_hooks,
        "on_estimator_round",
        lambda pid, c, bg: seen.append((pid, c, bg)),
    )
    bg = BackgroundTasks()
    out = estimator_router.submit_deliverables(PID, bg, user=_user(Role.ESTIMATOR, "est1"))
    assert out == {"submitted": True, "round": round_no, "counts": counts}
    assert seen == [(PID, counts, bg)]


@pytest.mark.parametrize("round_no", [1, 3])
def test_a_failing_hook_never_fails_the_submit(monkeypatch, round_no):
    _submit_env(monkeypatch, round_no, {"boq": 1})

    def _boom(*a, **k):
        raise RuntimeError("auto send exploded")

    monkeypatch.setattr(estimator_router.auto_send_hooks, "on_estimator_round", _boom)
    out = estimator_router.submit_deliverables(
        PID, BackgroundTasks(), user=_user(Role.ESTIMATOR, "est1")
    )
    assert out["submitted"] is True and out["round"] == round_no


def test_submit_stamps_the_dev_behind_view_as(monkeypatch):
    """Finding 1: an impersonated submit seals the round under the estimator's
    id, so the dev's id must ride along to the round row."""
    seen = _submit_env(monkeypatch, 1, {"estimate": 1})
    monkeypatch.setattr(estimator_router, "_auto_send_round_hook", lambda *a, **k: None)
    user = CurrentUser(id="est1", email="e@x.test", role=Role.ESTIMATOR, is_active=True,
                       impersonated_by="u-dev")
    estimator_router.submit_deliverables(PID, BackgroundTasks(), user=user)
    estimator_router.submit_deliverables(
        PID, BackgroundTasks(), user=_user(Role.ESTIMATOR, "est1")
    )
    assert seen == [(PID, "est1", "u-dev"), (PID, "est1", None)]


def test_round_insert_carries_impersonated_by_only_when_set(monkeypatch):
    db = FakeDB({"estimator_submissions": [], "project_files": [
        {"id": "f1", "project_id": PID, "category": "estimate", "estimator_deliverable": True,
         "uploaded_by": "est1", "submission_round": None},
    ]})
    monkeypatch.setattr(estimator_rounds, "get_supabase", lambda: db)
    monkeypatch.setattr(estimator_rounds, "latest_submission", lambda pid: None)
    estimator_rounds.create_submission_round(PID, "est1", impersonated_by="u-dev")
    assert db.tables["estimator_submissions"][0]["impersonated_by"] == "u-dev"
    db.tables["estimator_submissions"] = []
    db.tables["project_files"][0]["submission_round"] = None
    estimator_rounds.create_submission_round(PID, "est1")
    assert "impersonated_by" not in db.tables["estimator_submissions"][0]


# ── start_generation refactor keeps the route ────────────────────────────────


def test_route_still_inserts_dispatches_and_audits_as_the_user(scope_env):
    bg = BackgroundTasks()
    out = proposals_router.start_lines_generation(
        PID, ProposalGenerateIn(), bg, user=_user(uid="u7")
    )
    row = scope_env.db.tables["proposal_drafts"][0]
    assert out["id"] == row["id"] and out["lines"] == []
    assert row["created_by"] == "u7" and row["boq_file_id"] == "boq-1"
    assert [(t.func, t.args) for t in bg.tasks] == [(proposal_scope.run_generation, (row["id"],))]
    assert scope_env.audits == [
        ("u7", "proposal.lines_generate", "proposal_draft", row["id"], {"boq_file_id": "boq-1"})
    ]


def test_route_passes_the_explicit_boq_through(scope_env):
    proposals_router.start_lines_generation(
        PID, ProposalGenerateIn(boq_file_id="boq-9"), BackgroundTasks(), user=_user()
    )
    assert scope_env.db.tables["proposal_drafts"][0]["boq_file_id"] == "boq-9"


def test_route_enqueues_with_the_user_when_the_queue_is_on(scope_env, monkeypatch):
    monkeypatch.setattr(proposal_scope, "get_settings", lambda: _settings(llm_queue_enabled=True))
    captured = {}
    monkeypatch.setattr(
        llm_queue, "enqueue", lambda jt, **kw: captured.update(job_type=jt, **kw) or {"id": "j"}
    )
    bg = BackgroundTasks()
    proposals_router.start_lines_generation(PID, ProposalGenerateIn(), bg, user=_user(uid="u7"))
    row = scope_env.db.tables["proposal_drafts"][0]
    assert captured["job_type"] == llm_queue.JOB_PROPOSAL
    assert captured["target_id"] == row["id"] and captured["payload"] == {"draft_id": row["id"]}
    assert captured["created_by"] == "u7" and captured["project_id"] == PID
    assert bg.tasks == []


def test_route_falls_back_to_background_when_the_enqueue_fails(scope_env, monkeypatch):
    monkeypatch.setattr(proposal_scope, "get_settings", lambda: _settings(llm_queue_enabled=True))

    def _down(*a, **k):
        raise RuntimeError("queue down")

    monkeypatch.setattr(llm_queue, "enqueue", _down)
    bg = BackgroundTasks()
    proposals_router.start_lines_generation(PID, ProposalGenerateIn(), bg, user=_user())
    assert [t.func for t in bg.tasks] == [proposal_scope.run_generation]


def test_route_still_400s_without_a_boq(scope_env, monkeypatch):
    def _no_boq(pid, explicit):
        raise HTTPException(400, "No BOQ file uploaded for this project")

    monkeypatch.setattr(estimator_rounds, "resolve_boq_file_id", _no_boq)
    with pytest.raises(HTTPException) as exc:
        proposals_router.start_lines_generation(
            PID, ProposalGenerateIn(), BackgroundTasks(), user=_user()
        )
    assert exc.value.status_code == 400
    assert not scope_env.db.tables.get("proposal_drafts")


def test_route_still_409s_on_an_active_generation(scope_env):
    scope_env.db.tables["proposal_drafts"] = [
        {"id": "d1", "project_id": PID, "status": "running",
         "created_at": NOW_ISO, "updated_at": NOW_ISO},
    ]
    with pytest.raises(HTTPException) as exc:
        proposals_router.start_lines_generation(
            PID, ProposalGenerateIn(), BackgroundTasks(), user=_user()
        )
    assert exc.value.status_code == 409


def test_route_keeps_its_rate_limit():
    route = next(
        r for r in proposals_router.router.routes
        if r.path == "/projects/{project_id}/proposal-lines" and "POST" in r.methods
    )
    assert any(d.call is ai_rate_limit for d in route.dependant.dependencies)


def test_start_generation_without_background_uses_a_thread(scope_env, monkeypatch):
    started = []

    class _Thread:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            started.append("started")

    monkeypatch.setattr(proposal_scope.threading, "Thread", _Thread)
    row = proposal_scope.start_generation(PID, "boq-1", None)
    assert started == [(proposal_scope.run_generation, (row["id"],), True), "started"]


# ── abandoned 409 on the scope-line routes ───────────────────────────────────


def _abandon(scope_env):
    scope_env.db.tables["projects"][0]["abandoned_at"] = NOW_ISO
    scope_env.db.tables["proposal_drafts"] = [
        {"id": "d1", "project_id": PID, "status": "done", "lines_json": ["Line one."]},
    ]


def _assert_abandoned_409(call):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 409
    assert exc.value.detail == psend.ABANDONED_MESSAGE
    assert "abandoned" in exc.value.detail


def test_generate_refuses_an_abandoned_bid(scope_env):
    _abandon(scope_env)
    _assert_abandoned_409(lambda: proposals_router.start_lines_generation(
        PID, ProposalGenerateIn(), BackgroundTasks(), user=_user()
    ))
    assert len(scope_env.db.tables["proposal_drafts"]) == 1


def test_manual_refuses_an_abandoned_bid(scope_env):
    _abandon(scope_env)
    _assert_abandoned_409(lambda: proposals_router.start_manual_lines(
        PID, ProposalManualIn(lines=[]), user=_user()
    ))


def test_save_refuses_an_abandoned_bid(scope_env):
    _abandon(scope_env)
    _assert_abandoned_409(lambda: proposals_router.save_lines(
        PID, "d1", ProposalLinesIn(lines=["New line."]), user=_user()
    ))
    assert scope_env.db.tables["proposal_drafts"][0]["lines_json"] == ["Line one."]


def test_approve_refuses_an_abandoned_bid(scope_env):
    _abandon(scope_env)
    _assert_abandoned_409(lambda: proposals_router.approve_lines(PID, "d1", user=_user()))
    assert "approved_at" not in scope_env.db.tables["proposal_drafts"][0]


def test_abandoned_message_is_shared_with_the_send_window():
    src = inspect.getsource(psend.send_window_head)
    assert "ProposalSendError(ABANDONED_MESSAGE)" in src
    assert "\u2014" not in psend.ABANDONED_MESSAGE


# ── 6.2 lines review bell + dismiss ──────────────────────────────────────────


def _execute_env(monkeypatch, created_by, switch_on=True):
    monkeypatch.setattr(proposal_scope, "_auto_send_on", lambda: switch_on)
    db = FakeDB({
        "projects": [{"id": PID, "name": "Sunset", "number": "26.10.7300"}],
        "proposal_drafts": [{"id": "d1", "project_id": PID, "status": "pending",
                             "boq_file_id": "boq-1", "created_by": created_by}],
    })
    monkeypatch.setattr(proposal_scope, "get_supabase", lambda: db)
    monkeypatch.setattr(proposal_scope, "_load_boq_text", lambda draft: "BOQ TEXT")
    monkeypatch.setattr(
        proposal_scope,
        "_call_llm",
        lambda text: {
            "lines": [{"text": "Furnish and install panels.", "category": "switchgear"}],
            "notes": None,
        },
    )
    monkeypatch.setattr(llm, "active_model", lambda *a, **k: "test-model")
    notes = []
    monkeypatch.setattr(proposal_scope, "notify_role", lambda *a, **k: notes.append((a, k)))
    return db, notes


def test_system_draft_done_notifies_the_labor_engineer(monkeypatch):
    db, notes = _execute_env(monkeypatch, None)
    proposal_scope.execute("d1")
    assert db.tables["proposal_drafts"][0]["status"] == "done"
    assert len(notes) == 1
    (role, project_id, type_, msg), _ = notes[0]
    assert role is Role.ESTIMATING_ENGINEER_LABOR
    assert project_id == PID and type_ == "auto_send.lines_review"
    assert "Sunset (26.10.7300)" in msg and "5:05 PM" in msg
    assert "\u2014" not in msg


def test_system_draft_done_is_quiet_while_auto_send_is_off(monkeypatch):
    """Finding 24: with the switch off nothing goes out at 5:05 PM, so the
    notice that says it will is not sent."""
    db, notes = _execute_env(monkeypatch, None, switch_on=False)
    proposal_scope.execute("d1")
    assert db.tables["proposal_drafts"][0]["status"] == "done"
    assert notes == []


def test_auto_send_switch_read_fails_toward_quiet(monkeypatch, fake_auto_send):
    def _boom():
        raise RuntimeError("settings down")

    fake_auto_send(get_settings_row=_boom)
    assert proposal_scope._auto_send_on() is False
    fake_auto_send(get_settings_row=lambda: {"enabled": True})
    assert proposal_scope._auto_send_on() is True


def test_human_draft_done_does_not_notify(monkeypatch):
    _, notes = _execute_env(monkeypatch, "u7")
    proposal_scope.execute("d1")
    assert notes == []


def test_lines_review_notice_failure_never_fails_the_draft(monkeypatch):
    db, _ = _execute_env(monkeypatch, None)

    def _boom(*a, **k):
        raise RuntimeError("bell down")

    monkeypatch.setattr(proposal_scope, "notify_role", _boom)
    proposal_scope.execute("d1")
    assert db.tables["proposal_drafts"][0]["status"] == "done"


def test_approve_dismisses_the_lines_review_bell(scope_env, monkeypatch):
    scope_env.db.tables["proposal_drafts"] = [
        {"id": "d1", "project_id": PID, "status": "done", "lines_json": ["Line one."]},
    ]
    dismissed = []
    monkeypatch.setattr(
        proposals_router, "dismiss_notifications", lambda **kw: dismissed.append(kw)
    )
    out = proposals_router.approve_lines(PID, "d1", user=_user())
    assert out["approved_by"] == "u1"
    assert dismissed == [{"project_id": PID, "types": ["auto_send.lines_review"]}]


def test_approve_survives_a_dismiss_failure(scope_env, monkeypatch):
    scope_env.db.tables["proposal_drafts"] = [
        {"id": "d1", "project_id": PID, "status": "done", "lines_json": ["Line one."]},
    ]

    def _boom(**kw):
        raise RuntimeError("bell down")

    monkeypatch.setattr(proposals_router, "dismiss_notifications", _boom)
    out = proposals_router.approve_lines(PID, "d1", user=_user())
    assert out["approved_at"]


# ── 6.3 auto_send_pending on the project reads ───────────────────────────────


def test_schema_carries_the_flag_with_a_false_default():
    assert ProjectOut.model_fields["auto_send_pending"].default is False
    assert BidsTodayProjectOut.model_fields["auto_send_pending"].default is False


def _patch_list_helpers(monkeypatch):
    monkeypatch.setattr(projects_router.workflow, "load_category_states", lambda ids: {})
    monkeypatch.setattr(projects_router, "_pending_gc_pricing_counts", lambda ids: {})
    monkeypatch.setattr(projects_router, "_rfp_match_counts", lambda ids, rows, states: {})
    monkeypatch.setattr(projects_router, "_rfp_created_rows", lambda ids: {})


def _project(pid):
    return {"id": pid, "name": f"Job {pid}", "number": "26.10.1", "current_stage": "send_out",
            "abandoned_at": None, "actual_bid_at": None, "internal_bid_at": NOW_ISO,
            "created_at": NOW_ISO}


def test_list_projects_flags_pending_projects(monkeypatch, fake_auto_send):
    db = FakeDB({"projects": [_project("p1"), _project("p2")]})
    monkeypatch.setattr(projects_router, "get_supabase", lambda: db)
    _patch_list_helpers(monkeypatch)
    asked = []
    fake_auto_send(pending_project_ids=lambda ids, sb=None: asked.append(ids) or {"p1"})
    out = projects_router.list_projects(user=_user(Role.ESTIMATING_ADMIN))
    assert {p["id"]: p["auto_send_pending"] for p in out} == {"p1": True, "p2": False}
    assert sorted(asked[0]) == ["p1", "p2"]


def test_list_projects_survives_a_pending_lookup_failure(monkeypatch, fake_auto_send):
    db = FakeDB({"projects": [_project("p1")]})
    monkeypatch.setattr(projects_router, "get_supabase", lambda: db)
    _patch_list_helpers(monkeypatch)

    def _boom(ids, sb=None):
        raise RuntimeError("relation auto_send_runs does not exist")

    fake_auto_send(pending_project_ids=_boom)
    out = projects_router.list_projects(user=_user(Role.ESTIMATING_ADMIN))
    assert out[0]["auto_send_pending"] is False


class _BidsTodayQuery(_Query):
    """FakeDB query plus the few PostgREST verbs bids_today uses."""

    @property
    def not_(self):
        self._negate = True
        return self

    def is_(self, col, val):
        if getattr(self, "_negate", False):
            self._negate = False
            return self
        return super().is_(col, val)

    def lt(self, *a):
        return self

    def gte(self, *a):
        return self


class _BidsTodayDB(FakeDB):
    def table(self, name):
        return _BidsTodayQuery(self, name)


def test_bids_today_flags_pending_projects(monkeypatch, fake_auto_send):
    db = _BidsTodayDB({"projects": [_project("p1"), _project("p2")]})
    monkeypatch.setattr(projects_router, "get_supabase", lambda: db)
    monkeypatch.setattr(projects_router.workflow, "load_category_states", lambda ids: {})
    fake_auto_send(pending_project_ids=lambda ids, sb=None: {"p2"})
    out = projects_router.bids_today(user=_user(Role.ACCOUNTANT))
    assert {p["id"]: p["auto_send_pending"] for p in out} == {"p1": False, "p2": True}
    assert all(p["sent_today"] is False for p in out)


def _patch_detail(monkeypatch):
    monkeypatch.setattr(projects_router, "_fetch_project_with_outcome", lambda pid: _project(pid))
    monkeypatch.setattr(projects_router.workflow, "load_category_state", lambda pid: {})
    monkeypatch.setattr(projects_router, "_rfp_match_counts", lambda ids, rows, states: {})
    monkeypatch.setattr(projects_router, "_portal_sources", lambda ids: {})
    monkeypatch.setattr(projects_router, "_rfp_created_rows", lambda ids: {})
    monkeypatch.setattr(projects_router, "_pending_gc_pricing_counts", lambda ids: {})


def test_get_project_flags_and_degrades(monkeypatch, fake_auto_send):
    _patch_detail(monkeypatch)
    fake_auto_send(pending_project_ids=lambda ids, sb=None: set(ids))
    assert projects_router.get_project(PID, user=_user())["auto_send_pending"] is True

    def _boom(ids, sb=None):
        raise RuntimeError("down")

    fake_auto_send(pending_project_ids=_boom)
    assert projects_router.get_project(PID, user=_user())["auto_send_pending"] is False


def test_present_never_sets_the_flag_for_the_estimator():
    row = projects_router._present(_project(PID), Role.ESTIMATOR, auto_send_pending=True)
    assert "auto_send_pending" not in row
    row = projects_router._present(_project(PID), Role.EXECUTIVE, auto_send_pending=True)
    assert row["auto_send_pending"] is True


# ── 6.4 Revised Proposal subject per GC ──────────────────────────────────────

PROJECT = {"id": PID, "name": "Sunset", "number": "26.10.7300", "current_stage": "send_out"}
NORMAL = psend.build_cover_email(PROJECT)[0]
REVISED = "[G3 Electrical] Revised Proposal: Sunset (26.10.7300)"


def test_subject_helpers():
    assert psend.revised_cover_subject(PROJECT) == REVISED
    assert "\u2014" not in REVISED
    assert psend.cover_subject_for(PROJECT, "gA", {"gA"}) == REVISED
    assert psend.cover_subject_for(PROJECT, "gB", {"gA"}) == NORMAL
    # The normal subject is untouched, and every Send Out subject differs from
    # the auto send subject the crash reclaim must never match.
    assert NORMAL == "[G3 Electrical] Proposal \u2014 Sunset (26.10.7300)"
    auto_subject = "[G3 Electrical] Proposal: Sunset (26.10.7300)"
    assert auto_subject not in (NORMAL, REVISED)


def test_auto_sent_lookup_is_best_effort(fake_auto_send):
    fake_auto_send(auto_sent_gc_ids=lambda pid, sb=None: ["gA"])
    assert psend.auto_sent_gc_ids(PID) == {"gA"}

    def _boom(pid, sb=None):
        raise RuntimeError("relation auto_send_gcs does not exist")

    fake_auto_send(auto_sent_gc_ids=_boom)
    assert psend.auto_sent_gc_ids(PID) == set()


def _send_env(monkeypatch, fake_auto_send, status):
    from app.routers import pricing

    db = FakeDB({
        "projects": [dict(PROJECT)],
        "proposal_sends": [
            {"id": f"ps-{g}", "project_id": PID, "gc_id": f"g{g}", "gc_name": name,
             "status": status, "file_id": f"f-{g}", "draft_id": "d1",
             "gc_email": f"g{g}@gc.com", "updated_at": "2026-10-01T00:00:00+00:00"}
            for g, name in (("A", "Alpha"), ("B", "Bravo"))
        ],
        "proposal_drafts": [{"id": "d1", "project_id": PID, "created_at": NOW_ISO,
                             "lines_json": ["Line one."], "approved_at": NOW_ISO}],
        "project_files": [
            {"id": f"f-{g}", "storage_path": f"s/{g}",
             "filename": f"Proposal 7300 - {name}.docx"}
            for g, name in (("A", "Alpha"), ("B", "Bravo"))
        ],
    })
    fake_auto_send(auto_sent_gc_ids=lambda pid, sb=None: {"gA"})
    monkeypatch.setattr(psend, "get_supabase", lambda: db)
    monkeypatch.setattr(psend, "send_window_head", lambda pid: "send_out")
    monkeypatch.setattr(psend, "_project_gcs", lambda pid: [{"id": "gA"}, {"id": "gB"}])
    monkeypatch.setattr(psend, "_all_project_gc_names", lambda pid: ["Alpha", "Bravo"])
    monkeypatch.setattr(pricing, "section_summary", lambda rows: {"gear": {"includes_generator": False}})
    monkeypatch.setattr(pricing, "_materials_rows", lambda pid: [])
    monkeypatch.setattr(pricing, "_get_one", lambda table, pid: None)
    monkeypatch.setattr(
        psend, "resolve_recipients", lambda live_gc, chosen: [f"{live_gc['id']}@gc.com"]
    )
    monkeypatch.setattr(psend, "resolve_cc", lambda live_gc, ids, to: ([], None))
    monkeypatch.setattr(psend, "_gc_pending_now", lambda sb, pid, gid: False)
    monkeypatch.setattr(psend.storage, "download_file", lambda path: b"docx")
    monkeypatch.setattr(psend, "assert_send_isolation", lambda **kw: None)
    monkeypatch.setattr(psend, "assert_resend_isolation", lambda **kw: None)
    monkeypatch.setattr(psend.office_preview, "convert_for_send", lambda b, fn: b"pdf")
    monkeypatch.setattr(psend.office_preview, "extract_pdf_text", lambda b: "")
    monkeypatch.setattr(psend, "validate_pdf_isolation", lambda *a, **k: None)
    monkeypatch.setattr(psend.email_branding, "render_proposal_email", lambda body: body)
    monkeypatch.setattr(psend.email_branding, "logo_bytes", lambda: b"")
    monkeypatch.setattr(psend, "record_send_event", lambda *a, **k: None)
    monkeypatch.setattr(psend, "audit", lambda *a, **k: None)
    monkeypatch.setattr(psend, "dismiss_notifications", lambda **k: None)
    monkeypatch.setattr(psend, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(psend.time, "sleep", lambda s: None)
    sent = []
    monkeypatch.setattr(
        psend.graph_email,
        "send_mail",
        lambda **kw: sent.append(kw) or {"id": f"log-{len(sent)}"},
    )
    return db, sent


def test_send_uses_the_revised_subject_only_for_auto_sent_gcs(monkeypatch, fake_auto_send):
    _, sent = _send_env(monkeypatch, fake_auto_send, "generated")
    out = psend.send_proposals(PID, "u1", ["ps-A", "ps-B"])
    assert [r["status"] for r in out["results"]] == ["sent", "sent"]
    assert {tuple(m["to"]): m["subject"] for m in sent} == {
        ("gA@gc.com",): REVISED,
        ("gB@gc.com",): NORMAL,
    }


def test_send_reclaim_accepts_either_send_out_subject(monkeypatch, fake_auto_send):
    """Finding 25: the reclaim proves a crashed send by either Send Out
    subject, never by the one this request's auto sent set happens to pick."""
    _send_env(monkeypatch, fake_auto_send, "sending")
    seen = []

    def _reclaim(row, subject):
        seen.append((row["gc_id"], subject))
        return {**row, "status": "sent"}

    monkeypatch.setattr(psend, "_reclaim_stuck_sending", _reclaim)
    psend.send_proposals(PID, "u1", ["ps-A", "ps-B"])
    assert dict(seen) == {"gA": [NORMAL, REVISED], "gB": [NORMAL, REVISED]}


@pytest.mark.parametrize("sent_subject", [NORMAL, REVISED])
def test_reclaim_proves_a_send_under_either_send_out_subject(monkeypatch, sent_subject):
    db = FakeDB({
        "email_log": [
            {"id": "e1", "project_id": PID, "subject": sent_subject, "status": "sent",
             "to_addrs": "gA@gc.com", "created_at": NOW_ISO},
        ],
        "proposal_sends": [{"id": "ps-A", "status": "sending"}],
    })
    monkeypatch.setattr(psend, "get_supabase", lambda: db)
    row = {"id": "ps-A", "project_id": PID, "gc_email": "gA@gc.com",
           "updated_at": "2026-01-01T00:00:00+00:00"}
    out = psend._reclaim_stuck_sending(row, psend.send_out_subjects(PROJECT))
    assert out["status"] == "sent" and out["email_log_id"] == "e1"


def test_reclaim_never_takes_an_auto_send_email_as_proof(monkeypatch):
    db = FakeDB({
        "email_log": [
            {"id": "e1", "project_id": PID, "subject": "[G3 Electrical] Proposal: Sunset "
             "(26.10.7300)", "status": "sent", "to_addrs": "gA@gc.com", "created_at": NOW_ISO},
        ],
        "proposal_sends": [{"id": "ps-A", "status": "sending"}],
    })
    monkeypatch.setattr(psend, "get_supabase", lambda: db)
    row = {"id": "ps-A", "project_id": PID, "gc_email": "gA@gc.com",
           "updated_at": "2026-01-01T00:00:00+00:00"}
    out = psend._reclaim_stuck_sending(row, psend.send_out_subjects(PROJECT))
    assert out["status"] == "failed"
    assert out["error"].startswith(psend.OUTCOME_UNKNOWN_PREFIX)


def test_reclaim_queries_email_log_on_the_subject_it_is_given(monkeypatch):
    db = FakeDB({
        "email_log": [
            {"id": "e1", "project_id": PID, "subject": NORMAL, "status": "sent",
             "to_addrs": "gA@gc.com", "created_at": NOW_ISO},
            {"id": "e2", "project_id": PID, "subject": REVISED, "status": "sent",
             "to_addrs": "gA@gc.com", "created_at": NOW_ISO},
        ],
        "proposal_sends": [{"id": "ps-A", "status": "sending"}],
    })
    monkeypatch.setattr(psend, "get_supabase", lambda: db)
    row = {"id": "ps-A", "project_id": PID, "gc_email": "gA@gc.com",
           "updated_at": "2026-01-01T00:00:00+00:00"}
    assert psend._reclaim_stuck_sending(row, REVISED)["email_log_id"] == "e2"


def test_resend_uses_the_per_gc_subject_for_mail_and_reclaim(monkeypatch, fake_auto_send):
    _, sent = _send_env(monkeypatch, fake_auto_send, "sent")
    monkeypatch.setattr(
        psend,
        "send_events_by_proposal",
        lambda pid: {"ps-A": [{"id": "ev-A", "status": "sending"}]},
    )
    seen = []

    def _reclaim(event, subject):
        seen.append((event["id"], subject))
        return {**event, "status": "failed"}

    monkeypatch.setattr(psend, "_reclaim_stuck_event", _reclaim)
    out = psend.resend_proposals(PID, "u1", ["ps-A", "ps-B"])
    assert [r["status"] for r in out["results"]] == ["sent", "sent"]
    assert seen == [("ev-A", [NORMAL, REVISED])]
    assert {tuple(m["to"]): m["subject"] for m in sent} == {
        ("gA@gc.com",): REVISED,
        ("gB@gc.com",): NORMAL,
    }


def test_email_preview_shows_the_revised_subject(monkeypatch, fake_auto_send):
    db = FakeDB({"projects": [dict(PROJECT)]})
    monkeypatch.setattr(proposals_router, "get_supabase", lambda: db)
    fake_auto_send(auto_sent_gc_ids=lambda pid, sb=None: {"gB", "gA"})
    out = proposals_router.email_preview(PID, user=_user(Role.ACCOUNTANT))
    assert out["subject"] == NORMAL
    assert out["revised_subject"] == REVISED
    assert out["revised_gc_ids"] == ["gA", "gB"]


# ── 6.5 notification meta, deep link, subject ────────────────────────────────


@pytest.mark.parametrize(
    "type_, heading, cta",
    [
        ("auto_send.sent", "Auto send: proposal sent", "Open Auto send"),
        ("auto_send.failed", "Auto send: could not send", "Open Auto send"),
        ("auto_send.missed", "Auto send did not run", "Open Auto send"),
        ("auto_send.lines_review", "Review the scope lines", "Open scope lines"),
    ],
)
def test_auto_send_types_are_registered(type_, heading, cta):
    assert notification_email._TYPE_META[type_] == (heading, cta)
    assert notification_email.heading_for(type_) == heading
    assert "\u2014" not in heading + cta
    link = notification_email._deep_link(PID, Role.ESTIMATING_ADMIN.value, type_)
    assert link.endswith(f"/projects/{PID}?box=auto_send")


def test_estimator_never_gets_the_auto_send_box_link():
    link = notification_email._deep_link(PID, Role.ESTIMATOR.value, "auto_send.missed")
    assert link.endswith(f"/estimator/projects/{PID}")


def test_auto_send_mirror_subject_has_no_em_dash():
    project = {"number": "26.10.7300", "name": "Sunset"}
    subject = notification_email._subject("Auto send did not run", project, "auto_send.missed")
    assert subject == "G3 BDR · Auto send did not run · #26.10.7300 Sunset"
    assert "\u2014" not in subject
    # Other types keep their existing subject.
    assert "\u2014" in notification_email._subject("Estimate submitted", project, "estimate_submitted")


# ── 6.7 notification log classifier ──────────────────────────────────────────


def test_notification_log_classifies_auto_send_emails_per_run():
    db = FakeDB({
        "auto_send_gcs": [
            {"project_id": PID, "run_id": "r1", "email_log_id": "e1"},
            {"project_id": PID, "run_id": "r1", "email_log_id": "e2"},
            {"project_id": PID, "run_id": "r2", "email_log_id": "e3"},
            {"project_id": PID, "run_id": "r2", "email_log_id": None},
            {"project_id": "other", "run_id": "r9", "email_log_id": "e9"},
        ],
    })
    classes = notification_log._classify_emails(db, PID)
    assert set(classes) == {"e1", "e2", "e3"}
    assert classes["e1"]["type"] == "auto_send"
    assert classes["e1"]["title"] == "Auto send proposal sent to the GC"
    assert classes["e1"]["group_key"] == classes["e2"]["group_key"] == "auto_send:r1"
    assert classes["e3"]["group_key"] == "auto_send:r2"


def test_notification_log_survives_a_missing_auto_send_table():
    class _NoAutoSend(FakeDB):
        def table(self, name):
            if name == "auto_send_gcs":
                raise APIError({"code": "PGRST205", "message": "missing"})
            return super().table(name)

    db = _NoAutoSend({
        "proposal_sends": [{"project_id": PID, "email_log_id": "e1", "gc_name": "Alpha"}],
    })
    classes = notification_log._classify_emails(db, PID)
    assert classes["e1"]["type"] == "proposal_send"


# ── 6.8 file delete protection ───────────────────────────────────────────────


def _file_env(monkeypatch, auto_rows):
    db = FakeDB({
        "project_files": [{"id": "f1", "project_id": PID, "category": "proposal",
                           "storage_path": "s/f1", "uploaded_by": None}],
        "proposal_sends": [],
        "auto_send_gcs": auto_rows,
    })
    monkeypatch.setattr(files_router, "get_supabase", lambda: db)
    return db


@pytest.mark.parametrize("status", ["sent", "unknown"])
def test_delete_refuses_an_auto_sent_document(monkeypatch, status):
    _file_env(monkeypatch, [{"id": "a1", "file_id": "f1", "status": status}])
    with pytest.raises(HTTPException) as exc:
        files_router.delete_file(PID, "f1", user=_user(Role.ESTIMATING_ADMIN))
    assert exc.value.status_code == 409
    assert exc.value.detail == "Sent proposals are immutable and cannot be deleted"


@pytest.mark.parametrize("status", ["sending", "sent", "unknown"])
def test_in_flight_or_sent_auto_send_rows_protect_the_file(monkeypatch, status):
    """Finding 3/26: a GC row mid-send ('sending') protects its document."""
    _file_env(monkeypatch, [{"id": "a1", "run_id": "r1", "file_id": "f1", "status": status}])
    assert files_router._auto_send_references("f1") is True


def test_pending_row_of_a_run_still_sending_protects_the_file(monkeypatch):
    """The documents are stored before the emails go out one by one: a GC
    still waiting its turn keeps its document."""
    db = _file_env(monkeypatch, [{"id": "a1", "run_id": "r1", "file_id": "f1",
                                  "status": "pending"}])
    db.tables["auto_send_runs"] = [{"id": "r1", "status": "sending"}]
    assert files_router._auto_send_references("f1") is True
    with pytest.raises(HTTPException) as exc:
        files_router.delete_file(PID, "f1", user=_user(Role.ESTIMATING_ADMIN))
    assert exc.value.status_code == 409


@pytest.mark.parametrize("status", ["failed", "pending", "skipped"])
def test_unsent_auto_send_rows_do_not_protect_the_file(monkeypatch, status):
    db = _file_env(monkeypatch, [{"id": "a1", "run_id": "r1", "file_id": "f1",
                                  "status": status}])
    # The run is over: a pending row left behind by it never goes out.
    db.tables["auto_send_runs"] = [{"id": "r1", "status": "failed"}]
    assert files_router._auto_send_references("f1") is False


def test_missing_auto_send_table_does_not_block_deletes(monkeypatch):
    class _NoAutoSend(FakeDB):
        def table(self, name):
            if name == "auto_send_gcs":
                raise APIError({"code": "PGRST205", "message": "missing"})
            return super().table(name)

    monkeypatch.setattr(files_router, "get_supabase", lambda: _NoAutoSend({}))
    assert files_router._auto_send_references("f1") is False


def test_other_auto_send_errors_fail_closed(monkeypatch):
    class _Broken(FakeDB):
        def table(self, name):
            raise APIError({"code": "57014", "message": "statement timeout"})

    monkeypatch.setattr(files_router, "get_supabase", lambda: _Broken({}))
    with pytest.raises(APIError):
        files_router._auto_send_references("f1")


# ── 6.9 main.py lifespan gate + router mount ─────────────────────────────────

_MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"


def test_main_starts_the_auto_send_loop_under_its_gate():
    src = _MAIN.read_text(encoding="utf-8")
    gate = (
        "    if (\n"
        "        settings.bidding_enabled\n"
        "        and settings.auto_send_poller_enabled\n"
        "        and settings.supabase_url\n"
        "        and settings.ms_client_id\n"
        "    ):\n"
        "        from app.services import auto_send_scheduler\n"
        "\n"
        "        auto_send_task = asyncio.create_task(auto_send_scheduler.polling_loop())\n"
    )
    assert gate in src
    # Cancelled on shutdown with the other loops.
    shutdown = src[src.index("    for task in ("):]
    assert "auto_send_task," in shutdown[: shutdown.index("):")]
    assert "app.include_router(auto_send.router, dependencies=_BIDDING)" in src
    lines = src.splitlines()
    idx = lines.index("app.include_router(proposals.router, dependencies=_BIDDING)")
    assert lines[idx + 1] == "app.include_router(auto_send.router, dependencies=_BIDDING)"


def test_estimator_submit_calls_the_hook_in_both_branches_by_source():
    src = inspect.getsource(estimator_router.submit_deliverables)
    assert src.count("_auto_send_round_hook(project_id, counts, background)") == 2


# ── re-send of an accepted auto send (findings 20, 21, 22) ───────────────────


def test_resend_of_an_accepted_auto_send_checks_the_frozen_run_lines(
    monkeypatch, fake_auto_send
):
    """The draft behind the run stays editable until Accept, so the re-send
    gate reads the run's frozen lines and the auto send section rule."""
    db, sent = _send_env(monkeypatch, fake_auto_send, "sent")
    for row in db.tables["proposal_sends"]:
        if row["gc_id"] == "gA":
            row.update(origin="auto_send", auto_send_run_id="run-1")
    # Edited after the auto send went out.
    db.tables["proposal_drafts"][0]["lines_json"] = ["Line one, edited."]
    db.tables["auto_send_runs"] = [{"id": "run-1", "lines_json": ["Line one."]}]
    monkeypatch.setattr(psend, "send_events_by_proposal", lambda pid: {})
    seen, pdf_kwargs = {}, {}
    monkeypatch.setattr(psend, "assert_resend_isolation",
                        lambda **kw: seen.__setitem__(kw["row"]["gc_id"], kw["scope_lines"]))
    monkeypatch.setattr(
        psend, "validate_pdf_isolation",
        lambda text, **kw: pdf_kwargs.__setitem__(kw["gc_name"], kw["removed_sections"]),
    )
    out = psend.resend_proposals(PID, "u1", ["ps-A", "ps-B"])
    assert [r["status"] for r in out["results"]] == ["sent", "sent"]
    assert seen == {"gA": ("Line one.",), "gB": ("Line one, edited.",)}
    # The accepted auto send row skips the whole-body section check.
    assert pdf_kwargs["Alpha"] == ()


def test_resend_of_an_auto_send_whose_run_is_gone_fails_closed(monkeypatch, fake_auto_send):
    db, sent = _send_env(monkeypatch, fake_auto_send, "sent")
    db.tables["proposal_sends"][0].update(origin="auto_send", auto_send_run_id="run-gone")
    monkeypatch.setattr(psend, "send_events_by_proposal", lambda pid: {})
    out = psend.resend_proposals(PID, "u1", ["ps-A"])
    assert out["results"][0]["status"] == "failed"
    assert "no longer on file" in out["results"][0]["error"]
    assert sent == []


def test_resend_section_kwargs_by_origin():
    stamped = {"material": 1, "gear": None, "underground": None, "low_voltage": None,
               "labor": 2}
    auto = psend.resend_section_kwargs({"origin": "auto_send"}, stamped, True)
    assert auto == {"includes_generator": False, "removed_sections": ()}
    normal = psend.resend_section_kwargs({"origin": "send_out"}, stamped, True)
    assert normal["removed_sections"] == ("gear", "underground", "low_voltage")

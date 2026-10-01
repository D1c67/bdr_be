"""The Go/No-Go intake gate (docs/RFP_CREATE.md section 7).

A project the RFP creation step made arrives with its intake unanswered. No
decision (go, no_go, or a score-driven one) is taken until the intake is
complete; parking in review stays open, since the creation step enters every
project that way. GET /gono carries the missing list (bid time redacted for a
role that may not see the actual bid date), and the project's `rfp_created`
summary carries the source facts the intake modal shows.

Reuses the in-memory Supabase fake from test_reverify.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import GonoDecisionIn, RfpCreatedSummary, TransitionIn
from app.routers import gono as gono_router
from app.routers import projects as pr
from app.routers import workflow as workflow_router
from app.services import gono, workflow
from tests.test_reverify import FakeDB

P1 = "p1"
EMAIL_ID = "e1"

_COMPLETE = {
    "internal_bid_at": "2026-10-20T22:00:00+00:00",
    "due_from_estimator_at": "2026-10-15T22:00:00+00:00",
    "due_from_vendors_at": "2026-10-14T22:00:00+00:00",
    "project_type": "new_construction",
    "owner_type": "rtc",
    "labor_needed": "ce_cw",
    "bid_method": "cmar",
    "competitor_known": "only_ec_bidding",
    "gc_known": "no_gc_needed",
    "subs_needed": "no",
    "est_value_band": "over_3m",
    "scope_fit": "yes",
}
_BLANK = {key: None for key in _COMPLETE}

# Intake parked at Go/No-Go (in review), the other lanes still locked.
_IN_REVIEW = {
    "intake": ("go_no_go", "active"),
    "material_numbers": ("estimate_received", "locked"),
    "labor_numbers": ("labor_numbers", "locked"),
    "send_out": ("gc_pricing", "locked"),
}
_AT_INTAKE = {**_IN_REVIEW, "intake": ("intake", "active")}


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


def _cat_rows(spec):
    return [
        {"project_id": P1, "category": c, "current_task": t, "status": s,
         "owner_role": None, "completed_at": None}
        for c, (t, s) in spec.items()
    ]


def _db(*, intake: dict, rfp: bool = True, bid_time_unknown: bool = False, cats=None):
    project = {
        "id": P1,
        "name": "Test",
        "current_stage": "go_no_go",
        "current_owner_role": "executive",
        "abandoned_at": None,
        "actual_bid_at": "2026-10-21T07:00:00+00:00",
        **intake,
    }
    created = [{"project_id": P1, "bid_time_unknown": bid_time_unknown}] if rfp else []
    return FakeDB({
        "projects": [project],
        "project_category_state": _cat_rows(cats or _IN_REVIEW),
        "go_no_go_decisions": [],
        "stage_events": [],
        "rfp_created_projects": created,
    })


@pytest.fixture
def env(monkeypatch):
    """Point every module at a fake DB the test installs, with the RFP flag on,
    and record finalize calls instead of moving lanes."""
    state = SimpleNamespace(db=None, finalized=[],
                            settings=SimpleNamespace(rfp_ingest_enabled=True))

    def install(db):
        state.db = db
        for mod in (gono, gono_router, workflow, workflow_router):
            monkeypatch.setattr(mod, "get_supabase", lambda: db)
        return db

    def fake_finalize(project_id, outcome, method, decided_by, score=None):
        state.finalized.append((project_id, outcome, method, decided_by))
        return {"id": project_id}

    monkeypatch.setattr(gono, "get_settings", lambda: state.settings)
    monkeypatch.setattr(gono, "finalize", fake_finalize)
    monkeypatch.setattr(gono_router, "finalize", fake_finalize)
    state.install = install
    return state


# ── intake_missing_for ────────────────────────────────────────────────────────


def test_missing_list_for_an_rfp_project_in_rule_order(env):
    db = env.install(_db(intake=_BLANK, bid_time_unknown=True))
    missing = gono.intake_missing_for(db.tables["projects"][0])
    assert missing[:3] == ["internal_bid_at", "due_from_estimator_at", "due_from_vendors_at"]
    assert missing[-1] == "bid_time" and len(missing) == 13


def test_missing_list_is_empty_for_a_non_rfp_project_and_with_the_flag_off(env):
    db = env.install(_db(intake=_BLANK, rfp=False))
    assert gono.intake_missing_for(db.tables["projects"][0]) == []
    db = env.install(_db(intake=_BLANK))
    env.settings = SimpleNamespace()  # an absent flag fails closed
    assert gono.intake_missing_for(db.tables["projects"][0]) == []


# ── POST /gono/decide ─────────────────────────────────────────────────────────


def test_decide_is_refused_while_intake_is_missing(env):
    env.install(_db(intake={**_COMPLETE, "internal_bid_at": None, "scope_fit": None}))
    with pytest.raises(HTTPException) as exc:
        gono_router.decide(P1, GonoDecisionIn(outcome="go"), user=_user())
    assert exc.value.status_code == 409
    assert exc.value.detail == (
        "Complete the intake details before deciding Go/No-Go: "
        "internal bid date, Go/No-Go answers"
    )
    assert env.finalized == []


def test_decide_passes_once_intake_is_complete(env):
    env.install(_db(intake=_COMPLETE))
    out = gono_router.decide(P1, GonoDecisionIn(outcome="no_go"), user=_user())
    assert out == {"decided": "no_go", "method": "manual"}
    assert env.finalized == [(P1, "no_go", "manual", "u1")]


def test_decide_passes_for_a_non_rfp_project_with_a_blank_intake(env):
    env.install(_db(intake=_BLANK, rfp=False))
    out = gono_router.decide(P1, GonoDecisionIn(outcome="go"), user=_user())
    assert out["decided"] == "go"
    assert env.finalized == [(P1, "go", "manual", "u1")]


def test_decide_refusal_hides_the_bid_time_from_an_engineer(env):
    env.install(_db(intake=_COMPLETE, bid_time_unknown=True))
    with pytest.raises(HTTPException) as exc:
        gono_router.decide(P1, GonoDecisionIn(outcome="go"),
                           user=_user(Role.ESTIMATING_ENGINEER_LABOR))
    assert exc.value.status_code == 409
    assert exc.value.detail == "Complete the intake details before deciding Go/No-Go"
    with pytest.raises(HTTPException) as exc:
        gono_router.decide(P1, GonoDecisionIn(outcome="go"), user=_user(Role.EXECUTIVE))
    assert exc.value.detail == "Complete the intake details before deciding Go/No-Go: bid time"
    assert env.finalized == []


# ── apply_entry_action ────────────────────────────────────────────────────────


def test_entry_action_review_is_unaffected(env):
    env.install(_db(intake=_BLANK))
    assert gono.apply_entry_action(P1, "u1", "review") == (None, None)
    assert env.finalized == []


@pytest.mark.parametrize("action", ["go", "no_go", "score"])
def test_entry_action_decisions_are_blocked_while_intake_is_missing(env, action):
    env.install(_db(intake=_BLANK))
    with pytest.raises(HTTPException) as exc:
        gono.apply_entry_action(P1, "u1", action)
    assert exc.value.status_code == 409
    assert exc.value.detail.startswith("Complete the intake details before deciding Go/No-Go: ")
    assert env.finalized == []


def test_entry_action_go_passes_once_intake_is_complete(env):
    env.install(_db(intake=_COMPLETE))
    outcome, _ = gono.apply_entry_action(P1, "u1", "go")
    assert outcome == "go" and env.finalized == [(P1, "go", "manual", "u1")]


def test_advance_out_of_intake_refuses_before_the_lane_moves(env):
    db = env.install(_db(intake=_BLANK, cats=_AT_INTAKE))
    db.tables["projects"][0]["current_stage"] = "intake"
    with pytest.raises(HTTPException) as exc:
        workflow_router.advance(P1, TransitionIn(category="intake", gono_action="score"), user=_user())
    assert exc.value.status_code == 409
    assert exc.value.detail.startswith("Complete the intake details before deciding Go/No-Go: ")
    intake = next(r for r in db.tables["project_category_state"] if r["category"] == "intake")
    assert intake["current_task"] == "intake"
    assert db.tables["stage_events"] == []


# ── GET /gono ─────────────────────────────────────────────────────────────────


def test_status_carries_intake_missing_and_redacts_bid_time_for_an_engineer(env):
    env.install(_db(intake={**_COMPLETE, "due_from_vendors_at": None}, bid_time_unknown=True))
    out = gono_router.gono_status(P1, user=_user(Role.ESTIMATING_ADMIN))
    assert out["intake_missing"] == ["due_from_vendors_at", "bid_time"]
    out = gono_router.gono_status(P1, user=_user(Role.ESTIMATING_ENGINEER_MATERIALS))
    assert out["intake_missing"] == ["due_from_vendors_at"]


def test_status_intake_missing_is_empty_for_a_non_rfp_project(env):
    env.install(_db(intake=_BLANK, rfp=False))
    assert gono_router.gono_status(P1, user=_user())["intake_missing"] == []


# ── The rfp_created summary source facts ──────────────────────────────────────


def _summary_db():
    base = {"automatic": True, "sender_was_unauthorized": False, "sender_display": None,
            "sender_allowed_by": None, "files_status": "none", "files_promoted": 0,
            "bid_time_unknown": False, "invitation_method": None, "gc_plan": "likely"}
    return FakeDB({
        "rfp_created_projects": [
            {**base, "project_id": "p1", "source_kind": "rfp_email", "rfp_email_id": EMAIL_ID,
             "likely_gc_id": "gc-1", "likely_gc_name": "GC AA Builders"},
            {**base, "project_id": "p2", "source_kind": "rfp_portal", "rfp_email_id": None,
             "likely_gc_id": None, "likely_gc_name": None},
        ],
        "rfp_emails": [{"id": EMAIL_ID, "primary_mailbox": "bids@g3electrical.com"}],
        "profiles": [],
    })


def test_created_summary_carries_the_source_facts(monkeypatch):
    db = _summary_db()
    reads = []

    class Spy:
        def table(self, name):
            reads.append(name)
            return db.table(name)

    monkeypatch.setattr(pr, "get_supabase", lambda: Spy())
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=True))
    rows = pr._rfp_created_rows(["p1", "p2"])
    email = pr._rfp_created_summary(rows["p1"])
    portal = pr._rfp_created_summary(rows["p2"])
    assert email["source_kind"] == "rfp_email" and email["mailbox"] == "bids@g3electrical.com"
    assert email["likely_gc_id"] == "gc-1" and email["likely_gc_name"] == "GC AA Builders"
    assert portal["source_kind"] == "rfp_portal" and portal["mailbox"] is None
    assert portal["likely_gc_name"] is None
    # One batched rfp_emails read for the whole page.
    assert reads.count("rfp_emails") == 1
    assert RfpCreatedSummary(**email).mailbox == "bids@g3electrical.com"
    blank = RfpCreatedSummary()
    assert blank.source_kind is None and blank.mailbox is None and blank.likely_gc_name is None
    for col in ("source_kind", "rfp_email_id", "likely_gc_id", "likely_gc_name"):
        assert col in pr._RFP_CREATED_SELECT

    reads.clear()
    rows = pr._rfp_created_rows(["p1"], with_mailbox=False)
    assert "rfp_emails" not in reads and rows["p1"]["mailbox"] is None


def test_created_rows_stay_empty_with_the_flag_off(monkeypatch):
    monkeypatch.setattr(pr, "get_supabase", lambda: pytest.fail("no read while the flag is off"))
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace())
    assert pr._rfp_created_rows(["p1"]) == {}


# ── auto_go_if_scored (a complete RFP intake at 30+) ─────────────────────────

_LOW = {**_COMPLETE, "project_type": "other", "owner_type": "other", "labor_needed": "non_union",
        "bid_method": "other", "competitor_known": "other", "gc_known": "other",
        "subs_needed": "other"}  # 5 + 5 + 4 bid days = 14, a No-Go score


def test_auto_go_applies_a_score_go(env):
    db = env.install(_db(intake=_COMPLETE))
    db.tables["audit_log"] = []
    assert gono.compute_score(db.tables["projects"][0]) >= gono.THRESHOLDS["go"]
    assert gono.auto_go_if_scored(P1, "u1") == "go"
    assert env.finalized == [(P1, "go", "score", "u1")]


def test_auto_go_never_declines_a_low_score(env):
    db = env.install(_db(intake=_LOW))
    db.tables["audit_log"] = []
    assert gono.outcome_for_score(gono.compute_score(db.tables["projects"][0])) == "no_go"
    assert gono.auto_go_if_scored(P1, "u1") is None
    assert env.finalized == []


def test_auto_go_skips_a_project_already_decided_undone_or_moved(env):
    db = env.install(_db(intake=_COMPLETE))
    db.tables["audit_log"] = [{"id": "a1", "action": "gono.undo", "entity_id": P1}]
    assert gono.auto_go_if_scored(P1, "u1") is None
    db.tables["audit_log"] = []
    db.tables["go_no_go_decisions"] = [{"project_id": P1, "outcome": "go"}]
    assert gono.auto_go_if_scored(P1, "u1") is None
    db = env.install(_db(intake=_COMPLETE, cats=_AT_INTAKE))
    db.tables["audit_log"] = []
    assert gono.auto_go_if_scored(P1, "u1") is None
    assert env.finalized == []


def test_intake_patch_that_completes_the_intake_auto_goes(env, monkeypatch):
    db = env.install(_db(intake=_COMPLETE))
    db.tables["audit_log"] = []
    monkeypatch.setattr(pr, "get_supabase", lambda: db)
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=True))
    monkeypatch.setattr(pr, "dismiss_notifications", lambda **_: None)
    project = db.tables["projects"][0]
    assert pr._settle_rfp_intake(P1, project, {"scope_fit": "yes"}, "u1") is True
    assert env.finalized == [(P1, "go", "score", "u1")]

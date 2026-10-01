"""Mark submitted from the project side menu (0140).

Covers the pure seam (roles, eligibility, grants, section costs, markups, the
per-GC figures, validation, the lane target, the apply document), the
request / grant / deny / cancel / revoke services, the submit and undo gates
(role, grant, expiry, eligibility, database error mapping), the router's
error mapping, the notification deep link and the targeted dismissal.

The database functions themselves (apply / undo in one transaction) are
exercised by the live smoke on dev; here the RPC is recorded.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError

from app.core.roles import Role
from app.models.schemas import (
    ExternalSubmissionDenyIn,
    ExternalSubmissionGcIn,
    ExternalSubmissionIn,
    ExternalSubmissionLineIn,
    ExternalSubmissionMarkupIn,
    ExternalSubmissionMarkupsIn,
    ExternalSubmissionRequestIn,
    ExternalSubmissionUndoIn,
)
from app.routers import external_submission as router
from app.services import external_submission as xs
from app.services import notification_email as ne
from app.services import notifications as notif
from app.services import workflow
from app.services.external_submission import ExternalSubmissionError
from tests.test_late_gc_pricing_approval import FakeDB

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
P1 = "p1"

CATS = {
    "c-light": {"id": "c-light", "name": "Lighting", "pricing_section": "materials",
                "kind": "material", "is_active": True, "sort_order": 1},
    "c-wire": {"id": "c-wire", "name": "General Material", "pricing_section": "materials",
               "kind": "material", "is_active": True, "sort_order": 2, "is_general": True},
    "c-gear": {"id": "c-gear", "name": "Switchgear", "pricing_section": "gear",
               "kind": "material", "is_active": True, "sort_order": 3},
    "c-trench": {"id": "c-trench", "name": "Trenching", "pricing_section": "underground",
                 "kind": "material", "is_active": True, "sort_order": 4},
}


def _state(**heads):
    """A category-state map; each kwarg is category=(task, status)."""
    base = {
        "intake": ("to_estimator", "complete"),
        "material_numbers": ("rfqs", "active"),
        "labor_numbers": ("labor_numbers", "active"),
        "select_vendors": ("select_vendors", "locked"),
        "markup": ("markup", "locked"),
        "send_out": ("gc_pricing", "locked"),
    }
    base.update(heads)
    return {c: {"current_task": t, "status": s} for c, (t, s) in base.items()}


def _project(**kw):
    return {"id": P1, "name": "Sunset Plaza", "number": "26.9.7201",
            "current_stage": "rfqs", "abandoned_at": None, "reverify_return_stage": None,
            **kw}


# ── roles ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role,kind",
    [
        (Role.EXECUTIVE, "approver"),
        (Role.ESTIMATING_ADMIN, "approver"),
        (Role.IT_ADMIN, "approver"),
        (Role.ESTIMATING_ENGINEER_MATERIALS, "requester"),
        (Role.ESTIMATING_ENGINEER_LABOR, "requester"),
        (Role.ACCOUNTANT, "none"),
        (Role.ESTIMATOR, "none"),
        ("not_a_role", "none"),
        (None, "none"),
    ],
)
def test_role_kind(role, kind):
    assert xs.role_kind(role) == kind


# ── eligibility ────────────────────────────────────────────────────────────


def _elig(project=None, state=None, **kw):
    return xs.eligibility(
        project if project is not None else _project(),
        state or _state(),
        has_outcome=kw.get("has_outcome", False),
        has_active_submission=kw.get("active", False),
    )


def test_eligible_mid_pipeline_and_at_send_out():
    assert _elig() is None
    assert _elig(state=_state(send_out=("verify", "active"))) is None
    assert _elig(state=_state(send_out=("send_out", "active"))) is None


def test_intake_parked_at_to_estimator_counts_as_past_go_no_go():
    assert _elig(state=_state(intake=("to_estimator", "active"))) is None


@pytest.mark.parametrize("head", ["intake", "go_no_go"])
def test_before_go_no_go_is_not_eligible(head):
    assert _elig(state=_state(intake=(head, "active"))) == "before_go_no_go"


def test_declined_abandoned_and_never_bid():
    assert _elig(project=_project(current_stage="declined")) == "declined"
    assert _elig(project=_project(abandoned_at="2026-09-01T00:00:00Z")) == "abandoned"
    assert _elig(project=_project(current_stage="pm_only")) == "never_bid"
    assert _elig(project=_project(current_stage="cp_only")) == "never_bid"


@pytest.mark.parametrize("head", ["submitted", "bid_outcome"])
def test_already_submitted_heads(head):
    assert _elig(state=_state(send_out=(head, "active"))) == "already_submitted"


def test_post_submission_reverify_bounce_still_reads_as_submitted():
    project = _project(reverify_return_stage="submitted")
    assert _elig(project=project, state=_state(send_out=("verify", "active"))) == (
        "already_submitted"
    )


def test_outcome_and_active_submission_block():
    assert _elig(has_outcome=True) == "outcome_recorded"
    assert _elig(active=True) == "already_submitted"
    assert xs.eligibility(None, {}, has_outcome=False, has_active_submission=False) == (
        "not_found"
    )


# ── grants ─────────────────────────────────────────────────────────────────


def _grant(**kw):
    return {"id": "r1", "project_id": P1, "requested_by": "u-eng", "status": "approved",
            "expires_at": (NOW + timedelta(hours=10)).isoformat(), **kw}


def test_grant_valid_only_for_that_user_on_that_project_until_expiry():
    assert xs.grant_is_valid(_grant(), "u-eng", P1, NOW)
    assert not xs.grant_is_valid(_grant(), "u-other", P1, NOW)
    assert not xs.grant_is_valid(_grant(), "u-eng", "p2", NOW)
    assert not xs.grant_is_valid(_grant(expires_at=NOW.isoformat()), "u-eng", P1, NOW)
    assert not xs.grant_is_valid(
        _grant(expires_at=(NOW - timedelta(seconds=1)).isoformat()), "u-eng", P1, NOW
    )
    for status in ("pending", "used", "denied", "revoked", "cancelled", "expired"):
        assert not xs.grant_is_valid(_grant(status=status), "u-eng", P1, NOW)
    assert not xs.grant_is_valid(None, "u-eng", P1, NOW)


def test_display_status_shows_a_lapsed_grant_as_expired():
    assert xs.display_status(_grant(expires_at=(NOW - timedelta(hours=1)).isoformat()), NOW) == (
        "expired"
    )
    assert xs.display_status(_grant(), NOW) == "approved"
    assert xs.display_status({"status": "pending"}, NOW) == "pending"


# ── figures ────────────────────────────────────────────────────────────────


def _lines(*items):
    return [{"pricing_section": s, "amount": Decimal(a)} for s, a in items]


def test_section_costs_residual_materials_always_present_breakouts_only_with_lines():
    costs = xs.section_costs(_lines(("materials", "100"), ("materials", "50.5"), ("gear", "0")))
    assert costs == {
        "materials": Decimal("150.5"),
        "gear": Decimal("0"),
        "underground": None,
        "low_voltage": None,
    }
    assert xs.section_costs([])["materials"] == Decimal(0)


def test_resolve_markup_mirrors_the_markup_step():
    # A percent alone works out to the dollar amount, to the cent.
    assert xs.resolve_markup(Decimal("1000"), Decimal("12.5"), None) == (
        Decimal("12.5"), Decimal("125.00")
    )
    # A typed amount wins and the percent is derived when left blank.
    assert xs.resolve_markup(Decimal("1000"), None, Decimal("50")) == (
        Decimal("5.000"), Decimal("50.00")
    )
    # Both given: the amount prices the bid, the percent is kept as typed.
    assert xs.resolve_markup(Decimal("1000"), Decimal("10"), Decimal("99")) == (
        Decimal("10"), Decimal("99.00")
    )
    assert xs.resolve_markup(Decimal("1000"), None, None) == (None, None)
    # No cost: no percent can be derived; a percent of nothing is $0.
    assert xs.resolve_markup(Decimal("0"), None, Decimal("20")) == (None, Decimal("20.00"))
    assert xs.resolve_markup(Decimal("0"), Decimal("10"), None) == (Decimal("10"), Decimal("0.00"))
    # A derived percent that would overflow markups.*_markup_pct (numeric(6,3))
    # is dropped; the amount still prices the bid (Test03: $55,907.80 on $1,432.72).
    assert xs.resolve_markup(Decimal("1432.72"), None, Decimal("55907.80")) == (
        None, Decimal("55907.80")
    )


def test_compute_final_has_the_verify_snapshot_shape():
    costs = {"materials": Decimal("1000"), "gear": Decimal("500"), "underground": None,
             "low_voltage": None}
    markups = {
        "materials": (Decimal("10"), Decimal("100")),
        "gear": (None, None),
        "underground": (None, None),
        "low_voltage": (None, None),
        "labor": (None, Decimal("40")),
    }
    final = xs.compute_final(costs, Decimal("400"), markups)
    assert final == {
        "labor_amount": Decimal("400"),
        "materials_amount": Decimal("1000"),
        "labor_markup_amount": Decimal("40"),
        "materials_markup_amount": Decimal("100"),
        "gear_amount": Decimal("500"),
        "gear_markup_amount": Decimal(0),
        "underground_amount": None,
        "underground_markup_amount": None,
        "low_voltage_amount": None,
        "low_voltage_markup_amount": None,
    }


def test_gc_override_moves_only_that_section():
    final = xs.compute_final(
        {"materials": Decimal("1000"), "gear": None, "underground": None, "low_voltage": None},
        Decimal("400"),
        {k: (None, None) for k in xs.MARKUP_KEYS} | {"materials": (None, Decimal("100"))},
    )
    defaults, resolved = xs.gc_amounts(final, {"material": Decimal("1200")})
    assert defaults["material"] == Decimal("1100") and defaults["total"] == Decimal("1500")
    assert resolved["material"] == Decimal("1200")
    assert resolved["labor"] == Decimal("400")
    assert resolved["gear"] is None
    assert resolved["total"] == Decimal("1600")


# ── validation ─────────────────────────────────────────────────────────────


def _body(lines=None, gcs=None, markups=None, labor="400"):
    return ExternalSubmissionIn(
        lines=lines
        if lines is not None
        else [
            ExternalSubmissionLineIn(material_category_id="c-light", pricing_section="materials",
                                     amount=Decimal("1000"), note=" fixtures only "),
            ExternalSubmissionLineIn(material_category_id="c-gear", pricing_section="gear",
                                     amount=Decimal("500")),
        ],
        labor_amount=Decimal(labor),
        labor_note="night work",
        markups=markups
        or ExternalSubmissionMarkupsIn(
            materials=ExternalSubmissionMarkupIn(pct=Decimal("10")),
            gear=ExternalSubmissionMarkupIn(amount=Decimal("50")),
            labor=ExternalSubmissionMarkupIn(pct=Decimal("25")),
        ),
        gcs=gcs
        or [
            ExternalSubmissionGcIn(gc_id="g1", included=True),
            ExternalSubmissionGcIn(gc_id="g2", included=True, material_amount=Decimal("1200")),
            ExternalSubmissionGcIn(gc_id="g3", included=False),
        ],
    )


GCS = {"g1": {"name": "Alpha Builders"}, "g2": {"name": "Bravo GC"}, "g3": {"name": "Charlie Co"}}


def _validate(body, sent=frozenset()):
    xs.validate_submission(body, categories=CATS, project_gcs=GCS, sent_gc_ids=set(sent))


def test_valid_body_passes():
    _validate(_body())


def _code(fn):
    with pytest.raises(ExternalSubmissionError) as ei:
        fn()
    return ei.value.code


def test_category_must_exist_and_belong_to_the_posted_section():
    bad = _body(lines=[ExternalSubmissionLineIn(material_category_id="nope",
                                                pricing_section="materials", amount=Decimal(1))])
    assert _code(lambda: _validate(bad)) == "bad_category"
    wrong = _body(lines=[ExternalSubmissionLineIn(material_category_id="c-gear",
                                                  pricing_section="materials", amount=Decimal(1))])
    assert _code(lambda: _validate(wrong)) == "bad_section"


def test_duplicate_category_rejected():
    line = ExternalSubmissionLineIn(material_category_id="c-light", pricing_section="materials",
                                    amount=Decimal(1))
    assert _code(lambda: _validate(_body(lines=[line, line]))) == "duplicate_category"


def test_negative_amounts_rejected_by_the_schema():
    with pytest.raises(ValueError):
        ExternalSubmissionLineIn(material_category_id="c-light", pricing_section="materials",
                                 amount=Decimal("-1"))
    with pytest.raises(ValueError):
        ExternalSubmissionGcIn(gc_id="g1", included=True, labor_amount=Decimal("-5"))
    with pytest.raises(ValueError):
        ExternalSubmissionMarkupIn(pct=Decimal("-1"))


def test_markup_or_gc_price_for_a_section_with_no_categories_rejected():
    mk = ExternalSubmissionMarkupsIn(underground=ExternalSubmissionMarkupIn(pct=Decimal(5)))
    assert _code(lambda: _validate(_body(markups=mk))) == "markup_without_section"
    gcs = [ExternalSubmissionGcIn(gc_id="g1", included=True, underground_amount=Decimal(10))]
    assert _code(lambda: _validate(_body(gcs=gcs))) == "override_without_section"


def test_gcs_must_be_on_the_project_once_and_at_least_one_included():
    assert _code(lambda: _validate(_body(gcs=[ExternalSubmissionGcIn(gc_id="zz", included=True)]))) == (
        "gc_not_on_project"
    )
    dup = [ExternalSubmissionGcIn(gc_id="g1", included=True)] * 2
    assert _code(lambda: _validate(_body(gcs=dup))) == "duplicate_gc"
    none = [ExternalSubmissionGcIn(gc_id="g1", included=False)]
    assert _code(lambda: _validate(_body(gcs=none))) == "no_included_gc"


def test_a_gc_already_sent_through_send_out_counts_as_included():
    none = [ExternalSubmissionGcIn(gc_id="g1", included=False)]
    _validate(_body(gcs=none), sent={"g2"})


# ── lanes ──────────────────────────────────────────────────────────────────


def test_target_lanes_from_a_locked_send_out():
    rows, events = xs.target_lanes(_state())
    by_cat = {r["category"]: r for r in rows}
    assert by_cat["send_out"] == {"category": "send_out", "current_task": "submitted",
                                  "status": "active", "owner_role": "estimating_admin"}
    for cat in ("intake", "material_numbers", "labor_numbers", "select_vendors", "markup"):
        assert by_cat[cat]["status"] == "complete"
        assert by_cat[cat]["current_task"] == workflow.CATEGORY_TASKS[cat][-1]
    assert [(e["from_stage"], e["to_stage"]) for e in events] == [
        (None, "gc_pricing"), ("gc_pricing", "submitted")
    ]
    assert all(e["category"] == "send_out" for e in events)


def test_target_lanes_from_an_active_send_out_head():
    _, events = xs.target_lanes(_state(send_out=("verify", "active")))
    assert [(e["from_stage"], e["to_stage"]) for e in events] == [("verify", "submitted")]


# ── the apply document ─────────────────────────────────────────────────────


def _plan(body=None, sent=None, state=None):
    return xs.build_plan(
        body or _body(),
        categories=CATS,
        project_gcs=GCS,
        sent_rows=sent or {},
        state=state or _state(),
        who="Dana Admin",
    )


def test_build_plan_writes_the_canonical_figures():
    plan = _plan()
    doc = plan["doc"]
    # Materials 1000 + 10% = 1100; gear 500 + 50 = 550; labor 400 + 25% = 500.
    assert doc["verification"]["materials_amount"] == "1000.00"
    assert doc["verification"]["materials_markup_amount"] == "100.00"
    assert doc["verification"]["gear_amount"] == "500.00"
    assert doc["verification"]["gear_markup_amount"] == "50.00"
    assert doc["verification"]["underground_amount"] is None
    assert doc["verification"]["underground_markup_amount"] is None
    assert doc["verification"]["labor_markup_amount"] == "100.00"
    assert doc["markup"]["materials_markup_pct"] == "10"
    assert doc["markup"]["gear_markup_pct"] == "10.000"
    assert doc["labor_review"] == {"labor_amount": "400.00"}
    assert doc["submission"]["total_cost"] == "1900.00"
    assert doc["submission"]["total_price"] == "2150.00"
    assert doc["submission"]["labor_note"] == "night work"
    assert [line["note"] for line in doc["lines"]] == ["fixtures only", None]
    assert doc["lines"][0]["category_name"] == "Lighting"
    assert doc["headline"] == {"current_stage": "submitted",
                               "current_owner_role": "estimating_admin"}

    gcs = {g["gc_id"]: g for g in doc["gcs"]}
    assert gcs["g1"]["write"] and gcs["g1"]["total_amount"] == "2150.00"
    assert gcs["g1"]["material_amount"] == "1100.00"
    assert gcs["g1"]["override_material"] is None
    assert gcs["g2"]["material_amount"] == "1200.00"
    assert gcs["g2"]["override_material"] == "1200.00"
    assert gcs["g2"]["total_amount"] == "2250.00"
    assert gcs["g2"]["default_total"] == "2150.00"
    assert gcs["g3"] == {**gcs["g3"], "included": False, "write": False, "total_amount": None}
    assert plan["summary"]["submitted"] == ["Alpha Builders", "Bravo GC"]
    assert plan["summary"]["no_bid"] == ["Charlie Co"]
    note = doc["events"][-1]["note"]
    assert "Submitted to: Alpha Builders, Bravo GC." in note
    assert "No bid: Charlie Co." in note
    assert "\u2014" not in note


def test_build_plan_leaves_an_already_sent_gc_untouched():
    sent = {"g1": {"status": "sent", "material_amount": "900", "labor_amount": "300",
                   "gear_amount": None, "underground_amount": None,
                   "low_voltage_amount": None}}
    gcs = {g["gc_id"]: g for g in _plan(sent=sent)["doc"]["gcs"]}
    assert gcs["g1"]["already_sent"] and not gcs["g1"]["write"]
    assert gcs["g1"]["total_amount"] == "1200"


def test_error_code_of_reads_database_raises():
    exc = APIError({"message": "external_submission:grant_invalid", "code": "P0001",
                    "hint": None, "details": None})
    assert xs.error_code_of(exc) == "grant_invalid"
    assert xs.error_code_of(Exception("boom")) is None


def test_db_error_answers_uncoded_database_errors():
    exc = APIError({"message": "numeric field overflow", "code": "22003",
                    "hint": None, "details": None})
    err = xs._db_error(exc)
    assert err.status_code == 500 and err.code == "db_error"


# ── services against a fake database ──────────────────────────────────────


class RpcDB(FakeDB):
    def __init__(self, tables=None, rpc_error=None):
        super().__init__(tables)
        self.rpcs = []
        self.rpc_error = rpc_error

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        err = self.rpc_error

        class _Call:
            def execute(_self):
                if err:
                    raise err
                return SimpleNamespace(data="sub-1" if name.startswith("apply") else None)

        return _Call()


def _cat_rows(send_out=("gc_pricing", "locked")):
    st = _state(send_out=send_out)
    return [{"project_id": P1, "category": c, "current_task": v["current_task"],
             "status": v["status"], "owner_role": None, "completed_at": None}
            for c, v in st.items()]


def _db(**kw):
    tables = {
        "projects": [_project(**kw.pop("project", {}))],
        "project_category_state": _cat_rows(kw.pop("send_out", ("gc_pricing", "locked"))),
        "bid_outcomes": kw.pop("outcomes", []),
        "external_submissions": kw.pop("submissions", []),
        "external_submission_requests": kw.pop("requests", []),
        "external_submission_lines": [],
        "external_submission_gcs": [],
        "material_categories": list(CATS.values()),
        "project_gcs": [
            {"project_id": P1, "gc_id": g, "general_contractors": {"id": g, "name": v["name"]},
             "proposal_material_amount": None, "proposal_gear_amount": None,
             "proposal_underground_amount": None, "proposal_low_voltage_amount": None,
             "proposal_labor_amount": None}
            for g, v in GCS.items()
        ],
        "proposal_sends": kw.pop("sends", []),
        "profiles": [
            {"id": "u-eng", "full_name": "Eli Engineer"},
            {"id": "u-admin", "full_name": "Dana Admin"},
        ],
    }
    return RpcDB(tables, rpc_error=kw.pop("rpc_error", None))


@pytest.fixture
def env(monkeypatch):
    class Env:
        role_notes: list = []
        user_notes: list = []
        dismissals: list = []
        audits: list = []

        def install(self, db):
            self.db = db
            self.role_notes, self.user_notes, self.dismissals, self.audits = [], [], [], []
            monkeypatch.setattr(xs, "get_supabase", lambda: db)
            monkeypatch.setattr(workflow, "get_supabase", lambda: db)
            monkeypatch.setattr(xs, "_now", lambda: NOW)
            monkeypatch.setattr(
                xs, "notify_role",
                lambda role, pid, type_, msg, **kw: self.role_notes.append(
                    (role, type_, msg, kw.get("metadata"))),
            )
            monkeypatch.setattr(
                xs, "notify_user",
                lambda uid, pid, type_, msg, **kw: self.user_notes.append(
                    (uid, type_, msg, kw)),
            )
            monkeypatch.setattr(xs, "dismiss_notifications",
                                lambda **kw: self.dismissals.append(kw))
            monkeypatch.setattr(
                xs, "audit",
                lambda actor, action, entity=None, entity_id=None, payload=None:
                self.audits.append((actor, action, payload)),
            )
            monkeypatch.setattr(workflow, "_dismiss_stale_notifications", lambda *a: None)
            return db

        def req(self, rid):
            return next(r for r in self.db.tables["external_submission_requests"]
                        if r["id"] == rid)

    return Env()


ENG = Role.ESTIMATING_ENGINEER_LABOR
ADMIN = Role.ESTIMATING_ADMIN


def test_request_notifies_every_approver_role_with_the_request_id(env):
    env.install(_db())
    row = xs.create_request(P1, "u-eng", ENG, "  sent via the GC portal ")
    assert row["status"] == "pending"
    assert row["message"] == "sent via the GC portal"
    roles = {n[0] for n in env.role_notes}
    assert roles == {Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN}
    assert all(n[1] == "external_submission.requested" for n in env.role_notes)
    assert all(n[3] == {"request_id": row["id"]} for n in env.role_notes)
    assert "Eli Engineer" in env.role_notes[0][2]
    assert "sent via the GC portal" in env.role_notes[0][2]
    assert "\u2014" not in env.role_notes[0][2]
    assert env.audits[-1][1] == "external_submission.requested"


def test_approvers_do_not_request_and_one_open_request_per_user(env):
    env.install(_db())
    assert _code(lambda: xs.create_request(P1, "u-admin", ADMIN, None)) == "not_a_requester"
    xs.create_request(P1, "u-eng", ENG, None)
    assert _code(lambda: xs.create_request(P1, "u-eng", ENG, None)) == "request_open"


def test_an_expired_grant_frees_the_user_to_ask_again(env):
    stale = _grant(id="old", expires_at=(NOW - timedelta(hours=1)).isoformat())
    env.install(_db(requests=[stale]))
    xs.create_request(P1, "u-eng", ENG, None)
    assert env.req("old")["status"] == "expired"


def test_request_refused_when_the_project_is_not_eligible(env):
    env.install(_db(send_out=("submitted", "active")))
    assert _code(lambda: xs.create_request(P1, "u-eng", ENG, None)) == "already_submitted"


def _pending(rid="r1", user="u-eng"):
    return {"id": rid, "project_id": P1, "requested_by": user, "status": "pending",
            "message": None, "expires_at": None, "used_at": None}


def test_grant_sets_48_hours_and_tells_the_requester(env):
    env.install(_db(requests=[_pending()]))
    xs.grant_request(P1, "r1", "u-admin", ADMIN)
    r = env.req("r1")
    assert r["status"] == "approved" and r["decided_by"] == "u-admin"
    assert datetime.fromisoformat(r["expires_at"]) == NOW + timedelta(hours=48)
    (uid, type_, msg, kw), = env.user_notes
    assert uid == "u-eng" and type_ == "external_submission.granted"
    assert "48 hours" in msg and kw["metadata"] == {"request_id": "r1"}
    assert env.dismissals[-1]["metadata_eq"] == {"request_id": "r1"}
    assert env.dismissals[-1]["types"] == ["external_submission.requested"]


def test_first_approver_wins_the_decision(env):
    env.install(_db(requests=[_pending()]))
    xs.grant_request(P1, "r1", "u-admin", ADMIN)
    assert _code(lambda: xs.deny_request(P1, "r1", "u-exec", Role.EXECUTIVE, "no")) == (
        "already_decided"
    )


def test_requesters_cannot_decide(env):
    env.install(_db(requests=[_pending(user="u-other")]))
    assert _code(lambda: xs.grant_request(P1, "r1", "u-eng", ENG)) == "forbidden"
    assert _code(lambda: xs.revoke_grant(P1, "r1", "u-eng", ENG)) == "forbidden"


def test_deny_carries_the_reason(env):
    env.install(_db(requests=[_pending()]))
    xs.deny_request(P1, "r1", "u-admin", ADMIN, "  send it through Send Out ")
    assert env.req("r1")["deny_reason"] == "send it through Send Out"
    assert env.user_notes[0][1] == "external_submission.denied"
    assert "Reason: send it through Send Out" in env.user_notes[0][2]


def test_cancel_only_your_own_pending_request(env):
    env.install(_db(requests=[_pending()]))
    assert _code(lambda: xs.cancel_request(P1, "r1", "u-other")) == "not_pending"
    xs.cancel_request(P1, "r1", "u-eng")
    assert env.req("r1")["status"] == "cancelled"
    assert env.dismissals[-1]["metadata_eq"] == {"request_id": "r1"}


def test_revoke_an_unused_grant(env):
    env.install(_db(requests=[_grant(used_at=None)]))
    xs.revoke_grant(P1, "r1", "u-admin", ADMIN)
    assert env.req("r1")["status"] == "revoked"
    assert env.user_notes[0][1] == "external_submission.revoked"


# ── submit ─────────────────────────────────────────────────────────────────


def test_requester_without_a_grant_cannot_open_the_wizard_or_submit(env):
    env.install(_db())
    assert _code(lambda: xs.submit(P1, "u-eng", ENG, _body())) == "grant_required"
    assert env.db.rpcs == []


def test_an_expired_grant_is_refused_server_side(env):
    env.install(_db(requests=[_grant(expires_at=(NOW - timedelta(minutes=1)).isoformat())]))
    assert _code(lambda: xs.submit(P1, "u-eng", ENG, _body())) == "grant_required"
    assert env.req("r1")["status"] == "expired"


def test_someone_elses_grant_does_not_count(env):
    env.install(_db(requests=[_grant(requested_by="u-other")]))
    assert _code(lambda: xs.submit(P1, "u-eng", ENG, _body())) == "grant_required"


def test_accountant_and_estimator_are_refused(env):
    env.install(_db())
    assert _code(lambda: xs.submit(P1, "u-acc", Role.ACCOUNTANT, _body())) == "forbidden"
    assert _code(lambda: xs.wizard_data(P1, "u-x", Role.ESTIMATOR)) == "forbidden"


def test_requester_with_a_valid_grant_submits_and_consumes_it(env):
    env.install(_db(requests=[_grant()]))
    xs.submit(P1, "u-eng", ENG, _body())
    (name, params), = env.db.rpcs
    assert name == "apply_external_submission"
    doc = params["p"]
    assert doc["request_id"] == "r1" and doc["user_id"] == "u-eng"
    assert doc["expected_send_out_head"] == "gc_pricing"
    assert doc["project_id"] == P1
    assert env.audits[-1][1] == "external_submission.submitted"
    assert {n[1] for n in env.role_notes} == {"submitted"}


def test_approver_submits_without_a_request(env):
    env.install(_db())
    xs.submit(P1, "u-admin", ADMIN, _body())
    assert env.db.rpcs[0][1]["p"]["request_id"] is None


def test_submit_refused_when_not_eligible_or_a_send_is_in_flight(env):
    env.install(_db(outcomes=[{"id": "o1", "project_id": P1}]))
    assert _code(lambda: xs.submit(P1, "u-admin", ADMIN, _body())) == "outcome_recorded"
    env.install(_db(sends=[{"id": "s1", "project_id": P1, "gc_id": "g1", "status": "sending"}]))
    assert _code(lambda: xs.submit(P1, "u-admin", ADMIN, _body())) == "sending_in_progress"


def test_database_refusals_map_to_readable_409s(env):
    err = APIError({"message": "external_submission:grant_invalid", "code": "P0001",
                    "hint": None, "details": None})
    env.install(_db(requests=[_grant()], rpc_error=err))
    with pytest.raises(ExternalSubmissionError) as ei:
        xs.submit(P1, "u-eng", ENG, _body())
    assert ei.value.status_code == 409 and ei.value.code == "grant_invalid"
    assert "Request permission again" in str(ei.value)


def test_unrelated_database_errors_come_back_as_a_message(env):
    # A bare 500 loses its CORS headers and the wizard sits on "Saving".
    err = APIError({"message": "connection reset", "code": "XX000", "hint": None,
                    "details": None})
    env.install(_db(rpc_error=err))
    assert _code(lambda: xs.submit(P1, "u-admin", ADMIN, _body())) == "db_error"


# ── undo ───────────────────────────────────────────────────────────────────


def _active_sub():
    return {"id": "sub-1", "project_id": P1, "status": "active", "submitted_by": "u-eng",
            "approval_request_id": None}


def test_undo_is_for_approvers_only(env):
    env.install(_db(send_out=("submitted", "active"), submissions=[_active_sub()]))
    assert _code(lambda: xs.undo(P1, "u-eng", ENG, None)) == "forbidden"


def test_undo_refused_once_an_outcome_is_recorded(env):
    env.install(_db(send_out=("submitted", "active"), submissions=[_active_sub()],
                    outcomes=[{"id": "o1", "project_id": P1}]))
    assert _code(lambda: xs.undo(P1, "u-admin", ADMIN, None)) == "outcome_recorded"
    assert env.db.rpcs == []


def test_undo_calls_the_restore_and_tells_the_submitter_in_app(env):
    env.install(_db(send_out=("submitted", "active"), submissions=[_active_sub()]))
    xs.undo(P1, "u-admin", ADMIN, " wrong project ")
    (name, params), = env.db.rpcs
    assert name == "undo_external_submission"
    assert params == {"p_submission_id": "sub-1", "p_user_id": "u-admin",
                      "p_reason": "wrong project"}
    (uid, type_, msg, kw), = env.user_notes
    assert uid == "u-eng" and type_ == "external_submission.undone"
    assert kw["mirror_email"] is False
    assert "Reason: wrong project" in msg


def test_undo_with_nothing_to_undo(env):
    env.install(_db())
    assert _code(lambda: xs.undo(P1, "u-admin", ADMIN, None)) == "not_active"


# ── overview ───────────────────────────────────────────────────────────────


def test_overview_for_an_approver_lists_pending_and_live_grants(env):
    env.install(_db(requests=[_pending("r1"), _grant(id="r2", requested_by="u-other")]))
    ov = xs.overview(P1, "u-admin", ADMIN)
    assert ov["role_kind"] == "approver" and ov["eligible"] and ov["can_submit"]
    assert [r["id"] for r in ov["pending_requests"]] == ["r1"]
    assert [r["id"] for r in ov["active_grants"]] == ["r2"]
    assert ov["pending_requests"][0]["requested_by_name"] == "Eli Engineer"
    assert xs.pending_count(P1, ADMIN) == 1
    assert xs.pending_count(P1, ENG) == 0


def test_overview_for_a_requester_shows_their_grant_only(env):
    env.install(_db(requests=[_grant()]))
    ov = xs.overview(P1, "u-eng", ENG)
    assert ov["role_kind"] == "requester" and ov["can_submit"]
    assert ov["my_grant"]["id"] == "r1"
    assert ov["pending_requests"] == [] and ov["active_grants"] == []


def test_overview_of_a_submitted_project_offers_undo_to_approvers(env):
    env.install(_db(send_out=("submitted", "active"), submissions=[_active_sub()]))
    ov = xs.overview(P1, "u-admin", ADMIN)
    assert not ov["eligible"] and ov["reason"] == "already_submitted"
    assert ov["submission"]["submitted_by_name"] == "Eli Engineer"
    assert ov["can_undo"]
    ov = xs.overview(P1, "u-eng", ENG)
    assert not ov["can_undo"]


# ── router ─────────────────────────────────────────────────────────────────


def _user(role, uid="u-admin"):
    return SimpleNamespace(id=uid, role=role)


def test_router_maps_service_errors_with_the_error_code(env):
    env.install(_db())
    with pytest.raises(HTTPException) as ei:
        router.submit(P1, _body(), _user(ENG, "u-eng"))
    assert ei.value.status_code == 403
    assert ei.value.headers == {"X-Error-Code": "grant_required"}


def test_router_request_grant_undo_roundtrip(env):
    env.install(_db())
    ov = router.create_request(P1, ExternalSubmissionRequestIn(message="portal"),
                               _user(ENG, "u-eng"))
    assert ov["my_request"]["status"] == "pending"
    rid = ov["my_request"]["id"]
    ov = router.grant_request(P1, rid, _user(ADMIN))
    assert ov["pending_requests"] == []
    with pytest.raises(HTTPException) as ei:
        router.deny_request(P1, rid, ExternalSubmissionDenyIn(reason="late"), _user(ADMIN))
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        router.undo(P1, ExternalSubmissionUndoIn(), _user(ADMIN))
    assert ei.value.headers == {"X-Error-Code": "not_active"}


def test_every_route_is_writer_gated():
    from app.core.deps import require_writer

    for route in router.router.routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert require_writer in deps, route.path


# ── notifications ──────────────────────────────────────────────────────────


def test_deep_link_opens_the_mark_submitted_box(monkeypatch):
    monkeypatch.setattr(ne, "get_settings",
                        lambda: SimpleNamespace(frontend_url="https://bdr.example/"))
    for type_ in ("external_submission.requested", "external_submission.granted",
                  "external_submission.undone"):
        assert ne._deep_link(P1, "executive", type_, None) == (
            "https://bdr.example/projects/p1?box=mark_submitted"
        )
    assert ne.heading_for("external_submission.requested") == (
        "Permission requested to mark a bid submitted"
    )


def test_dismissal_can_target_one_request(monkeypatch):
    calls = []

    class Q:
        def update(self, *a):
            return self

        def is_(self, *a):
            return self

        def eq(self, col, val):
            calls.append((col, val))
            return self

        def in_(self, *a):
            return self

        def execute(self):
            return SimpleNamespace(data=[])

    monkeypatch.setattr(notif, "get_supabase", lambda: SimpleNamespace(table=lambda n: Q()))
    notif.dismiss_notifications(project_id=P1, types=["external_submission.requested"],
                                metadata_eq={"request_id": "r9"})
    assert ("metadata->>request_id", "r9") in calls


# ── review fixes (2026-09-29) ──────────────────────────────────────────────


def test_an_approver_cannot_decide_their_own_request(env):
    # A requester promoted to an approver role while the request was pending.
    env.install(_db(requests=[_pending(user="u-admin")]))
    assert _code(lambda: xs.grant_request(P1, "r1", "u-admin", ADMIN)) == "own_request"
    assert _code(lambda: xs.deny_request(P1, "r1", "u-admin", ADMIN, None)) == "own_request"
    assert env.req("r1")["status"] == "pending"
    # They can still cancel it like any requester.
    xs.cancel_request(P1, "r1", "u-admin")
    assert env.req("r1")["status"] == "cancelled"


def test_no_grant_for_a_project_that_can_no_longer_be_marked(env):
    env.install(_db(send_out=("submitted", "active"), requests=[_pending()]))
    assert _code(lambda: xs.grant_request(P1, "r1", "u-admin", ADMIN)) == "already_submitted"
    assert env.req("r1")["status"] == "pending"
    # Denying stays possible, so the queue can always be cleared.
    xs.deny_request(P1, "r1", "u-admin", ADMIN, "sent through Send Out")
    assert env.req("r1")["status"] == "denied"


def test_badge_is_zero_once_the_project_cannot_be_marked(env):
    env.install(_db(requests=[_pending()]))
    assert xs.pending_count(P1, ADMIN) == 1
    env.install(_db(send_out=("submitted", "active"), requests=[_pending()]))
    assert xs.pending_count(P1, ADMIN) == 0
    env.install(_db(requests=[_pending()], outcomes=[{"id": "o1", "project_id": P1}]))
    assert xs.pending_count(P1, ADMIN) == 0


def test_expected_head_is_the_stored_row_the_plan_was_built_from(env):
    db = env.install(_db())
    db.tables["project_category_state"] = [
        r for r in db.tables["project_category_state"] if r["category"] != "send_out"
    ]
    xs.submit(P1, "u-admin", ADMIN, _body())
    doc = env.db.rpcs[0][1]["p"]
    # No send_out row: the database reads NULL, so the CAS must expect NULL,
    # not the default map's 'gc_pricing'.
    assert doc["expected_send_out_head"] is None
    # The plan still unlocks the lane the normal way.
    assert doc["events"][0] == {"category": "send_out", "from_stage": None,
                                "to_stage": "gc_pricing", "note": "Category unlocked"}


def test_a_bid_already_sent_to_every_gc_posts_no_gc_rows(env):
    sent = [{"id": f"s-{g}", "project_id": P1, "gc_id": g, "status": "sent",
             "sent_via": "email", "material_amount": "1000", "labor_amount": "500"}
            for g in GCS]
    env.install(_db(sends=sent))
    body = ExternalSubmissionIn(
        lines=[ExternalSubmissionLineIn(material_category_id="c-light",
                                        pricing_section="materials", amount=Decimal("10"))],
        gcs=[],
    )
    xs.submit(P1, "u-admin", ADMIN, body)
    doc = env.db.rpcs[0][1]["p"]
    assert all(g["already_sent"] and not g["write"] for g in doc["gcs"])


def test_submit_clears_every_request_and_grant_notice_on_the_project(env):
    env.install(_db(requests=[_grant()]))
    xs.submit(P1, "u-eng", ENG, _body())
    assert {"project_id": P1, "types": [xs.REQUEST_TYPE, xs.DECISION_TYPES["granted"]]} in (
        env.dismissals
    )


def test_submit_after_the_lanes_moved_is_refused_before_the_database(env, monkeypatch):
    env.install(_db())
    real = xs._state_and_send_out_head

    def moved(sb, pid):
        state, _head = real(sb, pid)
        state["send_out"] = {"current_task": "submitted", "status": "active"}
        return state, "submitted"

    monkeypatch.setattr(xs, "_state_and_send_out_head", moved)
    assert _code(lambda: xs.submit(P1, "u-admin", ADMIN, _body())) == "stale"
    assert env.db.rpcs == []


@pytest.mark.parametrize(
    "code", ["never_bid", "declined", "abandoned", "before_go_no_go", "already_submitted"]
)
def test_in_lock_eligibility_refusals_map_to_readable_409s(env, code):
    err = APIError({"message": f"external_submission:{code}", "code": "P0001",
                    "hint": None, "details": None})
    env.install(_db(rpc_error=err))
    with pytest.raises(ExternalSubmissionError) as ei:
        xs.submit(P1, "u-admin", ADMIN, _body())
    assert ei.value.status_code == 409 and ei.value.code == code
    assert str(ei.value) == xs.REASON_MESSAGES[code]


def test_migration_rechecks_eligibility_and_closes_requests_inside_the_apply():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1]
           / "supabase/migrations/0140_external_submissions.sql").read_text()
    apply = sql[sql.index("create or replace function apply_external_submission"):
                sql.index("create or replace function undo_external_submission")]
    for code in ("never_bid", "declined", "abandoned", "before_go_no_go"):
        assert f"external_submission:{code}" in apply
    assert "for update" in apply.split("category = 'send_out'")[1][:40]
    assert "set status = 'cancelled'" in apply and "set status = 'expired'" in apply
    undo = sql[sql.index("create or replace function undo_external_submission"):]
    assert "entered_at >= v_sub.submitted_at" in undo


def test_prefill_markup_seeds_a_percent_only_when_it_reproduces_the_amount():
    d = Decimal
    # 10% of 1000 = 100: the percent drives.
    assert xs.prefill_markup(d("1000"), "10", d("100"), d("100")) == {"pct": "10", "amount": "100.00"}
    # A percent with more decimals than the submit accepts: the amount drives.
    assert xs.prefill_markup(d("1000"), "10.12345", d("101.23"), d("101.23")) == {
        "pct": None, "amount": "101.23"}
    # The percent does not reproduce the amount on the prefilled cost (the
    # Executive overrode the markup at Verify): the amount drives.
    assert xs.prefill_markup(d("1000"), "10", d("100"), d("150")) == {
        "pct": None, "amount": "150.00"}
    assert xs.prefill_markup(d("900"), "10", d("100"), d("100"))["pct"] is None
    assert xs.prefill_markup(None, "10", d("100"), d("100"))["pct"] is None
    assert xs.prefill_markup(d("1000"), None, None, None) == {"pct": None, "amount": None}
    # Sub-cent amounts are seeded to the cent.
    assert xs.prefill_markup(d("1"), None, None, d("12.345"))["amount"] == "12.35"


def test_wizard_prefill_is_rounded_to_what_the_submit_accepts(env, monkeypatch):
    from app.routers import pricing

    env.install(_db())
    env.db.tables["project_gcs"][0]["proposal_material_amount"] = "1234.567"
    monkeypatch.setattr(pricing, "_materials_rows", lambda pid: [
        {"material_category_id": "c-light", "amount": "1000.005"},
        {"material_category_id": "c-gear", "amount": "500"},
        {"material_category_id": "c-gone", "amount": "7"},
    ])
    rows = {
        "labor_reviews": {"labor_amount": "400.126"},
        "markups": {"materials_markup_pct": "10", "materials_markup_amount": "100.00",
                    "labor_markup_pct": "12.5", "labor_markup_amount": "50.02"},
        "verifications": None,
    }
    monkeypatch.setattr(pricing, "_get_one", lambda table, pid: rows.get(table))
    monkeypatch.setattr(pricing, "_verify_originals", lambda pid: {
        "labor_amount": Decimal("400.126"), "materials_amount": Decimal("1000.005"),
        "gear_amount": Decimal("500"), "underground_amount": None,
        "low_voltage_amount": None, "labor_markup_amount": Decimal("50.02"),
        "materials_markup_amount": Decimal("100.00"), "gear_markup_amount": None,
        "underground_markup_amount": None, "low_voltage_markup_amount": None,
    })
    data = xs.wizard_data(P1, "u-admin", ADMIN)
    assert [(ln["material_category_id"], ln["amount"]) for ln in data["prefill"]["lines"]] == [
        ("c-light", "1000.01"), ("c-gear", "500.00")]
    assert data["prefill"]["labor_amount"] == "400.13"
    # 10% of the prefilled 1000.01 is 100.00: the percent reproduces it.
    assert data["prefill"]["markups"]["materials"] == {"pct": "10", "amount": "100.00"}
    # 12.5% of 400.13 is 50.02: kept.
    assert data["prefill"]["markups"]["labor"] == {"pct": "12.5", "amount": "50.02"}
    gc = next(g for g in data["gcs"] if g["gc_id"] == "g1")
    assert gc["material_override"] == "1234.57"

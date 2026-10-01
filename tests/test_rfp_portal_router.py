"""The NGEM portal router (app/routers/rfp_portal) called as plain functions
with app/services/rfp_portal_ingest stubbed (it owns every state transition)
and a tiny fake Supabase for the existence pre-checks; plus the project
detail's `portal_sources` (app/routers/projects) and the feature flag.

Pinned (docs/RFP_NGEM_PORTAL.md section 5 and section 9's router bullet):

- The route table: exactly the ten (method, path) pairs of the section 5
  table, every one carrying the env-switch gate (router-level, before auth),
  the review-queue role dependency and the catch-all rate limiter; the
  literal /ngem/* paths registered before the parameterised ones.
- The switch: RFP_INGESTION_ENABLED and RFP_NGEM_ENABLED both on, fail
  closed, 404 with the bare body while off; a configured account is not
  part of it.
- Roles: the review queue (Estimating Admin, IT Admin, Executive, both
  engineer focuses) and nobody else.
- Every route's answers and codes: the list withholds match_score from
  roles outside ACTUAL_BID_VIEWER_ROLES; the detail never carries
  view_url, a download URL, cookies or the password and is run through
  rfp_match.redact_candidates for those roles only; each action 404s a
  malformed or missing id, maps RfpPortalError onto 409 / 400 / 503 under
  its own X-Error-Code, and answers the fresh detail; harvest answers 202
  {job}; Run now answers 202 {run}; the status route is passed through.
- GET /projects/{id} gains `portal_sources` only with both flags on.

BuildingConnected additions (docs/RFP_BUILDINGCONNECTED.md section 6, build
contract D4, D8, D21, D23, D30, D33):

- The router gate is "any portal served"; every request then 404s a portal
  that is not served (the list's `portal`, the row's `portal`, /ngem/*).
- Lanes are validated per portal (VIEWS_BY_PORTAL) and default per portal.
- The three invitation actions: POST {id}/gc (review roles), {id}/apply-dates
  (ACTUAL_BID_EDITOR_ROLES, the documented exception to the review-queue
  dependency) and {id}/restore (review roles).
- `payload` leaves the detail for IT Admin only.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import features
from app.core.config import Settings
from app.core.deps import CurrentUser
from app.core.error_codes import ErrorCode
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, RFP_REVIEW_ROLES, Role
from app.routers import projects as pr
from app.routers import rfp_portal as rp
from app.services.rfp_portal_ingest import RfpPortalError

I1 = "3f1c0b00-0000-4000-8000-000000000001"
I2 = "3f1c0b00-0000-4000-8000-000000000002"
I3 = "3f1c0b00-0000-4000-8000-000000000003"
GC1 = "6c000000-0000-4000-8000-000000000001"
GC2 = "6c000000-0000-4000-8000-000000000002"
MISSING = "3f1c0b00-0000-4000-8000-0000000000ff"
P1 = "5a000000-0000-4000-8000-000000000001"
NOT_A_UUID = "not-a-uuid"
VIEW = "https://supplier.ionwave.net/VendorResponse/Bid/VResponseEvent.aspx?e=SECRET-VIEW"


# ── Fakes ────────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._filters = []

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        return self

    def execute(self):
        rows = [r for r in self.db.tables.get(self.table, []) if all(r.get(c) == v for c, v in self._filters)]
        return SimpleNamespace(data=[dict(r) for r in rows], count=len(rows))


class FakeDB:
    def __init__(self, tables=None):
        self.tables = tables or {}

    def table(self, name):
        return _Query(self, name)


class _PortalError(RfpPortalError):
    """The service's refusal, built the way the tests want; the router
    catches the real class (the stub exposes it as the module does)."""

    def __init__(self, code, message="refused", locked_until=None, http_status=None):
        super().__init__(code, message, locked_until=locked_until, http_status=http_status)


def _detail_row(**over):
    row = {
        "id": I1, "portal": "ngem", "agency": "UNLV", "bid_number": "5584-GS", "bid_number_raw": "5584-GS Addendum 1",
        "addendum_no": 1, "title": "Emergency Phone Tower", "status": "review_match", "flag_reason": "match_uncertain",
        "match_project_id": P1, "match_score": 0.9, "view_url": VIEW,
        "match_candidates": [
            {"project_id": P1, "name": "Phone Tower", "number": "26.9.7200", "verdict": "unsure", "confidence": 0.4,
             "breakdown": {"total": 0.9, "name": 0.8, "date": 1.0, "closest_kind": "actual",
                           "dates_used": ["internal", "actual"], "exact_time": True, "notes_bonus": 0.0}},
        ],
        "excluded_projects": [], "change_log": [], "possible_rebid": None,
        "harvest": {"id": "hv-1", "status": "complete", "files": [{"file_name": "Plans.pdf", "status": "accepted"}]},
        "harvest_job": None, "harvest_available": {"ok": False, "reason": "x"},
        "resolved_by_name": None, "ignored_by_name": None,
    }
    row.update(over)
    return row


class _Service:
    """Stand-in for app/services/rfp_portal_ingest: only what the router reaches."""

    INVITATIONS_TABLE = "rfp_portal_invitations"
    PORTAL_NGEM = "ngem"
    CODE_NOT_ACTIONABLE = "rfp_portal_not_actionable"
    CODE_PROJECT_REQUIRED = "rfp_portal_project_required"
    CODE_HARVEST_ACTIVE = "rfp_portal_harvest_active"
    CODE_RUN_ACTIVE = "rfp_portal_run_active"
    CODE_LOCKED = "rfp_portal_locked"
    CODE_NOT_AVAILABLE = "rfp_portal_not_available"
    CODE_REASON_INVALID = "rfp_portal_reason_invalid"
    RfpPortalError = RfpPortalError
    VIEWS_BY_PORTAL = {
        "ngem": {"review": (), "new": (), "existing": (), "ignored": (), "all": ()},
        "buildingconnected": {"needs_action": (), "open": (), "existing": (), "historical": (),
                              "expired_withdrawn": (), "ignored": (), "all": ()},
    }

    def __init__(self):
        self.calls = []
        self.raise_error = None
        self.detail_row = _detail_row()
        self.list_out = {"items": [{"id": I1, "match_score": 0.9, "match_project": {"id": P1}}], "total": 1,
                         "offset": 0, "limit": 50, "counts": {"review": 1, "new": 0, "existing": 0, "ignored": 0, "all": 1}}
        self.status_out = {"enabled": True, "configured": True, "account": "acct", "locked_until": None,
                           "schedule_times": ["06:30", "12:00"], "last_run": None, "active_run": None, "active_jobs": 0}

    def _act(self, name, *args):
        self.calls.append((name, *args))
        if self.raise_error is not None:
            raise self.raise_error
        return copy.deepcopy(self.detail_row)

    def list_invitations(self, sb, *, portal, view, q, limit, offset, state=None):
        self.calls.append(("list", portal, view, q, limit, offset))
        self.last_state = state
        return copy.deepcopy(self.list_out)

    def detail(self, sb, invitation_id):
        self.calls.append(("detail", invitation_id))
        if invitation_id == I1:
            return copy.deepcopy(self.detail_row)
        if invitation_id == I3:
            return _detail_row(id=I3, portal="buildingconnected", agency="Acme GC", payload={"id": "opp"},
                               lead={"name": "Pat Lee", "email": "pat@acme.test", "phone": "702"})
        return None

    def restore(self, sb, invitation_id, actor_id):
        return self._act("restore", invitation_id, actor_id)

    def resolve_exists(self, sb, invitation_id, project_id, actor_id):
        return self._act("resolve", invitation_id, project_id, actor_id)

    def resolve_new(self, sb, invitation_id, actor_id):
        return self._act("new", invitation_id, actor_id)

    def reopen(self, sb, invitation_id, reason, actor_id):
        return self._act("reopen", invitation_id, reason, actor_id)

    def ignore(self, sb, invitation_id, reason, actor_id):
        return self._act("ignore", invitation_id, reason, actor_id)

    def unignore(self, sb, invitation_id, actor_id):
        return self._act("unignore", invitation_id, actor_id)

    def harvest_again(self, sb, invitation_id, actor_id, *, force=True):
        self.calls.append(("harvest", invitation_id, actor_id, force))
        if self.raise_error is not None:
            raise self.raise_error
        return {"id": "job-1", "status": "queued"}

    def create_project(self, sb, invitation_id, actor_id):
        from app.services import rfp_create

        self.calls.append(("create", invitation_id, actor_id))
        if self.raise_error is not None:
            raise self.raise_error
        return rfp_create.Created(project_id=P1, number="26.9.7204", linked=True, files_job=False)

    def run_now(self, sb, actor_id, portal="ngem"):
        self.calls.append(("run_now", actor_id, portal))
        if self.raise_error is not None:
            raise self.raise_error
        return {"id": "run-1", "status": "queued", "trigger": "manual"}

    def session_status(self, portal="ngem"):
        self.calls.append(("status", portal))
        return copy.deepcopy(self.status_out)


class _RfpMatch:
    def __init__(self):
        self.redact_calls = []

    def redact_candidates(self, obj, role):
        self.redact_calls.append(role)
        if role in ACTUAL_BID_VIEWER_ROLES:
            return obj
        out = copy.deepcopy(obj)
        for c in out.get("match_candidates") or []:
            b = c.get("breakdown") or {}
            if b.get("closest_kind") == "actual":
                b["date"] = None
                b["closest_kind"] = None
        out["match_score"] = None
        return out


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB({"rfp_portal_invitations": [
        {"id": I1, "portal": "ngem", "status": "review_match", "gc_id": None},
        {"id": I2, "portal": "ngem", "status": "done", "gc_id": None},
        {"id": I3, "portal": "buildingconnected", "status": "review_match", "gc_id": GC1},
    ]})
    monkeypatch.setattr(rp, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def svc(monkeypatch):
    stub = _Service()
    monkeypatch.setattr(rp, "_service", lambda: stub)
    return stub


@pytest.fixture
def match_mod(monkeypatch):
    stub = _RfpMatch()
    monkeypatch.setattr(rp, "_match", lambda: stub)
    return stub


def _on(**flags):
    """Settings with the master switch on and the named portal switches."""
    return Settings(_env_file=None, rfp_ingest_enabled=True, **flags)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(rp, "get_settings", lambda: _on(rfp_ngem_enabled=True, rfp_bc_enabled=True))


def _user(role=Role.ESTIMATING_ADMIN, uid="u1"):
    return CurrentUser(id=uid, email="e@g3electrical.com", role=role, is_active=True)


# ── Route table, gate, roles ─────────────────────────────────────────────


def _route_pairs():
    return {(method, r.path) for r in rp.router.routes for method in r.methods}


def test_route_table_is_exactly_the_spec():
    assert _route_pairs() == {
        ("GET", "/rfp-portal/invitations"),
        ("GET", "/rfp-portal/invitations/{invitation_id}"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/resolve"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/new"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/reopen"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/ignore"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/unignore"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/harvest"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/create"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/gc"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/apply-dates"),
        ("POST", "/rfp-portal/invitations/{invitation_id}/restore"),
        ("POST", "/rfp-portal/ngem/runs"),
        ("GET", "/rfp-portal/ngem/status"),
    }


# The one documented exception to "every route carries the review-queue
# dependency": apply-dates writes the actual bid date (D8).
_EDITOR_ONLY = {("POST", "/rfp-portal/invitations/{invitation_id}/apply-dates")}


def test_every_route_is_gated_role_checked_and_rate_limited():
    for route in rp.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        pairs = {(m, route.path) for m in route.methods}
        assert rp.require_rfp_portal in calls, route.path
        assert rp.rfp_portal_rate_limit in calls, route.path
        if pairs & _EDITOR_ONLY:
            assert rp.require_actual_bid_editor in calls and rp.require_review_queue not in calls, route.path
        else:
            assert rp.require_review_queue in calls, route.path
            assert rp.require_actual_bid_editor not in calls, route.path


def test_switch_needs_the_master_and_a_portal_and_fails_closed(monkeypatch):
    monkeypatch.setattr(rp, "get_settings", lambda: SimpleNamespace())
    assert rp.rfp_portal_enabled() is False
    with pytest.raises(HTTPException) as exc:
        rp.require_rfp_portal()
    assert exc.value.status_code == 404 and exc.value.detail == "Not Found"
    for flags in ({"rfp_ingest_enabled": True, "rfp_ngem_enabled": False, "rfp_bc_enabled": False},
                  {"rfp_ingest_enabled": False, "rfp_ngem_enabled": True, "rfp_bc_enabled": True}):
        monkeypatch.setattr(rp, "get_settings", lambda f=flags: SimpleNamespace(**f))
        assert rp.rfp_portal_enabled() is False
        assert rp.portal_served("ngem") is False and rp.portal_served("buildingconnected") is False
    # Either portal alone opens the router; each is served on its own switch.
    monkeypatch.setattr(rp, "get_settings", lambda: _on(rfp_ngem_enabled=True))
    assert rp.rfp_portal_enabled() is True and rp.require_rfp_portal() is None
    assert rp.portal_served("ngem") is True and rp.portal_served("buildingconnected") is False
    monkeypatch.setattr(rp, "get_settings", lambda: _on(rfp_bc_enabled=True))
    assert rp.rfp_portal_enabled() is True
    assert rp.portal_served("ngem") is False and rp.portal_served("buildingconnected") is True
    assert rp.portal_served(None) is False and rp.portal_served("procore") is False


def test_review_queue_roles_are_the_shared_tuple():
    assert rp.REVIEW_QUEUE_ROLES == RFP_REVIEW_ROLES
    assert Role.ACCOUNTANT not in RFP_REVIEW_ROLES and Role.ESTIMATOR not in RFP_REVIEW_ROLES


def test_error_codes_match_the_service_and_the_registry(svc):
    from app.services import rfp_portal_ingest as real

    assert {c.value for c in rp._CODES} == {
        svc.CODE_NOT_ACTIONABLE, svc.CODE_PROJECT_REQUIRED, svc.CODE_HARVEST_ACTIVE,
        svc.CODE_RUN_ACTIVE, svc.CODE_LOCKED, svc.CODE_NOT_AVAILABLE, svc.CODE_REASON_INVALID,
    } == {
        real.CODE_NOT_ACTIONABLE, real.CODE_PROJECT_REQUIRED, real.CODE_HARVEST_ACTIVE,
        real.CODE_RUN_ACTIVE, real.CODE_LOCKED, real.CODE_NOT_AVAILABLE, real.CODE_REASON_INVALID,
    }
    assert ErrorCode.RFP_PORTAL_LOCKED.value == "rfp_portal_locked"
    assert ErrorCode.RFP_PORTAL_REASON_INVALID.value == "rfp_portal_reason_invalid"


def test_bc_action_codes_match_the_bc_service_and_the_registry():
    from app.services import rfp_bc_portal

    assert {c.value for c in rp._BC_CODES} == {
        rfp_bc_portal.CODE_GC_REQUIRED, rfp_bc_portal.CODE_DATES_UNCHANGED, "rfp_portal_not_restorable",
    }
    assert ErrorCode.RFP_PORTAL_NOT_RESTORABLE.value == "rfp_portal_not_restorable"


# ── Reads ────────────────────────────────────────────────────────────────


def test_list_passes_the_query_through_and_withholds_the_score_for_engineers(db, svc):
    out = rp.list_invitations(portal="ngem", view="new", q="fire", limit=25, offset=50, user=_user())
    assert svc.calls == [("list", "ngem", "new", "fire", 25, 50)]
    assert out["items"][0]["match_score"] == 0.9 and out["counts"]["review"] == 1
    out = rp.list_invitations(portal="ngem", view="review", q=None, limit=50, offset=0,
                              user=_user(Role.ESTIMATING_ENGINEER_LABOR))
    assert out["items"][0]["match_score"] is None and out["items"][0]["match_project"] == {"id": P1}
    for role in ACTUAL_BID_VIEWER_ROLES:
        if role in RFP_REVIEW_ROLES:
            assert rp.list_invitations(user=_user(role))["items"][0]["match_score"] == 0.9


def test_detail_never_carries_the_view_url_and_redacts_per_role(db, svc, match_mod):
    out = rp.get_invitation(I1, user=_user(Role.EXECUTIVE))
    assert "view_url" not in out and "SECRET" not in str(out) and "cookie" not in str(out).lower()
    assert out["match_candidates"][0]["breakdown"]["date"] == 1.0 and out["match_score"] == 0.9
    assert match_mod.redact_calls == []
    out = rp.get_invitation(I1, user=_user(Role.ESTIMATING_ENGINEER_MATERIALS))
    assert out["match_candidates"][0]["breakdown"]["date"] is None and out["match_score"] is None
    assert match_mod.redact_calls == [Role.ESTIMATING_ENGINEER_MATERIALS]
    assert "view_url" not in out and "SECRET" not in str(out)
    with pytest.raises(HTTPException) as exc:
        rp.get_invitation(MISSING, user=_user())
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        rp.get_invitation(NOT_A_UUID, user=_user())
    assert exc.value.status_code == 404 and svc.calls[-1] == ("detail", MISSING)


def test_detail_over_the_real_read_path_never_carries_a_portal_link(monkeypatch):
    """The real service read path (not the stub) over a fake row carrying a
    view_url and an Extract.aspx link inside harvest.files: the response
    carries neither, for the executive and for the engineer alike."""
    from app.services import llm_queue
    from app.services import rfp_portal_ingest as real
    from tests.test_rfp_portal_ingest import PortalDB, _harvest_row, _invitation

    dl = "https://supplier.ionwave.net/Extract.aspx?e=SECRET-FILE"
    fake = PortalDB({
        "rfp_portal_invitations": [_invitation(id=I1, status="done", harvest_id="hv-1", view_url=VIEW,
                                               match_candidates=[{"project_id": P1, "name": "P", "number": "1",
                                                                  "breakdown": {"total": 0.9, "date": 1.0,
                                                                                "closest_kind": "actual"}}],
                                               match_project_id=P1, match_score=0.9)],
        "rfp_harvests": [_harvest_row(files=[
            {"index": 1, "file_name": "Plans.pdf", "description": None, "size": 300, "sandbox_file_id": "f-1",
             "status": "accepted", "error": None, "download_url": dl},
        ], claim_token="secret", raw={"x": dl})],
        "llm_jobs": [], "projects": [], "profiles": [],
    })
    monkeypatch.setattr(rp, "_service", lambda: real)
    monkeypatch.setattr(rp, "get_supabase", lambda: fake)
    monkeypatch.setattr(llm_queue, "get_supabase", lambda: fake)
    for role in (Role.EXECUTIVE, Role.ESTIMATING_ENGINEER_LABOR):
        out = rp.get_invitation(I1, user=_user(role))
        text = str(out)
        assert "view_url" not in out and "Extract.aspx" not in text and "SECRET" not in text
        assert "VResponseEvent" not in text and "claim_token" not in out["harvest"] and "raw" not in out["harvest"]
        assert out["harvest"]["files"][0] == {"index": 1, "file_name": "Plans.pdf", "description": None, "size": 300,
                                              "sandbox_file_id": "f-1", "status": "accepted", "error": None}
    assert rp.get_invitation(I1, user=_user(Role.ESTIMATING_ENGINEER_LABOR))["match_score"] is None


def test_status_is_passed_through(db, svc):
    out = rp.ngem_status(_user(Role.IT_ADMIN))
    assert out["account"] == "acct" and out["schedule_times"] == ["06:30", "12:00"]
    assert "cookies" not in out and "password" not in str(out).lower()
    assert svc.calls == [("status", "ngem")]


# ── Actions ──────────────────────────────────────────────────────────────


def _actions():
    return [
        ("resolve", lambda u: rp.resolve_invitation(I1, rp.ResolveIn(project_id=P1), user=u), ("resolve", I1, P1, "u1")),
        ("new", lambda u: rp.new_invitation(I1, user=u), ("new", I1, "u1")),
        ("reopen", lambda u: rp.reopen_invitation(I1, rp.ReasonIn(reason="wrong"), user=u), ("reopen", I1, "wrong", "u1")),
        ("ignore", lambda u: rp.ignore_invitation(I1, None, user=u), ("ignore", I1, None, "u1")),
        ("unignore", lambda u: rp.unignore_invitation(I1, user=u), ("unignore", I1, "u1")),
    ]


@pytest.mark.parametrize("name, call, expected", _actions(), ids=[a[0] for a in _actions()])
def test_each_action_calls_the_service_and_answers_the_redacted_detail(db, svc, match_mod, name, call, expected):
    out = call(_user())
    assert svc.calls[-1] == expected
    assert out["id"] == I1 and "view_url" not in out and "SECRET" not in str(out)
    out = call(_user(Role.ESTIMATING_ENGINEER_LABOR))
    assert out["match_score"] is None and match_mod.redact_calls[-1] == Role.ESTIMATING_ENGINEER_LABOR


@pytest.mark.parametrize("name, call, expected", _actions(), ids=[a[0] for a in _actions()])
def test_each_action_404s_a_missing_or_malformed_id_before_the_service(db, svc, match_mod, name, call, expected):
    db.tables["rfp_portal_invitations"] = []
    with pytest.raises(HTTPException) as exc:
        call(_user())
    assert exc.value.status_code == 404 and svc.calls == []


@pytest.mark.parametrize(
    "code, http_status",
    [
        ("rfp_portal_not_actionable", 409),
        ("rfp_portal_project_required", 400),
        ("rfp_portal_reason_invalid", 400),
        ("rfp_portal_locked", 503),
        ("rfp_portal_not_available", 409),
    ],
)
def test_action_refusals_carry_the_service_code(db, svc, match_mod, code, http_status):
    svc.raise_error = _PortalError(code, "The invitation moved on.")
    with pytest.raises(HTTPException) as exc:
        rp.resolve_invitation(I1, rp.ResolveIn(project_id=P1), user=_user())
    assert exc.value.status_code == http_status
    assert exc.value.detail == "The invitation moved on."
    assert exc.value.headers["X-Error-Code"] == code
    # An error that names its own status keeps it.
    svc.raise_error = _PortalError(code, "Own status.", http_status=503)
    with pytest.raises(HTTPException) as exc:
        rp.ignore_invitation(I1, None, user=_user())
    assert exc.value.status_code == 503 and exc.value.headers["X-Error-Code"] == code


def test_a_short_ignore_reason_is_a_400_not_a_409(db, svc, match_mod):
    """Through the real validator: a 1 or 2 character optional reason is the
    caller's input, so the tab must not reload as if the row had moved."""
    from app.services import rfp_portal_ingest as real

    def ignore(sb, invitation_id, reason, actor_id):
        real._validate_reason(reason, required=False)
        return svc._act("ignore", invitation_id, reason, actor_id)
    svc.ignore = ignore
    with pytest.raises(HTTPException) as exc:
        rp.ignore_invitation(I1, rp.ReasonIn(reason="ab"), user=_user())
    assert exc.value.status_code == 400 and exc.value.headers["X-Error-Code"] == "rfp_portal_reason_invalid"
    assert "too short" in exc.value.detail and svc.calls == []
    assert rp.ignore_invitation(I1, rp.ReasonIn(reason="abc"), user=_user())["id"] == I1
    assert rp.ignore_invitation(I1, None, user=_user())["id"] == I1


@pytest.mark.parametrize("exc", [LookupError("bare"), KeyError("id"), IndexError("list index out of range")])
def test_only_the_service_refusal_is_mapped_everything_else_500s(db, svc, match_mod, exc):
    """A KeyError or IndexError inside the service is a bug: it must not
    turn into a 409 carrying the exception text."""
    svc.raise_error = exc
    for call in (
        lambda: rp.new_invitation(I1, user=_user()),
        lambda: rp.harvest_invitation(I2, None, user=_user()),
        lambda: rp.run_ngem_now(_user()),
    ):
        with pytest.raises(type(exc)):
            call()


def test_harvest_route_answers_202_with_the_job_and_maps_the_codes(db, svc):
    out = rp.harvest_invitation(I2, None, user=_user(uid="u3"))
    assert out == {"job": {"id": "job-1", "status": "queued"}}
    assert svc.calls[-1] == ("harvest", I2, "u3", True)
    rp.harvest_invitation(I2, rp.HarvestIn(force=False), user=_user())
    assert svc.calls[-1] == ("harvest", I2, "u1", False)
    for code, status in (("rfp_portal_harvest_active", 409), ("rfp_portal_locked", 503), ("rfp_portal_not_available", 409)):
        svc.raise_error = _PortalError(code)
        with pytest.raises(HTTPException) as exc:
            rp.harvest_invitation(I2, None, user=_user())
        assert exc.value.status_code == status and exc.value.headers["X-Error-Code"] == code
    with pytest.raises(HTTPException) as exc:
        rp.harvest_invitation(MISSING, None, user=_user())
    assert exc.value.status_code == 404


def test_run_now_answers_202_with_the_run_and_maps_the_codes(db, svc):
    out = rp.run_ngem_now(_user(uid="u9"))
    assert out == {"run": {"id": "run-1", "status": "queued", "trigger": "manual"}}
    assert svc.calls == [("run_now", "u9", "ngem")]
    for code, status in (("rfp_portal_run_active", 409), ("rfp_portal_locked", 503)):
        svc.raise_error = _PortalError(code)
        with pytest.raises(HTTPException) as exc:
            rp.run_ngem_now(_user())
        assert exc.value.status_code == status and exc.value.headers["X-Error-Code"] == code


def test_run_now_answers_503_not_available_while_the_queue_is_off(db, svc, monkeypatch):
    """Through the real service: with LLM_QUEUE_ENABLED off nothing is
    inserted and the route answers 503 rfp_portal_not_available."""
    from app.services import rfp_portal_ingest as real
    from tests.test_rfp_portal_ingest import PortalDB

    fake = PortalDB({"rfp_portal_runs": [], "llm_jobs": [], "rfp_harvest_sessions": []})
    monkeypatch.setattr(rp, "_service", lambda: real)
    monkeypatch.setattr(rp, "get_supabase", lambda: fake)
    monkeypatch.setattr(real, "get_settings", lambda: Settings(
        _env_file=None, rfp_ingest_enabled=True, rfp_ngem_enabled=True, llm_queue_enabled=False,
        ngem_login_username="u", ngem_login_password="p",
        ngem_entry_url="https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=x",
    ))
    with pytest.raises(HTTPException) as exc:
        rp.run_ngem_now(_user())
    assert exc.value.status_code == 503 and exc.value.headers["X-Error-Code"] == "rfp_portal_not_available"
    assert "queue" in exc.value.detail and fake.tables["rfp_portal_runs"] == []


# ── The project side and the feature flag ────────────────────────────────


def test_project_portal_sources_is_flag_gated(monkeypatch):
    calls = []
    monkeypatch.setattr(pr, "get_supabase", lambda: "sb")
    from app.services import rfp_portal_ingest as real

    monkeypatch.setattr(real, "portal_sources_for_projects", lambda sb, ids: calls.append(list(ids)) or {P1: [{"portal": "ngem"}]})
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=True))
    assert pr._portal_sources([P1]) == {} and calls == []
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=False, rfp_ngem_enabled=True))
    assert pr._portal_sources([P1]) == {}
    monkeypatch.setattr(pr, "get_settings", lambda: SimpleNamespace(rfp_ingest_enabled=True, rfp_ngem_enabled=True))
    assert pr._portal_sources([]) == {} and calls == []
    assert pr._portal_sources([P1]) == {P1: [{"portal": "ngem"}]} and calls == [[P1]]


def test_project_out_carries_portal_sources_with_an_empty_default():
    from app.models.schemas import PortalSourceOut, ProjectOut

    assert ProjectOut.model_fields["portal_sources"].default_factory() == []
    src = PortalSourceOut(portal="ngem", invitation_id=I1, agency="UNLV", bid_number="5584-GS",
                          bid_number_raw="5584-GS Addendum 1", addendum_no=1, close_at=None,
                          resolution="system", resolved_at=None)
    assert src.model_dump()["bid_number"] == "5584-GS"


def test_rfp_ngem_flag_rides_along_in_features(monkeypatch):
    """The flag is the two switches, exactly the router's gate: an
    unconfigured account (or the queue off) must NOT hide the settings
    block that says so."""
    on = Settings(_env_file=None, rfp_ingest_enabled=True, rfp_ngem_enabled=True, ngem_login_username="u",
                  ngem_login_password="p", ngem_entry_url="https://supplier.ionwave.net/VendorResponse/ResponseList.aspx?e=x")
    monkeypatch.setattr(features, "get_settings", lambda: on)
    assert features.enabled_map()["rfp_ngem"] is True
    unconfigured = Settings(_env_file=None, rfp_ingest_enabled=True, rfp_ngem_enabled=True)
    monkeypatch.setattr(features, "get_settings", lambda: unconfigured)
    assert features.enabled_map()["rfp_ngem"] is True and unconfigured.rfp_ngem_active is False
    queue_off = Settings(_env_file=None, rfp_ingest_enabled=True, rfp_ngem_enabled=True, llm_queue_enabled=False)
    monkeypatch.setattr(features, "get_settings", lambda: queue_off)
    assert features.enabled_map()["rfp_ngem"] is True
    for flags in ({"rfp_ingest_enabled": True, "rfp_ngem_enabled": False},
                  {"rfp_ingest_enabled": False, "rfp_ngem_enabled": True}):
        off = Settings(_env_file=None, **flags)
        monkeypatch.setattr(features, "get_settings", lambda o=off: o)
        assert features.enabled_map()["rfp_ngem"] is False
        monkeypatch.setattr(rp, "get_settings", lambda o=off: o)
        assert rp.rfp_portal_enabled() is False



# ── Project creation (docs/RFP_CREATE.md 8) ──────────────────────────────


def test_create_route_answers_the_created_project_and_maps_the_refusals(db, svc):
    from app.services import rfp_create

    out = rp.create_invitation_project(I2, user=_user(uid="u3"))
    assert out == {"project_id": P1, "number": "26.9.7204", "linked": True}
    assert svc.calls[-1] == ("create", I2, "u3")
    svc.raise_error = rfp_create.CreateRefused("This invitation has no project name; set one first.")
    with pytest.raises(HTTPException) as exc:
        rp.create_invitation_project(I2, user=_user())
    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "rfp_create_refused"
    assert exc.value.detail == "This invitation has no project name; set one first."
    svc.raise_error = rfp_create.CreateInProgress("busy")
    with pytest.raises(HTTPException) as exc:
        rp.create_invitation_project(I2, user=_user())
    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "rfp_create_in_progress"
    svc.raise_error = _PortalError("rfp_portal_not_actionable")
    with pytest.raises(HTTPException) as exc:
        rp.create_invitation_project(I2, user=_user())
    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "rfp_portal_not_actionable"
    for bad in (MISSING, "not-a-uuid"):
        with pytest.raises(HTTPException) as exc:
            rp.create_invitation_project(bad, user=_user())
        assert exc.value.status_code == 404


# ── BuildingConnected: per-portal gate, lanes, payload ───────────────────


def test_list_404s_a_portal_that_is_not_served(db, svc, monkeypatch):
    monkeypatch.setattr(rp, "get_settings", lambda: _on(rfp_ngem_enabled=True))
    with pytest.raises(HTTPException) as exc:
        rp.list_invitations(portal="buildingconnected", view=None, q=None, limit=50, offset=0, state=None, user=_user())
    assert exc.value.status_code == 404 and exc.value.detail == "Not Found" and svc.calls == []
    rp.list_invitations(portal="ngem", view=None, q=None, limit=50, offset=0, state=None, user=_user())
    monkeypatch.setattr(rp, "get_settings", lambda: _on(rfp_bc_enabled=True))
    with pytest.raises(HTTPException) as exc:
        rp.list_invitations(portal="ngem", view=None, q=None, limit=50, offset=0, state=None, user=_user())
    assert exc.value.status_code == 404
    rp.list_invitations(portal="buildingconnected", view=None, q=None, limit=50, offset=0, state=None, user=_user())
    assert [c[1] for c in svc.calls] == ["ngem", "buildingconnected"]


def test_list_lanes_are_validated_and_defaulted_per_portal(db, svc):
    rp.list_invitations(portal="buildingconnected", view=None, q=None, limit=50, offset=0, state="WILL_SUBMIT",
                        user=_user())
    assert svc.calls[-1] == ("list", "buildingconnected", "needs_action", None, 50, 0)
    assert svc.last_state == "WILL_SUBMIT"
    rp.list_invitations(portal="ngem", view=None, q=None, limit=50, offset=0, state=None, user=_user())
    assert svc.calls[-1] == ("list", "ngem", "review", None, 50, 0)
    for lane in ("open", "historical", "expired_withdrawn", "existing", "ignored", "all"):
        rp.list_invitations(portal="buildingconnected", view=lane, q=None, limit=50, offset=0, state=None,
                            user=_user())
        assert svc.calls[-1][2] == lane
    calls = len(svc.calls)
    for portal, lane in (("ngem", "needs_action"), ("ngem", "historical"), ("buildingconnected", "review"),
                         ("buildingconnected", "new")):
        with pytest.raises(HTTPException) as exc:
            rp.list_invitations(portal=portal, view=lane, q=None, limit=50, offset=0, state=None, user=_user())
        assert exc.value.status_code == 422
    assert len(svc.calls) == calls


def test_detail_and_actions_404_a_row_whose_portal_is_not_served(db, svc, match_mod, monkeypatch):
    monkeypatch.setattr(rp, "get_settings", lambda: _on(rfp_ngem_enabled=True))
    with pytest.raises(HTTPException) as exc:
        rp.get_invitation(I3, user=_user())
    assert exc.value.status_code == 404 and exc.value.detail == "Not Found"
    for call in (
        lambda: rp.restore_invitation(I3, user=_user()),
        lambda: rp.confirm_invitation_gc(I3, rp.GcDecisionIn(decision="same"), user=_user()),
        lambda: rp.apply_invitation_dates(I3, rp.ApplyDatesIn(fields=["actual_bid_at"]), user=_user()),
        lambda: rp.ignore_invitation(I3, None, user=_user()),
        lambda: rp.harvest_invitation(I3, None, user=_user()),
        lambda: rp.create_invitation_project(I3, user=_user()),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404
    assert [c for c in svc.calls if c[0] != "detail"] == []
    # And the other way round: an NGEM row on a BuildingConnected-only deployment.
    monkeypatch.setattr(rp, "get_settings", lambda: _on(rfp_bc_enabled=True))
    with pytest.raises(HTTPException) as exc:
        rp.unignore_invitation(I1, user=_user())
    assert exc.value.status_code == 404
    assert rp.get_invitation(I3, user=_user())["id"] == I3


def test_ngem_routes_404_on_a_bc_only_deployment(db, svc, monkeypatch):
    monkeypatch.setattr(rp, "get_settings", lambda: _on(rfp_bc_enabled=True))
    for call in (lambda: rp.run_ngem_now(_user()), lambda: rp.ngem_status(_user())):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404
    assert svc.calls == []


def test_payload_leaves_the_detail_for_it_admin_only(db, svc, match_mod):
    assert rp.get_invitation(I3, user=_user(Role.IT_ADMIN))["payload"] == {"id": "opp"}
    for role in RFP_REVIEW_ROLES:
        if role != Role.IT_ADMIN:
            out = rp.get_invitation(I3, user=_user(role))
            assert "payload" not in out and "view_url" not in out
            assert out["lead"]["email"] == "pat@acme.test"


def test_harvest_is_refused_on_a_bc_row_before_the_service(db, svc):
    """A BuildingConnected invitation carries no documents: the route
    answers 409 rfp_portal_not_actionable and never reaches harvest_again,
    so no job that could only crash is queued."""
    with pytest.raises(HTTPException) as exc:
        rp.harvest_invitation(I3, None, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers["X-Error-Code"] == "rfp_portal_not_actionable"
    assert exc.value.detail == "BuildingConnected invitations carry no documents to harvest."
    assert not any(c[0] == "harvest" for c in svc.calls)
    # An NGEM row is unaffected.
    assert rp.harvest_invitation(I2, None, user=_user())["job"]["id"] == "job-1"


_BC_DATES = {"close_at": "2026-10-02T21:00:00+00:00", "job_walk_at": "2026-09-28T17:00:00+00:00",
             "expected_start_at": "2026-11-01T07:00:00+00:00", "expected_finish_at": "2027-03-01T07:00:00+00:00"}
_BC_LOG = [{"at": "t1", "field": "close_at", "old": "2026-10-01T21:00:00+00:00", "new": "2026-10-02T21:00:00+00:00", "run_id": "r"},
           {"at": "t2", "field": "job_walk_at", "old": None, "new": "2026-09-28T17:00:00+00:00", "run_id": "r"},
           {"at": "t3", "field": "title", "old": "Old", "new": "New", "run_id": "r"}]


def test_bc_dates_and_change_values_leave_only_for_the_actual_bid_viewers(db, svc, match_mod):
    """Contract D8: on a BuildingConnected row the GC's dates are the
    project's confidential actual bid date (and its companions); a review
    role outside ACTUAL_BID_VIEWER_ROLES gets them null on the detail and
    the list, and the change entries for those fields keep the field and
    the instant but lose their values. NGEM rows keep their public dates."""
    svc.detail_row = _detail_row(id=I1, portal="ngem", close_at="2026-10-02T21:00:00+00:00", change_log=copy.deepcopy(_BC_LOG))
    bc_detail = _detail_row(id=I3, portal="buildingconnected", change_log=copy.deepcopy(_BC_LOG), **_BC_DATES)
    original_detail = svc.detail
    svc.detail = lambda sb, invitation_id: copy.deepcopy(bc_detail) if invitation_id == I3 else original_detail(sb, invitation_id)
    # The engineer: every BC date null, values gone from the date entries only.
    out = rp.get_invitation(I3, user=_user(Role.ESTIMATING_ENGINEER_LABOR))
    assert all(out[f] is None for f in _BC_DATES)
    assert out["change_log"] == [
        {"at": "t1", "field": "close_at", "old": None, "new": None, "run_id": "r"},
        {"at": "t2", "field": "job_walk_at", "old": None, "new": None, "run_id": "r"},
        {"at": "t3", "field": "title", "old": "Old", "new": "New", "run_id": "r"},
    ]
    # The viewers see everything.
    for role in ACTUAL_BID_VIEWER_ROLES:
        if role in RFP_REVIEW_ROLES:
            full = rp.get_invitation(I3, user=_user(role))
            assert {f: full[f] for f in _BC_DATES} == _BC_DATES and full["change_log"][0]["old"] == _BC_LOG[0]["old"]
    # NGEM rows are untouched for the engineer (a public notice's close date).
    ngem = rp.get_invitation(I1, user=_user(Role.ESTIMATING_ENGINEER_LABOR))
    assert ngem["close_at"] == "2026-10-02T21:00:00+00:00" and ngem["change_log"][0]["old"] == _BC_LOG[0]["old"]
    # The list: the same rule per item.
    svc.list_out = {"items": [{"id": I3, "portal": "buildingconnected", "match_score": 0.9, "changes_recent": 2, **_BC_DATES},
                              {"id": I1, "portal": "ngem", "match_score": 0.5, "close_at": "2026-10-02T21:00:00+00:00"}],
                    "total": 2, "offset": 0, "limit": 50, "counts": {}}
    listed = rp.list_invitations(portal="buildingconnected", view="open", q=None, limit=50, offset=0,
                                 user=_user(Role.ESTIMATING_ENGINEER_MATERIALS))["items"]
    assert all(listed[0][f] is None for f in _BC_DATES) and listed[0]["match_score"] is None and listed[0]["changes_recent"] == 2
    assert listed[1]["close_at"] == "2026-10-02T21:00:00+00:00"
    listed = rp.list_invitations(portal="buildingconnected", view="open", q=None, limit=50, offset=0, user=_user())["items"]
    assert {f: listed[0][f] for f in _BC_DATES} == _BC_DATES
    # The list and the detail are review-queue routes (the lead's email and phone never reach a wider role).
    for path in ("/rfp-portal/invitations", "/rfp-portal/invitations/{invitation_id}"):
        route = next(r for r in rp.router.routes if r.path == path and "GET" in r.methods)
        assert rp.require_review_queue in {d.call for d in route.dependant.dependencies}


# ── BuildingConnected invitation actions ─────────────────────────────────


class _BcPortal:
    """Stand-in for app/services/rfp_bc_portal: records the calls."""

    def __init__(self, svc):
        self.svc = svc
        self.raise_error = None
        self.apply_out = {
            "project": {"id": P1, "name": "Tower", "number": "26.9.7204", "actual_bid_at": "2026-10-01T21:00:00+00:00",
                        "internal_bid_at": "2026-09-30T21:00:00+00:00", "gc_confirm_pending": False},
            "applied": ["actual_bid_at"],
        }

    def confirm_gc(self, sb, invitation_id, actor_id, *, gc_id=None, create=None):
        self.svc.calls.append(("confirm_gc", invitation_id, actor_id, gc_id, create))
        if self.raise_error is not None:
            raise self.raise_error
        return self.svc.detail(sb, invitation_id)

    def apply_dates(self, sb, invitation_id, actor_id, fields):
        self.svc.calls.append(("apply_dates", invitation_id, actor_id, list(fields)))
        if self.raise_error is not None:
            raise self.raise_error
        return copy.deepcopy(self.apply_out)


@pytest.fixture
def bc_mod(monkeypatch, svc):
    stub = _BcPortal(svc)
    monkeypatch.setattr(rp, "_bc_portal", lambda: stub)
    return stub


def test_gc_same_confirms_the_rows_provisional_gc(db, svc, match_mod, bc_mod):
    out = rp.confirm_invitation_gc(I3, rp.GcDecisionIn(decision="same"), user=_user(Role.ESTIMATING_ENGINEER_LABOR, uid="u7"))
    assert ("confirm_gc", I3, "u7", GC1, None) in svc.calls
    assert out["id"] == I3 and "payload" not in out


def test_gc_pick_and_create_delegate_with_the_right_arguments(db, svc, match_mod, bc_mod):
    rp.confirm_invitation_gc(I3, rp.GcDecisionIn(decision="pick", gc_id=GC2), user=_user(uid="u2"))
    assert ("confirm_gc", I3, "u2", GC2, None) in svc.calls
    body = rp.GcDecisionIn(decision="create", create=rp.GcCreateIn(
        name="Acme Builders", contact_name="Pat Lee", contact_email="pat@acme.test", contact_phone="702"))
    rp.confirm_invitation_gc(I3, body, user=_user(uid="u2"))
    assert ("confirm_gc", I3, "u2", None, {"name": "Acme Builders", "contact_name": "Pat Lee",
                                           "contact_email": "pat@acme.test", "contact_phone": "702"}) in svc.calls


def test_gc_without_a_usable_gc_is_a_400_before_the_service(db, svc, match_mod, bc_mod):
    db.tables["rfp_portal_invitations"][2]["gc_id"] = None
    for body in (rp.GcDecisionIn(decision="same"), rp.GcDecisionIn(decision="pick"),
                 rp.GcDecisionIn(decision="pick", gc_id="  "), rp.GcDecisionIn(decision="create")):
        with pytest.raises(HTTPException) as exc:
            rp.confirm_invitation_gc(I3, body, user=_user())
        assert exc.value.status_code == 400
        assert exc.value.headers["X-Error-Code"] == "rfp_portal_gc_required"
    assert not [c for c in svc.calls if c[0] == "confirm_gc"]
    with pytest.raises(Exception):
        rp.GcDecisionIn(decision="maybe")


def test_gc_maps_the_service_refusals(db, svc, match_mod, bc_mod):
    from app.services import gc_aliases

    for err, code, http in (
        (_PortalError("rfp_portal_gc_required", "masked"), "rfp_portal_gc_required", 400),
        (_PortalError("rfp_portal_not_actionable", "not bc"), "rfp_portal_not_actionable", 409),
        (ValueError("A GC name is required"), "rfp_portal_gc_required", 400),
        (gc_aliases.GcNotFound("gone"), "rfp_portal_gc_required", 400),
    ):
        bc_mod.raise_error = err
        with pytest.raises(HTTPException) as exc:
            rp.confirm_invitation_gc(I3, rp.GcDecisionIn(decision="pick", gc_id=GC2), user=_user())
        assert exc.value.status_code == http and exc.value.headers["X-Error-Code"] == code
    with pytest.raises(HTTPException) as exc:
        rp.confirm_invitation_gc(MISSING, rp.GcDecisionIn(decision="same"), user=_user())
    assert exc.value.status_code == 404


def test_apply_dates_delegates_and_answers_the_project_lite(db, svc, bc_mod):
    body = rp.ApplyDatesIn(fields=["actual_bid_at", "job_walk_at", "actual_bid_at"])
    out = rp.apply_invitation_dates(I3, body, user=_user(Role.EXECUTIVE, uid="u5"))
    assert svc.calls[-1] == ("apply_dates", I3, "u5", ["actual_bid_at", "job_walk_at"])
    assert out == {"project": {"id": P1, "name": "Tower", "number": "26.9.7204",
                               "actual_bid_at": "2026-10-01T21:00:00+00:00"},
                   "applied": ["actual_bid_at"]}
    assert "internal_bid_at" not in str(out)


def test_apply_dates_body_is_validated(db, svc, bc_mod):
    for bad in ({"fields": []}, {"fields": ["internal_bid_at"]}, {"fields": ["due"]}):
        with pytest.raises(Exception):
            rp.ApplyDatesIn(**bad)


def test_apply_dates_maps_the_refusals(db, svc, bc_mod):
    for err, code, http in (
        (_PortalError("rfp_portal_project_required", "no project"), "rfp_portal_project_required", 400),
        (_PortalError("rfp_portal_dates_unchanged", "nothing"), "rfp_portal_dates_unchanged", 400),
        (_PortalError("rfp_portal_not_actionable", "not bc"), "rfp_portal_not_actionable", 409),
        (ValueError("No date to apply"), "rfp_portal_dates_unchanged", 400),
    ):
        bc_mod.raise_error = err
        with pytest.raises(HTTPException) as exc:
            rp.apply_invitation_dates(I3, rp.ApplyDatesIn(fields=["job_walk_at"]), user=_user())
        assert exc.value.status_code == http and exc.value.headers["X-Error-Code"] == code
    for bad in (MISSING, NOT_A_UUID):
        with pytest.raises(HTTPException) as exc:
            rp.apply_invitation_dates(bad, rp.ApplyDatesIn(fields=["job_walk_at"]), user=_user())
        assert exc.value.status_code == 404


def test_restore_delegates_and_names_its_refusal(db, svc, match_mod):
    out = rp.restore_invitation(I3, user=_user(Role.ESTIMATING_ENGINEER_MATERIALS, uid="u8"))
    assert svc.calls[0] == ("restore", I3, "u8") and "view_url" not in out
    svc.raise_error = _PortalError("rfp_portal_not_actionable", "Not parked.")
    with pytest.raises(HTTPException) as exc:
        rp.restore_invitation(I3, user=_user())
    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "rfp_portal_not_restorable"
    svc.raise_error = _PortalError("rfp_portal_locked", "x")
    with pytest.raises(HTTPException) as exc:
        rp.restore_invitation(I3, user=_user())
    assert exc.value.status_code == 503 and exc.value.headers["X-Error-Code"] == "rfp_portal_locked"


def test_restore_over_the_real_service_refuses_a_row_that_is_not_parked(monkeypatch, match_mod):
    from app.services import rfp_portal_ingest as real
    from tests.test_rfp_portal_ingest import PortalDB, _invitation

    fake = PortalDB({"rfp_portal_invitations": [_invitation(id=I3, status="review_match")],
                     "llm_jobs": [], "projects": [], "profiles": []})
    monkeypatch.setattr(rp, "_service", lambda: real)
    monkeypatch.setattr(rp, "get_supabase", lambda: fake)
    with pytest.raises(HTTPException) as exc:
        rp.restore_invitation(I3, user=_user())
    assert exc.value.status_code == 409 and exc.value.headers["X-Error-Code"] == "rfp_portal_not_restorable"


@pytest.mark.parametrize("role", [r for r in Role if r not in (Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN)])
async def test_apply_dates_refuses_every_role_outside_the_editors(role):
    with pytest.raises(HTTPException) as exc:
        await rp.require_actual_bid_editor(user=_user(role))
    assert exc.value.status_code == 403


async def test_apply_dates_admits_the_editors_and_the_review_actions_admit_the_review_roles():
    for role in (Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN):
        assert (await rp.require_actual_bid_editor(user=_user(role))).role == role
    for role in RFP_REVIEW_ROLES:
        assert (await rp.require_review_queue(user=_user(role))).role == role
    for role in (Role.ACCOUNTANT, Role.ESTIMATOR):
        with pytest.raises(HTTPException) as exc:
            await rp.require_review_queue(user=_user(role))
        assert exc.value.status_code == 403

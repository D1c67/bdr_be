"""The RFP Processing page (docs/RFP_PROCESSING.md): the pure lane and stuck
classifier (services/rfp_processing.classify_row), the router
(app/routers/rfp_processing) called as plain functions against the in-memory
fake Supabase from tests/test_rfp_email_ingest, and the retry service
(rfp_email_ingest.retry_row).

Pinned (contract section 7):

- the route table (three routes, the literal /summary before the
  parameterised retry), the flag gate (404 while the email intake is not
  served, before auth), the role gates (VIEW roles read, the accountant
  included; REVIEW roles retry, the accountant 403);
- `classify_row` for every rule of section 3, the harvest-running exemption
  and both stall thresholds;
- the summary counts and their mailbox scope (a viewer with nothing in
  scope gets zeros and no email query), the portal side only when served
  and only for review roles;
- the list: lane filters, the sort order, paging over the email + portal
  union, the item shape (no match_score, no body fields);
- retry: failed back to its step, waiting made due now, 409 on done /
  created / review rows, a sibling follower refused, a lost CAS race 409,
  the audit row.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.deps import CurrentUser
from app.core.roles import RFP_REVIEW_ROLES, RFP_VIEW_ROLES, Role
from app.routers import rfp_emails as emails_router
from app.routers import rfp_processing as rp
from app.services import rfp_email_ingest as ingest
from app.services import rfp_email_visibility as visibility
from app.services import rfp_processing as svc
from tests.test_rfp_email_ingest import FakeDB
from tests.test_rfp_email_ingest import _settings as ingest_settings

NOW = datetime(2026, 9, 23, 17, 0, 0, tzinfo=timezone.utc)
SHARED = "bids@g3electrical.com"
TOM = "tmoore@g3electrical.com"
TIESHA = "tiesha@g3electrical.com"

E_LLM = "3f1c0b00-0000-4000-8000-000000000001"
E_UNAUTH = "3f1c0b00-0000-4000-8000-000000000002"
E_MATCH = "3f1c0b00-0000-4000-8000-000000000003"
E_FAILED = "3f1c0b00-0000-4000-8000-000000000004"
E_RETRY = "3f1c0b00-0000-4000-8000-000000000005"
E_WAIT = "3f1c0b00-0000-4000-8000-000000000006"
E_STALL = "3f1c0b00-0000-4000-8000-000000000007"
E_RUN = "3f1c0b00-0000-4000-8000-000000000008"
E_HARVEST = "3f1c0b00-0000-4000-8000-000000000009"
E_OLD_FAIL = "3f1c0b00-0000-4000-8000-00000000000a"
E_DONE = "3f1c0b00-0000-4000-8000-00000000000b"
E_FOREIGN = "3f1c0b00-0000-4000-8000-00000000000c"
I_MATCH = "9c000000-0000-4000-8000-000000000001"
I_HARVEST = "9c000000-0000-4000-8000-000000000002"
I_DONE = "9c000000-0000-4000-8000-000000000003"
I_BC = "9c000000-0000-4000-8000-000000000004"
P1 = "5a000000-0000-4000-8000-000000000001"
G1 = "6b000000-0000-4000-8000-000000000001"
HV_RUN = "8d000000-0000-4000-8000-000000000001"
MISSING = "3f1c0b00-0000-4000-8000-0000000000ff"


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


def _ahead(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


def _user(role=Role.ESTIMATING_ADMIN, *, is_dev=False, mailboxes=(), uid="u1"):
    return CurrentUser(
        id=uid, email="e@g3electrical.com", role=role, is_active=True,
        is_dev=is_dev, rfp_mailboxes=tuple(mailboxes),
    )


def _email(eid, status, *, received_min, updated_min=None, mailboxes=(SHARED,), **over):
    row = {
        "id": eid,
        "internet_message_id": f"msg-{eid}",
        "primary_mailbox": mailboxes[0] if mailboxes else None,
        "mailboxes": list(mailboxes),
        "from_address": "pm@examplegc.com",
        "from_name": "Example GC",
        "subject": f"Invitation to bid {eid[-2:]}",
        "body_text": "Please bid this project. SECRET BODY",
        "body_preview": "Please bid",
        "received_at": _ago(received_min),
        "created_at": _ago(received_min),
        "updated_at": _ago(received_min if updated_min is None else updated_min),
        "has_attachments": False,
        "attachments_meta": [],
        "keyword_hits": ["bid"],
        "llm_answer": "undetermined",
        "llm_confidence": 0.5,
        "llm_reasoning": "Unclear.",
        "status": status,
        "flag_reason": None,
        "decided_at_step": None,
        "attempts": 0,
        "next_attempt_at": None,
        "last_error": None,
        "invitation_method": "organic",
        "extracted_project_name": None,
        "extracted_gc_name": None,
        "match_project_id": None,
        "match_score": 0.91,
        "resolved_gc_id": None,
        "harvest_id": None,
        "created_project_id": None,
        "split_job_id": None,
        "excluded_project_ids": [],
        "authorization_kind": "rule",
        "authorization_rule_id": None,
    }
    row.update(over)
    return row


def _invitation(iid, status, *, seen_min, updated_min=None, **over):
    row = {
        "id": iid,
        "portal": "ngem",
        "title": f"Portal bid {iid[-2:]}",
        "status": status,
        "flag_reason": None,
        "decided_at_step": None,
        "attempts": 0,
        "next_attempt_at": None,
        "last_error": None,
        "first_seen_at": _ago(seen_min),
        "created_at": _ago(seen_min),
        "updated_at": _ago(seen_min if updated_min is None else updated_min),
        "match_project_id": None,
        "match_score": 0.8,
        "harvest_id": None,
        "created_project_id": None,
        "view_url": "https://portal.example/secret-token",
    }
    row.update(over)
    return row


class TrackingDB(FakeDB):
    """The shared fake, recording every table a read or write touched."""

    def __init__(self, tables=None):
        super().__init__(tables)
        self.touched: list[str] = []

    def table(self, name):
        self.touched.append(name)
        return super().table(name)


def _router_settings(**over):
    base = dict(
        rfp_email_ingestion_enabled=True,
        rfp_ingest_enabled=True,
        rfp_ngem_enabled=True,
        default_rate_limit_per_min=600,
        rfp_email_ingestion_classify_max_attempts=8,
        rfp_processing_stall_minutes=120,
        rfp_processing_slow_stall_minutes=360,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def db(monkeypatch):
    fake = TrackingDB({
        "rfp_emails": [],
        "rfp_email_sightings": [],
        "rfp_portal_invitations": [],
        "projects": [{"id": P1, "name": "Sunrise Elementary", "number": "26.9.7301"}],
        "general_contractors": [{"id": G1, "name": "Example Builders"}],
        "rfp_harvests": [{"id": HV_RUN, "status": "running"}],
        "audit_log": [],
    })
    monkeypatch.setattr(rp, "get_supabase", lambda: fake)
    monkeypatch.setattr(rp, "get_settings", lambda: _router_settings())
    monkeypatch.setattr(emails_router, "get_settings", lambda: _router_settings())
    monkeypatch.setattr(rp, "_now", lambda: NOW)
    monkeypatch.setattr(visibility, "shared_mailboxes", lambda: {SHARED})
    return fake


def _seed(db):
    """One row per lane and stuck kind, plus rows that must never load."""
    db.tables["rfp_emails"] = [
        _email(E_LLM, "review_llm", received_min=300, llm_answer="undetermined"),
        _email(E_UNAUTH, "flagged_unauthorized", received_min=400),
        _email(E_MATCH, "review_match", received_min=100, match_project_id=P1,
               extracted_project_name="Sunrise Elem", extracted_gc_name="Typed GC",
               resolved_gc_id=G1),
        _email(E_FAILED, "failed", received_min=5000, updated_min=60,
               decided_at_step="classify", attempts=8, last_error="model returned junk",
               flag_reason="classify_server_error"),
        _email(E_RETRY, "extract", received_min=90, attempts=2,
               next_attempt_at=_ahead(10), last_error="503 from the box"),
        _email(E_WAIT, "classify", received_min=80, next_attempt_at=_ahead(5),
               last_error="The model is away."),
        _email(E_STALL, "method", received_min=500, updated_min=121),
        _email(E_RUN, "keywords", received_min=10),
        _email(E_HARVEST, "harvest", received_min=700, updated_min=400, harvest_id=HV_RUN),
        # Never loaded: a failed row past the 14-day window, a finished row,
        # and a row in a mailbox nobody here owns.
        _email(E_OLD_FAIL, "failed", received_min=40000, updated_min=15 * 24 * 60,
               decided_at_step="classify"),
        _email(E_DONE, "done", received_min=20),
        _email(E_FOREIGN, "review_llm", received_min=1000, mailboxes=(TIESHA,)),
    ]
    db.tables["rfp_email_sightings"] = [
        {"id": f"s-{r['id']}", "rfp_email_id": r["id"], "mailbox": r["mailboxes"][0]}
        for r in db.tables["rfp_emails"]
    ]
    db.tables["rfp_portal_invitations"] = [
        _invitation(I_MATCH, "review_match", seen_min=350, match_project_id=P1),
        _invitation(I_HARVEST, "harvest", seen_min=30),
        _invitation(I_DONE, "done", seen_min=10),
    ]


# ── Route table and gates ────────────────────────────────────────────────


def test_route_table_is_exactly_the_contract():
    pairs = {(m, r.path) for r in rp.router.routes for m in r.methods}
    assert pairs == {
        ("GET", "/rfp-processing/summary"),
        ("GET", "/rfp-processing"),
        ("POST", "/rfp-processing/emails/{email_id}/retry"),
    }
    paths = [r.path for r in rp.router.routes]
    assert paths.index("/rfp-processing/summary") < paths.index(
        "/rfp-processing/emails/{email_id}/retry"
    )
    roles = {}
    for route in rp.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert emails_router.require_rfp_emails in calls, route.path
        assert rp.rfp_processing_rate_limit in calls, route.path
        roles[route.path] = calls & {emails_router.require_view_queue,
                                     emails_router.require_review_queue}
    assert roles["/rfp-processing/summary"] == {emails_router.require_view_queue}
    assert roles["/rfp-processing"] == {emails_router.require_view_queue}
    assert roles["/rfp-processing/emails/{email_id}/retry"] == {
        emails_router.require_review_queue}


def test_the_router_is_mounted_in_the_app():
    from app.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/rfp-processing", "/rfp-processing/summary",
            "/rfp-processing/emails/{email_id}/retry"} <= paths


def test_flag_gate_is_fail_closed(monkeypatch):
    monkeypatch.setattr(emails_router, "get_settings", lambda: SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        emails_router.require_rfp_emails()
    assert exc.value.status_code == 404 and exc.value.detail == "Not Found"
    monkeypatch.setattr(emails_router, "get_settings",
                        lambda: SimpleNamespace(rfp_email_ingestion_enabled=False))
    with pytest.raises(HTTPException):
        emails_router.require_rfp_emails()
    monkeypatch.setattr(emails_router, "get_settings",
                        lambda: SimpleNamespace(rfp_email_ingestion_enabled=True))
    assert emails_router.require_rfp_emails() is None


@pytest.mark.parametrize("role", list(RFP_VIEW_ROLES))
def test_view_roles_read_the_accountant_included(role):
    user = _user(role)
    assert asyncio.run(emails_router.require_view_queue(user=user)) is user
    assert Role.ACCOUNTANT in RFP_VIEW_ROLES


@pytest.mark.parametrize("role", [Role.ESTIMATOR])
def test_everyone_else_is_refused_the_reads(role):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(emails_router.require_view_queue(user=_user(role)))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", [Role.ACCOUNTANT, Role.ESTIMATOR])
def test_retry_refuses_the_accountant_and_the_estimator(role):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(emails_router.require_review_queue(user=_user(role)))
    assert exc.value.status_code == 403


def test_the_two_new_settings_default_per_section_6():
    assert Settings.model_fields["rfp_processing_stall_minutes"].default == 30
    assert Settings.model_fields["rfp_processing_slow_stall_minutes"].default == 360


# ── classify_row (section 3) ─────────────────────────────────────────────


def _classify(row, *, source="email", harvest_status=None, stall=120, slow=360):
    return svc.classify_row(
        row, now=NOW, source=source, max_attempts=8, stall_minutes=stall,
        slow_stall_minutes=slow, harvest_status=harvest_status,
    )


@pytest.mark.parametrize("status", ["review_llm", "flagged_unauthorized", "review_match"])
def test_rule_1_a_human_lane_is_its_own_lane_and_never_stuck(status):
    # Even with every stuck signal set: a person is the step.
    row = _email(E_LLM, status, received_min=10000, attempts=5, last_error="x",
                 next_attempt_at=_ago(1))
    assert _classify(row) == (status, None)


def test_rule_1_a_portal_review_match_is_the_match_lane():
    assert _classify(_invitation(I_MATCH, "review_match", seen_min=9999), source="portal") == (
        "review_match", None)


def test_rule_2_failed_is_stuck_at_the_step_that_failed():
    row = _email(E_FAILED, "failed", received_min=100, updated_min=30, decided_at_step="extract",
                 attempts=8, last_error="bad output")
    lane, stuck = _classify(row)
    assert lane == "stuck"
    assert stuck == {"kind": "failed", "step": "extract", "attempts": 8, "max_attempts": 8,
                     "next_attempt_at": None, "last_error": "bad output", "since": _ago(30)}


def test_rule_3_attempts_spent_is_retrying():
    row = _email(E_RETRY, "match", received_min=5, attempts=1, next_attempt_at=_ahead(5),
                 last_error="503")
    lane, stuck = _classify(row)
    assert lane == "stuck" and stuck["kind"] == "retrying"
    assert stuck["step"] == "match" and stuck["attempts"] == 1 and stuck["max_attempts"] == 8
    assert stuck["next_attempt_at"] == _ahead(5)


def test_rule_4_nothing_spent_with_an_error_and_a_next_attempt_is_model_wait():
    row = _email(E_WAIT, "classify", received_min=5, next_attempt_at=_ahead(5),
                 last_error="The model is away.")
    lane, stuck = _classify(row)
    assert lane == "stuck" and stuck["kind"] == "model_wait" and stuck["attempts"] == 0
    # An error alone, or a next attempt alone, is not a model wait.
    assert _classify(_email(E_WAIT, "classify", received_min=5, last_error="old"))[0] == "processing"
    assert _classify(_email(E_WAIT, "split", received_min=5, next_attempt_at=_ahead(1)))[0] == (
        "processing")


def test_rule_4_a_copy_waiting_on_an_older_copy_is_processing_not_stuck():
    # Both wordings the pipeline has written (rfp_email_ingest sibling rule).
    for sentence in (
        "Waiting for another copy of this message (2b8e2826) to finish.",
        "Waiting for an earlier copy of this message (2b8e2826) to finish.",
    ):
        row = _email(E_WAIT, "extract", received_min=5, next_attempt_at=_ahead(5),
                     last_error=sentence)
        assert _classify(row) == ("processing", None)
    # A spent attempt still counts as retrying whatever the sentence says.
    row = _email(E_WAIT, "extract", received_min=5, attempts=1, next_attempt_at=_ahead(5),
                 last_error="Waiting for another copy of this message (x) to finish.")
    assert _classify(row)[1]["kind"] == "retrying"


def test_rule_5_the_quick_threshold():
    fresh = _email(E_STALL, "method", received_min=500, updated_min=119)
    old = _email(E_STALL, "method", received_min=500, updated_min=121)
    assert _classify(fresh) == ("processing", None)
    lane, stuck = _classify(old)
    assert lane == "stuck" and stuck == {
        "kind": "stalled", "step": "method", "attempts": 0, "max_attempts": 8,
        "next_attempt_at": None, "last_error": None, "since": _ago(121)}
    # A past next_attempt_at still stalls; a future one does not.
    assert _classify({**old, "next_attempt_at": _ago(5)})[1]["kind"] == "stalled"
    assert _classify({**old, "next_attempt_at": _ahead(5)}) == ("processing", None)
    # The threshold is the parameter, not a constant.
    assert _classify(fresh, stall=60)[1]["kind"] == "stalled"


@pytest.mark.parametrize("status", ["harvest", "split"])
def test_rule_5_the_slow_threshold_covers_harvest_and_split(status):
    at_200 = _email(E_HARVEST, status, received_min=900, updated_min=200)
    assert _classify(at_200) == ("processing", None)          # quick steps would stall at 200
    assert _classify({**at_200, "status": "extract"})[1]["kind"] == "stalled"
    assert _classify({**at_200, "updated_at": _ago(359)}) == ("processing", None)
    assert _classify({**at_200, "updated_at": _ago(361)})[1]["kind"] == "stalled"


@pytest.mark.parametrize("job", ["running", "pending"])
def test_rule_5_a_harvest_whose_job_is_active_is_never_stalled(job):
    row = _email(E_HARVEST, "harvest", received_min=9000, updated_min=5000, harvest_id=HV_RUN)
    assert _classify(row, harvest_status=job) == ("processing", None)
    assert _classify(row, harvest_status="complete")[1]["kind"] == "stalled"
    assert _classify(row, harvest_status=None)[1]["kind"] == "stalled"
    # The exemption is harvest only: a split row with a running harvest still stalls.
    assert _classify({**row, "status": "split"}, harvest_status=job)[1]["kind"] == "stalled"


def test_rule_6_otherwise_processing_and_unknown_statuses_are_processing():
    assert _classify(_email(E_RUN, "keywords", received_min=5)) == ("processing", None)
    assert _classify(_invitation(I_HARVEST, "harvest", seen_min=5), source="portal") == (
        "processing", None)
    assert _classify(_email(E_RUN, "mystery", received_min=99999)) == ("processing", None)
    # The portal has no `failed`: a portal row is never classified by rule 2.
    assert _classify(_invitation(I_MATCH, "failed", seen_min=5), source="portal") == (
        "processing", None)


def test_portal_rows_follow_the_same_pending_rules():
    retry = _invitation(I_HARVEST, "match", seen_min=10, attempts=2, next_attempt_at=_ahead(3))
    assert _classify(retry, source="portal")[1]["kind"] == "retrying"
    stalled = _invitation(I_HARVEST, "create", seen_min=500, updated_min=200)
    assert _classify(stalled, source="portal")[1]["kind"] == "stalled"


def test_the_in_flight_filter_has_no_plus_sign_and_names_the_window():
    flt = svc.email_in_flight_filter(NOW)
    assert "+" not in flt
    assert flt.startswith("status.in.(received,auth,keywords,classify,authorize,method,extract,")
    assert "review_llm,flagged_unauthorized,review_match)" in flt
    assert "and(status.eq.failed,updated_at.gte.2026-09-09T17:00:00Z)" in flt


# ── Summary (section 2.1) ────────────────────────────────────────────────


def test_summary_counts_every_lane_step_and_stuck_kind(db):
    _seed(db)
    out = rp.rfp_processing_summary(user=_user())
    assert out["lanes"] == {
        "review_llm": 1, "flagged_unauthorized": 1, "review_match": 2,   # one email, one portal
        "processing": 3,    # keywords, the running harvest, the portal harvest
        "stuck": 4,         # failed, retrying, model wait, stalled
        "review_total": 4, "total": 11,
    }
    assert out["steps"] == {
        "received": 0, "auth": 0, "keywords": 1, "classify": 0, "authorize": 0, "method": 0,
        "extract": 0, "match": 0, "harvest": 2, "split": 0, "create": 0,
    }
    assert list(out["steps"]) == ["received", "auth", "keywords", "classify", "authorize",
                                  "method", "extract", "match", "harvest", "split", "create"]
    assert out["stuck_kinds"] == {"failed": 1, "retrying": 1, "model_wait": 1, "stalled": 1}
    assert out["portal_served"] is True
    assert out["generated_at"] == NOW.isoformat()


def test_summary_is_scoped_to_the_viewers_mailboxes(db):
    _seed(db)
    owner = _user(Role.EXECUTIVE, mailboxes=(TIESHA,))
    assert rp.rfp_processing_summary(user=owner)["lanes"]["review_llm"] == 2
    dev = _user(Role.IT_ADMIN, is_dev=True)
    assert rp.rfp_processing_summary(user=dev)["lanes"]["total"] == 12
    # A dev wearing another role is scoped like that role.
    dev_exec = _user(Role.EXECUTIVE, is_dev=True)
    assert rp.rfp_processing_summary(user=dev_exec)["lanes"]["total"] == 11


def test_an_empty_scope_gets_zeros_for_email_without_an_email_query(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(visibility, "shared_mailboxes", lambda: set())
    db.touched.clear()
    out = rp.rfp_processing_summary(user=_user(Role.EXECUTIVE))
    assert "rfp_emails" not in db.touched
    # Portal invitations are not mailbox rows: they still count for a review role.
    assert out["lanes"]["total"] == 2 and out["lanes"]["review_match"] == 1
    db.touched.clear()
    listed = rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user(Role.EXECUTIVE))
    assert "rfp_emails" not in db.touched
    assert {i["source"] for i in listed["items"]} == {"portal"}


def test_the_accountant_reads_emails_only_and_no_portal_side(db):
    _seed(db)
    out = rp.rfp_processing_summary(user=_user(Role.ACCOUNTANT))
    assert out["portal_served"] is False
    assert out["lanes"]["total"] == 9 and out["lanes"]["review_match"] == 1
    listed = rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user(Role.ACCOUNTANT))
    assert {i["source"] for i in listed["items"]} == {"email"}


def test_the_portal_side_follows_the_ngem_switches(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(rp, "get_settings", lambda: _router_settings(rfp_ngem_enabled=False))
    db.touched.clear()
    out = rp.rfp_processing_summary(user=_user())
    assert out["portal_served"] is False and out["lanes"]["total"] == 9
    assert "rfp_portal_invitations" not in db.touched
    monkeypatch.setattr(rp, "get_settings", lambda: SimpleNamespace(
        default_rate_limit_per_min=600))
    assert rp.portal_served() is False     # absent flags fail closed
    assert rp.served_portals() == frozenset()


def test_the_portal_side_is_any_portal_and_loads_only_the_served_portals_rows(db, monkeypatch):
    """Contract D4: the page gate is 'any portal served'; a BuildingConnected
    only deployment still lists its rows, and the rows of a portal switched
    off after they exist are dropped (their dialog would 404 them)."""
    _seed(db)
    db.tables["rfp_portal_invitations"].append(
        _invitation(I_BC, "review_match", seen_min=200, portal="buildingconnected", match_project_id=None)
    )
    # Both switches on: both portals' rows count.
    monkeypatch.setattr(rp, "get_settings", lambda: _router_settings(rfp_bc_enabled=True))
    assert rp.served_portals() == {"ngem", "buildingconnected"}
    out = rp.rfp_processing_summary(user=_user())
    assert out["portal_served"] is True and out["lanes"]["total"] == 12 and out["lanes"]["review_match"] == 3
    listed = rp.list_rfp_processing(lane="review_match", limit=50, offset=0, user=_user())
    assert {i["portal"] for i in listed["items"] if i["source"] == "portal"} == {"ngem", "buildingconnected"}
    # BuildingConnected only: the NGEM rows are gone, the BC row stays.
    monkeypatch.setattr(rp, "get_settings", lambda: _router_settings(rfp_ngem_enabled=False, rfp_bc_enabled=True))
    assert rp.served_portals() == {"buildingconnected"}
    out = rp.rfp_processing_summary(user=_user())
    assert out["portal_served"] is True and out["lanes"]["total"] == 10 and out["lanes"]["review_match"] == 2
    listed = rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user())
    assert [i["id"] for i in listed["items"] if i["source"] == "portal"] == [I_BC]
    # NGEM only after BC rows exist: the BC row is dropped.
    monkeypatch.setattr(rp, "get_settings", lambda: _router_settings(rfp_bc_enabled=False))
    assert rp.served_portals() == {"ngem"}
    out = rp.rfp_processing_summary(user=_user())
    assert out["portal_served"] is True and out["lanes"]["total"] == 11
    listed = rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user())
    assert I_BC not in {i["id"] for i in listed["items"]}
    # The master switch off: nothing portal-side, whatever the portal switches say.
    monkeypatch.setattr(rp, "get_settings", lambda: _router_settings(rfp_ingest_enabled=False, rfp_bc_enabled=True))
    assert rp.portal_served() is False and rp.served_portals() == frozenset()
    # A settings object carrying the computed property is honoured as the page gate.
    monkeypatch.setattr(rp, "get_settings", lambda: _router_settings(rfp_ngem_enabled=False, rfp_portal_any_enabled=True))
    assert rp.portal_served() is True
    # The service read with an empty served set touches no table.
    assert svc.load_portal_rows(db, portals=set()) == []


def test_the_summary_reads_only_the_classifier_columns(db):
    _seed(db)
    selects = []
    original = db.table

    def spy(name):
        query = original(name)
        inner = query.select

        def select(*a, **k):
            selects.append((name, a[0] if a else None))
            return inner(*a, **k)

        query.select = select
        return query

    db.table = spy
    rp.rfp_processing_summary(user=_user())
    email_selects = [s for n, s in selects if n == "rfp_emails"]
    assert email_selects == [svc.EMAIL_CLASSIFY_SELECT]
    assert all("*" not in (s or "") for _, s in selects)
    assert all("body" not in (s or "") for _, s in selects)


# ── The list (section 2.2) ───────────────────────────────────────────────


def _ids(out):
    return [i["id"] for i in out["items"]]


def test_the_list_sorts_review_then_stuck_then_processing_oldest_first(db):
    _seed(db)
    out = rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user())
    assert _ids(out) == [
        # review, oldest wait first across both sources
        E_UNAUTH,   # 400 min
        I_MATCH,    # 350 min (portal, first_seen_at)
        E_LLM,      # 300 min
        E_MATCH,    # 100 min
        # stuck
        E_FAILED,   # 5000 min
        E_STALL,    # 500 min
        E_RETRY,    # 90 min
        E_WAIT,     # 80 min
        # processing
        E_HARVEST,  # 700 min, harvest job running
        I_HARVEST,  # 30 min
        E_RUN,      # 10 min
    ]
    assert out["total"] == 11 and out["lane"] == "all"
    assert out["offset"] == 0 and out["limit"] == 50
    for item in out["items"]:
        assert item["awaiting_review"] is (item["lane"] in svc.REVIEW_LANES)


@pytest.mark.parametrize(
    "lane,expected",
    [
        ("review", [E_UNAUTH, I_MATCH, E_LLM, E_MATCH]),
        ("review_llm", [E_LLM]),
        ("flagged_unauthorized", [E_UNAUTH]),
        ("review_match", [I_MATCH, E_MATCH]),
        ("stuck", [E_FAILED, E_STALL, E_RETRY, E_WAIT]),
        ("processing", [E_HARVEST, I_HARVEST, E_RUN]),
    ],
)
def test_each_lane_filter(db, lane, expected):
    _seed(db)
    out = rp.list_rfp_processing(lane=lane, limit=50, offset=0, user=_user())
    assert _ids(out) == expected and out["total"] == len(expected) and out["lane"] == lane


def test_paging_slices_the_union_after_the_sort(db):
    _seed(db)
    full = _ids(rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user()))
    seen = []
    for offset in range(0, 12, 4):
        page = rp.list_rfp_processing(lane="all", limit=4, offset=offset, user=_user())
        assert page["total"] == 11
        seen += _ids(page)
    assert seen == full
    assert rp.list_rfp_processing(lane="all", limit=4, offset=40, user=_user())["items"] == []


ITEM_KEYS = {
    "source", "id", "status", "lane", "awaiting_review", "stuck", "name", "subject",
    "from_address", "from_name", "gc_name", "invitation_method", "received_at", "updated_at",
    "attempts", "next_attempt_at", "last_error", "harvest_status", "match_project",
    "created_project_id", "primary_mailbox", "portal",
    # additive (build record): the model's verdict for the Detail column
    "llm_answer", "llm_confidence", "llm_reasoning",
    # additive: the copy in a review lane a sibling-wait row is parked behind
    "waiting_on",
}


def test_the_item_shape_for_both_sources(db):
    _seed(db)
    out = rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user())
    by_id = {i["id"]: i for i in out["items"]}
    for item in out["items"]:
        assert set(item) == ITEM_KEYS, item["id"]
    text = repr(out)
    for banned in ("match_score", "body_text", "body_preview", "SECRET BODY", "view_url",
                   "secret-token", "attachments_meta", "auth_raw"):
        assert banned not in text, banned

    match = by_id[E_MATCH]
    assert match["source"] == "email" and match["lane"] == "review_match"
    assert match["name"] == "Sunrise Elem"
    assert match["gc_name"] == "Example Builders"       # the resolved GC wins over the typed one
    assert match["match_project"] == {"id": P1, "name": "Sunrise Elementary", "number": "26.9.7301"}
    assert match["primary_mailbox"] == SHARED and match["portal"] is None
    assert match["stuck"] is None and match["awaiting_review"] is True
    assert match["llm_answer"] == "undetermined"

    assert by_id[E_HARVEST]["harvest_status"] == "running"
    assert by_id[E_RUN]["harvest_status"] is None and by_id[E_RUN]["gc_name"] is None
    assert by_id[E_RETRY]["stuck"]["kind"] == "retrying" and by_id[E_RETRY]["attempts"] == 2

    portal = by_id[I_MATCH]
    assert portal == {
        "source": "portal", "id": I_MATCH, "status": "review_match", "lane": "review_match",
        "awaiting_review": True, "stuck": None, "name": "Portal bid 01", "subject": None,
        "from_address": None, "from_name": None, "gc_name": None, "invitation_method": "ngem",
        "received_at": _ago(350), "updated_at": _ago(350), "attempts": 0,
        "next_attempt_at": None, "last_error": None, "harvest_status": None,
        "match_project": {"id": P1, "name": "Sunrise Elementary", "number": "26.9.7301"},
        "created_project_id": None, "primary_mailbox": None, "portal": "ngem",
        "llm_answer": None, "llm_confidence": None, "llm_reasoning": None,
        "waiting_on": None,
    }
    # A portal row at harvest with no record yet reads `pending`, like the NGEM tab.
    assert by_id[I_HARVEST]["harvest_status"] == "pending"


def test_the_gc_name_falls_back_to_the_extracted_one(db):
    db.tables["rfp_emails"] = [_email(E_MATCH, "review_match", received_min=5,
                                      extracted_gc_name="Typed GC")]
    out = rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user())
    assert out["items"][0]["gc_name"] == "Typed GC"


# ── Copies waiting on a review (rule 4, `review_waits`) ──────────────────

E_COPY_A = "3f1c0b00-0000-4000-8000-00000000000d"
E_COPY_B = "3f1c0b00-0000-4000-8000-00000000000e"
E_COPY_RUN = "3f1c0b00-0000-4000-8000-00000000000f"
E_COPY_FOREIGN = "3f1c0b00-0000-4000-8000-000000000010"


def _copy_of(eid, leader_id, *, received_min, wording="another"):
    return _email(
        eid, "extract", received_min=received_min, next_attempt_at=_ahead(5),
        last_error=f"Waiting for {wording} copy of this message ({leader_id}) to finish.",
    )


def _seed_copies(db):
    """The leader E_MATCH in review_match with two copies behind it (one in
    the old wording, one naming the id in upper case), a copy behind a leader
    that is still processing, and a copy behind a leader in a mailbox only
    TIESHA's scope reaches."""
    _seed(db)
    db.tables["rfp_emails"] += [
        _copy_of(E_COPY_A, E_MATCH, received_min=50),
        _copy_of(E_COPY_B, E_MATCH.upper(), received_min=40, wording="an earlier"),
        _copy_of(E_COPY_RUN, E_RUN, received_min=8),
        _copy_of(E_COPY_FOREIGN, E_FOREIGN, received_min=30),
    ]
    db.tables["rfp_email_sightings"] = [
        {"id": f"s-{r['id']}", "rfp_email_id": r["id"], "mailbox": r["mailboxes"][0]}
        for r in db.tables["rfp_emails"]
    ]


def test_the_sibling_wait_sentence_yields_its_leader_id():
    assert svc.sibling_wait_leader_id(
        f"Waiting for another copy of this message ({E_MATCH}) to finish.") == E_MATCH
    assert svc.sibling_wait_leader_id(
        f"Waiting for an earlier copy of this message ({E_MATCH.upper()}) to finish.") == E_MATCH
    # Not the wait sentence, or the sentence with no id: nothing to follow.
    assert svc.sibling_wait_leader_id(f"The model is away ({E_MATCH}).") is None
    assert svc.sibling_wait_leader_id("Waiting for another copy of this message to finish.") is None
    assert svc.sibling_wait_leader_id(None) is None


def test_copies_behind_a_review_row_are_counted_and_point_at_it(db):
    _seed_copies(db)
    summary = rp.rfp_processing_summary(user=_user())
    # They stay in the processing lane (nothing is stuck); the new count is
    # the part of that lane a person's review is holding.
    assert summary["lanes"]["processing"] == 3 + 4
    assert summary["waiting_on_review"] == 2

    out = rp.list_rfp_processing(lane="processing", limit=50, offset=0, user=_user())
    by_id = {i["id"]: i for i in out["items"]}
    assert by_id[E_COPY_A]["waiting_on"] == {"id": E_MATCH, "lane": "review_match"}
    assert by_id[E_COPY_B]["waiting_on"] == {"id": E_MATCH, "lane": "review_match"}
    assert by_id[E_COPY_A]["lane"] == "processing" and by_id[E_COPY_A]["stuck"] is None
    # A leader still being processed is not a review wait.
    assert by_id[E_COPY_RUN]["waiting_on"] is None
    # A leader outside this viewer's mailboxes is never described.
    assert by_id[E_COPY_FOREIGN]["waiting_on"] is None
    assert by_id[E_RUN]["waiting_on"] is None


def test_a_viewer_who_sees_the_leader_gets_the_pointer(db):
    _seed_copies(db)
    dev = _user(Role.IT_ADMIN, is_dev=True)
    assert rp.rfp_processing_summary(user=dev)["waiting_on_review"] == 3
    out = rp.list_rfp_processing(lane="processing", limit=50, offset=0, user=dev)
    by_id = {i["id"]: i for i in out["items"]}
    assert by_id[E_COPY_FOREIGN]["waiting_on"] == {"id": E_FOREIGN, "lane": "review_llm"}


def test_a_copy_whose_leader_has_decided_is_not_a_review_wait(db):
    _seed_copies(db)
    for row in db.tables["rfp_emails"]:
        if row["id"] == E_MATCH:
            row["status"] = "merged"      # decided: no longer loaded as in flight
    assert rp.rfp_processing_summary(user=_user())["waiting_on_review"] == 0
    out = rp.list_rfp_processing(lane="processing", limit=50, offset=0, user=_user())
    assert all(i["waiting_on"] is None for i in out["items"])


def test_the_list_never_selects_star_and_reads_references_once(db):
    _seed(db)
    counts: dict[str, int] = {}
    original = db.table

    def spy(name):
        counts[name] = counts.get(name, 0) + 1
        return original(name)

    db.table = spy
    rp.list_rfp_processing(lane="all", limit=50, offset=0, user=_user())
    assert counts == {"rfp_emails": 1, "rfp_portal_invitations": 1, "rfp_harvests": 1,
                      "projects": 1, "general_contractors": 1}


def test_the_email_read_pages_at_1000_and_caps_at_2000(db, monkeypatch):
    monkeypatch.setattr(svc, "READ_PAGE", 2)
    monkeypatch.setattr(svc, "READ_CAP", 3)
    db.tables["rfp_emails"] = [
        _email(f"3f1c0b00-0000-4000-8000-0000000001{n:02d}", "keywords", received_min=100 - n)
        for n in range(6)
    ]
    rows = svc.load_email_rows(db, _user(), now=NOW, select=svc.EMAIL_CLASSIFY_SELECT)
    assert len(rows) == 3
    # Oldest received first, so the rows that waited longest are the ones kept.
    assert [r["id"][-2:] for r in rows] == ["00", "01", "02"]
    assert (svc.READ_PAGE, svc.READ_CAP) == (2, 3)


def test_the_real_page_and_cap_values():
    assert (svc.READ_PAGE, svc.READ_CAP, svc.FAILED_WINDOW_DAYS) == (1000, 2000, 14)


# ── Retry: the route (section 2.3) ───────────────────────────────────────


@pytest.fixture
def retry_stub(monkeypatch):
    calls = []

    class _Stub:
        def retry_row(self, sb, email_id, actor_id):
            calls.append((email_id, actor_id))
            if getattr(self, "refuse", None):
                raise LookupError(self.refuse)
            return {"id": email_id}

    stub = _Stub()
    monkeypatch.setattr(rp, "_ingest", lambda: stub)
    details = []
    monkeypatch.setattr(rp, "_detail", lambda sb, eid, role, user=None: details.append(
        (eid, role)) or {"id": eid, "detail": True})
    stub.calls, stub.details = calls, details
    return stub


def test_retry_route_answers_the_detail(db, retry_stub):
    _seed(db)
    out = rp.retry_rfp_processing_email(E_FAILED, user=_user())
    assert out == {"id": E_FAILED, "detail": True}
    assert retry_stub.calls == [(E_FAILED, "u1")]
    assert retry_stub.details == [(E_FAILED, Role.ESTIMATING_ADMIN)]


def test_retry_route_reuses_the_queue_detail():
    assert rp._detail is emails_router._detail
    assert rp._email_row_or_404 is emails_router._email_row_or_404


@pytest.mark.parametrize("email_id", ["not-a-uuid", MISSING, E_FOREIGN])
def test_retry_route_404s_unknown_and_invisible_rows_before_the_service(db, retry_stub, email_id):
    _seed(db)
    with pytest.raises(HTTPException) as exc:
        rp.retry_rfp_processing_email(email_id, user=_user())
    assert exc.value.status_code == 404 and exc.value.detail == "Email not found"
    assert retry_stub.calls == []


def test_retry_route_maps_a_refusal_to_409_with_the_code(db, retry_stub):
    _seed(db)
    retry_stub.refuse = "This email is not stuck, so there is nothing to retry."
    with pytest.raises(HTTPException) as exc:
        rp.retry_rfp_processing_email(E_DONE, user=_user())
    assert exc.value.status_code == 409
    assert exc.value.headers == {"X-Error-Code": "rfp_processing_not_retryable"}
    assert exc.value.detail == retry_stub.refuse
    assert retry_stub.details == []


# ── Retry: the service ───────────────────────────────────────────────────


@pytest.fixture
def svc_db(monkeypatch):
    fake = FakeDB({"rfp_emails": [], "audit_log": []})
    monkeypatch.setattr(ingest, "get_settings", lambda: ingest_settings())
    monkeypatch.setattr(
        ingest, "audit",
        lambda actor, action, entity=None, entity_id=None, payload=None: fake.tables[
            "audit_log"].append({"actor_id": actor, "action": action, "entity": entity,
                                 "entity_id": entity_id, "payload": payload}),
    )
    return fake


def _row(db, eid):
    return next(r for r in db.tables["rfp_emails"] if r["id"] == eid)


def test_retry_sends_a_failed_row_back_to_its_step_with_a_fresh_budget(svc_db):
    svc_db.tables["rfp_emails"] = [_email(
        E_FAILED, "failed", received_min=100, decided_at_step="classify", attempts=8,
        last_error="bad output", flag_reason="classify_server_error", next_attempt_at=None)]
    out = ingest.retry_row(svc_db, E_FAILED, "u1")
    row = _row(svc_db, E_FAILED)
    assert out["status"] == row["status"] == "classify"
    assert (row["attempts"], row["last_error"], row["next_attempt_at"], row["flag_reason"],
            row["decided_at_step"]) == (0, None, None, None, None)
    assert svc_db.tables["audit_log"] == [{
        "actor_id": "u1", "action": "rfp_email.retry", "entity": "rfp_email",
        "entity_id": E_FAILED,
        "payload": {"from_status": "failed", "to_status": "classify", "attempts": 8},
    }]


@pytest.mark.parametrize("step", [None, "match_done", "done"])
def test_retry_refuses_a_failed_row_whose_step_is_not_a_pending_step(svc_db, step):
    svc_db.tables["rfp_emails"] = [_email(E_FAILED, "failed", received_min=100,
                                          decided_at_step=step)]
    with pytest.raises(LookupError):
        ingest.retry_row(svc_db, E_FAILED, "u1")
    assert _row(svc_db, E_FAILED)["status"] == "failed"
    assert svc_db.tables["audit_log"] == []


@pytest.mark.parametrize(
    "over",
    [
        {"attempts": 3, "next_attempt_at": "2026-09-23T18:00:00+00:00", "last_error": "503"},
        {"attempts": 0, "next_attempt_at": "2026-09-23T18:00:00+00:00", "last_error": "away"},
        {"attempts": 2, "next_attempt_at": None, "last_error": "503"},
    ],
)
def test_retry_makes_a_waiting_row_due_now_and_keeps_its_attempts(svc_db, over):
    svc_db.tables["rfp_emails"] = [_email(E_WAIT, "extract", received_min=30, **over)]
    ingest.retry_row(svc_db, E_WAIT, "u1")
    row = _row(svc_db, E_WAIT)
    assert row["status"] == "extract" and row["next_attempt_at"] is None
    assert row["attempts"] == over["attempts"] and row["last_error"] == over["last_error"]
    assert svc_db.tables["audit_log"][0]["payload"] == {
        "from_status": "extract", "to_status": "extract", "attempts": over["attempts"]}


@pytest.mark.parametrize(
    "status", ["done", "created", "merged", "review_llm", "review_match",
               "flagged_unauthorized", "rejected_by_review", "blocked_sender"],
)
def test_retry_refuses_finished_and_review_rows(svc_db, status):
    svc_db.tables["rfp_emails"] = [_email(E_DONE, status, received_min=30, attempts=2,
                                          next_attempt_at="2026-09-23T18:00:00+00:00")]
    with pytest.raises(LookupError):
        ingest.retry_row(svc_db, E_DONE, "u1")
    assert _row(svc_db, E_DONE)["status"] == status
    assert svc_db.tables["audit_log"] == []


def test_retry_refuses_a_pending_row_that_is_simply_running(svc_db):
    svc_db.tables["rfp_emails"] = [_email(E_RUN, "keywords", received_min=5)]
    with pytest.raises(LookupError):
        ingest.retry_row(svc_db, E_RUN, "u1")


def test_retry_refuses_a_row_that_followed_a_sibling(svc_db):
    svc_db.tables["rfp_emails"] = [_email(E_WAIT, "harvest", received_min=5, flag_reason="sibling",
                                          next_attempt_at="2026-09-23T18:00:00+00:00")]
    with pytest.raises(LookupError, match="another copy"):
        ingest.retry_row(svc_db, E_WAIT, "u1")
    assert svc_db.tables["audit_log"] == []


def _copy_pair(leader_status):
    """Two copies of one message (same sender, subject, attachments and
    authorization) five minutes apart: the older one leads."""
    leader = _email(E_RUN, leader_status, received_min=20)
    follower = _email(E_WAIT, "extract", received_min=15, next_attempt_at=_ahead(5),
                      last_error=f"Waiting for another copy of this message ({E_RUN}) to finish.")
    follower["subject"] = leader["subject"]
    return leader, follower


def test_retry_refuses_a_row_parked_behind_an_undecided_leader(svc_db):
    leader, follower = _copy_pair("classify")
    svc_db.tables["rfp_emails"] = [leader, follower]
    with pytest.raises(LookupError, match="another copy"):
        ingest.retry_row(svc_db, E_WAIT, "u1")
    assert _row(svc_db, E_WAIT)["next_attempt_at"] == follower["next_attempt_at"]
    # The leader itself is not a follower and may be retried once it waits.
    _row(svc_db, E_RUN).update({"attempts": 1, "next_attempt_at": _ahead(5)})
    ingest.retry_row(svc_db, E_RUN, "u1")
    assert _row(svc_db, E_RUN)["next_attempt_at"] is None


@pytest.mark.parametrize("step", ["classify", "match", "harvest"])
def test_retry_lets_a_younger_copy_retrying_outside_extract_run_now(svc_db, step):
    """Only `extract` waits on an undecided older copy (_sibling_short_circuit);
    a copy retrying its own call at any other step is not parked behind the
    leader, so a retry is not refused."""
    leader, follower = _copy_pair("classify")
    follower.update({"status": step, "attempts": 1, "last_error": "503"})
    svc_db.tables["rfp_emails"] = [leader, follower]
    ingest.retry_row(svc_db, E_WAIT, "u1")
    row = _row(svc_db, E_WAIT)
    assert row["status"] == step and row["next_attempt_at"] is None
    assert len(svc_db.tables["audit_log"]) == 1


def test_retry_lets_a_row_whose_leader_decided_run_now(svc_db):
    # Its next tick follows the decided copy, which is what a retry is for.
    leader, follower = _copy_pair("done")
    svc_db.tables["rfp_emails"] = [leader, follower]
    ingest.retry_row(svc_db, E_WAIT, "u1")
    assert _row(svc_db, E_WAIT)["next_attempt_at"] is None


def test_retry_that_loses_its_cas_race_is_refused_and_not_audited(svc_db, monkeypatch):
    svc_db.tables["rfp_emails"] = [_email(E_FAILED, "failed", received_min=100,
                                          decided_at_step="extract")]
    monkeypatch.setattr(ingest, "_cas", lambda *a, **k: False)
    with pytest.raises(LookupError, match="moved on"):
        ingest.retry_row(svc_db, E_FAILED, "u1")
    assert svc_db.tables["audit_log"] == []


def test_retry_cas_is_on_the_status_it_read(svc_db):
    """The sweep moving the row between the read and the write wins: the
    CAS pins the status the retry decided on."""
    svc_db.tables["rfp_emails"] = [_email(E_FAILED, "failed", received_min=100,
                                          decided_at_step="extract")]
    real_get = ingest._get

    def racing_get(sb, email_id):
        row = real_get(sb, email_id)
        _row(sb, email_id)["status"] = "rejected_by_review"   # a person acted meanwhile
        return row

    import unittest.mock as mock

    with mock.patch.object(ingest, "_get", racing_get), pytest.raises(LookupError):
        ingest.retry_row(svc_db, E_FAILED, "u1")
    assert _row(svc_db, E_FAILED)["status"] == "rejected_by_review"
    assert svc_db.tables["audit_log"] == []


def test_every_review_role_may_retry():
    assert Role.ACCOUNTANT not in RFP_REVIEW_ROLES
    for role in RFP_REVIEW_ROLES:
        user = _user(role)
        assert asyncio.run(emails_router.require_review_queue(user=user)) is user

"""The evidence routes (app/routers/analytics_evidence.py) against
docs/ASSISTANT_PHASE4_PLAN.md 3.2.3, 3.8 and 5.3.

Invariants pinned here:

  gate       every route is EVIDENCE_ROLES only (the accountant and the
             engineers get 403) and 404s with Bidding off; ids that are not
             uuids, unknown projects and ids that are not profiles answer
             the bare 404 of a missing route;
  reads      the GETs forward their filters (Pacific dates, the sources
             list, the caller's role for the project redaction) and map the
             service refusals to 422 with X-Error-Code;
  bound      the two sensitive POSTs refuse a human, a chat turn principal
             and a header that differs (bare 404), a missing header (400),
             and an approval of another subject, another tool, another
             assignment, not executing or without a reason (409
             approval_mismatch); the estimator timeline needs an assignment
             (422) and an assignment of another project is 409;
  log        the access row carries the reason from the approval row (never
             the body), the approval id, the reader and the subject; the
             audit row has no reason; a replay re-runs the read and writes
             nothing more;
  xlsx       the person workbook is built before the access row, its id is
             in the row and the answer (never a URL), and a replay reuses it.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core import provenance, supabase_client
from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import EVIDENCE_ROLES, Role
from app.routers import analytics_evidence as ev_routes
from app.services import evidence, notifications

from tests.assistant_fakes import (
    APPROVAL_ID, CONV, OTHER_USER, PROJECT, PROJECT2, RUN, USER, FakeDB, FakeStorage, approval, profile,
)

HEADER = "X-Assistant-Idempotency-Key"
OTHER_APPROVAL = "99999999-9999-4999-8999-999999999990"
ASSIGNMENT = "abababab-abab-4bab-8bab-ababababab01"
ASSIGNMENT2 = "abababab-abab-4bab-8bab-ababababab02"
REASON = "Checking why the estimate came back late on 26.9.7127."
MARKER = "MARKER-5521 a free text summary"
EM_DASH = chr(0x2014)


class _Actor:
    """The shim's part: enter the assistant provenance around a request."""

    def __init__(self, app):
        self.app = app
        self.actor: tuple | None = None

    async def __call__(self, scope, receive, send):
        if self.actor is not None and scope["type"] == "http":
            with provenance.acting_as_assistant(*self.actor):
                return await self.app(scope, receive, send)
        return await self.app(scope, receive, send)


class FakeEvidence:
    """app.services.evidence as the routes see it (WI-3 tests the service)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.raise_code: str | None = None

    def _maybe_raise(self):
        if self.raise_code:
            raise evidence.EvidenceError(self.raise_code)

    def project_timeline(self, sb, project_id, *, role, since=None, until=None, sources=None, limit=50,
                         cursor=None):
        self.calls.append(("timeline", project_id, role, since, until, sources, limit, cursor))
        self._maybe_raise()
        if project_id == PROJECT2:
            raise evidence.EvidenceError("not_found")
        return {"project": {"id": project_id}, "window": {"since": "2026-07-26", "until": "2026-10-23"},
                "rows": [{"summary": MARKER}], "next_cursor": None, "truncated": False, "caps": {}}

    def project_opens(self, sb, project_id):
        self.calls.append(("opens", project_id))
        self._maybe_raise()
        return {"project": {"id": project_id}, "rows": [], "caveats": list(evidence.OPENS_CAVEATS),
                "truncated": False, "caps": {}}

    def change_history(self, sb, project_id, *, field=None, since=None, until=None, limit=50, max_limit=200):
        self.calls.append(("changes", project_id, field, since, until, limit))
        self._maybe_raise()
        return {"project": {"id": project_id}, "field": field, "rows": [], "total": 0, "truncated": False,
                "caps": {}}

    def ai_usage(self, sb, *, project_id=None, since=None, until=None, group_by="feature"):
        self.calls.append(("ai_usage", project_id, since, until, group_by))
        self._maybe_raise()
        return {"project": None, "group_by": group_by, "groups": [], "totals": {}, "ae_runs": [],
                "truncated": False, "caps": {}}

    def user_subject(self, sb, user_id):
        self.calls.append(("subject", user_id))
        if user_id != OTHER_USER:
            return None
        return {"id": OTHER_USER, "full_name": "Sam Estimator", "role": "estimator", "is_active": True}

    def estimator_subjects(self, sb, project_id):
        self.calls.append(("estimator_subjects", project_id))
        if project_id != PROJECT:
            return None
        return {"project": {"id": PROJECT, "number": "26.9.7127", "name": "Alpha"},
                "assignments": [{"assignment_id": ASSIGNMENT, "estimator_id": OTHER_USER, "name": "Sam Estimator",
                                 "created_at": None, "revoked_at": None}]}

    def estimator_timeline(self, sb, project_id, assignment_id):
        self.calls.append(("estimator_timeline", project_id, assignment_id))
        self._maybe_raise()
        if assignment_id == ASSIGNMENT2:
            raise evidence.EvidenceError("approval_mismatch")
        return {"view": "estimator_timeline", "project": {"id": project_id, "number": "26.9.7127"},
                "subject": {"user_id": OTHER_USER, "label": "Sam Estimator"},
                "summary": {"email_status": "sent"},
                "rows": [{"summary": MARKER}, {"summary": "assigned"}], "truncated": False, "caps": {}}

    def person_evidence(self, sb, user_id, view, *, since=None, until=None, project_id=None, limit=100,
                        cursor=None):
        self.calls.append(("person_evidence", user_id, view, since, until, project_id, limit, cursor))
        self._maybe_raise()
        if view not in evidence.VIEWS:
            raise evidence.EvidenceError("view_invalid")
        if user_id != OTHER_USER:
            raise evidence.EvidenceError("not_found")
        return {"subject": {"id": OTHER_USER, "full_name": "Sam Estimator", "role": "estimator",
                            "is_active": True},
                "view": view, "window": {"since": "2026-10-01", "until": "2026-10-20"},
                "rows": [{"at": "2026-10-02T17:00:00+00:00", "at_pt": "2026-10-02 10:00 AM PT", "source": "audit",
                          "kind": "project.update", "summary": MARKER}] * 3,
                "total": 3, "assistant_counts": {"audit": 2}, "next_cursor": None, "truncated": False,
                "caps": {"audit": 1000}}


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile(), profile(id=OTHER_USER, full_name="Sam Estimator", role="estimator")],
        assistant_approvals=[], assistant_evidence_access=[], assistant_reports=[], audit_log=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(notifications, "get_supabase", lambda: db)
    monkeypatch.setattr(get_settings(), "bidding_enabled", True)
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    fake = FakeEvidence()
    for name in ("project_timeline", "project_opens", "change_history", "ai_usage", "user_subject",
                 "estimator_subjects", "estimator_timeline", "person_evidence"):
        monkeypatch.setattr(evidence, name, getattr(fake, name))
    storage = FakeStorage().install(monkeypatch)
    state = SimpleNamespace(role=Role.EXECUTIVE, via=None)

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    app = FastAPI()
    app.include_router(ev_routes.router)
    app.dependency_overrides[get_current_user] = _user
    actor = _Actor(app)
    return SimpleNamespace(client=TestClient(actor), db=db, fake=fake, state=state, actor=actor, storage=storage,
                           app=app)


def _as_card(api, approval_id: str | None = APPROVAL_ID):
    """The execute job of an approved card (approval None: a chat turn)."""
    api.state.via = RUN
    api.actor.actor = (USER, RUN, approval_id)


def _human(api):
    api.state.via = None
    api.actor.actor = None


def _card(api, tool="person_evidence", args=None, **over):
    args = args if args is not None else (
        {"user_id": OTHER_USER, "view": "activity", "reason": "model text"} if tool == "person_evidence"
        else {"project_id": PROJECT, "assignment_id": ASSIGNMENT, "reason": "model text"})
    base = dict(action_kind=tool, status="executing", decision_reason=REASON, conversation_id=CONV,
                payload={"v": 1, "tool": tool, "args": args, "request": {"json": {}}})
    base.update(over)
    row = approval(**base)
    api.db.tables["assistant_approvals"].append(row)
    return row


def _person(api, body=None, headers=None, user_id=OTHER_USER):
    return api.client.post(f"/analytics/evidence/users/{user_id}", json=body or {"view": "activity"},
                           headers={HEADER: APPROVAL_ID} if headers is None else headers)


def _estimator(api, body=None, headers=None, project_id=PROJECT):
    return api.client.post(f"/analytics/evidence/projects/{project_id}/estimator-timeline",
                           json={"assignment_id": ASSIGNMENT} if body is None else body,
                           headers={HEADER: APPROVAL_ID} if headers is None else headers)


def _code(res, status_code, code):
    assert res.status_code == status_code, res.text
    assert res.headers["X-Error-Code"] == code
    assert res.json()["detail"]["code"] == code


GETS = (
    f"/analytics/evidence/projects/{PROJECT}/timeline",
    f"/analytics/evidence/projects/{PROJECT}/opens",
    f"/analytics/evidence/projects/{PROJECT}/changes",
    "/analytics/evidence/ai-usage",
    f"/analytics/evidence/subjects/users/{OTHER_USER}",
    f"/analytics/evidence/projects/{PROJECT}/estimator-subjects",
)


# ── the gate ─────────────────────────────────────────────────────────────────


def test_the_route_table_is_the_plans(api):
    paths = {(m, r.path) for r in ev_routes.router.routes if isinstance(r, APIRoute) for m in r.methods}
    assert paths == {
        ("GET", ev_routes.TIMELINE_ROUTE), ("GET", ev_routes.OPENS_ROUTE), ("GET", ev_routes.CHANGES_ROUTE),
        ("GET", ev_routes.AI_USAGE_ROUTE), ("GET", ev_routes.USER_SUBJECT_ROUTE),
        ("GET", ev_routes.ESTIMATOR_SUBJECTS_ROUTE), ("POST", ev_routes.ESTIMATOR_TIMELINE_ROUTE),
        ("POST", ev_routes.PERSON_EVIDENCE_ROUTE),
    }
    for route in ev_routes.router.routes:
        assert not route.endpoint.__code__.co_flags & 0x80, f"{route.path} must be a plain def"


@pytest.mark.parametrize("role", [r for r in Role if r not in EVIDENCE_ROLES])
def test_every_route_refuses_roles_outside_evidence(api, role):
    api.state.role = role
    for path in GETS:
        assert api.client.get(path).status_code == 403, (role, path)
    _card(api)
    _as_card(api)
    assert _person(api).status_code == 403
    assert _estimator(api).status_code == 403
    assert api.fake.calls == [] and api.db.tables["assistant_evidence_access"] == []


@pytest.mark.parametrize("role", sorted(EVIDENCE_ROLES))
def test_executive_and_it_admin_read(api, role):
    api.state.role = role
    for path in GETS:
        assert api.client.get(path).status_code == 200, (role, path)


def test_bidding_off_is_404_everywhere(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "bidding_enabled", False)
    for path in GETS:
        assert api.client.get(path).status_code == 404, path
    _card(api)
    _as_card(api)
    assert _person(api).status_code == 404
    assert api.fake.calls == []


def test_unknown_and_malformed_ids_are_the_bare_404(api):
    unknown_route = api.client.get("/analytics/evidence/nope/nothing")
    for path in ("/analytics/evidence/projects/nope/timeline", "/analytics/evidence/subjects/users/nope",
                 f"/analytics/evidence/subjects/users/{PROJECT}", f"/analytics/evidence/projects/{PROJECT2}/timeline",
                 f"/analytics/evidence/projects/{PROJECT2}/estimator-subjects",
                 "/analytics/evidence/ai-usage?project_id=nope"):
        res = api.client.get(path)
        assert res.status_code == 404 and res.content == unknown_route.content, path


# ── the plain reads ──────────────────────────────────────────────────────────


def test_reads_forward_their_filters_and_the_role(api):
    res = api.client.get(f"/analytics/evidence/projects/{PROJECT}/timeline",
                         params={"since": "2026-10-01", "until": "2026-10-20", "sources": "audit, stage,,email",
                                 "limit": "20", "cursor": "abc"})
    assert res.status_code == 200, res.text
    assert api.fake.calls[-1] == ("timeline", PROJECT, "executive", date(2026, 10, 1), date(2026, 10, 20),
                                  ["audit", "stage", "email"], 20, "abc")
    api.client.get(f"/analytics/evidence/projects/{PROJECT}/changes", params={"field": "pricing", "limit": 5})
    assert api.fake.calls[-1] == ("changes", PROJECT, "pricing", None, None, 5)
    api.client.get("/analytics/evidence/ai-usage", params={"project_id": PROJECT.upper(), "group_by": "day"})
    assert api.fake.calls[-1] == ("ai_usage", PROJECT, None, None, "day")
    api.client.get(f"/analytics/evidence/projects/{PROJECT}/opens")
    assert api.fake.calls[-1] == ("opens", PROJECT)
    assert api.client.get(f"/analytics/evidence/projects/{PROJECT}/timeline",
                          params={"limit": "201"}).status_code == 422


def test_service_refusals_map_to_422_with_the_code(api):
    for code in ("window_invalid", "sources_invalid", "field_invalid", "group_by_invalid", "cursor_invalid"):
        api.fake.raise_code = code
        _code(api.client.get(f"/analytics/evidence/projects/{PROJECT}/timeline"), 422, code)
    api.fake.raise_code = None
    for bad in ("10/01/2026", "2026-02-30", "2026-1-1"):
        _code(api.client.get(f"/analytics/evidence/projects/{PROJECT}/changes", params={"since": bad}),
              422, "window_invalid")


def test_subject_previews(api):
    body = api.client.get(f"/analytics/evidence/subjects/users/{OTHER_USER}").json()
    assert body == {"id": OTHER_USER, "full_name": "Sam Estimator", "role": "estimator", "is_active": True}
    body = api.client.get(f"/analytics/evidence/projects/{PROJECT}/estimator-subjects").json()
    assert body["assignments"][0]["assignment_id"] == ASSIGNMENT


# ── the sensitive POSTs: who may call them ──────────────────────────────────


def test_sensitive_posts_are_approval_bound(api):
    _card(api)
    _card(api, "estimator_timeline", id=OTHER_APPROVAL)
    _human(api)
    assert _person(api).status_code == 404
    assert _estimator(api, headers={HEADER: OTHER_APPROVAL}).status_code == 404
    _as_card(api, None)                                    # a chat turn principal
    assert _person(api).status_code == 404
    assert _estimator(api).status_code == 404
    _as_card(api)
    assert _person(api, headers={HEADER: OTHER_APPROVAL}).status_code == 404
    res = _person(api, headers={})
    assert res.status_code == 400 and res.headers["X-Error-Code"] == "idempotency_key_required"
    assert api.fake.calls == [] and api.db.tables["assistant_evidence_access"] == []
    assert api.db.tables["audit_log"] == []


@pytest.mark.parametrize("over", [
    {"action_kind": "estimator_timeline"},                      # another tool
    {"status": "approved"},                                     # not executing yet
    {"status": "executed"},
    {"decision_reason": None},                                  # no human reason
    {"decision_reason": "   "},
    {"user_id": OTHER_USER},                                    # not this person's card
])
def test_person_evidence_refuses_a_card_that_does_not_match(api, over):
    _card(api, **over)
    _as_card(api)
    _code(_person(api), 409, "approval_mismatch")
    assert api.fake.calls == [] and api.db.tables["assistant_evidence_access"] == []


def test_person_evidence_refuses_a_card_for_another_subject(api):
    _card(api, args={"user_id": USER, "view": "activity"})
    _as_card(api)
    _code(_person(api), 409, "approval_mismatch")
    api.db.tables["assistant_approvals"] = []
    _card(api, args={"view": "activity"})                       # no subject in the card
    _code(_person(api), 409, "approval_mismatch")
    assert api.fake.calls == []


def test_estimator_timeline_refusals(api):
    _card(api, "estimator_timeline")
    _as_card(api)
    _code(_estimator(api, body={}), 422, "assignment_required")
    _code(_estimator(api, body={"assignment_id": "  "}), 422, "assignment_required")
    _code(_estimator(api, body={"assignment_id": ASSIGNMENT2}), 409, "approval_mismatch")   # not the card's
    _code(_estimator(api, project_id=PROJECT2), 409, "approval_mismatch")                  # another project
    assert api.fake.calls == []
    api.db.tables["assistant_approvals"] = []
    _card(api, "estimator_timeline", args={"project_id": PROJECT, "assignment_id": ASSIGNMENT2})
    # The card names an assignment of another project: the service says so.
    _code(_estimator(api, body={"assignment_id": ASSIGNMENT2}), 409, "approval_mismatch")
    assert api.db.tables["assistant_evidence_access"] == []
    api.db.tables["assistant_approvals"] = []
    _card(api, "person_evidence", args={"project_id": PROJECT, "assignment_id": ASSIGNMENT})
    _code(_estimator(api), 409, "approval_mismatch")                                       # another tool


def test_person_evidence_maps_view_and_window_refusals(api):
    _card(api)
    _as_card(api)
    _code(_person(api, body={"view": "everything"}), 422, "view_invalid")
    _code(_person(api, body={"view": "activity", "since": "yesterday"}), 422, "window_invalid")
    api.fake.raise_code = "window_invalid"
    _code(_person(api), 422, "window_invalid")
    assert api.db.tables["assistant_evidence_access"] == []


# ── the access log ───────────────────────────────────────────────────────────


def test_estimator_timeline_logs_one_access_row_with_the_human_reason(api):
    _card(api, "estimator_timeline")
    _as_card(api)
    res = _estimator(api)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["summary"] == {"email_status": "sent"} and len(body["rows"]) == 2
    assert REASON not in res.text and "model text" not in res.text
    [row] = api.db.tables["assistant_evidence_access"]
    assert row["reason"] == REASON and row["approval_id"] == APPROVAL_ID
    assert row["reader_user_id"] == USER and row["reader_label"] == "Tom Moore" and row["reader_role"] == "executive"
    assert row["subject_kind"] == "estimator" and row["subject_user_id"] == OTHER_USER
    assert row["subject_label"] == "Sam Estimator" and row["project_id"] == PROJECT
    assert row["tool"] == "estimator_timeline" and row["view"] == "estimator_timeline"
    assert row["filters"] == {"assignment_id": ASSIGNMENT} and row["row_count"] == 2
    assert row["conversation_id"] == CONV and row["run_id"] == RUN and row["xlsx_report_id"] is None
    [audit] = api.db.tables["audit_log"]
    assert audit["action"] == "assistant.evidence_read" and audit["entity"] == "profile"
    assert audit["entity_id"] == OTHER_USER and audit["payload"] == {"view": "estimator_timeline",
                                                                      "approval_id": APPROVAL_ID}
    assert REASON not in str(audit)
    # A replay re-runs the read and writes nothing more.
    again = _estimator(api)
    assert again.status_code == 200 and again.json()["rows"] == body["rows"]
    assert len(api.db.tables["assistant_evidence_access"]) == 1 and len(api.db.tables["audit_log"]) == 1
    assert [c[0] for c in api.fake.calls] == ["estimator_timeline", "estimator_timeline"]


def test_person_evidence_logs_the_reason_from_the_card_never_the_body(api):
    _card(api)
    _as_card(api)
    res = _person(api, body={"view": "notifications", "since": "2026-10-01", "until": "2026-10-20",
                             "project_id": PROJECT, "limit": 50, "reason": "a body reason is ignored"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["xlsx_report_id"] is None and body["assistant_counts"] == {"audit": 2}
    assert api.fake.calls[-1] == ("person_evidence", OTHER_USER, "notifications", date(2026, 10, 1),
                                  date(2026, 10, 20), PROJECT, 50, None)
    [row] = api.db.tables["assistant_evidence_access"]
    assert row["reason"] == REASON and row["approval_id"] == APPROVAL_ID and row["view"] == "notifications"
    assert row["subject_kind"] == "user" and row["subject_user_id"] == OTHER_USER and row["project_id"] == PROJECT
    assert row["filters"] == {"since": "2026-10-01", "until": "2026-10-20", "project_id": PROJECT, "limit": 50,
                              "cursor": False, "xlsx": False}
    assert row["row_count"] == 3 and row["truncated"] is False
    assert "a body reason" not in str(api.db.tables) and REASON not in res.text
    assert _person(api, body={"view": "notifications"}).status_code == 200
    assert len(api.db.tables["assistant_evidence_access"]) == 1 and len(api.db.tables["audit_log"]) == 1


def test_a_person_evidence_page_with_more_pages_is_logged_truncated(api, monkeypatch):
    # DATA-2: the append-only access row never calls one page the whole set.
    real = api.fake.person_evidence

    def paged(*a, **k):
        return {**real(*a, **k), "total": 1200, "next_cursor": "abc"}

    monkeypatch.setattr(evidence, "person_evidence", paged)
    _card(api)
    _as_card(api)
    assert _person(api, body={"view": "activity"}).status_code == 200
    [row] = api.db.tables["assistant_evidence_access"]
    assert row["row_count"] == 3 and row["truncated"] is True


def test_an_unknown_subject_is_the_bare_404_and_logs_nothing(api):
    _card(api, args={"user_id": PROJECT2, "view": "activity"})
    _as_card(api)
    res = _person(api, user_id=PROJECT2)
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    assert api.db.tables["assistant_evidence_access"] == []


# ── the person workbook ──────────────────────────────────────────────────────


def test_xlsx_builds_the_workbook_first_and_links_its_id(api):
    _card(api)
    _as_card(api)
    res = _person(api, body={"view": "activity", "xlsx": True})
    assert res.status_code == 200, res.text
    body = res.json()
    [report] = api.db.tables["assistant_reports"]
    assert body["xlsx_report_id"] == report["id"]
    assert report["spec"] == "person_evidence" and report["approval_id"] == APPROVAL_ID
    assert report["storage_path"] == f"assistant-reports/{USER}/{report['id']}.xlsx"
    assert report["storage_path"] in api.storage.objects
    assert report["params"]["since"] == "2026-10-01" and report["params"]["subject_user_id"] == OTHER_USER
    assert REASON not in str(report["params"])
    [row] = api.db.tables["assistant_evidence_access"]
    assert row["xlsx_report_id"] == report["id"] and row["filters"]["xlsx"] is True
    assert "url" not in body and "supabase" not in res.text
    # A replay returns the first workbook and builds nothing more.
    again = _person(api, body={"view": "activity", "xlsx": True})
    assert again.status_code == 200 and again.json()["xlsx_report_id"] == report["id"]
    assert len(api.db.tables["assistant_reports"]) == 1 and len(api.storage.objects) == 1
    assert len(api.db.tables["assistant_evidence_access"]) == 1


def test_a_workbook_left_by_a_crashed_attempt_is_reused(api, monkeypatch):
    _card(api)
    _as_card(api)
    from tests.assistant_fakes import report as report_row

    left = report_row(spec="person_evidence", approval_id=APPROVAL_ID)
    api.db.tables["assistant_reports"].append(left)
    from app.services import assistant_reports

    monkeypatch.setattr(assistant_reports, "build_person_report",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("built twice")))
    res = _person(api, body={"view": "activity", "xlsx": True})
    assert res.status_code == 200 and res.json()["xlsx_report_id"] == left["id"]
    assert api.db.tables["assistant_evidence_access"][0]["xlsx_report_id"] == left["id"]


def test_no_em_dash_in_the_router():
    from pathlib import Path

    text = Path(ev_routes.__file__).read_text(encoding="utf-8")
    assert EM_DASH not in text and chr(0x2013) not in text

"""The report file routes (app/routers/assistant_reports.py;
docs/ASSISTANT_PHASE4_PLAN.md 3.9.2, 3.9.3, WI-9a).

Invariants pinned here:

  build     POST /assistant/tools/reports answers a turn principal only (a
            human gets the bare 404); the spec's role is checked against the
            caller's role before anything is built (403
            report_spec_forbidden), person_evidence is refused always; a
            range outside the spec's vocabulary is 422 range_invalid; the
            service gets the trusted conversation and run and ISO dates;
            ReportError keeps its status and code; the answer never carries
            a URL;
  download  the person's own ready, unexpired row only (410 report_expired
            otherwise), checked against the LIVE role (a demoted
            Executive's evidence workbook and a non-viewer's
            estimator_vs_bid are 403), a fresh 60 second link with the
            filename as the download name, never cached; refused to an
            assistant principal before any link is minted; the link is in
            no audit row;
  list      the person's own rows, human only.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access
from app.core import supabase_client
from app.core.deps import CurrentUser, get_current_user
from app.core.ratelimit import export_rate_limit
from app.core.roles import Role
from app.routers import assistant_reports as routes
from app.services import assistant_reports as reports
from app.services import storage

from tests.assistant_fakes import (
    CONV, MSG_ASSISTANT, NOW, OTHER_USER, PROJECT, REPORT, RUN, USER, FakeDB, conversation, message,
    pending_answer, profile, report, settings_row,
)

HEADER = "X-Assistant-Idempotency-Key"
KEY = "ledger-key-report"
OUT = {
    "report_id": REPORT, "title": "Bid Invitations: wtd", "filename": "Bid Invitations 2026-10-19 to 2026-10-24.xlsx",
    "spec": "bid_invitations", "row_count": 3, "truncated": False, "totals": {"rows": 3},
    "preview_rows": [{"project": "26.10.7267 Alpha"}], "expires_at_pt": "10/31/2026 9:00 AM",
}


@pytest.fixture
def api(monkeypatch):
    db = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_conversations=[], assistant_messages=[], assistant_tool_calls=[], assistant_reports=[],
        audit_log=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    state = SimpleNamespace(role=Role.EXECUTIVE, via=None, builds=[], signed=[], audits=[], build_exc=None)

    def _build(sb, user, spec, params, *, conversation_id, run_id, approval_id=None):
        state.builds.append({"spec": spec, "params": params, "conversation_id": conversation_id,
                             "run_id": run_id, "role": user.role})
        if state.build_exc is not None:
            raise state.build_exc
        return {**OUT, "spec": spec}

    monkeypatch.setattr(reports, "build", _build)

    def _signed(path, ttl_seconds=None, *, use_cache=True, download=None):
        state.signed.append({"path": path, "ttl": ttl_seconds, "use_cache": use_cache, "download": download})
        return f"https://storage.test/sign/{path}?token=abc"

    monkeypatch.setattr(storage, "signed_url", _signed)
    monkeypatch.setattr(routes, "audit", lambda *a: state.audits.append(a))

    def _user():
        return CurrentUser(id=USER, email="tom@g3.test", role=state.role, is_active=True,
                           via_assistant_run_id=state.via)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[export_rate_limit] = lambda: None
    return SimpleNamespace(client=TestClient(app), db=db, state=state)


def _turn(api, *, tool="build_report", mode="normal", key=KEY):
    api.state.via = RUN
    api.db.tables["assistant_conversations"] = [conversation(mode=mode)]
    api.db.tables["assistant_messages"] = [message(content="Bid invitations this week as a spreadsheet"),
                                           pending_answer(status="running")]
    api.db.tables["assistant_tool_calls"].append({
        "id": f"tc-{len(api.db.tables['assistant_tool_calls'])}", "user_id": USER, "run_id": RUN,
        "idempotency_key": key, "phase": "begun", "tool_name": tool, "conversation_id": CONV,
        "message_id": MSG_ASSISTANT, "tainted_context": False, "seq": 1,
    })
    return {HEADER: key}


def _build(api, headers, **body):
    payload = {"spec": "bid_invitations", "range": "wtd"}
    payload.update(body)
    return api.client.post("/assistant/tools/reports", json=payload, headers=headers)


# ── build ────────────────────────────────────────────────────────────────────


def test_a_human_gets_404_and_nothing_is_built(api):
    res = _build(api, {HEADER: KEY})
    assert res.status_code == 404 and res.json() == {"detail": "Not Found"}
    assert api.state.builds == []


def test_a_turn_without_its_ledger_row_is_refused(api):
    api.state.via = RUN
    res = _build(api, {HEADER: "no-such-key"})
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "turn_context_unavailable"
    headers = _turn(api, tool="memory_save")
    res = _build(api, headers)
    assert res.status_code == 409 and api.state.builds == []


def test_a_build_passes_the_trusted_turn_and_never_a_url(api):
    headers = _turn(api)
    res = _build(api, headers, range="custom", date_from="2026-10-01", date_to="2026-10-15")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["report_id"] == REPORT and body["spec"] == "bid_invitations"
    assert "url" not in json.dumps(body).lower().replace("_url", "") and "https://" not in json.dumps(body)
    assert api.state.builds == [{
        "spec": "bid_invitations", "params": {"range": "custom", "date_from": "2026-10-01", "date_to": "2026-10-15"},
        "conversation_id": CONV, "run_id": RUN, "role": Role.EXECUTIVE,
    }]
    assert api.state.signed == []


def test_person_evidence_is_always_forbidden(api):
    headers = _turn(api)
    for role in (Role.EXECUTIVE, Role.IT_ADMIN):
        api.state.role = role
        res = _build(api, headers, spec="person_evidence", range=None)
        assert res.status_code == 403 and res.headers["X-Error-Code"] == "report_spec_forbidden"
    assert api.state.builds == []


@pytest.mark.parametrize("spec,role", [
    ("estimator_vs_bid", Role.ESTIMATING_ENGINEER_LABOR),
    ("project_timeline", Role.ESTIMATING_ADMIN),
    ("project_file_access", Role.ACCOUNTANT),
    ("change_history", Role.ESTIMATING_ENGINEER_MATERIALS),
    ("ai_usage", Role.ESTIMATING_ADMIN),
])
def test_specs_the_role_may_not_build_are_403(api, spec, role):
    headers = _turn(api)
    api.state.role = role
    res = _build(api, headers, spec=spec, range=None, project_id=PROJECT)
    assert res.status_code == 403 and res.headers["X-Error-Code"] == "report_spec_forbidden"
    assert api.state.builds == []


def test_the_spec_roles_table_is_the_services(api):
    assert routes.reports.SPEC_ROLES is reports.SPEC_ROLES
    headers = _turn(api)
    api.state.role = Role.ESTIMATING_ENGINEER_LABOR
    assert _build(api, headers, spec="quote_readiness", range=None, week="2026-10-19").status_code == 200
    assert api.state.builds[-1]["params"] == {"week": "2026-10-19"}


@pytest.mark.parametrize("body", [
    {"range": "fortnight"},
    {"spec": "quote_readiness", "range": "wtd"},
    {"range": "custom"},
    {"range": "custom", "date_from": "2026-10-01"},
])
def test_a_range_outside_the_vocabulary_is_422_range_invalid(api, body):
    headers = _turn(api)
    res = _build(api, headers, **body)
    assert res.status_code == 422 and res.headers["X-Error-Code"] == "range_invalid"
    assert api.state.builds == []


def test_an_unknown_spec_or_a_bad_project_id_is_422(api):
    headers = _turn(api)
    assert _build(api, headers, spec="payroll").status_code == 422
    assert _build(api, headers, spec="project_timeline", range=None, project_id="nope").status_code == 422
    assert api.state.builds == []


@pytest.mark.parametrize("code,status", [
    ("report_too_large", 413), ("report_params_invalid", 422), ("report_not_found", 404),
    ("report_spec_forbidden", 403),
])
def test_report_errors_keep_their_status_and_code(api, code, status):
    headers = _turn(api)
    api.state.build_exc = reports.ReportError(code, "refused")
    res = _build(api, headers)
    assert res.status_code == status and res.headers["X-Error-Code"] == code


def test_mailbox_review_builds_nothing(api):
    headers = _turn(api, mode="mailbox_review")
    res = _build(api, headers)
    assert res.status_code == 409 and res.headers["X-Error-Code"] == "mailbox_review"
    assert api.state.builds == []


# ── download ─────────────────────────────────────────────────────────────────


def _download(api, rid=REPORT):
    return api.client.get(f"/assistant/reports/{rid}/download")


def test_download_mints_a_fresh_60_second_link_and_audits_without_it(api):
    api.db.tables["assistant_reports"] = [report()]
    res = _download(api)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["filename"] == "Bid Invitations week.xlsx" and body["url"].startswith("https://storage.test/")
    assert api.state.signed == [{"path": f"assistant-reports/{USER}/{REPORT}.xlsx", "ttl": 60, "use_cache": False,
                                 "download": "Bid Invitations week.xlsx"}]
    assert len(api.state.audits) == 1
    who, action, entity, entity_id, payload = api.state.audits[0]
    assert (who, action, entity, entity_id) == (USER, "assistant.report_download", "assistant_report", REPORT)
    assert "https://" not in json.dumps(payload) and "token" not in json.dumps(payload)
    assert api.db.tables["audit_log"] == []


@pytest.mark.parametrize("over", [
    {"status": "purged", "purged_at": NOW.isoformat()},
    {"expires_at": (NOW - timedelta(minutes=1)).isoformat()},
])
def test_a_purged_or_expired_report_is_410(api, over):
    api.db.tables["assistant_reports"] = [report(**over)]
    res = _download(api)
    assert res.status_code == 410 and res.headers["X-Error-Code"] == "report_expired"
    assert api.state.signed == []


@pytest.mark.parametrize("spec,role", [
    ("project_timeline", Role.ESTIMATING_ADMIN),       # a demoted Executive
    ("person_evidence", Role.ACCOUNTANT),
    ("estimator_vs_bid", Role.ESTIMATING_ENGINEER_LABOR),
])
def test_the_live_role_must_still_read_the_spec(api, spec, role):
    api.db.tables["assistant_reports"] = [report(spec=spec)]
    api.state.role = role
    res = _download(api)
    assert res.status_code == 403 and res.headers["X-Error-Code"] == "report_spec_forbidden"
    assert api.state.signed == []
    api.state.role = Role.EXECUTIVE
    assert _download(api).status_code == 200


@pytest.mark.parametrize("spec", ["quote_readiness", "bid_invitations"])
def test_a_workbook_built_with_actual_bid_dates_is_403_for_a_demoted_non_viewer(api, spec):
    # SEC-3: an Estimating Admin built it with bid dates, then became an engineer.
    api.db.tables["assistant_reports"] = [report(spec=spec, params={"week": "2026-10-19", "bid_dates": True})]
    api.state.role = Role.ESTIMATING_ENGINEER_LABOR
    res = _download(api)
    assert res.status_code == 403 and res.headers["X-Error-Code"] == "report_spec_forbidden"
    assert api.state.signed == []
    # The same spec built by a non-viewer (no bid dates) stays readable.
    api.db.tables["assistant_reports"] = [report(spec=spec, params={"week": "2026-10-19"})]
    assert _download(api).status_code == 200
    api.db.tables["assistant_reports"] = [report(spec=spec, params={"week": "2026-10-19", "bid_dates": True})]
    api.state.role = Role.ESTIMATING_ADMIN
    assert _download(api).status_code == 200


def test_another_persons_report_or_a_bad_id_is_404(api):
    api.db.tables["assistant_reports"] = [report(user_id=OTHER_USER)]
    assert _download(api).status_code == 404
    res = _download(api, "not-a-uuid")
    assert res.status_code == 404 and res.headers["X-Error-Code"] == "report_not_found"
    assert api.state.signed == []


def test_a_row_whose_path_is_not_its_own_is_never_signed(api):
    api.db.tables["assistant_reports"] = [report(storage_path=f"{PROJECT}/files/secret.pdf")]
    assert _download(api).status_code == 404
    assert api.state.signed == []


def test_an_assistant_principal_never_gets_a_link(api):
    api.db.tables["assistant_reports"] = [report()]
    api.state.via = RUN
    res = _download(api)
    assert res.status_code == 403
    assert api.client.get("/assistant/reports").status_code == 403
    assert api.state.signed == [] and api.state.audits == []


def test_the_routes_404_while_the_assistant_is_off(api, monkeypatch):
    api.db.tables["assistant_reports"] = [report()]
    monkeypatch.setattr(access, "env_enabled", lambda: False)
    assert _download(api).status_code == 404
    assert api.client.get("/assistant/reports").status_code == 404


def test_the_list_is_the_persons_own_rows_without_links(api):
    second = "12121212-1212-4212-8212-121212121212"
    api.db.tables["assistant_reports"] = [
        report(), report(id=second, created_at=(NOW + timedelta(seconds=5)).isoformat()),
        report(id="12121212-1212-4212-8212-121212121213", user_id=OTHER_USER),
    ]
    res = api.client.get("/assistant/reports?limit=20")
    assert res.status_code == 200, res.text
    rows = res.json()["reports"]
    assert [r["id"] for r in rows] == [second, REPORT]
    assert "storage_path" not in rows[0] and "url" not in rows[0]
    assert api.client.get("/assistant/reports?limit=0").status_code == 422


def test_the_route_literals_are_live():
    paths = {r.path for r in routes.router.routes}
    assert {routes.TOOL_REPORTS_ROUTE, routes.DOWNLOAD_ROUTE, routes.LIST_ROUTE} <= paths

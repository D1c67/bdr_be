"""The assistant report service (docs/ASSISTANT_PHASE4_PLAN.md 3.9.2, WI-4):
role gates, the eight specs, the upload and row, the audit, caps and the
preview. Storage, the repo and the audit are recorded fakes; the recipe specs
run the real recipe data over a filtering fake client."""

from __future__ import annotations

import io
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest

from app.assistant import repo
from app.core.deps import CurrentUser
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, EVIDENCE_ROLES, INTERNAL_ROLES, Role
from app.services import (
    assistant_recipes,
    assistant_reports as ar,
    auto_send_estimate,
    bid_invitations as bi,
    evidence,
    file_access,
    notifications,
    storage,
)
from tests.test_assistant_recipes_phase4 import ESTIMATES, _evb_world, _qr_world
from tests.test_assistant_report_xlsx import _bid_payload

EM = "\u2014"
FILENAME_RE = re.compile(r"^[A-Za-z0-9 ._()-]{1,120}\.xlsx$")
UID = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"


def _user(role=Role.EXECUTIVE):
    return CurrentUser(id=UID, email="exec@example.test", role=role, is_active=True)


@pytest.fixture
def rec(monkeypatch):
    r = SimpleNamespace(uploads=[], rows=[], audits=[], deleted=[], fail_insert=False)

    def upload(path, content, content_type, **kw):
        r.uploads.append((path, content, content_type))

    def insert(user_id, row):
        if r.fail_insert:
            raise RuntimeError("db down")
        r.rows.append((user_id, dict(row)))
        return dict(row)

    monkeypatch.setattr(storage, "upload_file", upload)
    monkeypatch.setattr(storage, "delete_file", lambda path: r.deleted.append(path))
    monkeypatch.setattr(repo, "insert_report", insert, raising=False)
    monkeypatch.setattr(notifications, "audit", lambda *a, **k: r.audits.append((a, k)))
    return r


def _workbook(rec_, index=-1):
    return openpyxl.load_workbook(io.BytesIO(rec_.uploads[index][1]))


def _strings(wb):
    return [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row if isinstance(c.value, str)]


def _build(spec, params=None, user=None, **kw):
    kw.setdefault("conversation_id", "33333333-3333-4333-8333-333333333333")
    kw.setdefault("run_id", "44444444-4444-4444-8444-444444444444")
    return ar.build(object(), user or _user(), spec, params or {}, **kw)


def _assert_recorded(rec_, out, spec):
    path, content, ctype = rec_.uploads[-1]
    user_id, row = rec_.rows[-1]
    assert user_id == UID
    assert path == f"assistant-reports/{UID}/{out['report_id']}.xlsx" == row["storage_path"]
    assert str(uuid.UUID(out["report_id"])) == out["report_id"] == row["id"]
    assert ctype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert row["user_id"] == UID and row["spec"] == spec and row["bytes"] == len(content)
    assert row["filename"] == out["filename"] and FILENAME_RE.match(row["filename"])
    assert 1 <= len(row["title"]) <= 200 and row["row_count"] == out["row_count"]
    assert row["approval_id"] is None
    expires = datetime.fromisoformat(row["expires_at"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(timezone.utc) <= timedelta(days=7)
    (audit_args, _), = rec_.audits[-1:]
    assert audit_args == (UID, "assistant.report_build", "assistant_report", out["report_id"],
                          {"spec": spec, "params": row["params"], "rows": out["row_count"]})
    assert set(out) == {"report_id", "title", "filename", "spec", "row_count", "truncated", "totals",
                        "preview_rows", "expires_at_pt"}
    assert out["spec"] == spec and len(out["preview_rows"]) <= 10
    assert not [s for s in _strings(_workbook(rec_)) if EM in s]
    blob = repr(out)
    assert "http" not in blob and "signed" not in blob.lower()


# ── the role table and refusals ──────────────────────────────────────────────


def test_spec_roles_table():
    assert ar.SPEC_ROLES["bid_invitations"] == INTERNAL_ROLES == ar.SPEC_ROLES["quote_readiness"]
    assert ar.SPEC_ROLES["estimator_vs_bid"] == ACTUAL_BID_VIEWER_ROLES
    for spec in ("project_timeline", "project_file_access", "change_history", "ai_usage", "person_evidence"):
        assert ar.SPEC_ROLES[spec] == EVIDENCE_ROLES
    assert set(ar.SPEC_ROLES) == {"bid_invitations", "quote_readiness", "estimator_vs_bid", "project_timeline",
                                  "project_file_access", "change_history", "ai_usage", "person_evidence"}


@pytest.mark.parametrize("spec,role", [
    ("estimator_vs_bid", Role.ESTIMATING_ENGINEER_LABOR),
    ("estimator_vs_bid", Role.ESTIMATING_ENGINEER_MATERIALS),
    ("project_timeline", Role.ESTIMATING_ADMIN), ("project_timeline", Role.ACCOUNTANT),
    ("project_file_access", Role.ESTIMATING_ADMIN), ("change_history", Role.ACCOUNTANT),
    ("ai_usage", Role.ESTIMATING_ENGINEER_LABOR), ("bid_invitations", Role.ESTIMATOR),
    ("quote_readiness", Role.ESTIMATOR),
])
def test_build_refuses_a_role_outside_the_spec(rec, spec, role):
    with pytest.raises(ar.ReportError) as e:
        _build(spec, {"project_id": PROJECT} if spec in ("project_timeline", "project_file_access",
                                                         "change_history") else {}, user=_user(role))
    assert e.value.code == "report_spec_forbidden" and e.value.status_code == 403
    assert not rec.uploads and not rec.rows


@pytest.mark.parametrize("role", [Role.EXECUTIVE, Role.IT_ADMIN])
def test_person_evidence_is_never_built_by_build(rec, role):
    with pytest.raises(ar.ReportError) as e:
        _build("person_evidence", {"view": "activity"}, user=_user(role))
    assert e.value.code == "report_spec_forbidden"
    assert not rec.uploads


def test_unknown_spec_and_foreign_params(rec):
    with pytest.raises(ar.ReportError) as e:
        _build("run_report")
    assert e.value.code == "report_spec_invalid" and e.value.status_code == 422
    with pytest.raises(ar.ReportError) as e:
        _build("bid_invitations", {"week": "next_week"})
    assert e.value.code == "report_params_invalid" and "week" in str(e.value)
    with pytest.raises(ar.ReportError) as e:
        _build("quote_readiness", {"project_id": PROJECT})
    assert e.value.code == "report_params_invalid"
    assert not rec.uploads


def test_empty_params_are_ignored(rec, monkeypatch):
    monkeypatch.setattr(bi, "report", lambda *a, **k: _bid_payload())
    out = _build("bid_invitations", {"range": None, "date_from": "", "week": None, "period": None})
    assert out["spec"] == "bid_invitations"


# ── bid_invitations ──────────────────────────────────────────────────────────


def _wtd_name() -> str:
    """The week-to-date filename for today (Monday through today, Pacific)."""
    today = datetime.now(bi.REPORT_TZ).date()
    monday = today - timedelta(days=today.weekday())
    span = monday.isoformat() if monday == today else f"{monday.isoformat()} to {today.isoformat()}"
    return f"Bid Invitations {span}.xlsx"


def test_pacific_span_names_the_inclusive_pacific_dates():
    utc = timezone.utc
    # custom: exclusive end is the next Pacific midnight
    assert ar._pacific_span(datetime(2026, 10, 1, 7, tzinfo=utc), datetime(2026, 10, 16, 7, tzinfo=utc)) \
        == "2026-10-01 to 2026-10-15"
    # a single day collapses to one date
    assert ar._pacific_span(datetime(2026, 10, 24, 7, tzinfo=utc), datetime(2026, 10, 25, 7, tzinfo=utc)) \
        == "2026-10-24"
    # open-ended runs to today, Pacific
    assert ar._pacific_span(datetime(2026, 10, 19, 7, tzinfo=utc), None, today=date(2026, 10, 24)) \
        == "2026-10-19 to 2026-10-24"
    assert ar._pacific_span(datetime(2026, 10, 24, 7, tzinfo=utc), None, today=date(2026, 10, 24)) == "2026-10-24"


def test_bid_invitations(rec, monkeypatch):
    seen = {}

    def report(date_from, date_to, role, range_):
        seen.update(date_from=date_from, date_to=date_to, role=role, range=range_)
        return _bid_payload()

    monkeypatch.setattr(bi, "report", report)
    out = _build("bid_invitations", {"range": "wtd"})
    _assert_recorded(rec, out, "bid_invitations")
    assert seen["range"] == "wtd" and seen["role"] == "executive" and seen["date_to"] is None
    assert out["title"] == "Bid Invitations: wtd" and out["filename"] == _wtd_name()
    assert re.fullmatch(r"Bid Invitations (\d{4}-\d{2}-\d{2})( to \d{4}-\d{2}-\d{2})?\.xlsx", out["filename"])
    assert out["row_count"] == 2 and out["truncated"] is False
    assert out["totals"]["bids"] == 2 and out["totals"]["waiting"] == 1
    first = out["preview_rows"][0]
    assert first["project"] == f"0101 Fire Station {EM} Phase 2" or first["project"].startswith("0101 Fire Station")
    assert first["quotes"] == "4/4" and first["gcs"] == "Monument / Rafael" and first["bid_at"] == "10/23/2026 2:00 PM"
    wb = _workbook(rec)
    assert wb.worksheets[0]["A1"].value.startswith("Bid Invitations: ")
    assert rec.rows[-1][1]["params"] == {"range": "wtd", "bid_dates": True}   # SEC-3: built by a viewer


def test_bid_invitations_default_range_custom_range_and_errors(rec, monkeypatch):
    seen = []
    monkeypatch.setattr(bi, "report", lambda df, dt, role, rng: seen.append((df, dt, rng)) or _bid_payload())
    out = _build("bid_invitations", {})
    assert seen[-1][2] == "wtd" and out["filename"] == _wtd_name()
    out = _build("bid_invitations", {"range": "custom", "date_from": "2026-10-01", "date_to": "2026-10-15"})
    assert out["filename"] == "Bid Invitations 2026-10-01 to 2026-10-15.xlsx"
    assert rec.rows[-1][1]["params"] == {"range": "custom", "date_from": "2026-10-01", "date_to": "2026-10-15",
                                         "bid_dates": True}
    assert seen[-1][1] is not None
    for params in ({"range": "decade"}, {"range": "custom"}, {"range": "custom", "date_from": "2026-10-09",
                                                           "date_to": "2026-10-01"}):
        with pytest.raises(ar.ReportError) as e:
            _build("bid_invitations", params)
        assert e.value.code == "report_params_invalid"


def test_bid_invitations_non_viewer_has_no_bid_dates_in_the_preview(rec, monkeypatch):
    payload = _bid_payload(visible=False)
    for row in payload["rows"]:
        row.pop("bid_at")
    monkeypatch.setattr(bi, "report", lambda *a: payload)
    out = _build("bid_invitations", {}, user=_user(Role.ESTIMATING_ENGINEER_LABOR))
    assert all("bid_at" not in r for r in out["preview_rows"])
    assert "bid_dates" not in rec.rows[-1][1]["params"]
    # Even if a payload carried the date, a non-viewer's preview leaves it out.
    leaky = _bid_payload()
    monkeypatch.setattr(bi, "report", lambda *a: leaky)
    out = _build("bid_invitations", {}, user=_user(Role.ESTIMATING_ENGINEER_LABOR))
    assert all("bid_at" not in r for r in out["preview_rows"])


def test_row_cap_truncates_and_says_so(rec, monkeypatch):
    monkeypatch.setattr(ar, "_max_rows", lambda: 1)
    monkeypatch.setattr(bi, "report", lambda *a: _bid_payload())
    out = _build("bid_invitations", {})
    assert out["row_count"] == 1 and out["truncated"] is True
    assert rec.rows[-1][1]["truncated"] is True
    ws = _workbook(rec).worksheets[0]
    assert sum(1 for row in ws.iter_rows(min_row=4) if row[0].value) == 1


def test_a_file_over_ten_megabytes_is_refused(rec, monkeypatch):
    monkeypatch.setattr(ar, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(bi, "report", lambda *a: _bid_payload())
    with pytest.raises(ar.ReportError) as e:
        _build("bid_invitations", {})
    assert e.value.code == "report_too_large" and e.value.status_code == 413
    assert not rec.uploads and not rec.rows


def test_limits_are_the_planned_ones():
    from app.core.config import get_settings

    assert get_settings().assistant_report_ttl_days == 7 and get_settings().assistant_report_max_rows == 20000
    assert Path(ar.__file__).read_text(encoding="utf-8").count("10 * 1024 * 1024") == 1


# ── the recipe specs, over the real recipe data ──────────────────────────────


def _build_with(sb, spec, params, user=None):
    return ar.build(sb, user or _user(), spec, params, conversation_id=None, run_id=None)


def test_quote_readiness_workbook(rec):
    out = _build_with(_qr_world(), "quote_readiness", {"week": "2026-10-26"})
    _assert_recorded(rec, out, "quote_readiness")
    assert out["title"] == "Key Quotes: week of 10/26/2026" and out["filename"] == "Key Quotes - week of 2026-10-26.xlsx"
    assert out["row_count"] == 5 and out["totals"] == {"bids": 5, "with_every_key_quote": 1,
                                                       "no_required_categories": 1}
    wb = _workbook(rec)
    assert wb.sheetnames == ["Key Quotes", "Category detail"]
    ws = wb["Key Quotes"]
    headers = [c.value for c in ws[3]]
    assert headers[:2] == ["Project", "Bid date (Pacific)"] and "Missing key categories" in headers
    banner = ws["A2"].value
    assert "General Material, Demolition, Lighting, Low Voltage" in banner and "5 rows" in banner
    by_name = {r[0].value: r for r in ws.iter_rows(min_row=4)}
    assert by_name["26.10.7101 Plaza <1>"][3].value == "1/2" and by_name["26.10.7101 Plaza <1>"][4].value == "Wiring"
    assert by_name["26.10.7102 Plaza <2>"][3].value == "n/a"
    detail = wb["Category detail"]
    cats = [(r[1].value, r[5].value, r[6].value) for r in detail.iter_rows(min_row=4) if r[0].value.endswith("<1>")]
    assert ("Lighting", "Yes", "No (excluded)") in cats and ("Wiring", "No", "Yes") in cats
    assert out["preview_rows"][0]["Key quotes"] == "0/1"
    assert rec.rows[-1][1]["params"] == {"week": "2026-10-26", "bid_dates": True}


def test_live_role_may_read_checks_the_build_time_bid_dates():
    row = {"spec": "quote_readiness", "params": {"week": "2026-10-26", "bid_dates": True}}
    assert ar.live_role_may_read(row, Role.ESTIMATING_ADMIN)
    assert not ar.live_role_may_read(row, Role.ESTIMATING_ENGINEER_LABOR)
    assert ar.live_role_may_read({**row, "params": {"week": "2026-10-26"}}, Role.ESTIMATING_ENGINEER_LABOR)
    assert not ar.live_role_may_read({"spec": "project_timeline", "params": {}}, Role.ESTIMATING_ADMIN)
    assert not ar.live_role_may_read(row, None) and not ar.live_role_may_read({"spec": "nope"}, Role.EXECUTIVE)


def test_quote_readiness_for_a_non_viewer_has_no_bid_date_column(rec):
    out = _build_with(_qr_world(), "quote_readiness", {"week": "2026-10-26"}, user=_user(Role.ESTIMATING_ENGINEER_LABOR))
    headers = [c.value for c in _workbook(rec)["Key Quotes"][3]]
    assert "Bid date (Pacific)" not in headers and headers[0] == "Project"
    assert out["row_count"] == 4
    assert rec.rows[-1][1]["params"] == {"week": "2026-10-26"}
    assert all("Bid date (Pacific)" not in r for r in out["preview_rows"])


def test_quote_readiness_bad_week(rec):
    for week in ("2026-10-27", "soon"):
        with pytest.raises(ar.ReportError) as e:
            _build_with(_qr_world(), "quote_readiness", {"week": week})
        assert e.value.code == "report_params_invalid"


@pytest.fixture
def estimates(monkeypatch):
    monkeypatch.setattr(auto_send_estimate, "load_estimate", lambda pid, *, sb=None: dict(ESTIMATES[pid]))


def test_estimator_vs_bid_workbook(rec, estimates):
    out = _build_with(_evb_world(), "estimator_vs_bid", {"period": "2026-09"})
    _assert_recorded(rec, out, "estimator_vs_bid")
    assert out["title"] == "Estimator vs Our Price: September 2026"
    assert out["filename"] == "Estimator vs Our Price - September 2026.xlsx"
    assert out["row_count"] == 6 and out["totals"]["sent_early"] == 1
    assert out["totals"]["like_for_like"]["count"] == 4
    assert rec.rows[-1][1]["params"] == {"period": "2026-09"}
    wb = _workbook(rec)
    assert wb.sheetnames == ["Compared", "Totals", "Sent early, due next month"]
    ws = wb["Compared"]
    first = [c.value for c in ws[4]]
    assert first[0] == "26.9.7001 Tower <1>" and first[4] == 1000000 and first[5] == 1250000
    assert first[6] == 250000 and first[7] == 25.0 and first[8] == "We bid higher" and first[9] == 2
    assert ws["E4"].number_format == '"$"#,##0.00' and ws["H4"].number_format == '0.0"%"'
    notes = {r[0].value: r[10].value for r in ws.iter_rows(min_row=4)}
    assert "newer round" in notes["26.9.7011 Tower <11>"] and "alternates" in notes["26.9.7011 Tower <11>"]
    assert "recorded price" in notes["26.9.7009 Tower <9>"]
    totals = {r[0].value: [c.value for c in r] for r in wb["Totals"].iter_rows(min_row=4)}
    assert totals["Like-for-like (bids with both prices)"][1:6] == [4, 2990000, 3200000, 210000, 7.0]
    assert totals["All rows (not like-for-like)"][1] == 6
    assert wb["Sent early, due next month"]["A4"].value == "26.9.7006 Tower <6>"
    assert out["preview_rows"][0]["Estimator price"] == 1000000


def test_estimator_vs_bid_period_default_and_errors(rec, estimates):
    for params in ({"period": "2026-13"}, {"period": "last year"}):
        with pytest.raises(ar.ReportError) as e:
            _build_with(_evb_world(), "estimator_vs_bid", params)
        assert e.value.code == "report_params_invalid"
    out = _build_with(_evb_world(), "estimator_vs_bid", {"period": "2025-01"})
    assert out["row_count"] == 0 and out["preview_rows"] == []


# ── preview stripping ────────────────────────────────────────────────────────

COLUMNS = [("Project", 20, "text"), ("Estimator price", 12, "money"), ("Our price", 12, "money"),
           ("Pct", 8, "pct"), ("Total amount", 10, "text"), ("Count", 6, "int")]


def test_preview_strips_amounts_for_non_viewers():
    rows = [["A", 100, 120, 20, "$5", 2]] * 12
    viewer = ar.preview_from_sheet(COLUMNS, rows, viewer=True)
    assert len(viewer) == 10 and viewer[0] == {"Project": "A", "Estimator price": 100, "Our price": 120, "Pct": 20,
                                               "Total amount": "$5", "Count": 2}
    other = ar.preview_from_sheet(COLUMNS, rows, viewer=False)
    assert other[0] == {"Project": "A", "Count": 2}


# ── the evidence specs ───────────────────────────────────────────────────────


def _event(i, **kw):
    return {"at": f"2026-10-2{i % 9}T20:00:00+00:00", "at_pt": "x", "source": "stage", "kind": "stage_change",
            "actor": {"id": "u", "name": "Pat", "kind": "human", "label": "Pat Lee"},
            "summary": f"Moved to RFQs {i}", "why": "The stage log records every move.",
            "ref": {"table": "stage_events", "id": str(i)}, **kw}


def test_project_timeline(rec, monkeypatch):
    pages = {None: {"project": {"id": PROJECT, "number": "26.9.7127", "name": "ZZ TEST"}, "rows": [_event(1), _event(2)],
                    "next_cursor": "c2", "truncated": False},
             "c2": {"project": {"id": PROJECT, "number": "26.9.7127"}, "rows": [_event(3)], "next_cursor": None,
                    "truncated": True}}
    calls = []

    def timeline(sb, project_id, *, role, since=None, until=None, sources=None, limit=50, cursor=None):
        calls.append((project_id, role, since, until, limit, cursor))
        return pages[cursor]

    monkeypatch.setattr(evidence, "project_timeline", timeline)
    out = _build("project_timeline", {"project_id": PROJECT, "date_from": "2026-10-01", "date_to": "2026-10-31"})
    _assert_recorded(rec, out, "project_timeline")
    assert [c[5] for c in calls] == [None, "c2"] and calls[0][2:5] == (date(2026, 10, 1), date(2026, 10, 31), 200)
    assert calls[0][1] == Role.EXECUTIVE
    assert out["row_count"] == 3 and out["truncated"] is True
    assert out["title"] == "Project Timeline: 26.9.7127 ZZ TEST" and out["filename"] == "Project Timeline - 26.9.7127.xlsx"
    assert rec.rows[-1][1]["params"] == {"project_id": PROJECT, "date_from": "2026-10-01", "date_to": "2026-10-31"}
    ws = _workbook(rec)["Timeline"]
    assert [c.value for c in ws[3]][:3] == ["When (Pacific)", "Source", "Kind"]
    assert ws["D4"].value == "Pat Lee" and ws["E4"].value == "Moved to RFQs 1" and ws["A4"].value == "10/21/2026 1:00 PM"
    assert "truncated" in ws["A2"].value
    assert out["preview_rows"][0]["Who"] == "Pat Lee"


def test_project_timeline_row_cap_and_errors(rec, monkeypatch):
    monkeypatch.setattr(ar, "_max_rows", lambda: 3)
    page = {"project": {}, "rows": [_event(i) for i in range(4)], "next_cursor": "more", "truncated": False}
    monkeypatch.setattr(evidence, "project_timeline", lambda *a, **k: page)
    out = _build("project_timeline", {"project_id": PROJECT})
    assert out["row_count"] == 3 and out["truncated"] is True

    def missing(*a, **k):
        raise evidence.EvidenceError("not_found")

    monkeypatch.setattr(evidence, "project_timeline", missing)
    with pytest.raises(ar.ReportError) as e:
        _build("project_timeline", {"project_id": PROJECT})
    assert e.value.code == "report_not_found" and e.value.status_code == 404

    def bad_window(*a, **k):
        raise evidence.EvidenceError("window_invalid", "The window is too long.")

    monkeypatch.setattr(evidence, "project_timeline", bad_window)
    with pytest.raises(ar.ReportError) as e:
        _build("project_timeline", {"project_id": PROJECT})
    assert e.value.code == "report_params_invalid"
    for params in ({}, {"project_id": "not-a-uuid"}, {"project_id": PROJECT, "date_from": "yesterday"}):
        with pytest.raises(ar.ReportError) as e:
            _build("project_timeline", params)
        assert e.value.code == "report_params_invalid"


def test_change_history(rec, monkeypatch):
    seen = {}

    def history(sb, project_id, *, field=None, since=None, until=None, limit=50, max_limit=200):
        seen.update(field=field, since=since, limit=limit, max_limit=max_limit)
        return {"project": {"number": "26.9.7127", "name": "ZZ"}, "rows": [
            {"at": "2026-10-20T20:00:00+00:00", "field": "labor_note", "before": f"old {EM} text", "after": "=cmd",
             "before_known": True, "actor": {"label": "Pat Lee"}, "action": "project.update"},
            {"at": "2026-10-21T20:00:00+00:00", "field": "wage_type", "before": None, "after": "prevailing",
             "before_known": False, "actor": {"name": "Sam"}, "action": "project.update"}]}

    monkeypatch.setattr(evidence, "change_history", history)
    out = _build("change_history", {"project_id": PROJECT, "field": "labor"})
    _assert_recorded(rec, out, "change_history")
    assert seen["field"] == "labor" and seen["limit"] == 20000 and seen["max_limit"] == 20000
    ws = _workbook(rec)["Changes"]
    assert ws["C4"].value == "old - text" and ws["D4"].value == "'=cmd" and ws["E4"].value == "Yes"
    assert ws["C5"].value is None and ws["E5"].value == "No (predates the trail)" and ws["F5"].value == "Sam"
    assert out["filename"] == "Change History - 26.9.7127.xlsx"


def test_project_file_access(rec, monkeypatch):
    monkeypatch.setattr(evidence, "project_opens", lambda sb, pid: {
        "rows": [{"user_id": "u1", "user_name": "Pat Lee", "kind": "project", "visits": 4,
                  "first_opened_at": "2026-10-01T20:00:00+00:00", "last_opened_at": "2026-10-20T20:00:00+00:00"}],
        "caveats": ["Accountant opens are not recorded."]})
    monkeypatch.setattr(evidence, "window", lambda since, until: (datetime(2026, 8, 1, tzinfo=timezone.utc),
                                                                  datetime(2026, 10, 24, tzinfo=timezone.utc)))
    captured = {}

    def access(**kw):
        captured.update(kw)
        return {"summary": [{"actor_id": "u1", "actor_name": "Pat Lee", "actor_email": "pat@example.test",
                             "filename": "plans.pdf", "opens": 3, "last_at": "2026-10-20T20:00:00+00:00"}],
                "events": [{"id": "e1", "actor_name": "Pat Lee", "action": "open", "at": "2026-10-20T20:00:00+00:00"}],
                "events_total": 1, "truncated": False}

    monkeypatch.setattr(file_access, "access_report", access)
    out = _build("project_file_access", {"project_id": PROJECT})
    _assert_recorded(rec, out, "project_file_access")
    assert captured["actor_kind"] == "human" and captured["include_prefetch"] is False
    assert captured["project_id"] == PROJECT
    wb = _workbook(rec)
    assert wb.sheetnames == ["Project visits", "Files by person", "File events", "Caveats"]
    flat = _strings(wb)
    assert "pat@example.test" not in flat and not any("email" in s.lower() for s in flat)      # no emails
    assert "Pat Lee" in flat and "plans.pdf" in flat and "Accountant opens are not recorded." in flat
    assert out["row_count"] == 2


def test_ai_usage(rec, monkeypatch):
    seen = {}

    def usage(sb, *, project_id=None, since=None, until=None, group_by="feature"):
        seen.update(project_id=project_id, group_by=group_by)
        return {"rows": [{"group": "bid_split", "calls": 12, "failures": 1, "cost_usd": "0.42"}],
                "ae_runs": [{"id": "r1", "name": "Run 1", "status": "done", "cost_usd": "1.10",
                             "finished_at": "2026-10-20T20:00:00+00:00"}],
                "retention_note": "AI call history is kept 90 days.", "truncated": False}

    monkeypatch.setattr(evidence, "ai_usage", usage)
    out = _build("ai_usage", {"group_by": "model"})
    _assert_recorded(rec, out, "ai_usage")
    assert seen == {"project_id": None, "group_by": "model"} and out["title"] == "AI Usage: company"
    wb = _workbook(rec)
    assert wb.sheetnames[:2] == ["Rows", "Ae runs"]
    assert "AI call history is kept 90 days." in wb["Rows"]["A2"].value
    with pytest.raises(ar.ReportError) as e:
        _build("ai_usage", {"group_by": "person"})
    assert e.value.code == "report_params_invalid"
    out = _build("ai_usage", {"project_id": PROJECT})
    assert seen["project_id"] == PROJECT and out["title"].startswith("AI Usage: project ")


# ── build_person_report ──────────────────────────────────────────────────────

SUBJECT = {"id": "55555555-5555-4555-8555-555555555555", "full_name": "Sam Rivers", "role": "estimating_admin",
           "is_active": True}


def test_build_person_report(rec):
    result = {"subject": SUBJECT, "view": "activity", "rows": [_event(1), _event(2)],
              "assistant_counts": {"audit_log": 7, "file_access": 2}, "truncated": False, "caps": {}}
    approval = "66666666-6666-4666-8666-666666666666"
    out = ar.build_person_report(object(), _user(), SUBJECT, "activity", result,
                                 conversation_id="c", run_id="r", approval_id=approval)
    path, content, _ = rec.uploads[-1]
    _, row = rec.rows[-1]
    assert path == f"assistant-reports/{UID}/{out['report_id']}.xlsx" and row["approval_id"] == approval
    assert row["spec"] == "person_evidence" and FILENAME_RE.match(row["filename"])
    assert row["params"] == {"view": "activity", "subject_user_id": SUBJECT["id"]}
    assert out["spec"] == "person_evidence" and out["row_count"] == 2 and out["title"].startswith("Person Evidence: Sam Rivers")
    wb = _workbook(rec)
    assert wb.sheetnames == ["Evidence", "Assistant rows"]
    assert [(r[0].value, r[1].value) for r in wb["Assistant rows"].iter_rows(min_row=4)] == [
        ("audit_log", 7), ("file_access", 2)]
    assert not [s for s in _strings(wb) if EM in s]
    (audit_args, _), = rec.audits[-1:]
    assert audit_args[1:4] == ("assistant.report_build", "assistant_report", out["report_id"])
    assert "reason" not in repr(audit_args) and "reason" not in repr(row["params"])
    assert "http" not in repr(out)


def test_a_person_report_of_one_page_says_it_is_truncated(rec):
    # DATA-2: 500 rows shown of 1,200, all under the source caps.
    result = {"subject": SUBJECT, "view": "activity", "rows": [_event(i) for i in range(3)], "total": 1200,
              "next_cursor": "abc", "assistant_counts": {}, "truncated": False, "caps": {}}
    out = ar.build_person_report(object(), _user(), SUBJECT, "activity", result,
                                 conversation_id=None, run_id=None, approval_id="p1")
    assert out["truncated"] is True and rec.rows[-1][1]["truncated"] is True
    assert out["totals"]["total"] == 1200
    assert "truncated" in _workbook(rec)["Evidence"]["A2"].value
    whole = {**result, "total": 3, "next_cursor": None}
    assert ar.build_person_report(object(), _user(), SUBJECT, "activity", whole, conversation_id=None,
                                  run_id=None, approval_id="p2")["truncated"] is False


def test_build_person_report_needs_an_evidence_role_and_handles_other_row_shapes(rec):
    with pytest.raises(ar.ReportError) as e:
        ar.build_person_report(object(), _user(Role.ESTIMATING_ADMIN), SUBJECT, "activity", {"rows": []},
                               conversation_id=None, run_id=None, approval_id="a")
    assert e.value.code == "report_spec_forbidden" and not rec.uploads
    result = {"rows": [{"id": "n1", "type": "assigned", "read_at": "2026-10-20T20:00:00+00:00", "dismissed_at": None,
                        "message": f"New bid {EM} Plaza"}], "truncated": True}
    out = ar.build_person_report(object(), _user(Role.IT_ADMIN), SUBJECT, "notifications", result,
                                 conversation_id=None, run_id=None, approval_id="b")
    assert out["truncated"] is True and out["row_count"] == 1
    ws = _workbook(rec)["Evidence"]
    assert [c.value for c in ws[3]] == ["Type", "Read at", "Message"]
    assert ws["C4"].value == "New bid - Plaza"


# ── failure handling ─────────────────────────────────────────────────────────


def test_a_failed_insert_removes_the_uploaded_object(rec, monkeypatch):
    monkeypatch.setattr(bi, "report", lambda *a: _bid_payload())
    rec.fail_insert = True
    with pytest.raises(RuntimeError):
        _build("bid_invitations", {})
    assert len(rec.uploads) == 1 and rec.deleted == [rec.uploads[0][0]]
    assert not rec.audits


def test_a_failed_audit_does_not_hide_the_report(rec, monkeypatch):
    monkeypatch.setattr(bi, "report", lambda *a: _bid_payload())

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(notifications, "audit", boom)
    out = _build("bid_invitations", {})
    assert rec.rows and out["report_id"] == rec.rows[-1][1]["id"]


def test_safe_filename():
    assert ar.safe_filename("Bid Invitations - wtd") == "Bid Invitations - wtd.xlsx"
    assert ar.safe_filename(f"a/b\\c:{EM}d?.xlsx") == "abcd.xlsx"
    assert ar.safe_filename("") == "Report.xlsx" and ar.safe_filename("...") == "Report.xlsx"
    long = ar.safe_filename("x" * 300)
    assert FILENAME_RE.match(long) and len(long) <= 120


def test_module_has_no_em_dash():
    assert EM not in Path(ar.__file__).read_text(encoding="utf-8")
    assert assistant_recipes.KEY_QUOTE_EXCLUDED == ("general material", "demolition", "lighting", "low voltage")

"""The PM and Certified Payroll tools (app/assistant/tools/pm.py and
payroll.py; docs/ASSISTANT_PHASE4_PLAN.md 3.13, 3.14).

  literals  the route templates, PM stages and report statuses equal the
            route and service side's, and every route is live;
  specs     read tools, deferred, sub-app tags, roles (billing_summary for
            the bid-amount roles only), payroll specs carry the
            payroll_aggregate retention class;
  pm        pm_status keeps contract values for viewers only; rfi_list
            strips RFI HTML, clips text and drops attachments and creator
            names; submittal_status drops recipient addresses and contact
            names; billing_summary keeps money as text;
  payroll   week arithmetic in Pacific time, the whitelist trimmers leave
            no name, wage, file name, storage path or person id, and the
            small-cell arithmetic of the route is not broken by the trim.
"""

from __future__ import annotations

import json
import typing
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

import app.main
from app.assistant import executor, registry
from app.assistant.registry import TrimContext
from app.assistant.tools import payroll, pm
from app.assistant.tools._common import BID_AMOUNT_KEYS, ToolArgumentError
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES, Role
from app.models.schemas import CpPayrollStatus
from app.services import pm_workflow

EM_DASH = chr(0x2014)
PID = "77777777-7777-4777-8777-777777777777"
RID = "88888888-8888-4888-8888-888888888888"
FLAGS = {"bidding": True, "pm": True, "certified_payroll": True, "ai_estimating": True}
PM_TOOLS = ("pm_status", "rfi_list", "submittal_status", "billing_summary")
CP_TOOLS = ("cp_week_summary", "cp_report_status")
ALL = PM_TOOLS + CP_TOOLS


def _spec(name):
    return registry.all_tools()[name]


def _ctx(fmt="concise", limit=20, role=Role.EXECUTIVE) -> TrimContext:
    return TrimContext(response_format=fmt, limit=limit, short=lambda v: str(v)[:8], role=role)


def _keys(value):
    found = set()
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(str(k))
            found |= _keys(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found |= _keys(v)
    return found


def _route_table() -> set[tuple[str, str]]:
    return {(m, r.path_format) for r in app.main.app.routes if isinstance(r, APIRoute) for m in (r.methods or ())}


# ── literals and specs ───────────────────────────────────────────────────────


def test_every_route_is_live():
    table = _route_table()
    for name in ALL:
        for route in _spec(name).routes:
            assert route in table, (name, route)


def test_literals_equal_the_other_side():
    assert pm.PM_STAGES == tuple(pm_workflow.PM_STAGES)
    assert payroll.REPORT_STATUSES == typing.get_args(CpPayrollStatus)
    assert pm.OPEN_RFIS_LIMIT == 200


def test_specs_shape():
    for name in ALL:
        spec = _spec(name)
        assert spec.effect == "read" and not spec.confirm and spec.write is None, name
        assert spec.deferred and spec.keywords, name
        assert all(r[0] == "GET" for r in spec.routes), name
        assert "response_format" in spec.schema["properties"] and "limit" in spec.schema["properties"], name
        assert EM_DASH not in json.dumps(spec.definition()), name
    for name in PM_TOOLS:
        assert _spec(name).namespace == "pm" and _spec(name).sub_app == "pm"
        assert _spec(name).retention_class == "standard"
    for name in CP_TOOLS:
        spec = _spec(name)
        assert spec.namespace == "payroll" and spec.sub_app == "certified_payroll"
        assert spec.retention_class == "payroll_aggregate"
        assert executor.RETENTION_DAYS[spec.retention_class] == 7
    assert _spec("billing_summary").roles == ACTUAL_BID_VIEWER_ROLES
    for name in PM_TOOLS[:3] + CP_TOOLS:
        assert _spec(name).roles == INTERNAL_ROLES, name


def test_rfi_and_submittal_text_is_tainted():
    assert {"subject", "question", "answer", "received_from", "responsible_contractor", "text"} <= set(
        _spec("rfi_list").taint_fields)
    assert {"response_notes", "message", "error", "vendor_name"} <= set(_spec("submittal_status").taint_fields)


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES))
def test_toolsets_follow_role_and_sub_app_flags(role):
    user = SimpleNamespace(id="u", role=role, is_active=True)
    names = {s.name for s in registry.toolset_for(user, FLAGS)}
    assert {"pm_status", "rfi_list", "submittal_status", "cp_week_summary", "cp_report_status"} <= names
    assert ("billing_summary" in names) == (role in ACTUAL_BID_VIEWER_ROLES)
    no_pm = {s.name for s in registry.toolset_for(user, {**FLAGS, "pm": False})}
    assert not no_pm & set(PM_TOOLS)
    no_cp = {s.name for s in registry.toolset_for(user, {**FLAGS, "certified_payroll": False})}
    assert not no_cp & set(CP_TOOLS)


def test_estimator_gets_none():
    user = SimpleNamespace(id="u", role=Role.ESTIMATOR, is_active=True)
    assert registry.toolset_for(user, FLAGS) == []


@pytest.mark.parametrize("name", ALL)
def test_request_builders_stay_inside_declared_routes(name):
    spec = _spec(name)
    args = {"project_id": PID, "pm_stage": None, "include_completed": None, "week": None, "status": None,
            "response_format": "concise", "limit": 5}
    for req in spec.requests(args):
        assert req.route in spec.routes and "{" not in req.path and req.path.startswith("/")


# ── pm_status ────────────────────────────────────────────────────────────────


def _pm_project(**over) -> dict:
    base = {
        "id": PID, "number": "26.8.7001", "name": "ZZ PM Job", "pm_stage": "active_construction", "status": "won",
        "pm_completed_at": None,
        "pm_details": {
            "customer_name": "ZZ GC", "original_contract_value": "250000.00", "awarded_at": "2026-08-01",
            "ntp_date": "2026-08-10", "planned_start_date": "2026-09-01", "planned_finish_date": "2027-01-15",
            "actual_start_date": "2026-09-03", "actual_finish_date": None, "superintendent_name": "Sam Super",
            "contract_number": "C-77", "retainage_percent": "5.00", "notes": "watch the gear lead time " * 30,
        },
    }
    base.update(over)
    return base


def test_pm_status_requests():
    spec = _spec("pm_status")
    (req,) = spec.requests({"project_id": None, "pm_stage": "closeout", "include_completed": True})
    assert req.route == pm.PM_LIST_GET and req.params == {"pm_stage": "closeout", "include_completed": "true"}
    (req,) = spec.requests({"project_id": None, "pm_stage": None, "include_completed": False})
    assert req.params == {}
    one, events = spec.requests({"project_id": PID, "pm_stage": "closeout"})
    assert one.route == pm.PM_PROJECT_GET and one.path == f"/pm/projects/{PID}" and not one.optional
    assert events.route == pm.PM_STAGE_EVENTS_GET and events.optional
    with pytest.raises(ToolArgumentError):
        spec.requests({"project_id": None, "pm_stage": "weird"})
    with pytest.raises(ToolArgumentError):
        spec.requests({"project_id": "nope"})


def test_pm_status_list_hides_contract_values_from_non_viewers():
    spec = _spec("pm_status")
    rows = [_pm_project(), _pm_project(id=RID, number="26.8.7002")]
    for role in set(Role) - set(ACTUAL_BID_VIEWER_ROLES):
        out = spec.response_trimmer([rows], {}, _ctx(role=role))
        assert not _keys(out) & BID_AMOUNT_KEYS, role
        assert out["projects"][0]["number"] == "26.8.7001" and out["projects"][0]["customer"] == "ZZ GC"
    out = spec.response_trimmer([rows], {}, _ctx(role=Role.EXECUTIVE))
    assert out["projects"][0]["original_contract_value"] == "250000.00"
    assert out["total"] == 2 and out["projects"][0]["id"] == PID[:8]
    assert "notes" not in out["projects"][0] and "superintendent" not in out["projects"][0]   # concise


def test_pm_status_single_project_has_details_events_and_headline():
    spec = _spec("pm_status")
    project = _pm_project(financials_headline={"original_contract_value": "250000.00", "approved_change_total": "5000.00",
                                               "current_contract_value": "255000.00"})
    events = [{"from_stage": "precon", "to_stage": "active_construction", "entered_at": "2026-09-03T17:00:00+00:00",
               "note": "NTP received"}]
    out = spec.response_trimmer([project, events], {}, _ctx())
    assert out["project"]["current_contract_value"] == "255000.00"
    assert out["project"]["superintendent"] == "Sam Super" and len(out["project"]["notes"]) <= 400
    assert out["stage_events"] == [{"from": "precon", "to": "active_construction", "at": "2026-09-03 10:00 AM PT",
                                    "note": "NTP received"}]
    hidden = spec.response_trimmer([project, events], {}, _ctx(role=Role.ESTIMATING_ENGINEER_LABOR))
    assert not _keys(hidden) & BID_AMOUNT_KEYS
    assert spec.response_trimmer([project, None], {}, _ctx())["project"]["number"] == "26.8.7001"


def test_pm_status_list_respects_limit_and_hints():
    rows = [_pm_project(id=f"{i:08d}-0000-4000-8000-000000000000", number=f"26.8.{7000 + i}") for i in range(5)]
    out = _spec("pm_status").response_trimmer([rows], {}, _ctx(limit=2))
    assert len(out["projects"]) == 2 and out["total"] == 5 and "2 of 5 shown" in out["hint"]
    assert _spec("pm_status").response_trimmer([[]], {}, _ctx())["projects"] == []


# ── rfi_list ─────────────────────────────────────────────────────────────────


def _open_rfis() -> dict:
    def rfi(i, **over):
        base = {"id": f"{i:08d}-0000-4000-8000-000000000000", "number": i, "subject": f"Panel schedule {i}",
                "status": "open", "ball_in_court": "GC", "due": "2026-10-20", "sent_at": "2026-10-10",
                "age_days": 14, "overdue": True, "source": "procore", "read_only": True}
        base.update(over)
        return base

    return {"projects": [
        {"project": {"id": PID, "number": "26.8.7001", "name": "ZZ PM Job"}, "rfis": [rfi(1), rfi(2, overdue=False, source="internal")]},
        {"project": {"id": RID, "number": "26.8.7002", "name": "ZZ Other"}, "rfis": [rfi(3)]},
    ], "truncated": False}


def test_rfi_requests():
    spec = _spec("rfi_list")
    (req,) = spec.requests({"project_id": None})
    assert req.route == pm.RFIS_OPEN_GET and req.params == {"limit": 200}
    rfis, link = spec.requests({"project_id": PID})
    assert rfis.route == pm.RFIS_GET and rfis.path == f"/pm/projects/{PID}/rfis" and not rfis.optional
    assert link.route == pm.PM_LINK_GET and link.optional    # the Procore link route may be switched off


def test_open_rfis_group_by_project_and_cap_by_limit():
    spec = _spec("rfi_list")
    out = spec.response_trimmer([_open_rfis()], {}, _ctx())
    assert [g["project"]["number"] for g in out["projects"]] == ["26.8.7001", "26.8.7002"]
    first = out["projects"][0]["rfis"][0]
    assert first["subject"] == "Panel schedule 1" and first["overdue"] is True and first["source"] == "procore"
    assert "source" not in out["projects"][0]["rfis"][1] and out["open_rfis"] == 3
    capped = spec.response_trimmer([_open_rfis()], {}, _ctx(limit=2))
    assert sum(len(g["rfis"]) for g in capped["projects"]) == 2 and "2 of 3 shown" in capped["hint"]


def test_rfi_rows_never_look_like_project_citations():
    """Live test 2026-10-24: an RFI row carrying {id, number} was harvested by
    the executor as a project, so "Const-153" became a citation block whose
    link opened /projects/<rfi id>. RFI rows name the key rfi_number."""
    spec = _spec("rfi_list")
    ids = {"00000003": "00000003-0000-4000-8000-000000000000", PID[:8]: PID, RID[:8]: RID}
    class Harvester:
        _harvest_projects = executor.Executor._harvest_projects

        def __init__(self):
            self.ids, self.projects = ids, {}

    harvester = Harvester()
    harvest = harvester._harvest_projects
    open_out = spec.response_trimmer([_open_rfis()], {}, _ctx())
    harvest(open_out)
    assert harvester.projects == {"26.8.7001": PID, "26.8.7002": RID}
    assert open_out["projects"][1]["rfis"][0]["rfi_number"] == 3
    one = spec.response_trimmer([[_rfi_row(rfi_number=None, external_number="Const-153")], None], {}, _ctx())
    harvester.projects = {}
    harvest(one)
    assert harvester.projects == {} and one["rfis"][0]["rfi_number"] == "Const-153"


def test_strip_html():
    assert pm.strip_html("<p>Is the panel <b>LP-1</b> 208V?<br>Please confirm &amp; reply.</p>") == (
        "Is the panel LP-1 208V? Please confirm & reply.")
    assert pm.strip_html("<p>One</p><p>Two</p>") == "One Two"
    assert pm.strip_html("<ul><li>A</li><li>B</li></ul>") == "A B"
    assert pm.strip_html("<p></p>") is None and pm.strip_html(None) is None and pm.strip_html("   ") is None
    assert "<" not in (pm.strip_html("<script>alert(1)</script>x") or "<")


def _rfi_row(**over) -> dict:
    base = {
        "id": RID, "project_id": PID, "rfi_number": 7, "external_number": None, "subject": "Feeder size at MDP",
        "question": "<p>" + "Which feeder? " * 60 + "</p>", "answer": "<p>Use <b>4 inch</b> conduit.</p>",
        "status": "open", "priority": "urgent", "ball_in_court": "Architect", "due_at": "2026-10-30",
        "sent_at": "2026-10-10", "answered_at": None, "assigned_gc_name": "ZZ GC", "created_by_name": "Pat Creator",
        "last_sent_by_name": "Lee Sender", "source": "procore", "responsible_contractor": "ZZ Electric",
        "received_from": "Dana GC", "attachments": [{"filename": "x.pdf", "folder": "RFIs"}],
        "answer_attachments": [], "last_send": {"to": "someone@gc.test"},
    }
    base.update(over)
    return base


def test_project_rfis_are_plain_text_and_drop_attachments_and_people():
    spec = _spec("rfi_list")
    link = {"platform": "procore", "last_synced_at": "2026-10-23T15:00:00+00:00", "last_sync_status": "ok",
            "last_error": None, "external_url": "https://procore.test/p/1"}
    out = spec.response_trimmer([[_rfi_row()], link], {}, _ctx())
    (row,) = out["rfis"]
    assert row["rfi_number"] == 7 and row["priority"] == "urgent" and row["source"] == "procore"
    assert row["answer"] == "Use 4 inch conduit." and len(row["question"]) <= 200 and "<" not in row["question"]
    assert out["procore_link"] == {"platform": "procore", "synced": "2026-10-23 8:00 AM PT", "sync_status": "ok"}
    dumped = json.dumps(out)
    for forbidden in ("Pat Creator", "Lee Sender", "someone@gc.test", "x.pdf", "https://procore.test"):
        assert forbidden not in dumped
    assert "responsible_contractor" not in row   # concise
    detailed = spec.response_trimmer([[_rfi_row()], None], {}, _ctx("detailed"))["rfis"][0]
    assert detailed["responsible_contractor"] == "ZZ Electric" and detailed["received_from"] == "Dana GC"
    assert len(detailed["question"]) <= 400 and detailed["question"].endswith("[cut]")
    assert "procore_link" not in spec.response_trimmer([[_rfi_row()], None], {}, _ctx())


def test_imported_rfi_uses_the_external_number():
    row = _rfi_row(rfi_number=None, external_number="EXT-12", source="procore", due_at=None, external_due_date="2026-11-01")
    out = _spec("rfi_list").response_trimmer([[row]], {}, _ctx())
    assert out["rfis"][0]["rfi_number"] == "EXT-12" and out["rfis"][0]["due"] == "2026-11-01"


# ── submittal_status ─────────────────────────────────────────────────────────


def _submittal_payloads():
    coverage = {"materials": [{"pm_material_id": PID, "requested": True, "request_count": 2,
                               "last_requested_at": "2026-10-10T17:00:00+00:00"}],
                "adhoc_items": [{"category_label": "Lighting", "description": "x"}]}
    packages = [{
        "id": RID, "number": 3, "gc_id": PID, "recipients": ["gc@gc.test"], "cc_recipients": ["cc@gc.test"],
        "subject": "Submittals for 26.8.7001", "message": "Please review", "send_status": "sent", "error": None,
        "sent_at": "2026-10-11T17:00:00+00:00", "sent_by": PID, "approval_status": "approved",
        "responded_at": "2026-10-13T17:00:00+00:00", "responded_by": PID, "response_notes": "Approved as noted",
        "submittal_package_items": [{"id": PID, "category_label": "Panelboards", "filename": "panel.pdf",
                                     "approval_status": "approved", "response_notes": "ok"}],
    }]
    requests = [{
        "id": PID, "status": "sent", "created_at": "2026-10-09T17:00:00+00:00", "created_by": PID,
        "submittal_request_items": [{"id": "i1"}, {"id": "i2"}],
        "submittal_request_sends": [
            {"id": "s1", "status": "sent", "response_received_at": "2026-10-10T17:00:00+00:00", "error": None,
             "vendor_contacts": {"name": "Vera Vendor", "email": "vera@vendor.test", "vendors": {"name": "ZZ Supply"}}},
            {"id": "s2", "status": "failed", "response_received_at": None, "error": "mailbox full",
             "vendor_contacts": {"name": "Ned Vendor", "email": "ned@vendor.test", "vendors": {"name": "ZZ Gear"}}},
        ],
    }]
    return [coverage, packages, requests]


def test_submittal_requests_and_optional_history():
    reqs = _spec("submittal_status").requests({"project_id": PID})
    assert [r.route for r in reqs] == [pm.COVERAGE_GET, pm.PACKAGES_GET, pm.REQUESTS_GET]
    assert [r.optional for r in reqs] == [False, True, True]
    assert reqs[1].path == f"/pm/projects/{PID}/submittals/approval/packages"


def test_submittal_trim_drops_addresses_and_contact_names():
    out = _spec("submittal_status").response_trimmer(_submittal_payloads(), {}, _ctx("detailed"))
    dumped = json.dumps(out)
    for forbidden in ("gc@gc.test", "cc@gc.test", "vera@vendor.test", "ned@vendor.test", "Vera Vendor", "Ned Vendor"):
        assert forbidden not in dumped
    assert out["materials_requested"] == 1 and out["adhoc_items"] == 1
    (package,) = out["packages"]
    assert package["approval_status"] == "approved" and package["response_notes"] == "Approved as noted"
    assert package["files"][0]["filename"] == "panel.pdf" and package["file_count"] == 1
    (request,) = out["requests"]
    assert request["sends"] == 2 and request["answered"] == 1 and request["items"] == 2
    assert request["failed"] == [{"vendor_name": "ZZ Gear", "error": "mailbox full"}]
    concise = _spec("submittal_status").response_trimmer(_submittal_payloads(), {}, _ctx())
    assert "files" not in concise["packages"][0] and "materials_detail" not in concise


def test_submittal_trim_tolerates_missing_optional_reads():
    out = _spec("submittal_status").response_trimmer([{"materials": [], "adhoc_items": []}, None, None], {}, _ctx())
    assert out["materials_requested"] == 0 and out["packages"] == [] and out["requests"] == []


# ── billing_summary ──────────────────────────────────────────────────────────


def _billing_row(**over):
    base = {"project": {"id": PID, "number": "26.8.7001", "name": "ZZ PM Job"},
            "original_contract_value": "250000.00", "approved_change_total": "5000.00",
            "current_contract_value": "255000.00", "billed_to_date": "100000.00", "retainage_held": "5000.00",
            "balance_to_finish": "155000.00", "open_change_order_count": 1, "pay_app_count": 3}
    base.update(over)
    return base


def test_billing_requests():
    spec = _spec("billing_summary")
    (req,) = spec.requests({"project_id": None})
    assert req.route == pm.BILLING_GET
    (req,) = spec.requests({"project_id": PID})
    assert req.route == pm.FINANCIALS_GET and req.path == f"/pm/projects/{PID}/financials"


def test_billing_trim_keeps_money_as_text_for_viewers_and_strips_for_anyone_else():
    spec = _spec("billing_summary")
    out = spec.response_trimmer([{"projects": [_billing_row()]}], {}, _ctx())
    row = out["projects"][0]
    assert row["billed_to_date"] == "100000.00" and row["current_contract_value"] == "255000.00"
    assert row["project"]["number"] == "26.8.7001" and row["pay_app_count"] == 3
    single = spec.response_trimmer([{k: v for k, v in _billing_row().items() if k != "project"}], {}, _ctx())
    assert single["billed_to_date"] == "100000.00"
    guard = spec.response_trimmer([{"projects": [_billing_row()]}], {}, _ctx(role=Role.ESTIMATING_ENGINEER_LABOR))
    assert not _keys(guard) & BID_AMOUNT_KEYS and guard["projects"][0]["billed_to_date"] == "100000.00"
    assert spec.response_trimmer([{"projects": []}], {}, _ctx())["projects"] == []


# ── payroll: week arithmetic ─────────────────────────────────────────────────


def test_sunday_and_week_start_arithmetic():
    saturday = date(2026, 10, 24)
    assert payroll.sunday_of(saturday) == date(2026, 10, 18)
    assert payroll.sunday_of(date(2026, 10, 18)) == date(2026, 10, 18)
    assert payroll.sunday_of(date(2026, 10, 19)) == date(2026, 10, 18)
    assert payroll.week_start_for("this_week", today=saturday) == "2026-10-18"
    assert payroll.week_start_for("last_week", today=saturday) == "2026-10-11"
    assert payroll.week_start_for(None, today=saturday) == "2026-10-11"
    assert payroll.week_start_for("2026-10-21", today=saturday) == "2026-10-18"
    assert payroll.week_start_for("2026-11-01", today=saturday) == "2026-11-01"
    sunday = date(2026, 10, 25)
    assert payroll.week_start_for("this_week", today=sunday) == "2026-10-25"
    assert payroll.week_start_for("last_week", today=sunday) == "2026-10-18"
    with pytest.raises(ToolArgumentError):
        payroll.week_start_for("next_week", today=saturday)
    with pytest.raises(ToolArgumentError):
        payroll.week_start_for("2026-02-30", today=saturday)


def test_week_summary_request():
    spec = _spec("cp_week_summary")
    (req,) = spec.requests({"week": "2026-10-21"})
    assert req.route == payroll.SUMMARY_GET and req.params == {"week_start": "2026-10-18"} and req.optional
    with pytest.raises(ToolArgumentError, match="last_week, this_week"):
        spec.requests({"week": "soon"})
    (req,) = spec.requests({"week": None})
    assert date.fromisoformat(req.params["week_start"]).weekday() == 6   # a Sunday


# ── payroll: whitelist trimmers ──────────────────────────────────────────────

FORBIDDEN = ("ssn", "wage", "gross", "net", "rate", "storage_path", "filename", "employee_name", "created_by",
             "finalized_by", "submitted_by", "timesheet_storage_path", "payroll_detail_storage_path")


def _summary_body(**over) -> dict:
    base = {
        "id": RID, "week_start": "2026-10-18", "week_end": "2026-10-24", "status": "processing",
        "timesheet_uploaded": True, "payroll_detail_uploaded": False, "total_hours": "1200.50",
        "total_employees": 31, "entry_count": 410, "unmatched_employee_entries": 2, "unmatched_project_entries": 0,
        "finalize_issues": ["Payroll detail has not been uploaded", "2 entries with unmatched employees"],
        "stale_reasons": [], "finalized_at": None, "submitted_at": None, "record_count": 0, "latest_revision": None,
        "hours_by_project": [
            {"number": "26.8.7001", "name": "ZZ PM Job", "hours": "800.25", "employees": 12},
            {"number": "26.8.7002", "name": "ZZ Other", "hours": "300.00", "employees": 5},
            {"number": None, "name": "Other enrolled projects", "hours": "50.25", "employees": 4},
        ],
        "other_hours": "50.00",
        # Keys a later change to the route might add: the trimmer never forwards them.
        "ssn": "123-45-6789", "wage": "40.00", "employee_name": "Pat Worker", "timesheet_filename": "week.xlsx",
        "timesheet_storage_path": "payroll/reports/x/uploads/week.xlsx", "created_by": PID, "gross": "1", "rate": "2",
    }
    base.update(over)
    return base


def test_week_summary_is_a_whitelist_and_keeps_the_hours_arithmetic():
    out = _spec("cp_week_summary").response_trimmer([_summary_body()], {}, _ctx())
    keys = _keys(out)
    for forbidden in FORBIDDEN:
        assert forbidden not in keys, forbidden
    dumped = json.dumps(out)
    for secret in ("123-45-6789", "Pat Worker", "week.xlsx", "payroll/reports"):
        assert secret not in dumped
    assert out["id"] == RID[:8] and out["total_hours"] == "1200.50" and out["total_employees"] == 31
    assert out["status"] == "processing" and out["timesheet_uploaded"] is True
    assert out["payroll_detail_uploaded"] is False
    assert out["finalize_issues"] == ["Payroll detail has not been uploaded", "2 entries with unmatched employees"]
    assert out["stale_reasons"] == []
    # total - shown project hours (the merged row included) - other_hours is exactly zero, as the route made it.
    shown = sum(Decimal(r["hours"]) for r in out["hours_by_project"])
    assert Decimal(out["total_hours"]) - shown - Decimal(out["other_hours"]) == 0
    assert out["hint"] == payroll.SMALL_CELL_HINT


def test_week_summary_with_no_report_is_a_plain_answer():
    out = _spec("cp_week_summary").response_trimmer([None], {}, _ctx())
    assert out == {"found": False, "hint": payroll.NO_REPORT_HINT}
    assert _spec("cp_week_summary").response_trimmer([], {}, _ctx())["found"] is False


def test_week_summary_limit_cuts_project_rows_with_a_hint():
    body = _summary_body(hours_by_project=[
        {"number": f"26.8.{7000 + i}", "name": f"P{i}", "hours": "10.00", "employees": 3} for i in range(10)])
    out = _spec("cp_week_summary").response_trimmer([body], {}, _ctx(limit=4))
    assert len(out["hours_by_project"]) == 4 and "4 of 10 shown" in out["hours_by_project_hint"]


def _report_row(i=1, **over) -> dict:
    base = {
        "id": f"{i:08d}-0000-4000-8000-000000000000", "week_start_date": "2026-10-11", "week_end_date": "2026-10-17",
        "status": "processed", "timesheet_filename": "week.xlsx", "timesheet_storage_path": "payroll/x/week.xlsx",
        "payroll_detail_filename": None, "payroll_detail_storage_path": None, "total_hours": "1180.00",
        "total_employees": 30, "finalized_at": "2026-10-19T17:00:00+00:00", "submitted_at": None,
        "created_by": PID, "finalized_by": PID, "submitted_by": None, "created_at": "2026-10-12T00:00:00+00:00",
    }
    base.update(over)
    return base


def test_report_status_requests_and_filter():
    spec = _spec("cp_report_status")
    (req,) = spec.requests({"status": None})
    assert req.route == payroll.REPORTS_GET and req.params == {}
    (req,) = spec.requests({"status": "processing"})
    assert req.params == {"status": "processing"}
    with pytest.raises(ToolArgumentError):
        spec.requests({"status": "weird"})


def test_report_status_is_a_whitelist():
    out = _spec("cp_report_status").response_trimmer([[_report_row(1), _report_row(2, status="draft")]], {}, _ctx())
    keys = _keys(out)
    for forbidden in FORBIDDEN + ("created_at",):
        assert forbidden not in keys, forbidden
    first = out["reports"][0]
    assert first == {
        "id": "00000001", "week_start": "2026-10-11", "week_end": "2026-10-17", "status": "processed",
        "total_hours": "1180.00", "total_employees": 30, "finalized": "2026-10-19 10:00 AM PT",
        "timesheet_uploaded": True, "payroll_detail_uploaded": False,
    }
    assert out["total"] == 2
    assert out["reports"][1]["payroll_detail_uploaded"] is False   # a false flag is an answer
    capped = _spec("cp_report_status").response_trimmer([[_report_row(i) for i in range(1, 6)]], {}, _ctx(limit=2))
    assert len(capped["reports"]) == 2 and "2 of 5 shown" in capped["hint"]
    assert _spec("cp_report_status").response_trimmer([None], {}, _ctx())["reports"] == []

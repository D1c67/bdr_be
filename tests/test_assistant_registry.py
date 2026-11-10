"""The tool registry (app/assistant/registry.py, app/assistant/tools/*).

Invariants pinned here (docs/ASSISTANT.md, Skills and tools):

  route     a ToolSpec without a route, with a DELETE route, a read tool on
            a non-GET route or a non-strict schema is refused;
  live      every registered tool's routes name a live route of the served
            app, or of the assistant router the integration stage mounts;
  bytes     the tool definitions serialize to the same bytes for the same
            toolset whatever order the specs come in, keys sorted;
  text      no em dash in any model-facing string; every read tool takes
            response_format, every list tool takes limit;
  requests  every tool builds requests only for the routes it declares;
  phase 1   14 offered tools (the 6 phase 0 reads, list_todos,
            list_gc_contacts and the 6 confirm writes) plus the 2 hidden
            revert specs (docs/ASSISTANT_PHASE1_PLAN.md 3.7). The write
            tools themselves are tested in tests/test_assistant_write_tools.py.
  phase 2   17 offered: splitter_status, split_files_into_project and
            attach_split_job_to_project join them
            (docs/ASSISTANT_PHASE2_PLAN.md 3.10; tested in
            tests/test_assistant_split_tools.py).
  phase 3   28 offered: schedule_list, schedule_preview and memory_list
            (reads), schedule_create (confirm), and schedule_pause,
            schedule_resume, schedule_end, schedule_run_now, memory_save,
            memory_update and memory_forget (housekeeping) join them
            (docs/ASSISTANT_PHASE3_PLAN.md 3.6 and 3.8; tested in
            tests/test_assistant_schedule_tools.py and
            tests/test_assistant_memory_tools.py).
  phase 4   55 offered: 27 new tools (docs/ASSISTANT_PHASE4_PLAN.md 3.17),
            25 of them deferred (find_tools and memory_search are always
            offered). Reports, evidence, memory_search and find_tools are
            tested in tests/test_assistant_report_tools.py,
            tests/test_assistant_evidence_tools.py and
            tests/test_assistant_memory_search_tool.py.
"""

from __future__ import annotations

import json

import pytest
from fastapi.routing import APIRoute

import app.main
from app.assistant import registry
from app.assistant.registry import RegistryError, ToolRequest, ToolSpec, schema
from app.assistant.tools._common import ToolArgumentError
from app.core.roles import INTERNAL_ROLES, Role
from app.routers import assistant as assistant_router

EM_DASH = chr(0x2014)


def _spec(**over) -> ToolSpec:
    base = dict(
        name="probe_tool", namespace="core", sub_app=None, route=("GET", "/features"),
        effect="read", confirm=False, schema=schema({"response_format": registry.RESPONSE_FORMAT_PROP}),
        response_trimmer=lambda payloads, args, ctx: payloads[0], progress_label="Probing",
        roles=INTERNAL_ROLES, description="A probe.",
        requests=lambda args: [ToolRequest(route=("GET", "/features"), path="/features")],
    )
    base.update(over)
    return ToolSpec(**base)


def _route_table() -> set[tuple[str, str]]:
    table = set()
    for source in (app.main.app.routes, assistant_router.router.routes):
        for route in source:
            if isinstance(route, APIRoute):
                for method in route.methods or ():
                    table.add((method, route.path_format))
    return table


# ── validation ───────────────────────────────────────────────────────────────


def test_registry_refuses_a_spec_without_a_route():
    with pytest.raises(RegistryError):
        registry.validate(_spec(route=None))
    with pytest.raises(RegistryError):
        registry.validate(_spec(route=("GET", "")))
    with pytest.raises(RegistryError):
        registry.validate(_spec(route=("GET", "projects")))  # not a template


def test_registry_refuses_delete_and_non_get_reads():
    with pytest.raises(RegistryError):
        registry.validate(_spec(route=("DELETE", "/todos/{todo_id}")))
    with pytest.raises(RegistryError):
        registry.validate(_spec(route=("POST", "/projects")))
    with pytest.raises(RegistryError):
        registry.validate(_spec(extra_routes=(("PATCH", "/projects/{project_id}"),)))


def test_registry_refuses_loose_schemas_and_bad_shapes():
    loose = {"type": "object", "properties": {"q": {"type": "string"}}, "required": [], "additionalProperties": False}
    with pytest.raises(RegistryError):
        registry.validate(_spec(schema=loose))
    with pytest.raises(RegistryError):
        registry.validate(_spec(schema={"type": "object", "properties": {}, "required": []}))
    with pytest.raises(RegistryError):
        registry.validate(_spec(effect="write_internal", confirm=False))
    with pytest.raises(RegistryError):
        registry.validate(_spec(effect="read", confirm=True))
    with pytest.raises(RegistryError):
        registry.validate(_spec(roles=frozenset({Role.ESTIMATOR})))
    with pytest.raises(RegistryError):
        registry.validate(_spec(description=f"a {EM_DASH} b"))
    with pytest.raises(RegistryError):
        registry.validate(_spec(name="Bad-Name"))


def test_validate_normalizes_method_case():
    spec = registry.validate(_spec(route=("get", "/features")))
    assert spec.route == ("GET", "/features")


# ── the registered set ───────────────────────────────────────────────────────


PHASE_ZERO_READS = {"app_search", "get_project_context", "list_teammates", "load_skill", "whats_due", "get_labor"}
PHASE_ONE_READS = {"list_todos", "list_gc_contacts"}
PHASE_ONE_WRITES = {
    "set_labor_numbers", "update_project_fields", "add_project_gc", "create_project_draft",
    "todo_create", "todo_complete",
}
HIDDEN = {"restore_labor_numbers", "todo_reopen"}
PHASE_TWO_READS = {"splitter_status"}
PHASE_TWO_CONFIRMS = {"split_files_into_project", "attach_split_job_to_project"}
PHASE_THREE_READS = {"schedule_list", "schedule_preview", "memory_list"}
PHASE_THREE_CONFIRMS = {"schedule_create"}
PHASE_THREE_HOUSEKEEPING = {
    "schedule_pause", "schedule_resume", "schedule_end", "schedule_run_now",
    "memory_save", "memory_update", "memory_forget",
}


# Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.17): 27 tools, two always offered
# (find_tools, memory_search) and 25 deferred.
PHASE_FOUR_ALWAYS = {"find_tools", "memory_search"}
PHASE_FOUR_PLANS = {"plans_list_runs", "plans_get_brief", "plans_list_documents", "plans_search_notes",
                    "plans_get_output", "plans_read_pages", "run_ai_estimating"}
PHASE_FOUR_PM = {"pm_status", "rfi_list", "submittal_status", "billing_summary", "cp_week_summary",
                 "cp_report_status"}
PHASE_FOUR_REPORTS = {"quotes_summary", "run_report", "coded_report", "build_report"}
PHASE_FOUR_EVIDENCE = {"project_timeline", "file_access_report", "change_history", "ai_usage",
                       "notification_history", "estimator_timeline", "person_evidence"}
PHASE_FOUR_DEFERRED = (PHASE_FOUR_PLANS | PHASE_FOUR_PM | PHASE_FOUR_REPORTS | PHASE_FOUR_EVIDENCE
                       | {"advance_lane"})


PHASE_FIVE_DEFERRED = {"email_search", "email_read", "email_attachment_to_staging", "email_draft", "read_mailbox", "web_search", "web_fetch"}
# Browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 4.3) replaced the two Phase 6 tools.
PHASE_SIX_DEFERRED = {"browser_task_start", "browser_task_status", "browser_task_cancel", "browser_sites_list",
                      "request_site_access", "request_site_credential"}
# Phase 8 (docs/ASSISTANT_PHASE8_PLAN.md 6.1): the one deferred inbox read tool.
PHASE_EIGHT_DEFERRED = {"inbox_list"}


def test_registered_set_is_pinned():
    tools = registry.all_tools()
    offered_names = (PHASE_ZERO_READS | PHASE_ONE_READS | PHASE_ONE_WRITES | PHASE_TWO_READS | PHASE_TWO_CONFIRMS
                     | PHASE_THREE_READS | PHASE_THREE_CONFIRMS | PHASE_THREE_HOUSEKEEPING
                     | PHASE_FOUR_ALWAYS | PHASE_FOUR_DEFERRED | PHASE_FIVE_DEFERRED | PHASE_SIX_DEFERRED
                     | PHASE_EIGHT_DEFERRED)
    assert set(tools) == offered_names | HIDDEN
    offered = {name for name, spec in tools.items() if spec.offered}
    assert offered == offered_names and len(offered) == 69
    assert {name for name, spec in tools.items() if not spec.offered} == HIDDEN


def test_phase_four_deferral_is_pinned():
    tools = registry.all_tools()
    assert len(PHASE_FOUR_DEFERRED) == 25
    assert {name for name, spec in tools.items() if spec.deferred} == (
        PHASE_FOUR_DEFERRED | PHASE_FIVE_DEFERRED | PHASE_SIX_DEFERRED | PHASE_EIGHT_DEFERRED
    )
    for name in PHASE_FOUR_ALWAYS:
        assert not tools[name].deferred and tools[name].roles == INTERNAL_ROLES
    assert tools["find_tools"].namespace == "core" and tools["memory_search"].namespace == "memory"
    for name in PHASE_FOUR_DEFERRED:
        assert tools[name].keywords, name       # the find resolver needs words to score


def test_read_tools_are_read_only():
    tools = registry.all_tools()
    for name in PHASE_ZERO_READS | PHASE_ONE_READS | PHASE_TWO_READS | PHASE_THREE_READS:
        spec = tools[name]
        assert spec.effect == "read" and not spec.confirm and spec.write is None
        assert all(r[0] == "GET" for r in spec.routes)
    assert tools["whats_due"].sub_app == "bidding" and tools["get_labor"].sub_app == "bidding"
    assert tools["app_search"].sub_app is None and tools["get_project_context"].sub_app is None
    assert tools["list_todos"].sub_app is None and tools["list_gc_contacts"].sub_app is None
    assert tools["splitter_status"].sub_app == "bidding"
    assert tools["schedule_list"].sub_app is None and tools["memory_list"].sub_app is None
    assert tools["schedule_preview"].sub_app == "bidding"


def test_write_tools_confirm_and_carry_a_write_spec():
    tools = registry.all_tools()
    for name in PHASE_ONE_WRITES | HIDDEN:
        spec = tools[name]
        assert spec.effect == "write_internal" and spec.confirm and spec.write is not None, name
        assert spec.route[0] != "GET", name
        if spec.write.revert_tool:
            assert spec.write.revert_tool in tools, name
    assert tools["split_files_into_project"].effect == "expensive"
    assert tools["attach_split_job_to_project"].effect == "write_internal"
    for name in PHASE_TWO_CONFIRMS | PHASE_THREE_CONFIRMS:
        spec = tools[name]
        assert spec.confirm and spec.write is not None and spec.route[0] == "POST", name
        assert spec.write.revert_tool is None, name


def test_housekeeping_tools_run_inline_on_own_rows():
    tools = registry.all_tools()
    for name in PHASE_THREE_HOUSEKEEPING:
        spec = tools[name]
        assert spec.effect == registry.EFFECT_HOUSEKEEPING and not spec.confirm and spec.write is None, name
        assert spec.namespace in registry.OWN_ROW_NAMESPACES and spec.route[0] == "POST", name
        assert spec.route[1].startswith("/assistant/tools/"), name
        assert spec.roles == INTERNAL_ROLES, name
    assert tools["schedule_create"].namespace == "schedule" and tools["schedule_create"].roles == INTERNAL_ROLES


def test_every_registered_route_is_live():
    table = _route_table()
    missing = [(name, r) for name, spec in registry.all_tools().items() for r in spec.routes if r not in table]
    assert missing == [], f"tool routes that match no route: {missing}"


def test_every_tool_takes_response_format_and_no_em_dash():
    for spec in registry.all_tools().values():
        props = spec.schema["properties"]
        # A confirm tool's output is the card, not a trimmed read.
        if spec.name != "load_skill" and spec.effect == "read":
            assert "response_format" in props, spec.name
        assert set(spec.schema["required"]) == set(props)
        assert spec.schema["additionalProperties"] is False
        assert EM_DASH not in json.dumps(spec.definition())
    assert "limit" in registry.all_tools()["app_search"].schema["properties"]
    assert "limit" in registry.all_tools()["whats_due"].schema["properties"]
    assert "limit" in registry.all_tools()["list_todos"].schema["properties"]
    assert "limit" in registry.all_tools()["list_gc_contacts"].schema["properties"]


def test_definitions_are_byte_stable_and_sorted():
    specs = list(registry.all_tools().values())
    a = registry.definitions_bytes(specs)
    b = registry.definitions_bytes(list(reversed(specs)))
    assert a == b
    names = [d["name"] for d in registry.tool_definitions(specs)]
    assert names == sorted(names)
    for d in registry.tool_definitions(specs):
        assert list(d) == sorted(d)
        assert d["type"] == "function" and d["strict"] is True
        assert list(d["parameters"]) == sorted(d["parameters"])


def test_allowlist_collects_every_route_including_extras():
    pairs = registry.allowlist_for(registry.all_tools().values())
    assert ("GET", "/projects/{project_id}/stage-events") in pairs
    assert ("GET", "/projects/today") in pairs
    assert ("GET", "/projects/due-checks") in pairs
    assert not any(m == "DELETE" for m, _ in pairs)


def test_request_builders_stay_inside_declared_routes():
    tools = registry.all_tools()
    args = {"q": "alpha", "kind": None, "response_format": "concise", "limit": 5,
            "project_id": "77777777-7777-4777-8777-777777777777", "name": "bdr-glossary", "scope": "today",
            "gc_id": "88888888-8888-4888-8888-888888888888",
            "schedule_id": "99999999-9999-4999-8999-999999999999", "recipe": "due_today",
            # Phase 4: the plans reads take a run and a document.
            "run_id": "66666666-6666-4666-8666-666666666666",
            "document_id": "55555555-5555-4555-8555-555555555555"}
    # Read tools here; the write tools' preview requests are covered per
    # tool in tests/test_assistant_write_tools.py.
    # Per-tool arguments for the phase 4 reads that need their own words.
    own = {
        "find_tools": {"query": "drawings sheet notes"},
        "web_fetch": {"url": "https://example.com"},
        "browser_task_status": {"task_id": "44444444-4444-4444-8444-444444444444"},
        "email_search": {"mailbox": "owner@example.test", "query": "subject", "window_days": 30},
        "email_read": {"mailbox": "owner@example.test", "message_id": "graph-id"},
        "memory_search": {"q": "bid reminders"},
        "run_report": {"report": "overview"},
        "coded_report": {"recipe": "quote_readiness"},
        "plans_get_output": {"category": "main", "section": "brief"},
        "inbox_list": {"segment": "all", "kind": "bell.verify", "project": "26.10.7301",
                       "before": "2026-11-02T10:00:00.000000Z|55555555-5555-4555-8555-555555555555"},
    }
    for spec in tools.values():
        if spec.effect != "read":
            continue
        for req in spec.requests({**args, **own.get(spec.name, {})}):
            assert req.route in spec.routes, (spec.name, req.route)
            assert req.path.startswith("/")
            assert "{" not in req.path


def test_request_builders_refuse_bad_ids_and_names():
    tools = registry.all_tools()
    with pytest.raises(ToolArgumentError):
        tools["get_project_context"].requests({"project_id": "not-a-uuid", "response_format": "concise"})
    with pytest.raises(ToolArgumentError):
        tools["get_labor"].requests({"project_id": "", "response_format": "concise", "limit": None})
    with pytest.raises(ToolArgumentError):
        tools["load_skill"].requests({"name": "../etc"})
    with pytest.raises(ToolArgumentError):
        tools["app_search"].requests({"q": "   ", "kind": None, "response_format": "concise", "limit": None})


def test_common_args_default_and_clamp():
    assert registry.common_args({}) == ("concise", registry.DEFAULT_LIMIT)
    assert registry.common_args({"response_format": "detailed", "limit": 999}) == ("detailed", registry.MAX_LIMIT)
    assert registry.common_args({"response_format": "weird", "limit": "x"}) == ("concise", registry.DEFAULT_LIMIT)
    assert registry.common_args({"limit": 0}) == ("concise", 1)

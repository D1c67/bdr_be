"""The phase 4 registry rules (app/assistant/registry.py;
docs/ASSISTANT_PHASE4_PLAN.md 3.1).

Invariants pinned here:

  sets       SUB_APPS gains ai_estimating, NAMESPACES plans, evidence and
             report, OWN_ROW_NAMESPACES report, WRITE_SPEC_EFFECTS
             sensitive_read;
  sensitive  a sensitive_read spec has a WriteSpec, a POST primary route
             under /analytics/evidence/, roles inside EVIDENCE_ROLES, a
             required string `reason` it binds as text, and a
             result_summary;
  fields     retention_class is standard or payroll_aggregate, and
             payroll_aggregate exactly in the payroll namespace; deferred is
             a bool and never in core, memory or schedule; keywords are a
             tuple of lowercase ASCII words;
  persons    only a sensitive_read spec may name one person (actor_id,
             user_id, file_id) on the per-person routes, statically in its
             route templates and in every request it sends;
  toolset    a plans tool follows the ai_estimating flag; Mailbox review
             drops the report namespace, Incognito keeps it;
  unattended only app/assistant/jobs.py calls loop.run_turn.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.assistant import registry
from app.assistant.registry import (
    EFFECT_HOUSEKEEPING,
    EFFECT_SENSITIVE_READ,
    ProposalRefused,
    RegistryError,
    ToolRequest,
    ToolSpec,
    WriteSpec,
    nullable,
    per_person_violation,
    schema,
)
from app.core.roles import EVIDENCE_ROLES, INTERNAL_ROLES, WRITER_ROLES, Role

APP = Path(__file__).resolve().parents[1] / "app"
POST = ("POST", "/analytics/evidence/users/{user_id}")
SUBJECT_GET = ("GET", "/analytics/evidence/subjects/users/{user_id}")
FLAGS = {"bidding": True, "pm": True, "certified_payroll": True, "ai_estimating": True}


def _refuse(args, reads, ctx):
    raise ProposalRefused("probe")


def _write(**over) -> WriteSpec:
    base = dict(preview_requests=lambda a: [], build=_refuse, snapshot=lambda a, r: ({}, {}),
                bound_text_fields=("reason",), result_summary=lambda p: {"row_count": 0})
    base.update(over)
    return WriteSpec(**base)


def _sensitive(**over) -> ToolSpec:
    base = dict(
        name="person_probe", namespace="evidence", sub_app="bidding", route=POST, extra_routes=(SUBJECT_GET,),
        effect=EFFECT_SENSITIVE_READ, confirm=True,
        schema=schema({"user_id": {"type": "string"}, "reason": {"type": "string"}}),
        response_trimmer=lambda p, a, c: p, progress_label="Reading evidence", roles=EVIDENCE_ROLES,
        description="probe", requests=lambda a: [], write=_write(), deferred=True,
        keywords=("evidence", "activity", "who"),
    )
    base.update(over)
    return ToolSpec(**base)


def _read(**over) -> ToolSpec:
    route = over.pop("route", ("GET", "/analytics/evidence/projects/{project_id}/timeline"))
    base = dict(
        name="timeline_probe", namespace="evidence", sub_app="bidding", route=route, effect="read",
        confirm=False, schema=schema({"project_id": {"type": "string"}}), response_trimmer=lambda p, a, c: p,
        progress_label="Reading the timeline", roles=EVIDENCE_ROLES, description="probe",
        requests=lambda a: [ToolRequest(route=route, path=route[1])],
    )
    base.update(over)
    return ToolSpec(**base)


def _user(role: Role):
    return SimpleNamespace(id="u", role=role, is_active=True)


# ── sets and defaults ────────────────────────────────────────────────────────


def test_phase4_sets():
    assert "ai_estimating" in registry.SUB_APPS
    assert {"plans", "evidence", "report"} <= registry.NAMESPACES
    assert registry.OWN_ROW_NAMESPACES == {"memory", "schedule", "report"}
    assert EFFECT_SENSITIVE_READ in registry.WRITE_SPEC_EFFECTS
    assert registry.RETENTION_CLASSES == {"standard", "payroll_aggregate", "mail", "cross_mailbox"}


def test_new_fields_default_so_older_specs_stay_valid():
    spec = registry.validate(_read(namespace="core", name="core_probe", roles=INTERNAL_ROLES,
                                   route=("GET", "/features")))
    assert spec.retention_class == "standard" and spec.deferred is False and spec.keywords == ()
    for existing in registry.all_tools().values():
        assert registry.validate(existing) == existing


# ── sensitive_read ───────────────────────────────────────────────────────────


def test_a_valid_sensitive_spec_passes():
    spec = registry.validate(_sensitive())
    assert spec.effect == EFFECT_SENSITIVE_READ and spec.confirm
    assert registry.turn_allowlist([spec]) == {SUBJECT_GET}
    assert registry.execute_allowlist(spec) == {POST, SUBJECT_GET}


@pytest.mark.parametrize("over,message", [
    ({"write": None}, "needs a write spec"),
    ({"route": ("GET", "/analytics/evidence/users/{user_id}")}, "not a GET"),
    ({"route": ("POST", "/analytics/file-access")}, "POST under /analytics/evidence/"),
    ({"route": ("POST", "/projects/{project_id}/labor")}, "POST under /analytics/evidence/"),
    ({"roles": WRITER_ROLES}, "EVIDENCE_ROLES only"),
    ({"roles": frozenset({Role.EXECUTIVE, Role.ACCOUNTANT})}, "EVIDENCE_ROLES only"),
    ({"schema": schema({"user_id": {"type": "string"}})}, "required string argument 'reason'"),
    ({"schema": schema({"user_id": {"type": "string"}, "reason": nullable({"type": "string"})})},
     "required string argument 'reason'"),
    ({"write": _write(bound_text_fields=())}, "binds 'reason' as text"),
    ({"write": _write(result_summary=None)}, "needs a result_summary"),
])
def test_sensitive_spec_refusals(over, message):
    with pytest.raises(RegistryError, match=message):
        registry.validate(_sensitive(**over))


def test_mailbox_review_keeps_a_sensitive_tool(monkeypatch):
    spec = registry.validate(_sensitive())
    registry.all_tools()
    monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    review = registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, mode=registry.MODE_MAILBOX_REVIEW)
    assert spec.name in {s.name for s in review}
    assert spec.name not in {s.name for s in registry.toolset_for(_user(Role.ESTIMATING_ADMIN), FLAGS)}


# ── retention, deferred, keywords ────────────────────────────────────────────


def test_payroll_retention_pairs_with_the_payroll_namespace():
    payroll = ("GET", "/payroll/reports/summary")
    ok = registry.validate(_read(namespace="payroll", sub_app="certified_payroll", route=payroll,
                                 retention_class="payroll_aggregate", roles=INTERNAL_ROLES))
    assert ok.retention_class == "payroll_aggregate"
    with pytest.raises(RegistryError, match="payroll namespace"):
        registry.validate(_read(namespace="payroll", sub_app="certified_payroll", route=payroll,
                                roles=INTERNAL_ROLES))
    with pytest.raises(RegistryError, match="payroll namespace"):
        registry.validate(_read(namespace="pm", sub_app="pm", route=("GET", "/pm/rfis/open"),
                                retention_class="payroll_aggregate", roles=INTERNAL_ROLES))
    with pytest.raises(RegistryError, match="unknown retention_class"):
        registry.validate(_read(retention_class="cross_mailbox"))


@pytest.mark.parametrize("namespace", ["core", "memory", "schedule"])
def test_core_memory_and_schedule_tools_are_never_deferred(namespace):
    with pytest.raises(RegistryError, match="never deferred"):
        registry.validate(_read(namespace=namespace, sub_app=None, route=("GET", "/features"),
                                roles=INTERNAL_ROLES, deferred=True))


def test_deferred_is_a_bool_and_report_may_be_deferred():
    with pytest.raises(RegistryError, match="deferred must be a bool"):
        registry.validate(_read(deferred=1))
    save = ("POST", "/assistant/tools/reports")
    spec = registry.validate(ToolSpec(
        name="report_probe", namespace="report", sub_app="bidding", route=save, effect=EFFECT_HOUSEKEEPING,
        confirm=False, schema=schema({"spec": {"type": "string"}}), response_trimmer=lambda p, a, c: p,
        progress_label="Building a spreadsheet", roles=INTERNAL_ROLES, description="probe",
        requests=lambda a: [ToolRequest(route=save, path=save[1])], deferred=True,
        keywords=("spreadsheet", "xlsx", "report"),
    ))
    assert spec.deferred and spec.namespace in registry.OWN_ROW_NAMESPACES


@pytest.mark.parametrize("keywords,ok", [
    (("plans", "drawings", "e-201", "go_no_go"), True),
    (("Plans",), False),
    (("two words",), False),
    (("fire" + chr(0x2014) + "alarm",), False),
    (("café",), False),
    ((7,), False),
    (["plans"], False),
])
def test_keywords_are_lowercase_ascii_words(keywords, ok):
    spec = _read(keywords=keywords)
    if ok:
        assert registry.validate(spec).keywords == keywords
    else:
        with pytest.raises(RegistryError):
            registry.validate(spec)


# ── per-person routes ────────────────────────────────────────────────────────


@pytest.mark.parametrize("route", [SUBJECT_GET, ("GET", "/analytics/evidence/users/{user_id}/feed"),
                                   ("GET", "/analytics/evidence/files/{file_id}")])
def test_a_non_sensitive_spec_may_not_declare_a_person_route(route):
    with pytest.raises(RegistryError, match="names one person"):
        registry.validate(_read(route=route))
    write = dataclasses.replace(_write(), bound_text_fields=())
    with pytest.raises(RegistryError, match="names one person"):
        registry.validate(ToolSpec(
            name="write_probe", namespace="bidding", sub_app="bidding", route=("POST", "/projects/{project_id}/x"),
            extra_routes=(route,), effect="write_internal", confirm=True, schema=schema({}),
            response_trimmer=lambda p, a, c: p, progress_label="Writing", roles=WRITER_ROLES,
            description="probe", requests=lambda a: [], write=write,
        ))


FILE_ACCESS = ("GET", "/analytics/file-access")
ACTIVITY = ("GET", "/analytics/activity")
NOTICES = ("GET", "/projects/{project_id}/notification-log")
TIMELINE = ("GET", "/analytics/evidence/projects/{project_id}/timeline")


@pytest.mark.parametrize("request_,refused", [
    (ToolRequest(route=FILE_ACCESS, path="/analytics/file-access", params={"project_id": "p"}), False),
    (ToolRequest(route=FILE_ACCESS, path="/analytics/file-access", params={"actor_id": "u1"}), True),
    (ToolRequest(route=FILE_ACCESS, path="/analytics/file-access", params={"file_id": "f1"}), True),
    (ToolRequest(route=FILE_ACCESS, path="/analytics/file-access", params={"actor_id": None}), False),
    (ToolRequest(route=ACTIVITY, path="/analytics/activity", params={"user_id": "u1"}), True),
    (ToolRequest(route=NOTICES, path="/projects/p/notification-log", params={"user_id": "u1"}), True),
    (ToolRequest(route=TIMELINE, path="/analytics/evidence/projects/p/timeline",
                 json={"filters": [{"actor_id": "u1"}]}), True),
    (ToolRequest(route=SUBJECT_GET, path="/analytics/evidence/subjects/users/u1"), True),
    (ToolRequest(route=("GET", "/projects/{project_id}"), path="/projects/p", params={"user_id": "u1"}), False),
])
def test_per_person_violation(request_, refused):
    assert (per_person_violation("read", request_) is not None) is refused
    assert per_person_violation(EFFECT_SENSITIVE_READ, request_) is None


# ── toolset ──────────────────────────────────────────────────────────────────


def test_a_plans_tool_follows_the_ai_estimating_flag(monkeypatch):
    route = ("GET", "/ai-estimating/projects/{project_id}/runs")
    spec = registry.validate(_read(name="plans_probe", namespace="plans", sub_app="ai_estimating", route=route,
                                   roles=INTERNAL_ROLES, deferred=True, keywords=("plans",)))
    registry.all_tools()
    monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    on = {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE), FLAGS)}
    off = {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE), {**FLAGS, "ai_estimating": False})}
    missing = {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE),
                                                   {k: v for k, v in FLAGS.items() if k != "ai_estimating"})}
    assert "plans_probe" in on and "plans_probe" not in off and "plans_probe" not in missing


def test_mailbox_review_drops_and_incognito_keeps_the_report_namespace(monkeypatch):
    save = ("POST", "/assistant/tools/reports")
    spec = registry.validate(ToolSpec(
        name="report_probe", namespace="report", sub_app="bidding", route=save, effect=EFFECT_HOUSEKEEPING,
        confirm=False, schema=schema({}), response_trimmer=lambda p, a, c: p, progress_label="Building",
        roles=INTERNAL_ROLES, description="probe", requests=lambda a: [ToolRequest(route=save, path=save[1])],
    ))
    registry.all_tools()
    monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    assert "report_probe" in {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, incognito=True)}
    review = registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, mode=registry.MODE_MAILBOX_REVIEW)
    assert "report_probe" not in {s.name for s in review}


# ── unattended runs ──────────────────────────────────────────────────────────


def _run_turn_callers() -> list[str]:
    """Files that call app.assistant.loop.run_turn: `loop.run_turn(...)`,
    or `run_turn(...)` after importing it from app.assistant.loop."""
    callers = []
    for path in sorted(APP.rglob("*.py")):
        if path.name == "loop.py" and path.parent.name == "assistant":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = any(isinstance(n, ast.ImportFrom) and (n.module or "").endswith("assistant.loop")
                       and any(a.name == "run_turn" for a in n.names) for n in ast.walk(tree))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            via_module = (isinstance(func, ast.Attribute) and func.attr == "run_turn"
                          and isinstance(func.value, ast.Name) and func.value.id == "loop")
            via_import = imported and isinstance(func, ast.Name) and func.id == "run_turn"
            if via_module or via_import:
                callers.append(str(path.relative_to(APP.parent)))
    return callers


def test_only_jobs_calls_loop_run_turn():
    """Phase 7 (the proactive router) must add the unattended toolset
    filter before anything else may run a model turn (3.1)."""
    assert _run_turn_callers() == ["app/assistant/jobs.py"]
    source = (APP / "assistant" / "jobs.py").read_text(encoding="utf-8")
    assert source.count("loop.run_turn(") == 1

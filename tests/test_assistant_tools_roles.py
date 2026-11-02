"""Every tool as each of the seven roles with each sub-app flag off
(docs/ASSISTANT.md, Permissions items 3, 10 and 15, Prompt injection and
taint, the test matrix under Changes required).

  estimator   never gets a toolset, whatever the flags;
  accountant  gets no BDR write tool: its effects are read and
              housekeeping (its own notes and routines, phase 3), and its
              only confirm tool is schedule_create once WI-7 registers it;
              a write tool registered for the writer roles never reaches it;
  flags       a tool tagged with a sub-app disappears when that flag is
              off; spine tools stay; nothing else changes;
  switches    assistant_settings.disabled_tools hides a tool by name;
  mode        Mailbox review keeps read and sensitive_read tools only and
              drops the memory and schedule namespaces whatever the effect.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.assistant import registry
from app.assistant.registry import ProposalRefused, ToolRequest, ToolSpec, WriteSpec, schema
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role

ROLES = list(Role)
# ai_estimating joined in phase 4 (the plans tools, docs/ASSISTANT_PHASE4_PLAN.md 3.1).
SUB_APPS = ("bidding", "pm", "certified_payroll", "ai_estimating")


def _flags(**off) -> dict[str, bool]:
    return {name: not off.get(name, False) for name in SUB_APPS}


def _user(role: Role):
    return SimpleNamespace(id="u", role=role, is_active=True)


def _refuse(args, reads, ctx):
    raise ProposalRefused("probe")


@pytest.fixture
def write_tool(monkeypatch):
    """A write_internal tool for writer roles, registered for one test."""
    spec = registry.validate(ToolSpec(
        name="set_labor_probe", namespace="bidding", sub_app="bidding",
        route=("PUT", "/projects/{project_id}/labor"), effect="write_internal", confirm=True,
        schema=schema({"project_id": {"type": "string"}}), response_trimmer=lambda p, a, c: p,
        progress_label="Writing labor", roles=WRITER_ROLES, description="probe write",
        requests=lambda a: [ToolRequest(route=("PUT", "/projects/{project_id}/labor"), path="/projects/x/labor")],
        write=WriteSpec(preview_requests=lambda a: [], build=_refuse, snapshot=lambda a, r: ({}, {})),
    ))
    registry.all_tools()  # load the real modules first
    monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    yield spec


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("off", [None, *SUB_APPS])
def test_every_role_with_each_flag_off(role, off, write_tool, own_row_tools):
    flags = _flags(**({off: True} if off else {}))
    specs = registry.toolset_for(_user(role), flags)
    names = {s.name for s in specs}
    if role == Role.ESTIMATOR:
        assert specs == []
        return
    assert role in INTERNAL_ROLES
    # Spine tools are always there for an internal role.
    assert {"app_search", "get_project_context", "list_teammates", "load_skill"} <= names
    bidding_tools = {"whats_due", "get_labor", "set_labor_probe"}
    if off == "bidding":
        assert not (names & bidding_tools)
    else:
        assert {"whats_due", "get_labor"} <= names
    if role == Role.ACCOUNTANT:
        # Holds before and after the phase 3 and phase 4 tools register:
        # reads and its own housekeeping only, and the one confirm tool
        # schedule_create (no sensitive_read, expensive or BDR write).
        confirm = {s.name for s in specs if s.confirm}
        assert confirm <= {"schedule_create", "email_draft"}
        assert all(s.effect in (registry.EFFECT_READ, registry.EFFECT_HOUSEKEEPING)
                   for s in specs if s.name not in confirm)
        assert not any(s.effect in (registry.EFFECT_SENSITIVE_READ, registry.EFFECT_EXPENSIVE,
                                    registry.EFFECT_WRITE_INTERNAL, registry.EFFECT_EXTERNAL_SEND)
                       for s in specs if s.name not in {"schedule_create", "email_draft"})
        assert "set_labor_probe" not in names and "memory_save_probe" in names
    elif off != "bidding":
        assert "set_labor_probe" in names
    for spec in specs:
        assert role in spec.roles
        assert spec.sub_app is None or flags[spec.sub_app]
    assert [s.name for s in specs] == sorted(names)


def test_unknown_or_missing_role_gets_nothing():
    assert registry.toolset_for(SimpleNamespace(role="nope"), _flags()) == []
    assert registry.toolset_for(SimpleNamespace(role=None), _flags()) == []


def test_disabled_tools_hide_by_name():
    row = {"disabled_tools": ["whats_due", " get_labor "]}
    names = {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE), _flags(), settings_row=row)}
    assert "whats_due" not in names and "get_labor" not in names
    assert "app_search" in names


@pytest.fixture
def own_row_tools(monkeypatch):
    """A memory housekeeping tool and a schedule read tool, registered for
    one test (the real ones arrive with WI-7)."""
    save = ("POST", "/assistant/tools/memory/save")
    listing = ("GET", "/assistant/schedules")
    specs = [
        registry.validate(ToolSpec(
            name="memory_save_probe", namespace="memory", sub_app=None, route=save,
            effect=registry.EFFECT_HOUSEKEEPING, confirm=False, schema=schema({"title": {"type": "string"}}),
            response_trimmer=lambda p, a, c: p, progress_label="Saving a note", roles=INTERNAL_ROLES,
            description="probe housekeeping", requests=lambda a: [ToolRequest(route=save, path=save[1])],
            redact_args=lambda a: {"title_len": len(str(a.get("title") or ""))},
        )),
        registry.validate(ToolSpec(
            name="schedule_list_probe", namespace="schedule", sub_app=None, route=listing, effect="read",
            confirm=False, schema=schema({}), response_trimmer=lambda p, a, c: p,
            progress_label="Listing routines", roles=INTERNAL_ROLES, description="probe read",
            requests=lambda a: [ToolRequest(route=listing, path=listing[1])],
        )),
    ]
    registry.all_tools()
    for spec in specs:
        monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    return specs


def test_mailbox_review_mode_offers_reads_only(write_tool, own_row_tools):
    normal = registry.toolset_for(_user(Role.EXECUTIVE), _flags())
    review = registry.toolset_for(_user(Role.EXECUTIVE), _flags(), mode=registry.MODE_MAILBOX_REVIEW)
    assert {"set_labor_probe", "memory_save_probe", "schedule_list_probe"} <= {s.name for s in normal}
    assert all(s.effect in registry.READ_EFFECTS for s in review)
    assert "set_labor_probe" not in {s.name for s in review}
    # Memory and schedule tools go too, a read one included (docs/ASSISTANT.md:94).
    assert not any(s.namespace in ("memory", "schedule") for s in review)
    assert {s.name for s in review} == {s.name for s in normal if s.effect in registry.READ_EFFECTS
                                        and s.namespace not in ("memory", "schedule", "mail", "web", "browser")} | {"read_mailbox"}


def test_tool_block_is_stable_per_flags_and_role():
    from app.assistant import prompts

    a = prompts.tool_block(registry.toolset_for(_user(Role.EXECUTIVE), _flags()))
    b = prompts.tool_block(registry.toolset_for(_user(Role.IT_ADMIN), _flags()))
    c = prompts.tool_block(registry.toolset_for(_user(Role.EXECUTIVE), _flags(bidding=True)))
    assert a == b                      # same flags, same tool roles: same bytes
    assert a != c and "whats_due" not in c
    assert chr(0x2014) not in a


def test_tool_block_names_only_the_offered_write_tools():
    """The identity rules no longer list write tools; the tool block names
    the ones this role and these flags offer, or says read-only."""
    from app.assistant import prompts
    from app.assistant.tools import writes as _writes  # noqa: F401 - registers the real write tools

    assert "set_labor_numbers" not in prompts.IDENTITY_RULES
    executive = prompts.tool_block(registry.toolset_for(_user(Role.EXECUTIVE), _flags()))
    assert "Write tools (each call becomes an approval card):" in executive
    assert "set_labor_numbers" in executive and prompts.READ_ONLY_TEXT not in executive
    accountant = prompts.tool_block(registry.toolset_for(_user(Role.ACCOUNTANT), _flags()))
    # The Accountant still gets the notes and routines tools (3.7.4).
    assert prompts.READ_ONLY_BDR_TEXT in accountant and "Write tools" not in accountant
    review = prompts.tool_block(registry.toolset_for(_user(Role.EXECUTIVE), _flags(),
                                                     mode=registry.MODE_MAILBOX_REVIEW))
    assert prompts.READ_ONLY_TEXT in review
    no_bidding = prompts.tool_block(registry.toolset_for(_user(Role.EXECUTIVE), _flags(bidding=True)))
    assert "set_labor_numbers" not in no_bidding and "todo_create" in no_bidding
    assert prompts.READ_ONLY_TEXT in prompts.tool_block([])

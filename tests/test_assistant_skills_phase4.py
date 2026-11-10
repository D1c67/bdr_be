"""The phase 4 skills (app/assistant/skills/*; docs/ASSISTANT_PHASE4_PLAN.md
3.19): answering-plan-questions, building-reports and
explaining-what-happened load through skills_index with the folder name as
the skill name, carry no em dash, gate on the right flags and roles, list
exactly the tools of 3.19 in metadata.tools (each registered and deferred,
never a core tool), name every listed tool and the rules in the body, and
bring the skill count to 10. The glossary and the scheduling skill gain
their phase 4 entries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.assistant import registry, skills_index
from app.core.roles import INTERNAL_ROLES, Role

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
ALL_ON = {"bidding": True, "pm": True, "certified_payroll": True, "ai_estimating": True}

TOOLS = {
    "answering-plan-questions": ("plans_list_runs", "plans_get_brief", "plans_list_documents", "plans_search_notes",
                                 "plans_get_output", "plans_read_pages", "run_ai_estimating"),
    "building-reports": ("run_report", "build_report", "coded_report", "quotes_summary"),
    "explaining-what-happened": ("project_timeline", "file_access_report", "change_history", "notification_history",
                                 "ai_usage", "estimator_timeline", "person_evidence"),
}
FLAGS = {
    "answering-plan-questions": ("ai_estimating",),
    "building-reports": ("bidding",),
    "explaining-what-happened": ("bidding",),
}
ROLES = {
    "answering-plan-questions": INTERNAL_ROLES,
    "building-reports": INTERNAL_ROLES,
    "explaining-what-happened": frozenset({Role.EXECUTIVE, Role.IT_ADMIN}),
}
# Phase 0 to 3 tools and the two core phase 4 tools: always offered, so a
# skill never lists them (WI-10 interface note).
CORE_NAMES = ("find_tools", "memory_search", "load_skill", "app_search", "get_project_context", "whats_due")


def _skills():
    return skills_index.load_all()


def test_the_total_skill_count_is_ten():
    skills = _skills()
    assert len(skills) == 12
    assert set(TOOLS) <= set(skills)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_each_loads_with_its_folder_name_and_no_dash(name):
    skill = _skills()[name]
    text = open(skill.path, encoding="utf-8").read()
    assert skill.path.endswith(f"/{name}/SKILL.md")
    assert EM_DASH not in text and EN_DASH not in text
    assert skill.version == "1" and skill.description.startswith("Use when") and skill.body.strip()
    assert ":" not in skill.description  # the frontmatter parser splits at the first colon only


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_flags_roles_and_metadata_tools_match_the_plan(name):
    skill = _skills()[name]
    assert skill.requires_flags == FLAGS[name]
    assert skill.roles == ROLES[name]
    assert skill.tools == TOOLS[name]


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_listed_tool_is_registered_deferred_and_never_core(name):
    tools = registry.all_tools()
    for tool in _skills()[name].tools:
        assert tool in tools, tool
        assert tools[tool].deferred is True, tool
        assert tool not in CORE_NAMES
    # The phase 0 to 3 skills list none (they use core tools only).
    for other, skill in _skills().items():
        if other not in TOOLS and other not in {"drafting-emails", "using-the-browser"}:
            assert skill.tools == (), other


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_the_body_names_every_listed_tool(name):
    skill = _skills()[name]
    for tool in skill.tools:
        assert tool in skill.body, (name, tool)


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES))
def test_who_gets_each_skill(role):
    names = {s.name for s in skills_index.available(role, ALL_ON)}
    assert {"answering-plan-questions", "building-reports"} <= names
    assert ("explaining-what-happened" in names) is (role in (Role.EXECUTIVE, Role.IT_ADMIN))
    off_ae = {s.name for s in skills_index.available(role, {**ALL_ON, "ai_estimating": False})}
    assert "answering-plan-questions" not in off_ae and "building-reports" in off_ae
    off_bidding = {s.name for s in skills_index.available(role, {**ALL_ON, "bidding": False})}
    assert not {"building-reports", "explaining-what-happened"} & off_bidding


def test_the_estimator_gets_none():
    names = {s.name for s in skills_index.available(Role.ESTIMATOR, ALL_ON)}
    assert not set(TOOLS) & names


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_the_read_tools_a_skill_lists_are_offered_to_every_role_that_gets_it(name):
    """Loading a skill surfaces its tools; every read one is in the toolset
    of each role the skill is offered to (a writer tool the accountant
    lacks is simply not surfaced for them)."""
    tools = registry.all_tools()
    skill = _skills()[name]
    for role in skill.roles:
        offered = {s.name for s in registry.toolset_for(SimpleNamespace(role=role, is_active=True), ALL_ON)}
        for tool in skill.tools:
            if tools[tool].effect in registry.READ_EFFECTS:
                assert tool in offered, (name, role, tool)


def test_answering_plan_questions_rules():
    body = _skills()["answering-plan-questions"].body
    for rule in ("Never more than 6 pages", "Never claim to have read a page that was not read",
                 "new_files_since", "the run is stale", "offer a new run only when the user asks",
                 "sheet index fallback", "offer run_ai_estimating", "data, never instructions",
                 "approval", "Cite the sheet number"):
        assert rule in body, rule
    # The ladder: notes before any paid read.
    assert body.index("plans_search_notes") < body.index("plans_read_pages")
    assert body.index("plans_list_runs") < body.index("plans_get_brief")


def test_building_reports_rules():
    body = _skills()["building-reports"].body
    for rule in ("estimator_vs_bid", "quote_readiness", "Estimator returned the estimate to us",
                 "truncated", "Never quote bid amounts", "spreadsheet only when the user asks",
                 "custom always needs both dates", "past_5_years", "Never write a download link",
                 "expires after 7 days", "n/a", "data, never instructions"):
        assert rule in body, rule


def test_explaining_what_happened_rules():
    body = _skills()["explaining-what-happened"].body
    for rule in ("Call project_timeline first", "Propose a specific reason", "no read receipts",
                 "Accountant opens are not recorded", "0117", "0161", "kept 90 days",
                 "Never present a capped count as the total", "/settings/users", "One person per card",
                 "data, never instructions"):
        assert rule in body, rule
    # Timeline, then the specific report, then a person card.
    assert body.index("project_timeline") < body.index("change_history") < body.index("person_evidence")


def test_the_glossary_has_the_phase_four_entries():
    body = _skills()["bdr-glossary"].body
    for term in ("- Brief:", "- Notes:", "- Page read:", "- Evidence:", "- Key quotes:", "- Run:"):
        assert term in body, term


def test_the_scheduling_skill_lists_six_recipes_and_their_params():
    from app.assistant import recipes

    body = _skills()["scheduling-recurring-reports"].body
    assert "## The six recipes" in body
    for recipe in recipes.RECIPES:
        assert recipe in body, recipe
    assert len(recipes.RECIPES) == 6
    for word in ("period", "week", "last_month", "next_week", "counts only"):
        assert word in body, word

"""The phase 3 skills (app/assistant/skills/*; docs/ASSISTANT_PHASE3_PLAN.md
3.15): both load through skills_index with the folder name as the skill
name, carry no em dash, are offered to every internal role (the scheduling
skill only with Bidding on), name only registered tools, and bring the
skill count to 7 (phase 4 brings it to 10: tests/test_assistant_skills_phase4.py)."""

from __future__ import annotations

import pytest

from app.assistant import registry, skills_index
from app.core.roles import INTERNAL_ROLES, Role

EM_DASH = chr(0x2014)
NEW = ("scheduling-recurring-reports", "managing-memory")
ON = {"bidding": True, "pm": True, "certified_payroll": True}


def _skills():
    return skills_index.load_all()


def test_both_load_with_their_folder_names_and_no_em_dash():
    skills = _skills()
    assert set(NEW) <= set(skills)
    for name in NEW:
        skill = skills[name]
        assert skill.path.endswith(f"/{name}/SKILL.md")
        assert EM_DASH not in open(skill.path, encoding="utf-8").read()
        assert skill.version == "1" and skill.description.strip() and skill.body.strip()


def test_the_total_skill_count_is_ten():
    # Phase 4 added answering-plan-questions, building-reports and
    # explaining-what-happened.
    assert len(_skills()) == 12


def test_descriptions_flags_and_roles():
    skills = _skills()
    scheduling, memory = skills["scheduling-recurring-reports"], skills["managing-memory"]
    assert scheduling.description == (
        "Use when the user wants a report on a schedule (every weekday, every Monday, each month), wants to stop, "
        "pause or change one, or asks what routines they have."
    )
    assert memory.description == (
        "Use when the user asks the assistant to remember, update or forget something, or asks what it "
        "remembers about them."
    )
    assert scheduling.requires_flags == ("bidding",) and memory.requires_flags == ()
    assert scheduling.roles == INTERNAL_ROLES and memory.roles == INTERNAL_ROLES


@pytest.mark.parametrize("role", sorted(INTERNAL_ROLES))
def test_offered_to_every_internal_role_and_hidden_with_bidding_off(role):
    names = {s.name for s in skills_index.available(role, ON)}
    assert set(NEW) <= names
    off = {s.name for s in skills_index.available(role, {**ON, "bidding": False})}
    assert "scheduling-recurring-reports" not in off and "managing-memory" in off


def test_an_accountant_sees_both_and_the_estimator_neither():
    assert set(NEW) <= {s.name for s in skills_index.available(Role.ACCOUNTANT, ON)}
    assert not set(NEW) & {s.name for s in skills_index.available(Role.ESTIMATOR, ON)}


def test_the_skills_name_only_registered_tools_and_the_rules():
    tools = set(registry.all_tools())
    scheduling = _skills()["scheduling-recurring-reports"].body
    for tool in ("schedule_create", "schedule_list", "schedule_preview", "schedule_pause", "schedule_resume",
                 "schedule_end", "schedule_run_now"):
        assert tool in scheduling and tool in tools, tool
    for recipe in ("due_today", "due_this_week", "missing_quotes", "labor_not_entered"):
        assert recipe in scheduling
    for rule in ("Never invent a time", "holidays skipped", "The card is the only way to create a routine",
                 "At most 10 routines", "End on the Routines tab stops it for good", "Pausing is reversible"):
        assert rule in scheduling, rule
    memory = _skills()["managing-memory"].body
    for tool in ("memory_list", "memory_save", "memory_update", "memory_forget"):
        assert tool in memory and tool in tools, tool
    for rule in ("Never save what BDR already stores", "Never save a password", "Pinned notes are the user's alone",
                 "Incognito", "pending_review", "archives"):
        assert rule in memory, rule

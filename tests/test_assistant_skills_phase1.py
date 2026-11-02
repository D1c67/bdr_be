"""The phase 1 skills (app/assistant/skills/*; docs/ASSISTANT_PHASE1_PLAN.md
3.16): they load through skills_index with the folder name as the skill
name, carry no em dash, are offered to the writer roles with Bidding on
only, and name the right tools and values."""

from __future__ import annotations

import typing

import pytest

from app.assistant import registry, skills_index
from app.assistant.tools import writes
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role

EM_DASH = chr(0x2014)
NEW = ("entering-labor-numbers", "creating-projects")
ON = {"bidding": True, "pm": True, "certified_payroll": True}


def _skills():
    return skills_index.load_all()


def test_every_skill_loads_with_its_folder_name_and_no_em_dash():
    skills = _skills()
    assert set(NEW) <= set(skills)
    for name, skill in skills.items():
        assert skill.path.endswith(f"/{name}/SKILL.md")
        text = open(skill.path, encoding="utf-8").read()
        assert EM_DASH not in text, name
        assert skill.version == "1"


@pytest.mark.parametrize("name", NEW)
def test_new_skills_are_for_writers_with_bidding_on(name):
    skill = _skills()[name]
    assert skill.roles == WRITER_ROLES
    assert skill.requires_flags == ("bidding",)
    for role in INTERNAL_ROLES:
        assert skill.available_to(role, ON) is (role in WRITER_ROLES)
    assert not skill.available_to(Role.EXECUTIVE, {**ON, "bidding": False})
    assert skills_index.get(name, Role.ESTIMATING_ENGINEER_LABOR, ON) is not None
    assert skills_index.get(name, Role.ACCOUNTANT, ON) is None


def test_skill_descriptions():
    skills = _skills()
    assert skills["entering-labor-numbers"].description == (
        "Use when the user wants to enter or change the labor numbers on a project's bidding labor panel."
    )
    assert skills["creating-projects"].description == "Use when the user wants to create a new project in BDR."


def test_skills_name_only_registered_tools():
    tools = set(registry.all_tools())
    labor = _skills()["entering-labor-numbers"].body
    for tool in ("set_labor_numbers", "get_labor", "app_search"):
        assert tool in labor and tool in tools
    assert "Activity tab" in labor and "Verify" in labor
    create = _skills()["creating-projects"].body
    for tool in ("create_project_draft", "app_search", "list_gc_contacts"):
        assert tool in create and tool in tools


def test_creating_projects_lists_every_go_no_go_value():
    body = _skills()["creating-projects"].body
    for field, (literal, _hint) in writes.GONO_FIELDS.items():
        assert field in body, field
        for value in typing.get_args(literal):
            assert value in body, (field, value)
    for field in writes.CREATE_DATETIMES:
        assert field in body
    assert "YYYY-MM-DDTHH:MM" in body and "no_bidding_url" in body


def test_status_skill_no_longer_forbids_every_change():
    body = _skills()["answering-status-questions"].body
    assert "Do not propose a change while answering a status question unless the user asks for one." in body
    assert "a change to a record" not in body

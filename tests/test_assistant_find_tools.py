"""The find_tools resolver and the skills' tools metadata
(docs/ASSISTANT_PHASE4_PLAN.md 3.16.1, 3.16.5; WI-10).

  ranking   name and keyword hits weigh 3, the namespace 2, description
            words 1; ties go by name; only deferred specs come back, and a
            spec with no hit never does; stop words and plurals fold;
  catalog   the line names the deferred areas present, in a fixed order,
            with no em dash, and is empty without deferred tools;
  skills    metadata.tools parses as a list of tool names, a bad name is
            refused at load, and every name a real skill lists is a
            registered deferred tool.
"""

from __future__ import annotations

import pytest

from app.assistant import find, registry, skills_index
from app.assistant.registry import EFFECT_READ, EFFECT_WRITE_INTERNAL, ToolRequest, ToolSpec, schema
from app.core.roles import INTERNAL_ROLES

EM_DASH = chr(0x2014)


def _spec(name, namespace="plans", *, deferred=True, keywords=(), description="A probe tool.",
          effect=EFFECT_READ) -> ToolSpec:
    """A spec as the resolver sees it (no validate: a write stand-in needs
    no WriteSpec for scoring)."""
    route = ("GET", f"/probe/{name}")
    return ToolSpec(
        name=name, namespace=namespace, sub_app=None, route=route, effect=effect,
        confirm=effect != EFFECT_READ, schema=schema({}), response_trimmer=lambda p, a, c: {},
        progress_label="Probe", roles=INTERNAL_ROLES, description=description,
        requests=lambda a: [ToolRequest(route=route, path=route[1])], deferred=deferred, keywords=tuple(keywords),
    )


SPECS = [
    _spec("plans_search_notes", keywords=("plans", "drawings", "sheet", "spec", "notes", "scope", "panel"),
          description="Use to search the AI Estimating notes of a project's plans."),
    _spec("plans_get_brief", keywords=("brief", "summary", "plans"),
          description="Use to read the brief of an AI Estimating run."),
    _spec("plans_list_runs", keywords=("runs", "ai", "estimating"),
          description="Use to list the AI Estimating runs of a project."),
    _spec("rfi_list", "pm", keywords=("rfi", "question", "open"),
          description="Use to list open RFIs on PM projects."),
    _spec("project_timeline", "evidence", keywords=("timeline", "who", "history", "why"),
          description="Use to explain what happened on a project and who did it."),
    _spec("advance_lane", "bidding", keywords=("advance", "lane", "stage"), effect=EFFECT_WRITE_INTERNAL,
          description="Use to move a bidding lane to its next step."),
    _spec("whats_due", "bidding", deferred=False, keywords=("due", "plans"),
          description="Use to list what is due today."),
]


def _names(found) -> list[str]:
    return [s.name for s in found]


def test_tokens_drop_stop_words_and_fold_plurals():
    assert find.tokens("Show me the open RFIs on my drawings, please") == {"open", "rfi", "drawing"}
    assert find.tokens("entries classes status") == {"entry", "class", "statu"}
    assert find.tokens("") == frozenset() and find.tokens(None) == frozenset()
    assert find.tokens("low_voltage gear-room") == {"low", "voltage", "gear", "room"}


def test_resolver_ranks_name_and_keyword_hits_over_description_words():
    found = find.resolve("search the plan notes for the panel", SPECS)
    assert _names(found)[0] == "plans_search_notes"
    # plans_get_brief: "plan" in name, keyword and namespace (8);
    # plans_list_runs: name and namespace only (5).
    assert _names(found)[1:3] == ["plans_get_brief", "plans_list_runs"]
    assert [find.score(find.tokens("search the plan notes for the panel"), s) for s in found] == [23, 8, 5]
    assert "whats_due" not in _names(found)        # core tools are already offered


def test_resolver_scores_exactly_by_the_weights():
    spec = _spec("plans_search_notes", keywords=("drawings",), description="Search drawings and notes.")
    # "plan": name 3 + namespace 2; "drawing": keyword 3 + description 1;
    # "note": name 3 + description 1; "zebra": nothing.
    assert find.score(find.tokens("plans drawings notes zebra"), spec) == 3 + 2 + 3 + 1 + 3 + 1
    assert find.score(find.tokens("zebra"), spec) == 0


def test_resolver_returns_only_deferred_hits_in_order_with_a_limit():
    assert _names(find.resolve("open RFIs", SPECS)) == ["rfi_list"]
    assert _names(find.resolve("who changed the timeline and why", SPECS)) == ["project_timeline"]
    assert _names(find.resolve("move the lane to the next stage", SPECS)) == ["advance_lane"]
    assert find.resolve("zebra unicorn", SPECS) == []
    assert find.resolve("the of and", SPECS) == []
    assert _names(find.resolve("plans", SPECS, limit=1)) == ["plans_search_notes"]
    assert len(find.resolve("plans", SPECS, limit=2)) == 2
    assert find.resolve("plans", SPECS, limit=0) == []
    assert len(find.resolve("plans", SPECS * 3, limit=50)) == 3      # deduped by name, capped
    assert find.resolve("plans", SPECS) == find.resolve("PLANS!", list(reversed(SPECS)))


def test_resolver_ties_go_by_name():
    a = _spec("plans_zeta", keywords=("lighting",))
    b = _spec("plans_alpha", keywords=("lighting",))
    assert _names(find.resolve("lighting", [a, b])) == ["plans_alpha", "plans_zeta"]


def test_resolve_keeps_its_binding_signature():
    import inspect
    sig = inspect.signature(find.resolve)
    assert list(sig.parameters) == ["query", "specs", "limit"] and sig.parameters["limit"].default == 6
    assert list(inspect.signature(find.catalog_line).parameters) == ["specs"]


def test_catalog_line_names_the_deferred_areas_in_a_fixed_order():
    line = find.catalog_line(SPECS)
    assert line == (
        "More tools load on demand with find_tools: plans and drawings, evidence (who did what and when), "
        "PM and RFIs, moving a lane."
    )
    full = SPECS + [_spec("run_report", "bidding", keywords=("report",)),
                    _spec("cp_week_summary", "payroll"), _spec("build_report", "report")]
    assert find.catalog_line(full) == (
        "More tools load on demand with find_tools: plans and drawings, reports and spreadsheets, "
        "evidence (who did what and when), PM and RFIs, certified payroll, moving a lane."
    )
    assert EM_DASH not in find.catalog_line(full)
    # Core tools name no area; no deferred tool, no line.
    assert find.catalog_line([s for s in SPECS if not s.deferred]) == ""
    assert find.catalog_line([]) == ""


def test_find_module_is_pure_and_dash_free():
    text = open(find.__file__, encoding="utf-8").read()
    assert EM_DASH not in text and chr(0x2013) not in text
    for banned in ("get_supabase", "supabase_client", ".table(", "app.routers", "app.services"):
        assert banned not in text


# ── skills metadata.tools (3.16.5) ───────────────────────────────────────────


def _write_skill(tmp_path, tools_line: str) -> None:
    folder = tmp_path / "probe-skill"
    folder.mkdir(exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: probe-skill\ndescription: A probe.\nmetadata:\n  roles: [executive]\n{tools_line}"
        "  version: \"1\"\n---\nBody.\n",
        encoding="utf-8",
    )


def test_metadata_tools_parse_as_a_deduped_tuple(tmp_path):
    _write_skill(tmp_path, "  tools: [plans_list_runs, plans_get_brief, plans_list_runs]\n")
    skill = skills_index.load_all(tmp_path)["probe-skill"]
    assert skill.tools == ("plans_list_runs", "plans_get_brief")


def test_a_skill_without_metadata_tools_lists_none(tmp_path):
    _write_skill(tmp_path, "")
    assert skills_index.load_all(tmp_path)["probe-skill"].tools == ()


@pytest.mark.parametrize("bad", ["Plans_List", "x", "plans-list-runs", "drop table"])
def test_a_bad_tool_name_in_metadata_is_refused(tmp_path, bad):
    _write_skill(tmp_path, f"  tools: [plans_list_runs, {bad}]\n")
    with pytest.raises(skills_index.SkillError):
        skills_index.load_all(tmp_path)


def test_every_tool_a_real_skill_lists_is_registered_and_deferred():
    tools = registry.all_tools()
    for name, skill in skills_index.load_all().items():
        for tool in skill.tools:
            assert tool in tools, (name, tool)
            assert tools[tool].deferred, (name, tool)

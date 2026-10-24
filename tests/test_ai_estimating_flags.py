"""AI Estimating build flags and settings (docs/AI_ESTIMATING_BUILD_SPEC.md 5.7).

Pins the code defaults of every section 5.7 setting (parents off, children
as listed), the boot validators, the PLANS_READER_ROUTES parser (section 8.3
defaults, every route on OpenAI by owner decision) and the
LLM_QUEUE_CLAIM_TYPES parser. Defaults are read off the field definitions and
every Settings is built with `_env_file=None`, so the local .env never
changes what is asserted. No database, no network.
"""

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    PLANS_READER_DEFAULT_ROUTES,
    PLANS_READER_ROUTE_FIELDS,
    Settings,
)

# (field, code default) for every row of the section 5.7 table.
SPEC_DEFAULTS: dict[str, object] = {
    "plans_reader_page_facts_enabled": False,
    "plans_reader_page_facts_in_subs": True,
    "plans_reader_spec_select": True,
    "plans_reader_facts_relabel": False,
    "plans_reader_facts_title_vision": False,
    "plans_reader_facts_page_budget_s": 4,
    "plans_reader_facts_doc_budget_s": 240,
    "plans_reader_facts_block_max_tokens": 2500,
    "plans_reader_ledger_legend_max_tokens": 6000,
    "plans_reader_ledger_schedule_max_tokens": 1500,
    "plans_reader_ledger_scales_max_tokens": 300,
    "plans_reader_routes": "",
    "plans_reader_run_ceiling_factor": 1.25,
    "anthropic_plans_refusal_fallback": True,
    "plans_reader_derive_enabled": False,
    "plans_reader_branch_conduit": '3/4"C EMT',
    "plans_reader_min_branch_awg": 12,
    "plans_reader_vd_max_pct": 3.0,
    "plans_reader_route_factor": 1.0,
    "plans_reader_drop_ft_per_device": 0,
    "plans_reader_panel_rise_ft": 0,
    "plans_reader_existing_tie_in_ft": 25,
    "plans_reader_homerun_default_ft": 150,
    "plans_reader_floor_to_floor_ft": 15,
    "plans_reader_receptacles_per_circuit": 6,
    "plans_reader_control_cable_ft_per_device": 25,
    "plans_reader_measure_conventions_version": "g3-branch-2026-10-a",
    "plans_reader_measure_assist_enabled": False,
    "plans_reader_measure_assist_max_items": 8,
    "plans_reader_measure_assist_max_usd": 0.75,
    "plans_reader_consolidator_mode": "legacy",
    "plans_reader_consolidator_max_rounds": 20,
    "plans_reader_consolidator_max_tool_calls": 400,
    "plans_reader_consolidator_max_usd": 6.0,
    "plans_reader_llm_rollup": False,
    "plans_reader_code_regions": False,
    "plans_reader_region_pad": 0.03,
    "plans_reader_cluster_gap": 0.06,
    "plans_reader_census_tol_strong": 0.05,
    "plans_reader_census_tol_weak": 0.10,
    "plans_reader_gates_blocking": False,
    "plans_reader_demo_two_line_style": False,
    "plans_reader_recount_enabled": False,
    "plans_reader_recount_max_targets": 12,
    "plans_reader_recount_max_sheets": 6,
    "plans_reader_recount_budget_usd": 2.0,
    "plans_reader_review_mode": "off",
    "plans_reader_review_budget_usd": 4.0,
    "plans_reader_review_local_rules_fallback": False,
    "ai_scope_policy_enforce": False,
    "ai_scope_attrib_llm": False,
    "ai_scope_attrib_max_statements": 40,
    "ai_scope_digest_max_tokens": 3500,
    "ai_pricing_enabled": False,
    "ai_pricing_state": "test",
    "ai_pricing_autorun": False,
    "ai_pricing_chat_tools_enabled": False,
    "ai_pricing_agent_tools_enabled": False,
    "quote_lines_llm_fallback_enabled": False,
    "plans_reader_marked_plans_mode": "model",
    "plans_reader_annotated_plans_enabled": False,
    "eval_root": "/Users/thomasmoore/Desktop/BDR/backups/eval",
    "ai_eval_ledger_enforce": False,
    "llm_cassette_mode": "off",
    "llm_cassette_dir": "",
    "eval_sweep_id": "",
    "llm_queue_claim_types": "",
}

# Parent flags (each turns a behavior on): off in code, always.
PARENT_FLAGS = (
    "plans_reader_page_facts_enabled",
    "plans_reader_derive_enabled",
    "plans_reader_measure_assist_enabled",
    "plans_reader_code_regions",
    "plans_reader_gates_blocking",
    "plans_reader_recount_enabled",
    "ai_scope_policy_enforce",
    "ai_scope_attrib_llm",
    "ai_pricing_enabled",
    "ai_pricing_autorun",
    "ai_pricing_chat_tools_enabled",
    "ai_pricing_agent_tools_enabled",
    "quote_lines_llm_fallback_enabled",
    "plans_reader_annotated_plans_enabled",
    "ai_eval_ledger_enforce",
)

ROUTE_STEPS = (
    "sub", "escalation", "recount", "main", "review", "revise",
    "scope_attrib", "quote", "derive", "chat",
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ── Defaults ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field,default", sorted(SPEC_DEFAULTS.items()))
def test_code_default_matches_the_spec_table(field, default):
    assert field in Settings.model_fields, f"{field} is missing from Settings"
    assert Settings.model_fields[field].default == default


@pytest.mark.parametrize("field", PARENT_FLAGS)
def test_parent_flags_are_off_in_code(field):
    assert Settings.model_fields[field].default is False


def test_defaults_build_a_valid_settings():
    s = _settings()
    assert s.plans_reader_consolidator_mode == "legacy"
    assert s.plans_reader_review_mode == "off"
    assert s.ai_pricing_state == "test"
    assert s.llm_cassette_mode == "off"
    assert s.llm_queue_claim_job_types == ()


def test_every_spec_setting_is_documented_in_env_example():
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    for field in SPEC_DEFAULTS:
        assert f"{field.upper()}=" in text, f"{field.upper()} missing from .env.example"


def test_env_names_are_the_upper_case_field_names(monkeypatch):
    monkeypatch.setenv("AI_PRICING_STATE", "live")
    monkeypatch.setenv("PLANS_READER_REVIEW_MODE", "shadow")
    monkeypatch.setenv("LLM_QUEUE_CLAIM_TYPES", "plans_reader_agent, ai_pricing")
    s = _settings()
    assert s.ai_pricing_state == "live"
    assert s.plans_reader_review_mode == "shadow"
    assert s.llm_queue_claim_job_types == ("plans_reader_agent", "ai_pricing")


# ── Mode validators ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field,good,bad",
    [
        ("plans_reader_consolidator_mode", ["tools", "legacy"], "hybrid"),
        ("plans_reader_review_mode", ["off", "shadow", "apply"], "on"),
        ("plans_reader_marked_plans_mode", ["code", "model"], "vision"),
        ("ai_pricing_state", ["test", "live"], "dev"),
        ("llm_cassette_mode", ["off", "replay"], "playback"),
    ],
)
def test_modes_accept_their_vocabulary_and_refuse_anything_else(field, good, bad):
    extra = {"llm_cassette_dir": "/tmp/cassettes"} if field == "llm_cassette_mode" else {}
    for value in good:
        assert getattr(_settings(**{field: value}, **extra), field) == value
    # Case and padding are normalized, like PLANS_READER_REASONING_EFFORT.
    assert getattr(_settings(**{field: f" {good[0].upper()} "}, **extra), field) == good[0]
    with pytest.raises(ValidationError, match=field.upper()):
        _settings(**{field: bad}, **extra)


@pytest.mark.parametrize("mode", ["record", "replay", "record_missing"])
def test_cassette_modes_need_a_directory(mode):
    with pytest.raises(ValidationError, match="LLM_CASSETTE_DIR"):
        _settings(llm_cassette_mode=mode)
    assert _settings(llm_cassette_mode=mode, llm_cassette_dir="/x").llm_cassette_mode == mode


# ── Numeric validators ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides,env_name",
    [
        ({"plans_reader_ledger_legend_max_tokens": 40_000}, "PLANS_READER_LEDGER_LEGEND_MAX_TOKENS"),
        (
            {"plans_reader_ledger_legend_max_tokens": 5_000, "plans_reader_ledger_max_tokens": 5_000},
            "PLANS_READER_LEDGER_LEGEND_MAX_TOKENS",
        ),
        ({"plans_reader_ledger_legend_max_tokens": 0}, "PLANS_READER_LEDGER_LEGEND_MAX_TOKENS"),
        ({"plans_reader_ledger_schedule_max_tokens": 40_000}, "PLANS_READER_LEDGER_SCHEDULE_MAX_TOKENS"),
        ({"plans_reader_ledger_scales_max_tokens": -1}, "PLANS_READER_LEDGER_SCALES_MAX_TOKENS"),
        ({"plans_reader_facts_page_budget_s": 0}, "PLANS_READER_FACTS_PAGE_BUDGET_S"),
        ({"plans_reader_facts_page_budget_s": 300}, "PLANS_READER_FACTS_PAGE_BUDGET_S"),
        ({"plans_reader_facts_block_max_tokens": 50}, "PLANS_READER_FACTS_BLOCK_MAX_TOKENS"),
        ({"plans_reader_run_ceiling_factor": 0.9}, "PLANS_READER_RUN_CEILING_FACTOR"),
        ({"plans_reader_branch_conduit": "  "}, "PLANS_READER_BRANCH_CONDUIT"),
        ({"plans_reader_min_branch_awg": 13}, "PLANS_READER_MIN_BRANCH_AWG"),
        ({"plans_reader_vd_max_pct": 0}, "PLANS_READER_VD_MAX_PCT"),
        ({"plans_reader_route_factor": 3.0}, "PLANS_READER_ROUTE_FACTOR"),
        ({"plans_reader_drop_ft_per_device": -1}, "PLANS_READER_DROP_FT_PER_DEVICE"),
        ({"plans_reader_homerun_default_ft": 0}, "PLANS_READER_HOMERUN_DEFAULT_FT"),
        ({"plans_reader_receptacles_per_circuit": 0}, "PLANS_READER_RECEPTACLES_PER_CIRCUIT"),
        ({"plans_reader_measure_assist_max_usd": -0.1}, "PLANS_READER_MEASURE_ASSIST_MAX_USD"),
        ({"plans_reader_consolidator_max_rounds": 0}, "PLANS_READER_CONSOLIDATOR_MAX_ROUNDS"),
        ({"plans_reader_consolidator_max_usd": 0}, "PLANS_READER_CONSOLIDATOR_MAX_USD"),
        ({"plans_reader_region_pad": 0.8}, "PLANS_READER_REGION_PAD"),
        ({"plans_reader_cluster_gap": 0}, "PLANS_READER_CLUSTER_GAP"),
        (
            {"plans_reader_census_tol_strong": 0.2, "plans_reader_census_tol_weak": 0.1},
            "PLANS_READER_CENSUS_TOL_STRONG",
        ),
        ({"plans_reader_recount_max_targets": 0}, "PLANS_READER_RECOUNT_MAX_TARGETS"),
        ({"plans_reader_recount_budget_usd": 0}, "PLANS_READER_RECOUNT_BUDGET_USD"),
        ({"plans_reader_review_budget_usd": 6.01}, "PLANS_READER_REVIEW_BUDGET_USD"),
        ({"plans_reader_review_budget_usd": 0}, "PLANS_READER_REVIEW_BUDGET_USD"),
        ({"ai_scope_attrib_max_statements": 0}, "AI_SCOPE_ATTRIB_MAX_STATEMENTS"),
        ({"ai_scope_digest_max_tokens": 10}, "AI_SCOPE_DIGEST_MAX_TOKENS"),
    ],
)
def test_bad_values_refuse_to_boot(overrides, env_name):
    with pytest.raises(ValidationError, match=env_name):
        _settings(**overrides)


def test_review_budget_cap_is_inclusive():
    assert _settings(plans_reader_review_budget_usd=6.00).plans_reader_review_budget_usd == 6.00


def test_legend_cap_just_below_the_ledger_cap_is_accepted():
    s = _settings(plans_reader_ledger_legend_max_tokens=39_999)
    assert s.plans_reader_ledger_legend_max_tokens == 39_999


# ── Eval sweep id and claim types ──────────────────────────────────────────


def test_eval_sweep_id_is_empty_or_one_uuid():
    sid = str(uuid.uuid4())
    assert _settings(eval_sweep_id=f" {sid.upper()} ").eval_sweep_id == sid
    assert _settings(eval_sweep_id="  ").eval_sweep_id == ""
    with pytest.raises(ValidationError, match="EVAL_SWEEP_ID"):
        _settings(eval_sweep_id="sweep-1")


def test_claim_types_parse_to_a_tuple_and_empty_means_all():
    assert _settings().llm_queue_claim_job_types == ()
    s = _settings(llm_queue_claim_types=" plans_reader_agent,ai_pricing,,PLANS_READER_AGENT ")
    assert s.llm_queue_claim_job_types == ("plans_reader_agent", "ai_pricing")


@pytest.mark.parametrize("raw", ["plans reader", "bid-split", "1job", "a;b"])
def test_claim_types_refuse_names_that_are_not_job_types(raw):
    with pytest.raises(ValidationError, match="LLM_QUEUE_CLAIM_TYPES"):
        _settings(llm_queue_claim_types=raw)


# ── Routes ─────────────────────────────────────────────────────────────────


def test_default_routes_cover_every_step_with_the_full_shape():
    routes = _settings().plans_reader_route_map
    assert tuple(routes) == ROUTE_STEPS
    for step, route in routes.items():
        assert set(route) == set(PLANS_READER_ROUTE_FIELDS), step
        assert isinstance(route["tools"], bool), step
        assert route["budget_usd"] is None or route["budget_usd"] > 0, step


def test_every_default_route_is_on_openai_gpt_6_astra():
    # Owner decision 2026-10-21: all live spend on OpenAI.
    for step, route in _settings().plans_reader_route_map.items():
        assert route["provider"] == "openai", step
        assert route["model"] == "gpt-6-astra", step


def test_default_route_efforts_and_tools_follow_section_8_3():
    routes = _settings().plans_reader_route_map
    for step in ("sub", "escalation", "recount", "main", "review", "revise"):
        assert routes[step]["effort"] == "medium", step
    assert routes["scope_attrib"]["effort"] == "low"
    # Sub and escalation stay single calls with no tools in this build.
    assert routes["sub"]["tools"] is False
    assert routes["escalation"]["tools"] is False
    for step in ("recount", "main", "review", "revise", "derive", "chat"):
        assert routes[step]["tools"] is True, step


def test_null_effort_inherits_the_plans_reasoning_effort():
    routes = _settings(plans_reader_reasoning_effort="high").plans_reader_route_map
    for step in ("quote", "derive", "chat"):
        assert routes[step]["effort"] == "high", step
    assert routes["main"]["effort"] == "medium"


def test_route_map_is_a_fresh_copy_per_call():
    s = _settings()
    s.plans_reader_route_map["main"]["model"] = "changed"
    assert s.plans_reader_route_map["main"]["model"] == "gpt-6-astra"
    assert PLANS_READER_DEFAULT_ROUTES["main"]["model"] == "gpt-6-astra"


def test_route_overrides_merge_over_the_defaults():
    raw = json.dumps(
        {"recount": {"effort": "HIGH", "budget_usd": 3}, "review": {"model": " gpt-6-astra-pro "}}
    )
    routes = _settings(plans_reader_routes=raw).plans_reader_route_map
    assert routes["recount"] == {
        "provider": "openai", "model": "gpt-6-astra", "effort": "high",
        "budget_usd": 3, "tools": True,
    }
    assert routes["review"]["model"] == "gpt-6-astra-pro"
    assert routes["main"] == _settings().plans_reader_route_map["main"]


def test_an_anthropic_route_is_accepted_when_named():
    raw = json.dumps({"recount": {"provider": "anthropic", "model": "claude-opus-5-5"}})
    assert _settings(plans_reader_routes=raw).plans_reader_route_map["recount"]["provider"] == "anthropic"
    for step in ("review", "revise", "derive", "scope_attrib", "quote"):
        raw = json.dumps({step: {"provider": "anthropic", "model": "claude-opus-5-5"}})
        assert _settings(plans_reader_routes=raw).plans_reader_route_map[step]["provider"] == "anthropic"


@pytest.mark.parametrize("step", ["sub", "escalation", "chat"])
def test_anthropic_on_a_complete_parts_step_refuses_to_boot(step):
    raw = json.dumps({step: {"provider": "anthropic", "model": "claude-opus-5-5"}})
    with pytest.raises(ValidationError, match=f"PLANS_READER_ROUTES is invalid: step\\(s\\) {step} cannot use provider 'anthropic'"):
        _settings(plans_reader_routes=raw, plans_reader_consolidator_mode="tools")


def test_anthropic_main_refuses_to_boot_only_while_the_consolidator_is_legacy():
    raw = json.dumps({"main": {"provider": "anthropic", "model": "claude-opus-5-5"}})
    with pytest.raises(ValidationError, match="main does while PLANS_READER_CONSOLIDATOR_MODE is legacy"):
        _settings(plans_reader_routes=raw, plans_reader_consolidator_mode="legacy")
    with pytest.raises(ValidationError, match="PLANS_READER_ROUTES is invalid"):
        _settings(plans_reader_routes=raw)  # legacy is the code default
    s = _settings(plans_reader_routes=raw, plans_reader_consolidator_mode="TOOLS")
    assert s.plans_reader_route_map["main"]["provider"] == "anthropic"


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        json.dumps({"bogus": {}}),
        json.dumps({"main": "gpt-6-astra"}),
        json.dumps({"main": {"temperature": 0}}),
        json.dumps({"main": {"provider": "google"}}),
        json.dumps({"main": {"model": ""}}),
        json.dumps({"main": {"effort": "ultra"}}),
        json.dumps({"main": {"budget_usd": 0}}),
        json.dumps({"main": {"budget_usd": True}}),
        json.dumps({"main": {"budget_usd": "5"}}),
        json.dumps({"main": {"tools": "yes"}}),
    ],
)
def test_bad_routes_refuse_to_boot(raw):
    with pytest.raises(ValidationError, match="PLANS_READER_ROUTES"):
        _settings(plans_reader_routes=raw)

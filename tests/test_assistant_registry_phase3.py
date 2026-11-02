"""The housekeeping effect tier in the registry (app/assistant/registry.py;
docs/ASSISTANT_PHASE3_PLAN.md 3.7.1).

Invariants pinned here:

  tier       housekeeping is an effect of its own: not a confirm effect, not
             a write spec effect, not a read effect;
  validate   a housekeeping spec never confirms, has no write spec, lives in
             the memory or schedule namespace, writes through one POST, PUT
             or PATCH route plus GET extras, and a memory one carries
             redact_args; redact_args and precheck are callables;
  ceilings   max_proposals_per_turn is allowed on confirm and housekeeping
             specs, max_tainted_calls_per_turn on housekeeping specs only,
             max_cards_per_turn on confirm specs only;
  toolset    Mailbox review drops every memory and schedule tool whatever
             its effect, and every non-read effect; Incognito drops the
             memory namespace and nothing else;
  allowlist  a housekeeping spec contributes all its routes to the turn's
             allowlist; execute_allowlist is unchanged.

The specs are test doubles registered for one test: the real memory and
schedule tools arrive with WI-7.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from app.assistant import registry
from app.assistant.registry import (
    EFFECT_HOUSEKEEPING,
    ProposalRefused,
    RegistryError,
    ToolRequest,
    ToolSpec,
    WriteSpec,
    schema,
)
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role

FLAGS = {"bidding": True, "pm": True, "certified_payroll": True}
SAVE = ("POST", "/assistant/tools/memory/save")
MEMORY_LIST = ("GET", "/assistant/tools/memory")
PAUSE = ("POST", "/assistant/tools/schedules/{schedule_id}/pause")
SCHEDULES = ("GET", "/assistant/schedules")


def _user(role: Role):
    return SimpleNamespace(id="u", role=role, is_active=True)


def _redact(args):
    return {"title_len": len(str(args.get("title") or ""))}


def _refuse(args, reads, ctx):
    raise ProposalRefused("probe")


def _spec(**over) -> ToolSpec:
    """A memory housekeeping double (the memory_save shape)."""
    base = dict(
        name="memory_save_probe", namespace="memory", sub_app=None, route=SAVE,
        effect=EFFECT_HOUSEKEEPING, confirm=False,
        schema=schema({"title": {"type": "string"}, "body": {"type": "string"}}),
        response_trimmer=lambda p, a, c: p[0], progress_label="Saving a note", roles=INTERNAL_ROLES,
        description="probe housekeeping", requests=lambda a: [ToolRequest(route=SAVE, path=SAVE[1], json=dict(a))],
        redact_args=_redact,
    )
    base.update(over)
    return ToolSpec(**base)


def _schedule_spec(**over) -> ToolSpec:
    base = dict(
        name="schedule_pause_probe", namespace="schedule", sub_app="bidding", route=PAUSE,
        redact_args=None, schema=schema({"schedule_id": {"type": "string"}}),
        requests=lambda a: [ToolRequest(route=PAUSE, path="/assistant/tools/schedules/x/pause")],
    )
    base.update(over)
    return _spec(**base)


def _read_spec(**over) -> ToolSpec:
    base = dict(name="memory_list_probe", route=MEMORY_LIST, effect="read", redact_args=None,
                schema=schema({"limit": registry.LIMIT_PROP}),
                requests=lambda a: [ToolRequest(route=MEMORY_LIST, path=MEMORY_LIST[1])])
    base.update(over)
    return _spec(**base)


def _write_spec(**over) -> ToolSpec:
    route = ("PUT", "/projects/{project_id}/labor")
    base = dict(
        name="set_labor_probe", namespace="bidding", sub_app="bidding", route=route,
        effect="write_internal", confirm=True, roles=WRITER_ROLES, redact_args=None,
        schema=schema({"project_id": {"type": "string"}}),
        requests=lambda a: [ToolRequest(route=route, path="/projects/x/labor")],
        write=WriteSpec(preview_requests=lambda a: [], build=_refuse, snapshot=lambda a, r: ({}, {})),
    )
    base.update(over)
    return _spec(**base)


@pytest.fixture
def probes(monkeypatch):
    """Register the doubles next to the real tools for one test."""
    registry.all_tools()  # load the real modules first
    specs = {
        "save": registry.validate(_spec(max_proposals_per_turn=3, max_tainted_calls_per_turn=1)),
        "pause": registry.validate(_schedule_spec(max_proposals_per_turn=3, max_tainted_calls_per_turn=1)),
        "list": registry.validate(_read_spec()),
        "schedules": registry.validate(_read_spec(
            name="schedule_list_probe", namespace="schedule", route=SCHEDULES,
            requests=lambda a: [ToolRequest(route=SCHEDULES, path=SCHEDULES[1])])),
        "write": registry.validate(_write_spec()),
    }
    for spec in specs.values():
        monkeypatch.setitem(registry._REGISTRY, spec.name, spec)
    return specs


# ── the tier ─────────────────────────────────────────────────────────────────


def test_housekeeping_is_its_own_tier():
    assert EFFECT_HOUSEKEEPING == "housekeeping"
    assert EFFECT_HOUSEKEEPING in registry.EFFECTS
    assert EFFECT_HOUSEKEEPING not in registry.CONFIRM_EFFECTS
    assert EFFECT_HOUSEKEEPING not in registry.WRITE_SPEC_EFFECTS
    assert EFFECT_HOUSEKEEPING not in registry.READ_EFFECTS
    assert registry.OWN_ROW_NAMESPACES == {"memory", "schedule", "report"}


def test_new_spec_fields_default_to_none():
    spec = registry.validate(_read_spec(namespace="core", name="core_probe"))
    assert spec.redact_args is None and spec.precheck is None and spec.max_tainted_calls_per_turn is None


# ── validate ─────────────────────────────────────────────────────────────────


def test_a_well_formed_housekeeping_spec_validates():
    spec = registry.validate(_spec(extra_routes=(MEMORY_LIST,), precheck=lambda a: None,
                                   max_proposals_per_turn=3, max_tainted_calls_per_turn=1))
    assert spec.effect == EFFECT_HOUSEKEEPING and spec.routes == (SAVE, MEMORY_LIST)
    # A schedule housekeeping spec needs no redact_args; PUT and PATCH work too.
    for method in ("POST", "PUT", "PATCH"):
        registry.validate(_schedule_spec(route=(method, PAUSE[1])))


@pytest.mark.parametrize("over, message", [
    ({"confirm": True}, "never confirms"),
    ({"write": WriteSpec(preview_requests=lambda a: [], build=_refuse, snapshot=lambda a, r: ({}, {}))},
     "no write spec"),
    ({"namespace": "bidding"}, "memory or schedule namespace"),
    ({"namespace": "core"}, "memory or schedule namespace"),
    ({"route": ("GET", "/assistant/tools/memory")}, "POST, PUT or PATCH"),
    ({"extra_routes": (("POST", "/assistant/tools/memory/other"),)}, "GET only"),
    ({"redact_args": None}, "needs redact_args"),
    ({"redact_args": "hash it"}, "redact_args must be a callable"),
    ({"precheck": {"credential": True}}, "precheck must be a callable"),
    ({"max_cards_per_turn": 1}, "confirm tools only"),
    ({"max_tainted_calls_per_turn": 0}, "positive integer"),
    ({"max_tainted_calls_per_turn": True}, "positive integer"),
    ({"max_proposals_per_turn": "3"}, "positive integer"),
])
def test_housekeeping_refusals(over, message):
    with pytest.raises(RegistryError, match=message):
        registry.validate(_spec(**over))


def test_tainted_ceiling_is_refused_on_every_other_effect():
    with pytest.raises(RegistryError, match="housekeeping tools only"):
        registry.validate(_read_spec(max_tainted_calls_per_turn=1))
    with pytest.raises(RegistryError, match="housekeeping tools only"):
        registry.validate(_write_spec(max_tainted_calls_per_turn=1))


def test_proposal_ceiling_is_allowed_on_confirm_and_housekeeping_only():
    assert registry.validate(_write_spec(max_proposals_per_turn=2)).max_proposals_per_turn == 2
    assert registry.validate(_spec(max_proposals_per_turn=3)).max_proposals_per_turn == 3
    with pytest.raises(RegistryError, match="confirm and housekeeping tools only"):
        registry.validate(_read_spec(max_proposals_per_turn=3))


def test_redact_args_and_precheck_are_allowed_on_other_tiers():
    """The ledger form and the precheck are generic: a read or a write spec
    may carry them too (the executor applies them on every ledger row)."""
    read = registry.validate(_read_spec(redact_args=_redact, precheck=lambda a: None))
    write = registry.validate(_write_spec(redact_args=_redact))
    assert read.redact_args is _redact and write.redact_args is _redact


def test_registration_keeps_the_new_fields(monkeypatch):
    registry.all_tools()
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))
    spec = registry.register(_spec(max_tainted_calls_per_turn=1))
    assert registry.get(spec.name) is spec and spec.redact_args is _redact
    assert spec.max_tainted_calls_per_turn == 1
    # The same spec registers twice; a different one under the name is refused.
    assert registry.register(spec) == spec
    with pytest.raises(RegistryError, match="already registered"):
        registry.register(_spec(max_tainted_calls_per_turn=2))


# ── the toolset ──────────────────────────────────────────────────────────────


def test_mailbox_review_drops_memory_and_schedule_tools(probes):
    normal = {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE), FLAGS)}
    review_specs = registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, mode=registry.MODE_MAILBOX_REVIEW)
    review = {s.name for s in review_specs}
    assert {s.name for s in probes.values()} <= normal
    # Read tools in those namespaces go too, not only the housekeeping ones.
    assert not review & {s.name for s in probes.values()}
    assert all(s.namespace not in registry.OWN_ROW_NAMESPACES for s in review_specs)
    assert all(s.effect in registry.READ_EFFECTS for s in review_specs)
    assert "app_search" in review


def test_incognito_drops_only_the_memory_namespace(probes):
    normal = registry.toolset_for(_user(Role.EXECUTIVE), FLAGS)
    incognito = registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, incognito=True)
    names = {s.name for s in incognito}
    assert "memory_save_probe" not in names and "memory_list_probe" not in names
    assert {"schedule_pause_probe", "schedule_list_probe", "set_labor_probe"} <= names
    assert names == {s.name for s in normal if s.namespace != "memory"}
    # Both together: review already drops memory and schedule.
    both = registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, mode=registry.MODE_MAILBOX_REVIEW, incognito=True)
    assert {s.name for s in both} == {s.name for s in registry.toolset_for(
        _user(Role.EXECUTIVE), FLAGS, mode=registry.MODE_MAILBOX_REVIEW)}


def test_an_accountant_keeps_housekeeping_tools_but_no_write(probes):
    names = {s.name for s in registry.toolset_for(_user(Role.ACCOUNTANT), FLAGS)}
    assert {"memory_save_probe", "schedule_pause_probe"} <= names
    assert "set_labor_probe" not in names


def test_sub_app_and_disabled_tools_still_apply_to_housekeeping(probes):
    off = {**FLAGS, "bidding": False}
    assert "schedule_pause_probe" not in {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE), off)}
    row = {"disabled_tools": ["memory_save_probe"]}
    names = {s.name for s in registry.toolset_for(_user(Role.EXECUTIVE), FLAGS, settings_row=row)}
    assert "memory_save_probe" not in names and "memory_list_probe" in names


# ── allowlists ───────────────────────────────────────────────────────────────


def test_turn_allowlist_holds_every_housekeeping_route():
    save = registry.validate(_spec(extra_routes=(MEMORY_LIST,)))
    write = registry.validate(_write_spec(extra_routes=(("GET", "/projects/{project_id}"),)))
    pairs = registry.turn_allowlist([save, write])
    assert SAVE in pairs and MEMORY_LIST in pairs
    assert write.route not in pairs and ("GET", "/projects/{project_id}") in pairs
    assert registry.execute_allowlist(save) == frozenset({SAVE, MEMORY_LIST})


def test_validate_returns_a_copy_with_normalized_routes():
    spec = registry.validate(_spec(route=("post", SAVE[1])))
    assert spec.route == SAVE
    assert dataclasses.replace(spec) == spec

"""Registry rules added in phase 2 (app/assistant/registry.py;
docs/ASSISTANT_PHASE2_PLAN.md 3.9.1). tests/test_assistant_registry.py
stays the tool set's own file.

  writespec  write_internal, expensive and external_send specs need a
             WriteSpec (an expensive spec without one would be inert: no
             preview routes in turn_allowlist, propose answers not_offered);
  ceilings   max_cards_per_turn and max_proposals_per_turn default to None,
             take positive integers only, and only on confirm tools;
  live       every registered tool passes the stricter validate().
"""

from __future__ import annotations

import dataclasses

import pytest

from app.assistant import registry
from app.assistant.registry import RegistryError, ToolSpec, WriteSpec, schema
from app.core.roles import WRITER_ROLES


def _write() -> WriteSpec:
    return WriteSpec(preview_requests=lambda a: [], build=lambda a, r, c: None, snapshot=lambda a, r: ({}, {}))


def _spec(**over) -> ToolSpec:
    base = dict(
        name="probe_split", namespace="bidding", sub_app="bidding", route=("POST", "/projects/{project_id}/x"),
        extra_routes=(("GET", "/projects/{project_id}/x/preview"),), effect="expensive", confirm=True,
        schema=schema({"project_id": {"type": "string"}}), response_trimmer=lambda p, a, c: p,
        progress_label="Preparing a probe", roles=WRITER_ROLES, description="probe", requests=lambda a: [],
        write=_write(),
    )
    base.update(over)
    return ToolSpec(**base)


@pytest.mark.parametrize("effect", ["write_internal", "expensive", "external_send"])
def test_write_effects_need_a_write_spec(effect):
    assert registry.validate(_spec(effect=effect)).effect == effect
    with pytest.raises(RegistryError, match="needs a write spec"):
        registry.validate(_spec(effect=effect, write=None))


def test_a_sensitive_read_needs_a_write_spec():
    # Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md 3.1): sensitive_read joined
    # WRITE_SPEC_EFFECTS; without a WriteSpec the card would be inert.
    spec = _spec(effect="sensitive_read", route=("GET", "/projects/{project_id}/x"), extra_routes=(), write=None)
    with pytest.raises(RegistryError, match="needs a write spec"):
        registry.validate(spec)


def test_an_expensive_spec_offers_only_its_previews_in_a_turn():
    spec = registry.validate(_spec())
    assert registry.turn_allowlist([spec]) == {("GET", "/projects/{project_id}/x/preview")}
    assert registry.execute_allowlist(spec) == {("POST", "/projects/{project_id}/x"),
                                                ("GET", "/projects/{project_id}/x/preview")}


def test_ceilings_default_to_none_and_take_positive_integers():
    spec = registry.validate(_spec())
    assert spec.max_cards_per_turn is None and spec.max_proposals_per_turn is None
    spec = registry.validate(_spec(max_cards_per_turn=1, max_proposals_per_turn=3))
    assert (spec.max_cards_per_turn, spec.max_proposals_per_turn) == (1, 3)
    for bad in (0, -1, True, 1.5, "1"):
        with pytest.raises(RegistryError, match="positive integer"):
            registry.validate(_spec(max_cards_per_turn=bad))
        with pytest.raises(RegistryError, match="positive integer"):
            registry.validate(_spec(max_proposals_per_turn=bad))


def test_ceilings_apply_to_confirm_tools_only():
    read = _spec(effect="read", confirm=False, route=("GET", "/projects/{project_id}/x"), extra_routes=(),
                 write=None, max_cards_per_turn=1)
    with pytest.raises(RegistryError, match="confirm tools only"):
        registry.validate(read)


def test_every_registered_tool_passes_the_phase_2_rules():
    for spec in registry.all_tools().values():
        again = registry.validate(dataclasses.replace(spec))
        if spec.effect in registry.WRITE_SPEC_EFFECTS:
            assert again.write is not None, spec.name

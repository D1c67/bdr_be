"""The actor provenance core (app/core/provenance.py, 0172).

Invariants pinned here (docs/ASSISTANT_PHASE1_PLAN.md 3.12):

  fields()           {} outside an assistant run, so a human write sends no
                     new key (code deployed before 0172 never hits PGRST204);
                     {} for a row that names another actor; the assistant
                     pair inside acting_as_assistant.
  stamp()            always both keys (the "last writer" rows: labor_reviews).
  metadata_fields()  {} outside a run, the pair inside (notification metadata).
  context            the contextvar follows the request into asyncio.to_thread
                     and Starlette's run_in_threadpool, where sync writers run.

No database: pure functions over a contextvar.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.concurrency import run_in_threadpool

from app.core import provenance

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "99999999-9999-4999-8999-999999999999"
RUN = "22222222-2222-4222-8222-222222222222"
APPROVAL = "33333333-3333-4333-8333-333333333333"

ASSISTANT_PAIR = {"actor_kind": "assistant", "assistant_run_id": RUN}
HUMAN_PAIR = {"actor_kind": "human", "assistant_run_id": None}


# ── fields() ─────────────────────────────────────────────────────────────────


def test_fields_is_empty_outside_a_run():
    assert provenance.fields() == {}
    assert provenance.fields(USER) == {}
    assert provenance.fields(None) == {}


def test_fields_carries_both_keys_inside_a_run():
    with provenance.acting_as_assistant(USER, RUN, APPROVAL):
        assert provenance.fields() == ASSISTANT_PAIR
        assert provenance.fields(USER) == ASSISTANT_PAIR
    assert provenance.fields() == {}


def test_fields_is_empty_for_a_row_naming_another_actor():
    with provenance.acting_as_assistant(USER, RUN):
        assert provenance.fields(OTHER) == {}
        # A row with no actor at all is still written inside the run.
        assert provenance.fields(None) == ASSISTANT_PAIR


def test_fields_compares_the_actor_as_text():
    import uuid

    with provenance.acting_as_assistant(USER, RUN):
        assert provenance.fields(uuid.UUID(USER)) == ASSISTANT_PAIR


def test_fields_output_never_trips_the_run_id_check():
    """actor_kind <> 'assistant' or assistant_run_id is not null."""
    outputs = [provenance.fields(), provenance.fields(OTHER), provenance.stamp(),
               provenance.metadata_fields()]
    with provenance.acting_as_assistant(USER, RUN):
        outputs += [provenance.fields(), provenance.fields(USER), provenance.fields(OTHER),
                    provenance.stamp(), provenance.metadata_fields()]
    for out in outputs:
        if out.get("actor_kind") == "assistant":
            assert out.get("assistant_run_id")
        assert out.get("actor_kind", "human") in provenance.ACTOR_KINDS


# ── stamp() and metadata_fields() ────────────────────────────────────────────


def test_stamp_always_has_both_keys():
    assert provenance.stamp() == HUMAN_PAIR
    with provenance.acting_as_assistant(USER, RUN):
        assert provenance.stamp() == ASSISTANT_PAIR
    assert provenance.stamp() == HUMAN_PAIR


def test_metadata_fields_follow_the_context():
    assert provenance.metadata_fields() == {}
    with provenance.acting_as_assistant(USER, RUN, APPROVAL):
        assert provenance.metadata_fields() == ASSISTANT_PAIR
    assert provenance.metadata_fields() == {}


def test_outputs_are_fresh_dicts():
    """A caller may mutate what it spreads; the next call is unaffected."""
    with provenance.acting_as_assistant(USER, RUN):
        a = provenance.fields()
        a["actor_kind"] = "human"
        assert provenance.fields() == ASSISTANT_PAIR
    s = provenance.stamp()
    s["actor_kind"] = "assistant"
    assert provenance.stamp() == HUMAN_PAIR


def test_context_resets_after_an_error():
    with pytest.raises(KeyError):
        with provenance.acting_as_assistant(USER, RUN):
            raise KeyError("boom")
    assert provenance.fields() == {} and provenance.stamp() == HUMAN_PAIR


# ── the context crosses into worker threads ──────────────────────────────────


async def test_fields_survive_asyncio_to_thread():
    with provenance.acting_as_assistant(USER, RUN):
        inside = await asyncio.to_thread(provenance.fields, USER)
        stamped = await asyncio.to_thread(provenance.stamp)
        md = await asyncio.to_thread(provenance.metadata_fields)
    outside = await asyncio.to_thread(provenance.fields, USER)
    assert inside == ASSISTANT_PAIR and stamped == ASSISTANT_PAIR and md == ASSISTANT_PAIR
    assert outside == {}


async def test_fields_survive_run_in_threadpool():
    with provenance.acting_as_assistant(USER, RUN):
        inside = await run_in_threadpool(provenance.fields, USER)
        other = await run_in_threadpool(provenance.fields, OTHER)
    outside = await run_in_threadpool(provenance.fields)
    assert inside == ASSISTANT_PAIR
    assert other == {} and outside == {}


async def test_parallel_tasks_do_not_leak_the_context():
    """A human request served concurrently with an assistant one never sees
    the assistant's context (each task has its own copy)."""
    gate = asyncio.Event()

    async def assistant():
        with provenance.acting_as_assistant(USER, RUN):
            await gate.wait()
            return provenance.fields()

    async def human():
        gate.set()
        return provenance.fields()

    a, h = await asyncio.gather(assistant(), human())
    assert a == ASSISTANT_PAIR and h == {}

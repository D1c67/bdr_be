"""The v1 personal agent (user_browser_read, agents/user_browser/) is gone in 1.1.0.

Shared-code checks that used to live in test_user_browser.py also live here:
the personal heartbeat must never touch the org routes.
"""
import importlib.util
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bdr_agent.agents import REGISTRY, ceiling_for
from bdr_agent.browser.browser import ChromiumProvider
from bdr_agent.heartbeat import HeartbeatThread, JobTracker
from bdr_agent.protocol import ClaimedJob, HeartbeatResponse, TypeConfig
from bdr_agent.redact import LiveSecrets
from bdr_agent.slot import Slot, SlotDeps
from bdr_agent.spool import Spool

from .test_heartbeat import Sink

RETIRED = "user_browser_read"
TC = TypeConfig(enabled=True, max_concurrency=1, max_steps=10, max_active_seconds=60)


def job(principal="user", agent_type=RETIRED):
    return ClaimedJob(job_id=uuid4(), agent_type=agent_type, principal_kind=principal,
                      principal_user_id=uuid4() if principal == "user" else None, epoch=1, attempts=1,
                      lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
                      input={"service": "procore", "url": "https://app.procore.com/", "objective": "x"})


class MustNotRun:
    needs_browser = False
    version = "1.0.0"

    async def run(self, ctx):
        raise AssertionError("a retired type must never run")

    def user_slot_parts(self, base_client):
        raise AssertionError("a retired type must never build a personal client")


def test_v1_package_is_gone_and_nothing_registers_it():
    assert importlib.util.find_spec("bdr_agent.agents.user_browser") is None
    assert RETIRED not in REGISTRY
    assert "user_browser" in REGISTRY
    with pytest.raises(ValueError, match="no browser ceiling"):
        ceiling_for(RETIRED)


@pytest.mark.parametrize("principal", ["user", "org"])
async def test_slot_refuses_the_retired_type_for_any_principal(tmp_path, principal):
    base = NS(spool_dir=tmp_path / "spool", finish=AsyncMock(), requeue=AsyncMock(), call=AsyncMock(),
              append_steps=AsyncMock())
    deps = SlotDeps(client=base, tracker=JobTracker(), spool=Spool(tmp_path / "spool"), secrets=LiveSecrets(),
                    runners={RETIRED: MustNotRun()}, tick=0.01, flush_interval=0.01)
    claimed = job(principal)
    outcome = await Slot(RETIRED, 1, deps).run(claimed, TC)
    assert (outcome.terminal, outcome.detail) == ("none", "retired_type")
    assert not deps.tracker.holds(claimed.job_id, claimed.epoch)
    base.finish.assert_not_awaited()
    base.requeue.assert_not_awaited()
    base.call.assert_not_awaited()
    assert deps.client is base


@pytest.mark.parametrize("principal", ["user", "org"])
async def test_provider_never_builds_a_context_for_the_retired_type(principal):
    provider = ChromiumProvider(ceilings=ceiling_for)    # not started: nothing may launch
    with pytest.raises(ValueError, match="Personal job principal invalid"):
        await provider.new_context(job(principal))
    with pytest.raises(ValueError, match="Personal job principal invalid"):
        await provider.new_context(job("user", "rfi_from_email"))


def test_user_heartbeat_and_orphans_never_use_org_routes():
    calls = []
    response = HeartbeatResponse(config_version=1, orphans=[{"job_id": uuid4(), "epoch": 2}])
    client = NS(call=lambda name, **kw: (calls.append(name) or response))
    thread = HeartbeatThread(client, JobTracker(), Sink(), boot_id=uuid4(), config_version=lambda: 1,
                             slots_free=lambda: {"user_browser": 1}, principal_kind=lambda: "user")
    thread.beat_once()
    thread.beat_once()
    assert calls == ["user_heartbeat", "user_heartbeat", "user_requeue"]

"""Shared fixtures: a stateful mock BDR and clients wired to it (no network)."""

import uuid

import pytest

from bdr_agent.client import BdrClient, RetryPolicy, SyncBdrClient
from bdr_agent.local_config import Paths

from .mock_bdr import MockBdr

BASE = "https://bdr.test"
FAST_RETRY = RetryPolicy(max_attempts=3, base_s=0.001, cap_s=0.002)


async def _no_sleep(_s: float) -> None:
    return None


@pytest.fixture
def bdr() -> MockBdr:
    return MockBdr()


@pytest.fixture
def worker(bdr: MockBdr):
    return bdr.add_worker()


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "home")
    p.ensure()
    return p


def make_clients(bdr: MockBdr, token: str | None, boot_id: uuid.UUID | None = None,
                 retry: RetryPolicy = FAST_RETRY,
                 in_job_retry: RetryPolicy | None = None) -> tuple[BdrClient, SyncBdrClient]:
    transport = bdr.transport()
    # Tests default the in-job policy to the fast one: the production policy
    # (no attempt cap, 15-minute deadline) would spin against a down mock.
    ac = BdrClient(BASE, token=token, boot_id=boot_id, version="1.0.0", transport=transport,
                   retry=retry, sleep=_no_sleep, in_job_retry=in_job_retry or retry)
    sc = SyncBdrClient(BASE, token=token, boot_id=boot_id, version="1.0.0", transport=transport,
                       retry=retry, sleep=lambda _s: None)
    return ac, sc


async def hello(ac: BdrClient, *, types: dict[str, str] | None = None) -> uuid.UUID:
    from bdr_agent.protocol import HelloRequest, WorkerCapabilities

    boot = uuid.uuid4()
    ac.boot_id = boot
    caps = WorkerCapabilities(types=types or {"rfi_from_email": "1.0.0", "canary": "1.0.0"}, slots=3)
    await ac.hello(HelloRequest(boot_id=boot, version="1.0.0", capabilities=caps))
    return boot


async def hello_and_claim(bdr: MockBdr, ac: BdrClient, agent_type: str = "rfi_from_email"):
    from bdr_agent.protocol import ClaimRequest

    boot = await hello(ac)
    bdr.add_job(agent_type)
    resp = await ac.claim(ClaimRequest(boot_id=boot, capacity={agent_type: 1}))
    assert len(resp.jobs) == 1
    return resp.jobs[0]

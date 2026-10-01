"""Security review, group llm-timeouts (finding 33): third-party LLM clients
carry explicit timeout and retry bounds, and the RFP sweep lease is sized to
outlive one worst-case call on either route."""

import pytest

from app.core.config import Settings
from app.services import llm


def _s(**over) -> Settings:
    return Settings(_env_file=None, **over)


@pytest.fixture(autouse=True)
def _fresh_client_cache(monkeypatch):
    monkeypatch.setattr(llm, "_clients", {})


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_third_party_clients_have_explicit_timeout_and_retries(provider):
    s = _s(third_party_llm_timeout_seconds=90, third_party_llm_max_retries=1)
    client = llm._build_client(llm.Route(provider=provider, model="m", api_key="k"), s)
    assert client.max_retries == 1
    timeout = client.timeout
    read = getattr(timeout, "read", timeout)
    assert read == 90.0


def test_third_party_defaults_are_well_below_sdk_defaults():
    s = _s()
    assert s.third_party_llm_timeout_seconds == 120
    assert s.third_party_llm_max_retries == 1
    client = llm._build_client(llm.Route(provider="openai", model="m", api_key="k"), s)
    assert client.max_retries == 1
    assert getattr(client.timeout, "read", client.timeout) == 120.0


def test_client_cache_key_tracks_third_party_bounds():
    route = llm.Route(provider="anthropic", model="m", api_key="k")
    a = llm._client_for(route, _s(third_party_llm_timeout_seconds=60))
    b = llm._client_for(route, _s(third_party_llm_timeout_seconds=30))
    assert a is not b
    assert getattr(b.timeout, "read", b.timeout) == 30.0


def test_lease_default_stretches_to_cover_third_party_worst_case():
    # 180 wait + 400 x (1 + 1) = 980, above the 600 default and the
    # self-hosted 180 + 120 = 300.
    s = _s(third_party_llm_timeout_seconds=400, third_party_llm_max_retries=1)
    assert s.rfp_email_ingestion_lease_seconds == 980


def test_explicit_lease_shorter_than_third_party_call_refuses_to_boot():
    with pytest.raises(ValueError, match="THIRD_PARTY_LLM_TIMEOUT_SECONDS"):
        _s(
            rfp_email_ingestion_lease_seconds=600,
            third_party_llm_timeout_seconds=300,
            third_party_llm_max_retries=1,
        )


def test_default_lease_still_covers_both_routes():
    s = _s()
    assert s.rfp_email_ingestion_lease_seconds == 600
    assert s.rfp_email_ingestion_lease_seconds >= s.llm_background_wait_seconds + max(
        s.self_hosted_llm_timeout_seconds,
        s.third_party_llm_timeout_seconds * (s.third_party_llm_max_retries + 1),
    )


def test_bad_third_party_bounds_refuse_to_boot():
    with pytest.raises(ValueError, match="THIRD_PARTY_LLM_TIMEOUT_SECONDS"):
        _s(third_party_llm_timeout_seconds=0)
    with pytest.raises(ValueError, match="THIRD_PARTY_LLM_MAX_RETRIES"):
        _s(third_party_llm_max_retries=-1)

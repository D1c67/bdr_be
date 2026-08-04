"""LLM health monitoring (services/llm_health) — what the sidebar's Model
status indicator reports.

The indicator is only worth having if its colours are trustworthy, so these
pin the grading rules rather than the plumbing:

  * green ONLY when every AI feature has a reachable provider serving its model;
  * red when nothing works, amber when some of it does — never the reverse;
  * a self-hosted server running the WRONG model is a fault (calls 404), while
    an unlisted 3rd-party model id is not (aliases resolve fine);
  * the probe fails fast on its own short timeout, not the multi-minute
    generation timeout, so a stopped box can't pin a worker;
  * the payload never carries the endpoint URL or an API key, matching the
    posture of services/llm itself.
"""

from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services import llm, llm_health

SH_URL = "http://model-box.internal:8000/v1"
SH_KEY = "sh-secret-key"


def _sh(**over) -> Settings:
    """Self-hosted mode with every feature pointed at one served model."""
    base = dict(
        _env_file=None,
        full_self_hosted_llms_enabled=True,
        self_hosted_llm_local_base_url=SH_URL,
        self_hosted_llm_local_api_key=SH_KEY,
        self_hosted_llm_timeout_seconds=1800,
        **{f"self_hosted_{f}_model": "qwen-3.5-4b" for f in llm._FEATURES},
    )
    base.update(over)
    return Settings(**base)


def _tp(**over) -> Settings:
    base = dict(_env_file=None, anthropic_api_key="a-key", openai_api_key="o-key")
    base.update(over)
    return Settings(**base)


def _stub_client(monkeypatch, *, serves=(), raises=None):
    """Replace the SDK client with one exposing only `models.list`, recording
    the timeout each probe asks for."""
    seen: dict = {"timeouts": [], "calls": 0}

    def models_list():
        seen["calls"] += 1
        if raises is not None:
            raise raises
        return [SimpleNamespace(id=m) for m in serves]

    client = SimpleNamespace(models=SimpleNamespace(list=models_list))
    client.with_options = lambda **kw: (seen["timeouts"].append(kw.get("timeout")), client)[1]
    monkeypatch.setattr(llm, "_client_for", lambda route, settings: client)
    return seen


@pytest.fixture(autouse=True)
def _clean_cache():
    llm_health.reset_cache()
    yield
    llm_health.reset_cache()


def _by_key(snapshot):
    return {f.key: f for f in snapshot.features}


# ── Green ────────────────────────────────────────────────────────────────────


def test_server_up_and_serving_the_configured_model_is_green(monkeypatch):
    _stub_client(monkeypatch, serves=["qwen-3.5-4b"])
    snap = llm_health.check(_sh())

    assert snap.status == "ok"
    assert snap.mode == "self_hosted"
    assert {f.state for f in snap.features} == {"ok"}
    assert len(snap.features) == len(llm._FEATURES)
    (provider,) = snap.providers
    assert provider.state == "ok"
    assert provider.model_count == 1
    assert provider.latency_ms is not None


def test_third_party_pool_probes_both_vendors(monkeypatch):
    s = _tp()
    _stub_client(monkeypatch, serves=[s.claude_boq_model, s.openai_proposal_model])
    snap = llm_health.check(s)

    assert snap.mode == "third_party"
    assert {p.provider for p in snap.providers} == {"anthropic", "openai"}
    assert snap.status == "ok"


# ── Red ──────────────────────────────────────────────────────────────────────


def test_stopped_model_server_is_red(monkeypatch):
    req = httpx.Request("GET", f"{SH_URL}/models")
    _stub_client(monkeypatch, raises=openai.APIConnectionError(request=req))
    snap = llm_health.check(_sh())

    assert snap.status == "down"
    assert snap.providers[0].state == "unreachable"
    assert {f.state for f in snap.features} == {"provider_down"}


def test_timeout_reads_as_unreachable(monkeypatch):
    req = httpx.Request("GET", f"{SH_URL}/models")
    _stub_client(monkeypatch, raises=openai.APITimeoutError(request=req))
    snap = llm_health.check(_sh())

    assert snap.providers[0].state == "unreachable"
    assert snap.status == "down"


def test_rejected_credentials_read_as_unauthorized(monkeypatch):
    err = openai.AuthenticationError(
        "invalid key",
        response=httpx.Response(
            401, request=httpx.Request("GET", f"{SH_URL}/models")
        ),
        body=None,
    )
    _stub_client(monkeypatch, raises=err)
    snap = llm_health.check(_sh())

    assert snap.providers[0].state == "unauthorized"
    assert snap.status == "down"


def test_probe_without_an_endpoint_fails_closed(monkeypatch):
    # Settings refuses to BOOT in this shape (config validates the active
    # target's URL), so this only guards the probe itself: report it rather
    # than dialing an empty base URL.
    seen = _stub_client(monkeypatch, serves=["qwen-3.5-4b"])
    route = llm.Route(provider="self_hosted", model="qwen-3.5-4b", api_key="", base_url="")
    status, ids = llm_health._probe("self_hosted", route, _sh())

    assert (status.state, ids) == ("not_configured", [])
    assert seen["calls"] == 0


def test_no_keys_at_all_is_red(monkeypatch):
    _stub_client(monkeypatch, serves=[])
    snap = llm_health.check(Settings(_env_file=None))

    assert snap.status == "down"
    assert {p.state for p in snap.providers} == {"not_configured"}
    assert {f.state for f in snap.features} == {"unconfigured"}


# ── Amber ────────────────────────────────────────────────────────────────────


def test_server_serving_a_different_model_flags_those_features(monkeypatch):
    # The box was restarted on another model; one feature was pointed at it.
    _stub_client(monkeypatch, serves=["llama-3.3-8b"])
    snap = llm_health.check(_sh(self_hosted_boq_model="llama-3.3-8b"))
    features = _by_key(snap)

    assert snap.status == "degraded"
    assert features["boq"].state == "ok"
    assert features["proposal"].state == "model_missing"
    assert "qwen-3.5-4b" in features["proposal"].detail
    # The server itself is fine — the fault is the mismatch, and the modal must
    # say so rather than blaming the connection.
    assert snap.providers[0].state == "ok"


def test_one_vendor_unconfigured_leaves_the_rest_green(monkeypatch):
    s = _tp(anthropic_api_key="")
    _stub_client(monkeypatch, serves=[s.openai_proposal_model])
    snap = llm_health.check(s)
    features = _by_key(snap)

    assert snap.status == "degraded"
    assert features["boq"].state == "unconfigured"       # Anthropic-routed
    assert features["proposal"].state == "ok"            # OpenAI-routed


def test_unset_self_hosted_model_is_unconfigured_not_broken(monkeypatch):
    _stub_client(monkeypatch, serves=["qwen-3.5-4b"])
    snap = llm_health.check(_sh(self_hosted_translate_model=""))
    features = _by_key(snap)

    assert snap.status == "degraded"
    assert features["translate"].state == "unconfigured"
    assert "SELF_HOSTED_TRANSLATE_MODEL" in features["translate"].detail


# ── Catalog matching ─────────────────────────────────────────────────────────


def test_unlisted_third_party_model_is_a_note_not_a_fault(monkeypatch):
    # 3rd-party catalogs don't necessarily list alias ids — an unlisted model
    # must not paint the whole indicator amber.
    _stub_client(monkeypatch, serves=["some-other-model"])
    snap = llm_health.check(_tp())

    assert snap.status == "ok"
    assert all(f.model_listed is False for f in snap.features)
    assert "alias" in _by_key(snap)["boq"].detail


def test_dated_variant_counts_as_the_configured_alias(monkeypatch):
    s = _tp()
    _stub_client(monkeypatch, serves=[f"{s.claude_boq_model}-20260214"])
    snap = llm_health.check(s)

    assert _by_key(snap)["boq"].model_listed is True


# ── Safety / cost ────────────────────────────────────────────────────────────


def test_probe_uses_the_short_health_timeout(monkeypatch):
    seen = _stub_client(monkeypatch, serves=["qwen-3.5-4b"])
    s = _sh(llm_health_probe_timeout_seconds=3)
    llm_health.check(s)

    # Never the generation timeout (1800s here) — a status check must fail fast.
    assert seen["timeouts"] == [3.0]


def test_payload_carries_no_endpoint_or_key(monkeypatch):
    req = httpx.Request("GET", f"{SH_URL}/models")
    _stub_client(monkeypatch, raises=openai.APIConnectionError(request=req))
    body = repr(llm_health.check(_sh()).to_dict())

    assert SH_URL not in body
    assert SH_KEY not in body
    assert "model-box.internal" not in body


def test_cache_serves_repeat_reads_and_force_bypasses_it(monkeypatch):
    seen = _stub_client(monkeypatch, serves=["qwen-3.5-4b"])
    s = _sh()

    llm_health.cached(s)
    llm_health.cached(s)
    assert seen["calls"] == 1                    # second read served from cache

    llm_health.cached(s, force=True)
    assert seen["calls"] == 2                    # "Check now" always re-probes


# ── Endpoint ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client() -> TestClient:
    import app.main

    return TestClient(app.main.app, raise_server_exceptions=False)


def test_status_endpoints_require_a_token(client):
    assert client.get("/model-status").status_code == 401
    assert client.post("/model-status/check").status_code == 401

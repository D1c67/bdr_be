"""Liveness monitoring for the ACTIVE LLM pool — what the sidebar's "Model
status" indicator reads.

Every AI feature routes through services/llm, which resolves each one to a
provider + model from the env (see that module). This module answers the
question that routing table can't: is what we resolved to actually reachable
right now? The self-hosted box is the sharp edge — it is a single EC2 instance
that can be stopped, and while FULL_SELF_HOSTED_LLMS_ENABLED is on there is no
fallback by design, so a stopped box silently degrades eight features at once.
Before this, the first sign was a user's BOQ extraction failing.

How a provider is probed
------------------------
`GET /models` (the OpenAI-compatible catalog, and its Anthropic equivalent) —
a free metadata call that proves three things at once: the network path is
open, the credentials are accepted, and, for the self-hosted server, WHICH
model it is currently serving. It is deliberately not a test generation: a
status check must not spend tokens, and for the self-hosted box liveness of
/models and of /chat/completions are the same process.

That distinction is stated in the UI, so nobody reads a green bar as "the model
answers well" — only as "the model is connected and serving".

What each state means
---------------------
- ok        — provider answered, and the feature's model is served by it.
- unconfigured — no model/key set for this feature, so the feature is off. Its
  callers already degrade gracefully (manual entry, skipped generation).
- model_missing — self-hosted only: the server is up but is serving a DIFFERENT
  model than this feature asks for, so every call 404s. (A third-party catalog
  is only advisory — aliases like `claude-opus-4-8` may resolve without being
  listed — so an unlisted 3rd-party model is reported as a note, never a fault.)
- provider_down — the provider did not answer, or rejected our credentials.

Overall = green when every feature is ok, red when NONE is, amber in between.

Caching / cost
--------------
A snapshot is held in memory and refreshed by a background poller (one per
uvicorn worker — the probe is a free metadata call, so N workers polling is not
worth coordinating; it does mean two workers can report timestamps seconds
apart). Reads serve the cached snapshot; a read that finds it stale re-probes
inline under a lock, so the endpoint still works with the poller switched off
and a burst of page loads collapses into ONE probe.

Security posture: matches services/llm — the endpoint URL and API keys are
never returned, logged, or embedded in an error string. Provider error messages
are replaced with fixed sentences (a raw SDK message can carry the URL or the
response body); only the HTTP status code is passed through.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.services import llm

logger = logging.getLogger(__name__)

# Human names for the eight AI features, in the order the modal lists them.
# Keyed by services/llm._FEATURES; the frontend prefers its own translated
# label and falls back to these, so a feature added there still reads sensibly
# before the catalog catches up.
FEATURE_LABELS: dict[str, str] = {
    "boq": "BOQ extraction",
    "estimate": "Estimate recap extraction",
    "quote_pdf": "Vendor quote reading",
    "proposal": "Proposal scope lines",
    "email_vary": "RFQ email wording",
    "email_match": "Email → project matching",
    "aliases": "Submittal alternate names",
    "translate": "Interface translation",
}

_PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI",
}


@dataclass(frozen=True)
class ProviderStatus:
    provider: str          # "self_hosted" | "anthropic" | "openai"
    label: str             # human name — never a URL
    state: str             # ok | unreachable | unauthorized | error | not_configured
    detail: str            # one fixed sentence, safe to show a user
    latency_ms: int | None = None
    model_count: int | None = None   # models the provider says it serves


@dataclass(frozen=True)
class FeatureStatus:
    key: str
    label: str
    provider: str
    model: str             # "" when nothing is configured
    state: str             # ok | unconfigured | model_missing | provider_down
    detail: str
    # Only meaningful for 3rd-party providers: whether the configured model id
    # appeared in the catalog. Advisory (aliases), so it never sets the state.
    model_listed: bool | None = None


@dataclass(frozen=True)
class Snapshot:
    status: str            # ok | degraded | down  →  green | amber | red
    mode: str              # self_hosted | third_party
    checked_at: str        # ISO-8601 UTC
    duration_ms: int
    providers: list[ProviderStatus] = field(default_factory=list)
    features: list[FeatureStatus] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Probing ──────────────────────────────────────────────────────────────────


def _provider_label(provider: str, settings: Settings) -> str:
    if provider != "self_hosted":
        return _PROVIDER_LABELS[provider]
    where = "EC2" if settings.self_hosted_llm_target == "ec2" else "local"
    return f"Self-hosted model server ({where})"


def _probe(
    provider: str, route: llm.Route, settings: Settings
) -> tuple[ProviderStatus, list[str]]:
    """One free catalog call against a provider → (status, model ids it serves).

    Never raises: any failure is the answer, not an exception.
    """
    label = _provider_label(provider, settings)

    if provider == "self_hosted" and not route.base_url:
        return ProviderStatus(
            provider, label, "not_configured",
            "Self-hosted mode is on but no endpoint is configured for the "
            "selected target.",
        ), []
    if provider != "self_hosted" and not route.api_key:
        return ProviderStatus(
            provider, label, "not_configured",
            "No API key is configured on the server for this provider.",
        ), []

    # The route's own timeout can be very long (a BOQ run legitimately takes
    # minutes); a status check must fail fast instead of pinning a worker.
    timeout = float(settings.llm_health_probe_timeout_seconds)
    started = time.monotonic()
    try:
        client = llm._client_for(route, settings).with_options(timeout=timeout)
        ids = [getattr(m, "id", "") for m in client.models.list()]
    except Exception as exc:  # noqa: BLE001 — every failure is a status, not a raise
        elapsed = int((time.monotonic() - started) * 1000)
        return _failure(provider, label, exc, elapsed), []

    return ProviderStatus(
        provider,
        label,
        "ok",
        "Connected — the provider answered and listed its models.",
        latency_ms=int((time.monotonic() - started) * 1000),
        model_count=len(ids),
    ), ids


def _failure(provider: str, label: str, exc: Exception, elapsed_ms: int) -> ProviderStatus:
    """Classify an SDK exception WITHOUT quoting it — a raw message can carry
    the endpoint URL or a response body. Only the status code is passed on."""
    import openai

    if isinstance(exc, openai.APITimeoutError):
        return ProviderStatus(
            provider, label, "unreachable",
            "No response within the health-check timeout — the server may be "
            "stopped, overloaded, or unreachable from the API.",
            latency_ms=elapsed_ms,
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderStatus(
            provider, label, "unreachable",
            "Could not connect — the server is not answering on its endpoint.",
            latency_ms=elapsed_ms,
        )
    status_code = getattr(exc, "status_code", None)
    if status_code in (401, 403):
        return ProviderStatus(
            provider, label, "unauthorized",
            f"The provider rejected our credentials (HTTP {status_code}).",
            latency_ms=elapsed_ms,
        )
    if status_code is not None:
        return ProviderStatus(
            provider, label, "error",
            f"The provider answered with an error (HTTP {status_code}).",
            latency_ms=elapsed_ms,
        )
    # Anthropic's SDK raises its own class hierarchy; the isinstance checks above
    # only cover openai's. Fall back on the class name, which is safe to show.
    name = type(exc).__name__
    if "Connection" in name or "Timeout" in name:
        return ProviderStatus(
            provider, label, "unreachable",
            "Could not reach the provider (connection failed or timed out).",
            latency_ms=elapsed_ms,
        )
    if "Authentication" in name or "PermissionDenied" in name:
        return ProviderStatus(
            provider, label, "unauthorized",
            "The provider rejected our credentials.",
            latency_ms=elapsed_ms,
        )
    logger.warning("LLM health probe failed for %s: %s", provider, name)
    return ProviderStatus(
        provider, label, "error",
        "The health check failed for an unexpected reason — see the API logs.",
        latency_ms=elapsed_ms,
    )


def _model_matches(configured: str, listed: list[str]) -> bool:
    """Is `configured` served? Exact id, or either side being the other's alias
    stem (`claude-opus-4-8` ↔ `claude-opus-4-8-20260214`)."""
    if configured in listed:
        return True
    return any(
        served.startswith(f"{configured}-") or configured.startswith(f"{served}-")
        for served in listed
    )


def check(settings: Settings | None = None) -> Snapshot:
    """Probe every provider the active routing table uses and grade each feature.

    Sequential by design: the active pool is at most two providers (one when
    self-hosted), and each probe is a single sub-second metadata call.
    """
    s = settings or get_settings()
    started = time.monotonic()

    routes = {key: llm.resolve(key, s) for key in llm._FEATURES}
    probes: dict[str, ProviderStatus] = {}
    catalogs: dict[str, list[str]] = {}
    # dict.fromkeys keeps first-seen order, so the modal lists providers in the
    # order the features use them.
    for provider in dict.fromkeys(r.provider for r in routes.values()):
        route = next(r for r in routes.values() if r.provider == provider)
        probes[provider], catalogs[provider] = _probe(provider, route, s)

    features: list[FeatureStatus] = []
    for key in FEATURE_LABELS:
        if key not in routes:  # a label kept for a feature since removed
            continue
        features.append(_grade(key, routes[key], probes, catalogs, s))
    # Any feature added to llm._FEATURES without a label here still gets graded.
    for key in routes:
        if key not in FEATURE_LABELS:
            features.append(_grade(key, routes[key], probes, catalogs, s))

    healthy = sum(1 for f in features if f.state == "ok")
    status = "ok" if healthy == len(features) else "down" if healthy == 0 else "degraded"

    return Snapshot(
        status=status,
        mode="self_hosted" if s.full_self_hosted_llms_enabled else "third_party",
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        duration_ms=int((time.monotonic() - started) * 1000),
        providers=list(probes.values()),
        features=features,
    )


def _grade(
    key: str,
    route: llm.Route,
    probes: dict[str, ProviderStatus],
    catalogs: dict[str, list[str]],
    settings: Settings,
) -> FeatureStatus:
    label = FEATURE_LABELS.get(key, key.replace("_", " ").capitalize())
    provider_label = _provider_label(route.provider, settings)

    if not llm.is_configured(key, settings):
        missing = (
            f"No model is set for this feature (SELF_HOSTED_{key.upper()}_MODEL is "
            "empty), so it stays switched off."
            if route.provider == "self_hosted"
            else "This feature has no model or API key configured, so it stays "
            "switched off."
        )
        return FeatureStatus(key, label, provider_label, route.model, "unconfigured", missing)

    probe = probes[route.provider]
    if probe.state != "ok":
        return FeatureStatus(
            key, label, provider_label, route.model, "provider_down", probe.detail
        )

    listed = _model_matches(route.model, catalogs.get(route.provider, []))
    if route.provider == "self_hosted" and not listed:
        # vLLM/Ollama answer 404 for a model they aren't serving — a real fault,
        # and the most likely one after someone restarts the box on a new model.
        return FeatureStatus(
            key, label, provider_label, route.model, "model_missing",
            f"The server is running but is not serving “{route.model}” — "
            "every request for this feature will fail until the configured model "
            "and the loaded model match.",
            model_listed=False,
        )
    detail = "Connected and serving this model."
    if route.provider != "self_hosted" and not listed:
        detail = (
            "Connected. The provider's catalog doesn't list this exact model id — "
            "usually harmless (it may be an alias), but worth checking if calls fail."
        )
    return FeatureStatus(
        key, label, provider_label, route.model, "ok", detail,
        model_listed=None if route.provider == "self_hosted" else listed,
    )


# ── Cache ────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_cached: Snapshot | None = None
_cached_at: float = 0.0


def cached(settings: Settings | None = None, *, force: bool = False) -> Snapshot:
    """The current snapshot, re-probing only when stale (or forced).

    Called from threadpool request handlers and from the poller thread; the lock
    makes a burst of concurrent page loads collapse into a single probe, and the
    double-check inside it means the ones that queued serve the fresh result.
    """
    s = settings or get_settings()
    max_age = float(s.llm_health_poll_interval_seconds)
    now = time.monotonic()
    if not force and _cached is not None and now - _cached_at < max_age:
        return _cached
    with _lock:
        if not force and _cached is not None and time.monotonic() - _cached_at < max_age:
            return _cached
        return _store(check(s))


def _store(snapshot: Snapshot) -> Snapshot:
    global _cached, _cached_at
    _cached, _cached_at = snapshot, time.monotonic()
    return snapshot


def reset_cache() -> None:
    """Drop the snapshot (tests, and any future config-reload path)."""
    global _cached, _cached_at
    _cached, _cached_at = None, 0.0


# ── Background poller ────────────────────────────────────────────────────────


async def polling_loop() -> None:
    """Keep the snapshot warm so the indicator is never waiting on a probe.

    Probing in a thread (never inline in the loop) — the SDK clients are sync,
    and blocking the event loop is what the app's async convention exists to
    prevent.
    """
    interval = get_settings().llm_health_poll_interval_seconds
    while True:
        try:
            snapshot = await asyncio.to_thread(check)
            _store(snapshot)
            if snapshot.status != "ok":
                # One line per tick while degraded: the operator's trail for
                # "when did the model server go down?". Never logs URLs/keys.
                logger.warning(
                    "LLM health: %s — %s",
                    snapshot.status,
                    "; ".join(
                        f"{p.label}: {p.state}" for p in snapshot.providers if p.state != "ok"
                    )
                    or "some features are not configured",
                )
        except Exception:  # noqa: BLE001 — the loop must survive any tick failure
            logger.exception("LLM health poll failed")
        await asyncio.sleep(interval)

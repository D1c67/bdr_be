"""Model providers for the browser agents: adapters, model listing, probe.

`get_adapter(provider)` returns the adapter for one of protocol.PROVIDERS,
built on a cached SDK client keyed by (provider, key fingerprint), with
max_retries=0 (the turn service retries once itself). Keys come from the
Railway env only (OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY,
XAI_API_KEY, META_API_KEY) and never leave BDR.

The admin API (Settings > Browser agents) uses the two helpers in models.py:
`list_models(provider)` (1 h cache) and `probe(provider, model)` (the Test
model check: one forced tool call plus a 1x1 PNG).
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from app.core.config import Settings, get_settings
from app.services.browser_agents import protocol
from app.services.browser_agents.llm.base import (
    META_BASE_URL,
    PROVIDER_KEY_ATTR,
    XAI_BASE_URL,
    CompleteArgs,
    ImagePart,
    LlmResult,
    LlmUsage,
    Message,
    ModelInfo,
    ProviderAdapter,
    ProviderNotConfigured,
    ToolCall,
    UnknownProvider,
    classify,
    is_transient,
    safe_error_text,
)

__all__ = [
    "CompleteArgs", "ImagePart", "LlmResult", "LlmUsage", "Message", "ModelInfo", "ProviderAdapter",
    "ProviderNotConfigured", "ToolCall", "UnknownProvider", "classify", "is_transient", "safe_error_text",
    "get_adapter", "provider_key", "key_configured", "providers_info", "reset_clients",
]

# SDK-level timeout; each call narrows it (CompleteArgs.timeout_seconds).
CLIENT_TIMEOUT_SECONDS = 90.0

_clients: dict[tuple[str, str], Any] = {}
_clients_lock = threading.Lock()


def provider_key(provider: str, settings: Settings | None = None) -> str:
    if provider not in PROVIDER_KEY_ATTR:
        raise UnknownProvider(provider)
    s = settings or get_settings()
    return (getattr(s, PROVIDER_KEY_ATTR[provider], "") or "").strip()


def key_configured(provider: str, settings: Settings | None = None) -> bool:
    return bool(provider_key(provider, settings))


def providers_info(settings: Settings | None = None) -> list[dict[str, Any]]:
    """C2 ProviderInfo rows: {provider, key_configured, experimental}."""
    s = settings or get_settings()
    return [
        {"provider": p, "key_configured": key_configured(p, s),
         "experimental": p in protocol.EXPERIMENTAL_PROVIDERS}
        for p in protocol.PROVIDERS
    ]


def key_fingerprint(key: str) -> str:
    """Cache key part for a provider key: a short hash, never the key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _build_client(provider: str, key: str) -> Any:
    if provider == "openai":
        import openai

        return openai.OpenAI(api_key=key, timeout=CLIENT_TIMEOUT_SECONDS, max_retries=0)
    if provider in ("xai", "meta"):
        import openai

        base_url = XAI_BASE_URL if provider == "xai" else META_BASE_URL
        return openai.OpenAI(api_key=key, base_url=base_url, timeout=CLIENT_TIMEOUT_SECONDS, max_retries=0)
    if provider == "anthropic":
        from anthropic import Anthropic

        return Anthropic(api_key=key, timeout=CLIENT_TIMEOUT_SECONDS, max_retries=0)
    if provider == "google":
        from google import genai
        from google.genai import types

        # api_key -> the x-goog-api-key header only (never a ?key= query).
        return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(CLIENT_TIMEOUT_SECONDS * 1000)))
    raise UnknownProvider(provider)


def _client(provider: str, key: str) -> Any:
    cache_key = (provider, key_fingerprint(key))
    with _clients_lock:
        client = _clients.get(cache_key)
        if client is None:
            client = _build_client(provider, key)
            _clients[cache_key] = client
    return client


def reset_clients() -> None:
    """Test hook: drop cached SDK clients."""
    with _clients_lock:
        _clients.clear()


def adapter_for_client(provider: str, client: Any) -> ProviderAdapter:
    """The adapter class for `provider` around an existing SDK client."""
    if provider == "openai":
        from app.services.browser_agents.llm.openai_ import OpenAIAdapter

        return OpenAIAdapter(client)
    if provider == "anthropic":
        from app.services.browser_agents.llm.anthropic_ import AnthropicAdapter

        return AnthropicAdapter(client)
    if provider == "google":
        from app.services.browser_agents.llm.google_ import GoogleAdapter

        return GoogleAdapter(client)
    if provider in ("xai", "meta"):
        from app.services.browser_agents.llm.compat import CompatAdapter

        return CompatAdapter(client, provider)
    raise UnknownProvider(provider)


def get_adapter(provider: str, settings: Settings | None = None) -> ProviderAdapter:
    """The adapter for `provider` with its env key. Raises
    ProviderNotConfigured when the key is missing."""
    if provider not in protocol.PROVIDERS:
        raise UnknownProvider(provider)
    key = provider_key(provider, settings)
    if not key:
        raise ProviderNotConfigured(f"No API key is configured for {provider}.")
    return adapter_for_client(provider, _client(provider, key))

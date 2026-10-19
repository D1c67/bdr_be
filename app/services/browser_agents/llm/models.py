"""Model picker and Test model probe for Settings > Browser agents (SPEC.md 1).

list_models(provider)
    The provider's model list through its env key, filtered to models that
    can run an agent loop (by reported capability where the provider reports
    one, Gemini's supported_actions; by family name elsewhere). Cached 1 h
    per (provider, key fingerprint) and per process. Prices are prefilled
    from services/llm_pricing when the model has a verified row, else null
    (the admin must enter them; a type cannot be enabled without them).

probe(provider, model)
    The Test model check a provider/model change needs before it is saved:
    one FORCED tool call (`report_probe`) on a request that also carries a
    1x1 PNG, so tool calling and image input are both proven. Returns
    {ok, latency_ms, error, probed_at}; error is a fixed text per failure
    kind, never the SDK message. Mirrored to llm_call_log.

record_probe / recent_probe
    Probe results are written to browser_agent_settings_changes
    (field 'model_probe', the result in `probe`), so any API process can
    check "a passing probe for this pair within 10 min" before a PATCH, and
    the run is audited. A passing probe for the type's current pair also
    stamps model_probe_ok_at (through the audited settings RPC).

Everything is plain `def` (sync SDKs and the sync Supabase client).
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core import supabase_client
from app.core.config import Settings, get_settings
from app.services import llm_gate, llm_pricing
from app.services.browser_agents import protocol
from app.services.browser_agents import llm as providers
from app.services.browser_agents.llm.base import (
    CompleteArgs,
    ImagePart,
    Message,
    ProviderNotConfigured,
    classify,
    safe_error_text,
)

logger = logging.getLogger(__name__)

UTC = timezone.utc
MODELS_CACHE_SECONDS = 3600
PROBE_TIMEOUT_SECONDS = 45.0
PROBE_MAX_OUTPUT_TOKENS = 1024
PROBE_VALID_SECONDS = 600              # "a passing probe for that pair within 10 min"
PROBE_FIELD = "model_probe"

# A 1x1 PNG (one opaque pixel).
PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PROBE_TOOL = {
    "name": "report_probe",
    "description": "Report that you can see the attached image.",
    "parameters": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "description": "true when an image was attached"},
            "color": {"type": "string", "maxLength": 40, "description": "the image's main color"},
        },
        "required": ["ok"],
        "additionalProperties": False,
    },
}
PROBE_SYSTEM = "You are a connectivity check. Always answer by calling the report_probe tool exactly once."

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


class ProviderKeyMissing(ProviderNotConfigured):
    """The admin API maps this to 409 provider_key_missing."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _check_provider(provider: str, settings: Settings) -> str:
    if provider not in protocol.PROVIDERS:
        raise providers.UnknownProvider(provider)
    key = providers.provider_key(provider, settings)
    if not key:
        raise ProviderKeyMissing(f"No API key is configured for {provider}.")
    return key


def _price(model: str) -> tuple[float | None, float | None]:
    row = llm_pricing.price_for(model)
    if row is None:
        return None, None
    return float(row.input), float(row.output)


def reset_cache() -> None:
    """Test hook."""
    with _cache_lock:
        _cache.clear()


def list_models(provider: str, *, settings: Settings | None = None, force: bool = False) -> dict[str, Any]:
    """{provider, models: [{id, label, price_in_per_mtok, price_out_per_mtok}], cached_at}.
    Raises ProviderKeyMissing without a key; provider errors propagate (the
    caller maps them with classify / safe_error_text)."""
    s = settings or get_settings()
    key = _check_provider(provider, s)
    cache_key = (provider, providers.key_fingerprint(key))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and not force and now - hit[0] < MODELS_CACHE_SECONDS:
            return hit[1]
    adapter = providers.get_adapter(provider, s)
    with llm_gate.slot(provider, llm_gate.TIER_INTERACTIVE, s):
        infos = adapter.list_models()
    models = []
    for info in infos:
        price_in, price_out = _price(info.id)
        models.append({"id": info.id, "label": info.label,
                       "price_in_per_mtok": price_in, "price_out_per_mtok": price_out})
    payload = {"provider": provider, "models": models, "cached_at": _iso(_now())}
    with _cache_lock:
        _cache[cache_key] = (now, payload)
    return payload


def _probe_failure(kind: str) -> str:
    return safe_error_text(kind)


def probe(provider: str, model: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Run the Test model check. Never raises for a provider failure (the
    result says ok=false with a fixed error text); raises ProviderKeyMissing
    without a key and ValueError for a malformed model id."""
    s = settings or get_settings()
    _check_provider(provider, s)
    if not model or len(model) > 120 or not all(c.isalnum() or c in "._:/-" for c in model):
        raise ValueError("model id is not valid")
    adapter = providers.get_adapter(provider, s)
    args = CompleteArgs(
        model=model,
        system=PROBE_SYSTEM,
        messages=[Message(role="user", text="Here is a tiny image. Call report_probe.",
                          images=(ImagePart("image/png", PROBE_PNG),))],
        tools=[PROBE_TOOL],
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        force_tool=PROBE_TOOL["name"],
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    started = time.monotonic()
    error: str | None = None
    exc_seen: Exception | None = None
    usage = None
    try:
        with llm_gate.slot(provider, llm_gate.TIER_INTERACTIVE, s):
            result = adapter.complete(args)
        usage = result.usage
        calls = result.tool_calls
        if len(calls) != 1 or calls[0].name != PROBE_TOOL["name"]:
            error = "The model did not answer with exactly one tool call."
        elif calls[0].parse_error or not isinstance(calls[0].args.get("ok"), bool):
            error = "The model's tool call did not match the schema."
    except Exception as exc:  # noqa: BLE001 - the probe reports, it never raises
        exc_seen = exc
        kind = classify(exc)
        error = _probe_failure(kind)
        logger.info("browser agents: probe of %s/%s failed (%s)", provider, model, kind)
    latency_ms = int((time.monotonic() - started) * 1000)
    llm_gate.log_call(
        feature="browser_agent_probe", provider=provider, model=model, tier_name=llm_gate.TIER_INTERACTIVE,
        ok=error is None, error=exc_seen, duration_ms=latency_ms,
        input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None),
        cached_tokens=getattr(usage, "cached_tokens", None),
    )
    return {"ok": error is None, "latency_ms": latency_ms, "error": error, "probed_at": _iso(_now())}


def record_probe(type_key: str, provider: str, model: str, result: dict[str, Any], *,
                 actor_id: str | None, actor_label: str) -> None:
    """Audit one probe run, and stamp model_probe_ok_at when it passed for
    the type's CURRENT provider and model."""
    sb = supabase_client.get_supabase()
    sb.table("browser_agent_settings_changes").insert({
        "scope": "agent_type", "target": type_key, "field": PROBE_FIELD,
        "old_value": None, "new_value": {"provider": provider, "model": model},
        "probe": {k: result.get(k) for k in ("ok", "latency_ms", "error", "probed_at")},
        "changed_by": actor_id, "changed_by_label": actor_label,
    }).execute()
    if not result.get("ok"):
        return
    rows = sb.table("browser_agent_types").select("provider, model").eq("key", type_key).limit(1).execute().data or []
    if rows and rows[0].get("provider") == provider and rows[0].get("model") == model:
        sb.rpc("browser_agent_update_settings", {
            "p_scope": "agent_type", "p_target": type_key,
            "p_patch": {"model_probe_ok_at": result.get("probed_at") or _iso(_now())},
            "p_actor": actor_id, "p_actor_label": actor_label,
            "p_probe": {k: result.get(k) for k in ("ok", "latency_ms", "probed_at")},
        }).execute()


def recent_probe(type_key: str, provider: str, model: str, *,
                 within_seconds: int = PROBE_VALID_SECONDS) -> dict[str, Any] | None:
    """The latest passing probe for (type, provider, model) newer than
    `within_seconds`, as {ok, latency_ms, error, probed_at}, else None."""
    since = _iso(_now() - timedelta(seconds=within_seconds))
    rows = (
        supabase_client.get_supabase().table("browser_agent_settings_changes")
        .select("new_value, probe, changed_at").eq("scope", "agent_type").eq("target", type_key)
        .eq("field", PROBE_FIELD).gte("changed_at", since).order("changed_at", desc=True).limit(20).execute()
    ).data or []
    for row in rows:
        nv = row.get("new_value") or {}
        pr = row.get("probe") or {}
        if nv.get("provider") == provider and nv.get("model") == model and pr.get("ok") is True:
            return pr
    return None

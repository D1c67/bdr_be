"""Live switch for the self-hosted LLM box (docs/SELF_HOSTED_LLM.md).

GPU capacity on AWS comes and goes, so IT Admins and devs can move every AI
feature to another box from Settings without a redeploy. The env var
SELF_HOSTED_LLM_TARGET stays the default; `llm_runtime_settings` (0150) holds
an optional override that names a target (never a URL: base URLs, keys and
models still come from the env, so a browser can only pick among endpoints the
operator already configured).

How the override reaches the routing layer:

- `apply(settings)` returns the settings with the override swapped in. It reads
  ONLY in-process memory, so it is safe on any hot path (services/llm.resolve
  calls it on every AI request) and never touches the DB.
- `polling_loop()` (one per worker, started in main.lifespan) refreshes that
  memory from the DB every few seconds, so a switch made through one uvicorn
  worker reaches the other within `_POLL_SECONDS`.
- `switch()` writes the row, logs it to `llm_target_changes`, and updates this
  worker's memory at once.

Fail safe: a DB error keeps the last known value; an override naming a target
that is no longer usable (its env vars were removed) is ignored, so the env
default serves rather than nothing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from app.core.config import SELF_HOSTED_LLM_TARGETS, Settings, get_settings

logger = logging.getLogger(__name__)

_POLL_SECONDS = 10

# Shown in Settings and the status modal. Only names and places: never a URL.
TARGET_INFO: dict[str, dict[str, str]] = {
    "local": {"label": "Local server", "detail": "A server or SSH tunnel on the API host"},
    "ec2": {"label": "EC2 box A", "detail": "g6.xlarge, us-west-2 (Oregon)"},
    "ec2b": {"label": "EC2 box B", "detail": "g7e.2xlarge, us-west-2 (Oregon)"},
    "ec2c": {"label": "EC2 box C", "detail": "g7e.2xlarge, us-east-1 (N. Virginia)"},
}

_lock = threading.Lock()
_override: str | None = None
_applied: dict[tuple[int, str], Settings] = {}


class TargetUnavailable(ValueError):
    """The requested target is unknown or has no endpoint/model configured."""


# ── Reading the override (hot path, memory only) ────────────────────────────


def current_override() -> str | None:
    return _override


def target_problem(target: str, s: Settings) -> str | None:
    """Why `target` cannot serve right now, or None when it is usable. Mirrors
    the boot validator in core/config for the env target."""
    if target not in SELF_HOSTED_LLM_TARGETS:
        return "Unknown model server."
    url = (getattr(s, f"self_hosted_llm_{target}_base_url") or "").strip()
    model = (getattr(s, f"self_hosted_llm_{target}_model") or "").strip()
    if not url:
        return "Not set up yet: the API has no endpoint for this box."
    if not url.startswith(("https://", "http://")):
        return "Misconfigured: the endpoint has no https:// scheme."
    if url.startswith("http://") and s.is_production and not s.self_hosted_llm_allow_http:
        return "Misconfigured: plain http is not allowed in production."
    if not model:
        return "Not set up yet: the API does not know which model this box serves."
    return None


def apply(s: Settings) -> Settings:
    """`s` with the live override as its target (or `s` itself when there is
    none, it matches already, or the override target is not usable)."""
    target = _override
    if not target or target == s.self_hosted_llm_target:
        return s
    key = (id(s), target)
    cached = _applied.get(key)
    if cached is not None:
        return cached
    if target_problem(target, s):
        return s
    switched = s.model_copy(update={"self_hosted_llm_target": target})
    with _lock:
        # Settings objects are long-lived (get_settings is lru_cached); tests
        # build many, so keep the memo from growing without bound.
        if len(_applied) > 32:
            _applied.clear()
        _applied[key] = switched
    return switched


def live_target(s: Settings | None = None) -> str:
    return apply(s or get_settings()).self_hosted_llm_target


def _set_memory(target: str | None) -> None:
    global _override
    with _lock:
        if target != _override:
            _override = target
            _applied.clear()


def reset() -> None:
    """Drop the in-memory override (tests)."""
    _set_memory(None)


# ── DB sync ─────────────────────────────────────────────────────────────────


def _db():
    from app.core.supabase_client import get_supabase

    return get_supabase()


def refresh() -> str | None:
    """Re-read the override row into memory. Sync (runs in a thread). Keeps
    the last known value when the read fails."""
    try:
        res = (
            _db().table("llm_runtime_settings")
            .select("self_hosted_target")
            .eq("id", 1)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001 - a missed tick keeps the last value
        logger.warning("llm target refresh failed; keeping %r", _override, exc_info=True)
        return _override
    rows = res.data or []
    value = (rows[0].get("self_hosted_target") or None) if rows else None
    if value != _override:
        logger.info("LLM target override changed: %r -> %r", _override, value)
    _set_memory(value)
    return value


async def polling_loop() -> None:
    while True:
        await asyncio.to_thread(refresh)
        await asyncio.sleep(_POLL_SECONDS)


# ── Settings page ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TargetOption:
    key: str
    label: str
    detail: str
    model: str
    available: bool
    problem: str | None
    is_default: bool   # the env SELF_HOSTED_LLM_TARGET
    is_live: bool


def _pinned_features(target: str, s: Settings) -> list[str]:
    """Features whose SELF_HOSTED_<FEATURE>_MODEL pins a model that `target`
    does not serve (they would 404 on that box). "off" is not a pin."""
    from app.services import llm

    target_model = (getattr(s, f"self_hosted_llm_{target}_model", "") or "").strip()
    pinned = []
    for key, spec in llm._FEATURES.items():
        explicit = (spec.sh_model(s) or "").strip()
        if explicit and explicit.lower() != llm.SELF_HOSTED_MODEL_OFF and explicit != target_model:
            pinned.append(key)
    return pinned


def state(s: Settings | None = None) -> dict:
    s = s or get_settings()
    live = live_target(s)
    options = []
    for key in SELF_HOSTED_LLM_TARGETS:
        info = TARGET_INFO.get(key, {"label": key, "detail": ""})
        problem = target_problem(key, s)
        if key == "local" and problem and key != live:
            # A local server only exists on a developer machine; hide it where
            # it is not configured instead of listing a dead option.
            continue
        options.append(
            asdict(
                TargetOption(
                    key=key,
                    label=info["label"],
                    detail=info["detail"],
                    model=(getattr(s, f"self_hosted_llm_{key}_model") or "").strip(),
                    available=problem is None,
                    problem=problem,
                    is_default=key == s.self_hosted_llm_target,
                    is_live=key == live,
                )
            )
        )
    return {
        "self_hosted_enabled": s.full_self_hosted_llms_enabled,
        "live_target": live,
        "default_target": s.self_hosted_llm_target,
        "override": _override,
        "options": options,
        "pinned_features": {o["key"]: _pinned_features(o["key"], s) for o in options},
        "recent_changes": _recent_changes(),
    }


def _recent_changes(limit: int = 5) -> list[dict]:
    try:
        res = (
            _db().table("llm_target_changes")
            .select("from_target, to_target, forced, probe_state, changed_at, "
                    "who:profiles!llm_target_changes_changed_by_fkey(full_name, email)")
            .order("changed_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception:  # noqa: BLE001 - history is a nicety, never a failure
        logger.warning("llm target history read failed", exc_info=True)
        return []
    out = []
    for row in res.data or []:
        who = row.get("who") or {}
        out.append({
            "from_target": row["from_target"],
            "to_target": row["to_target"],
            "forced": row.get("forced", False),
            "probe_state": row.get("probe_state"),
            "changed_at": row["changed_at"],
            "changed_by": who.get("full_name") or who.get("email"),
        })
    return out


def probe(target: str, s: Settings | None = None) -> dict:
    """Ask `target` for its model list (the same free call as the status
    check). Returns {state, detail, latency_ms, model_served}."""
    from app.services import llm, llm_health

    s = s or get_settings()
    shifted = s.model_copy(update={"self_hosted_llm_target": target})
    route = llm.Route(
        provider="self_hosted",
        model=shifted.self_hosted_llm_target_model,
        api_key=shifted.self_hosted_llm_api_key,
        base_url=shifted.self_hosted_llm_base_url,
    )
    # One retry, not the SDK's default two: someone is waiting on the button,
    # and a dead box otherwise takes three full probe timeouts to say so.
    status, ids = llm_health._probe("self_hosted", route, shifted, max_retries=1)
    served = status.state == "ok" and llm_health._model_matches(route.model, ids)
    detail = status.detail
    if status.state == "ok" and not served:
        detail = "Connected, but the box is not serving the configured model yet."
    return {
        "state": status.state if served or status.state != "ok" else "model_missing",
        "detail": detail,
        "latency_ms": status.latency_ms,
    }


def switch(target: str | None, *, user_id: str, force: bool = False) -> dict:
    """Make `target` live (None = back to the env default). Probes the box
    first and refuses an unhealthy one unless `force`. Returns
    {ok, probe, state} or raises TargetUnavailable."""
    s = get_settings()
    resolved = target or s.self_hosted_llm_target
    problem = target_problem(resolved, s)
    if problem:
        raise TargetUnavailable(problem)
    pinned = _pinned_features(resolved, s)
    if pinned:
        raise TargetUnavailable(
            "These features are pinned to a model this box does not serve: "
            f"{', '.join(pinned)}. Clear their SELF_HOSTED_<FEATURE>_MODEL env "
            "vars first."
        )

    result = probe(resolved, s)
    if result["state"] != "ok" and not force:
        return {"ok": False, "probe": result, "state": state(s)}

    before = live_target(s)
    stored = None if resolved == s.self_hosted_llm_target else resolved
    db = _db()
    db.table("llm_runtime_settings").upsert(
        {"id": 1, "self_hosted_target": stored, "updated_by": user_id,
         "updated_at": datetime.now(UTC).isoformat()},
    ).execute()
    if before != resolved:
        db.table("llm_target_changes").insert({
            "from_target": before,
            "to_target": resolved,
            "forced": result["state"] != "ok",
            "probe_state": result["state"],
            "changed_by": user_id,
        }).execute()
    _set_memory(stored)
    logger.warning(
        "LLM target switched %s -> %s by %s (probe %s%s)",
        before, resolved, user_id, result["state"], ", forced" if force else "",
    )

    from app.services import llm_health

    llm_health.reset_cache()
    return {"ok": True, "probe": result, "state": state(s)}

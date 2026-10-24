"""Per-step model routes for AI Estimating (WS2 step 11, spec 4.2 and 8.3).

A route says which provider, model and reasoning effort one pipeline step
runs on, its dollar budget and whether it gets tools. The code defaults
(`core.config.PLANS_READER_DEFAULT_ROUTES`) are the section 8.3 table with
every step on OpenAI (owner decision 2026-10-21: all live spend on OpenAI);
`PLANS_READER_ROUTES` (JSON, validated at boot) overrides fields per step.

Two rules keep today's behaviour when no route is set:

- A step the env does NOT override keeps the model in `OPENAI_PLANS_MODEL`
  (the existing per-run model) on OpenAI, so changing OPENAI_PLANS_MODEL
  still moves every plans call as it did before routes existed. A step the
  env overrides uses the override exactly as given.
- Effort follows the route; a null route effort inherits
  PLANS_READER_REASONING_EFFORT (config does that merge).

Runs snapshot their routes at creation (`routes_snapshot` goes into
`plans_reader_runs.settings_snapshot["routes"]`, hook in
plans_reader_runs._settings_snapshot) so a run's calls are explainable after
the env changes; `resolve_step(..., snapshot=...)` prefers the snapshot.

Pure module (settings in, dicts out); no network, no database.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from app.core.config import (
    PLANS_READER_DEFAULT_ROUTES,
    PLANS_READER_REASONING_EFFORTS,
    PLANS_READER_ROUTE_PROVIDERS,
    Settings,
    get_settings,
)

STEPS: tuple[str, ...] = tuple(PLANS_READER_DEFAULT_ROUTES)

# llm feature key -> route step. Features without a step (plans_modify) run
# as today: OpenAI on OPENAI_PLANS_MODEL at the run's effort.
FEATURE_STEP: dict[str, str | None] = {
    "plans_sub": "sub",
    "plans_main": "main",
    "plans_modify": None,
    "plans_chat": "chat",
    "plans_recount": "recount",
    "plans_review": "review",
    "plans_revise": "revise",
    "plans_derive": "derive",
    "plans_scope_attrib": "scope_attrib",
    "pricing_quote": "quote",
}

# Group kind -> route step (FEATURE_BY_KIND maps escalation to plans_sub, but
# the escalation step has its own route row). modify has no route (spec 4.2:
# it runs as today), matching FEATURE_STEP["plans_modify"] = None.
KIND_STEP: dict[str, str | None] = {
    "sub": "sub",
    "rollup": "sub",
    "escalation": "escalation",
    "recount": "recount",
    "main": "main",
    "review": "review",
    "revise": "revise",
    "derive": "derive",
    "chat": "chat",
    "modify": None,
}


# Steps whose provider and model come from another step's route unless the
# env names them: scope attribution is "the run's main route model, low
# effort" (spec 8.3). Effort, budget and tools stay the step's own.
DERIVED_FROM: dict[str, str] = {"scope_attrib": "main"}


@dataclass(frozen=True)
class StepRoute:
    step: str
    provider: str  # openai | anthropic
    model: str
    effort: str
    budget_usd: Decimal | None
    tools: bool
    explicit: bool  # True when PLANS_READER_ROUTES (or a snapshot) named this step

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["budget_usd"] = float(self.budget_usd) if self.budget_usd is not None else None
        return d


def explicit_steps(settings: Settings | None = None) -> frozenset[str]:
    """Steps named in PLANS_READER_ROUTES (already validated at boot; a
    malformed value here reads as no overrides)."""
    s = settings or get_settings()
    raw = (s.plans_reader_routes or "").strip()
    if not raw:
        return frozenset()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    return frozenset(str(k) for k in data if k in PLANS_READER_DEFAULT_ROUTES)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except Exception:  # noqa: BLE001 - a bad snapshot value reads as no budget
        return None
    return out if out.is_finite() and out > 0 else None


def _from_dict(step: str, route: dict, *, explicit: bool, settings: Settings) -> StepRoute:
    provider = str(route.get("provider") or "openai").strip().lower()
    if provider not in PLANS_READER_ROUTE_PROVIDERS:
        raise ValueError(f"Route step {step!r} names an unknown provider {provider!r}.")
    model = str(route.get("model") or "").strip()
    if not model:
        raise ValueError(f"Route step {step!r} has no model.")
    effort = route.get("effort")
    effort = str(effort).strip().lower() if effort else settings.plans_reader_reasoning_effort
    if effort not in PLANS_READER_REASONING_EFFORTS:
        raise ValueError(f"Route step {step!r} has an unknown effort {effort!r}.")
    return StepRoute(
        step=step,
        provider=provider,
        model=model,
        effort=effort,
        budget_usd=_dec(route.get("budget_usd")),
        tools=bool(route.get("tools", False)),
        explicit=explicit,
    )


def resolve_step(
    step: str,
    settings: Settings | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> StepRoute:
    """The route for `step`. `snapshot` is a run's
    settings_snapshot["routes"] (the output of routes_snapshot); when it
    holds the step, it wins over the live settings. Without an explicit
    env route the step runs on OpenAI with OPENAI_PLANS_MODEL, except a
    step in DERIVED_FROM, which takes provider and model from its parent
    step's resolved route (scope_attrib follows main)."""
    if step not in PLANS_READER_DEFAULT_ROUTES:
        raise ValueError(f"Unknown route step {step!r} (steps: {', '.join(STEPS)}).")
    s = settings or get_settings()
    if snapshot and isinstance(snapshot.get(step), dict):
        row = snapshot[step]
        return _from_dict(step, row, explicit=bool(row.get("explicit", True)), settings=s)
    routes = s.plans_reader_route_map
    route = dict(routes[step])
    explicit = step in explicit_steps(s)
    if not explicit and step in DERIVED_FROM:
        # Spec 8.3: scope attribution runs on the run's MAIN route model at
        # its own (low) effort, so it follows main wherever main goes.
        parent = resolve_step(DERIVED_FROM[step], s, snapshot=snapshot)
        route["provider"] = parent.provider
        route["model"] = parent.model
    elif not explicit:
        route["provider"] = "openai"
        route["model"] = s.openai_plans_model
    return _from_dict(step, route, explicit=explicit, settings=s)


def resolve_feature(
    feature: str,
    settings: Settings | None = None,
    *,
    step: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> StepRoute | None:
    """The route for an llm feature (or an explicit `step`), None for a
    feature that has no route step (it runs as today)."""
    chosen = step or FEATURE_STEP.get(feature)
    if chosen is None:
        return None
    return resolve_step(chosen, settings, snapshot=snapshot)


def step_for_feature(feature: str) -> str | None:
    return FEATURE_STEP.get(feature)


def step_for_kind(kind: str) -> str | None:
    """The route step of a group kind, None for a kind without a route
    (modify) or an unknown kind."""
    return KIND_STEP.get(kind)


def routes_snapshot(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """Every step's resolved route as plain JSON (for
    plans_reader_runs._settings_snapshot). Budgets are floats; `explicit`
    says whether the env named the step."""
    s = settings or get_settings()
    return {step: resolve_step(step, s).as_dict() for step in STEPS}


def providers_in_use(settings: Settings | None = None) -> frozenset[str]:
    """Providers any step routes to (the health page and the ledger use it
    to know whether an Anthropic key is needed)."""
    s = settings or get_settings()
    return frozenset(resolve_step(step, s).provider for step in STEPS)


__all__ = [
    "DERIVED_FROM",
    "FEATURE_STEP",
    "KIND_STEP",
    "STEPS",
    "StepRoute",
    "explicit_steps",
    "providers_in_use",
    "resolve_feature",
    "resolve_step",
    "routes_snapshot",
    "step_for_feature",
    "step_for_kind",
]

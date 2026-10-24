"""Shared plumbing of the wave 4 dev_probe drivers
(docs/AI_ESTIMATING_BUILD_SPEC.md 0.4 ledger rules 2, 3 and 6; the live
script pattern of scripts/plans_reader_ab.py).

Used by scripts/scope_attrib_probe.py (WS5) and
scripts/pricing_quote_fallback_probe.py (WS6). Nothing here calls a model:

- `arm_sweep`: checks the sweep (dev database, exists, open, purpose
  dev_probe, the probe's workstream) and arms the process (EVAL_SWEEP_ID,
  AI_EVAL_LEDGER_ENFORCE=true, LLM_CASSETTE_MODE=record_missing for
  dev_probe per rule 6, LLM_CASSETTE_DIR under EVAL_ROOT);
- `captured_requests`: swaps `llm.complete_turn` for a stub that records
  the ModelRequest a production caller builds and raises before anything is
  sent, so a dry run plans on the exact request at $0;
- `request_input_tokens` / `estimate_usd`: the ledger's own token estimate
  of a request and the llm_pricing dollars of it;
- `CallMeter`: wraps a production caller for a live call, sets the
  ledger reserve of the next guarded call (eval_ledger.set_call_estimate)
  and records the reported cost, usage, cassette replay and any error.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

DEV_REF = "bpidntbyvoooqvaispup"
SWEEP_PURPOSE = "dev_probe"
CASSETTE_MODE = "record_missing"
DEFAULT_EVAL_ROOT = Path("/Users/thomasmoore/Desktop/BDR/backups/eval")


class ProbeError(RuntimeError):
    """The probe cannot go ahead as asked (usage, preflight, ledger)."""


class PlanningCapture(Exception):
    """Raised by the captured complete_turn: the request was recorded and
    nothing was sent."""


def eval_root_for(arg: Path | None, settings: Any) -> Path:
    return Path(arg or getattr(settings, "eval_root", "") or DEFAULT_EVAL_ROOT)


def check_dev(settings: Any) -> None:
    url = str(getattr(settings, "supabase_url", "") or "")
    if DEV_REF not in url:
        raise ProbeError(f"SUPABASE_URL must point at the dev project {DEV_REF}; refusing")


def money(v: Any) -> str:
    return "n/a" if v is None else f"${Decimal(str(v)):.2f}"


def json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


# ---------------------------------------------------------------------------
# Golden candidates
# ---------------------------------------------------------------------------


PROBE_TIERS: tuple[str, ...] = ("smoke", "core", "pool")


def ordered_candidates(data: Any, tiers: Any = PROBE_TIERS) -> list[tuple[str, str | None, str, str | None]]:
    """(case, project id or None, tier, why missing) in the probes'
    selection order: smoke and core cases in spec 8.1 order (resolved
    against the backup), then the golden pool in id order. The holdout is
    never a tier here."""
    from app.services import eval_manifest as em

    out: list[tuple[str, str | None, str, str | None]] = []
    for tier in tiers:
        if tier in ("smoke", "core"):
            for case, prefix, _name in em.TIER_CASES[tier]:
                try:
                    out.append((case, data.resolve_project_id(prefix), tier, None))
                except (KeyError, ValueError) as exc:
                    out.append((case, None, tier, f"not in the backup ({exc})"))
        elif tier == "pool":
            for pid in sorted(em.TIER_IDS["pool"]):
                out.append((pid[:8], pid, tier, None))
        else:
            raise ProbeError(f"unknown tier {tier!r}; one of smoke, core, pool (holdout is never probed)")
    return out


def matches_only(case: str, project_id: str | None, wanted: Any) -> bool:
    """True when `wanted` (case ids or project id prefixes) is empty or
    names this candidate."""
    items = [w.strip() for w in wanted or () if w and w.strip()]
    return not items or any(case == w or (project_id or "").startswith(w) for w in items)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def arm_sweep(sweep: str, *, workstream: str, eval_root: Path, settings: Any = None,
              cassette: bool = True) -> dict[str, Any]:
    """Check the sweep (dev database, exists, open, purpose dev_probe, the
    probe's workstream) and arm this process for it. Returns the sweep row
    of eval_ledger.report with `remaining_usd`."""
    from app.core.config import get_settings
    from app.services import eval_ledger

    s = settings or get_settings()
    check_dev(s)
    rep = eval_ledger.report()
    wanted = rep.get("open_sweep_id") if sweep == "open" else sweep
    if not wanted:
        raise ProbeError(
            f"no sweep is open; open one with scripts/eval_ledger.py open --workstream {workstream} "
            f"--purpose {SWEEP_PURPOSE} --budget ..."
        )
    try:
        wanted = str(uuid.UUID(str(wanted)))
    except ValueError as exc:
        raise ProbeError(f"--sweep must be a sweep id or 'open', got {sweep!r}") from exc
    row = next((r for r in rep.get("sweeps") or [] if str(r.get("id")) == wanted), None)
    if row is None:
        raise ProbeError(f"sweep {wanted} not found")
    if row.get("status") != "open":
        raise ProbeError(f"sweep {wanted} is {row.get('status')}, not open")
    if row.get("purpose") != SWEEP_PURPOSE:
        raise ProbeError(
            f"sweep {wanted} has purpose {row.get('purpose')}; this probe needs a sweep of purpose {SWEEP_PURPOSE}"
        )
    if row.get("workstream") != workstream:
        raise ProbeError(f"sweep {wanted} belongs to {row.get('workstream')}; this probe spends {workstream}")
    remaining = Decimal(str(row.get("budget_usd") or 0)) - Decimal(str(row.get("spent_usd") or 0))
    os.environ["EVAL_SWEEP_ID"] = wanted
    os.environ["AI_EVAL_LEDGER_ENFORCE"] = "true"
    if cassette:
        os.environ["LLM_CASSETTE_MODE"] = CASSETTE_MODE
        os.environ["LLM_CASSETTE_DIR"] = str(eval_root / "cassettes")
    get_settings.cache_clear()
    return {**row, "remaining_usd": remaining}


def check_route(route: Any, settings: Any) -> None:
    """Live preflight of a route: AI Estimating on (the feature switch of
    every plans feature) and a verified price row (rule 3)."""
    from app.services import llm_pricing

    if not getattr(settings, "plans_reader_enabled", False):
        raise ProbeError("PLANS_READER_ENABLED must be true for a live probe (the feature switch of every AI Estimating call)")
    if getattr(route, "provider", "") == "self_hosted":
        raise ProbeError("the route is self-hosted; AI Estimating calls never go to the box")
    if llm_pricing.price_for(getattr(route, "model", "") or "") is None:
        raise ProbeError(f"no verified llm_pricing row for {getattr(route, 'model', None)!r} (spec 0.4 rule 3)")


def out_dir_for(eval_root: Path, sweep_id: str, name: str, override: Path | None = None) -> Path:
    out = override or eval_root / "sweeps" / str(sweep_id) / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_results(out_dir: Path, payload: dict[str, Any], table: list[str]) -> Path:
    path = out_dir / "results.json"
    path.write_text(json.dumps(payload, indent=1, default=json_default))
    (out_dir / "table.md").write_text("\n".join(table) + "\n")
    return path


# ---------------------------------------------------------------------------
# Requests and estimates ($0)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def captured_requests() -> Iterator[list[Any]]:
    """Inside the block `llm.complete_turn` records (feature, request) and
    raises PlanningCapture; the original is restored on exit."""
    from app.services import llm

    seen: list[Any] = []
    original = llm.complete_turn

    def _capture(feature: str, req: Any, *, settings: Any = None) -> Any:
        seen.append(req)
        raise PlanningCapture(feature)

    llm.complete_turn = _capture
    try:
        yield seen
    finally:
        llm.complete_turn = original


def request_input_tokens(req: Any, provider: str) -> int:
    """The ledger's input token estimate of a complete_turn request."""
    from app.services import eval_ledger

    tokens, _out = eval_ledger.request_tokens({"op": "turn", "req": req}, provider)
    return int(tokens)


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    from app.services import llm_pricing

    cost = llm_pricing.estimate_cost(model, input_tokens=int(input_tokens), output_tokens=int(output_tokens))
    return None if cost is None else Decimal(str(cost)).quantize(Decimal("0.0001"))


# ---------------------------------------------------------------------------
# Live metering
# ---------------------------------------------------------------------------


@dataclass
class CallRecord:
    called: bool = False
    cost_usd: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    replayed: bool = False
    error: str | None = None
    error_type: str | None = None
    reserve_usd: float | None = None
    seconds: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ledger_refused(self) -> bool:
        return self.error_type == "EvalBudgetRefused"


class CallMeter:
    """Wrap a production caller: before the call the ledger reserve of the
    next guarded call is set, after it the reported cost and usage are
    read from llm.last_cost_usd() / last_usage(). Errors are recorded and
    re-raised (the production code decides what a failure means)."""

    def __init__(self, reserve_usd: Decimal | float | None) -> None:
        self.reserve = None if reserve_usd is None else Decimal(str(reserve_usd))
        self.record = CallRecord(reserve_usd=None if reserve_usd is None else float(reserve_usd))

    def wrap(self, inner: Callable[..., Any]) -> Callable[..., Any]:
        def _call(*args: Any, **kwargs: Any) -> Any:
            from app.services import eval_ledger, llm

            rec = self.record
            rec.called = True
            started = time.monotonic()
            eval_ledger.set_call_estimate(self.reserve)
            try:
                return inner(*args, **kwargs)
            except Exception as exc:
                rec.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                rec.error_type = type(exc).__name__
                raise
            finally:
                eval_ledger.set_call_estimate(None)
                rec.seconds = round(time.monotonic() - started, 2)
                cost = llm.last_cost_usd()
                rec.cost_usd = float(cost) if cost is not None else 0.0
                usage = llm.last_usage()
                if usage is not None:
                    rec.input_tokens = getattr(usage, "input_tokens", None)
                    rec.output_tokens = getattr(usage, "output_tokens", None)
                    rec.cached_tokens = getattr(usage, "cached_tokens", None)
                rec.replayed = bool(llm.last_call_replayed())

        return _call


__all__ = [
    "CASSETTE_MODE", "CallMeter", "CallRecord", "DEV_REF", "PROBE_TIERS", "PlanningCapture", "ProbeError",
    "SWEEP_PURPOSE", "arm_sweep", "captured_requests", "check_dev", "check_route", "estimate_usd", "eval_root_for",
    "json_default", "matches_only", "money", "ordered_candidates", "out_dir_for", "request_input_tokens",
    "write_results",
]

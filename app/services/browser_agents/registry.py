"""Agent type and PM-app adapter registries (SPEC.md 3.6, D20).

Adding an agent type is one `AgentTypeSpec` here (plus a worker module, an
enqueue call site and a browser_agent_types row); no CHECK constraint changes.
Adding a PM app is one `PmAppAdapter` keyed by pm_external_links.platform.

The spec is everything BDR needs to serve a type: how to validate its seed
(`input_model`) and checkpoint, its tool set (protocol.TOOLS_BY_TYPE), which
identity it logs in as, which finish outcomes the worker may report, and the
`prepare(job)` hook the claim runs before handing a job out (D19: a job that
needs no browser at all is finished right there).

The built-ins (canary, rfi_from_email and the procore adapter) are
registered at import from their own modules (types/canary.py,
types/rfi_from_email.py, pm_apps/procore.py).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.services.browser_agents import protocol


class UnknownAgentType(KeyError):
    """No spec is registered for this agent type key."""


@dataclass(frozen=True)
class PrepareResult:
    """What `prepare(job)` decided at claim time.

    run       hand the job to the worker (the normal case);
    finish    close it without a browser: succeeded with `outcome`
              (already_current, nothing_to_do, ...) or failed with
              `error_kind` (no_link, unsupported, ...). The lease is released
              in the same fenced write, so nothing is charged twice.
    notify    a failed finish bells IT Admin (job_failed) unless False: a
              job whose PM link was removed is expected, not a fault
              (SPEC.md 5.2: no_link, no notice).
    """
    action: Literal["run", "finish"]
    status: Literal["succeeded", "failed", "canceled"] | None = None
    outcome: str | None = None
    error_kind: str | None = None
    summary: str = ""
    notify: bool = True

    @classmethod
    def run(cls) -> "PrepareResult":
        return cls(action="run")

    @classmethod
    def succeed(cls, outcome: str, summary: str) -> "PrepareResult":
        return cls(action="finish", status="succeeded", outcome=outcome, summary=summary)

    @classmethod
    def fail(cls, error_kind: str, summary: str, *, notify: bool = True) -> "PrepareResult":
        return cls(action="finish", status="failed", error_kind=error_kind, summary=summary, notify=notify)


# prepare(job_row) -> PrepareResult. Plain def (runs in the claim's threadpool
# request); it may read the database. Raising releases the lease uncharged.
PrepareFn = Callable[[dict[str, Any]], PrepareResult]


@dataclass(frozen=True)
class AgentTypeSpec:
    key: str
    label: str
    input_model: type[BaseModel]
    checkpoint_model: type[BaseModel] | None
    identity: str | None                      # 'procore' | None
    prompt_version: str
    # finish(succeeded, outcome) values the worker may send for this type.
    # Captures never come through /finish (they go to /rfi-capture).
    succeed_outcomes: tuple[str, ...]
    prepare: PrepareFn
    # Whether POST /rfi-capture is valid for this type.
    allows_capture: bool = False
    # Help reasons the type may open (protocol.HelpReasonCode subset).
    help_reasons: tuple[str, ...] = ()
    # PM-app platform this type works against (registry key), if any.
    platform: str | None = None
    principal_kind: Literal["org", "user"] = "org"

    @property
    def tools(self) -> tuple[str, ...]:
        return protocol.TOOLS_BY_TYPE[self.key]

    def validate_input(self, data: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(data)

    def validate_checkpoint(self, data: dict[str, Any]) -> None:
        """Raises ValueError on a checkpoint that does not fit the type's
        shape. Types without a declared shape accept any JSON object."""
        if self.checkpoint_model is None:
            return
        try:
            self.checkpoint_model.model_validate(data)
        except ValidationError as exc:
            raise ValueError(str(exc.errors()[0].get("msg", "invalid checkpoint"))) from None


@dataclass(frozen=True)
class PmAppAdapter:
    """One PM application the agents can read from (D20). Hooks are filled
    in by the phase that implements them; None means not available yet."""
    platform: str                              # pm_external_links.platform
    identity: str                              # secrets and login locks key
    hosts: tuple[str, ...]                     # web hosts a seed may name
    login_host: str
    agent_types: tuple[str, ...] = ()
    # seed(email_row, link_row) -> input dict | None        (RFI phase)
    seed: Callable[..., dict[str, Any] | None] | None = None
    # validate_capture(job_row, artifact_row, envelope) -> None, raises on refusal
    validate_capture: Callable[..., None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_lock = threading.Lock()
_TYPES: dict[str, AgentTypeSpec] = {}
_PM_APPS: dict[str, PmAppAdapter] = {}


def register_type(spec: AgentTypeSpec, *, replace: bool = False) -> AgentTypeSpec:
    if not protocol.AGENT_TYPE_RE.match(spec.key):
        raise ValueError(f"bad agent type key {spec.key!r}")
    if spec.key not in protocol.TOOLS_BY_TYPE:
        raise ValueError(f"agent type {spec.key!r} has no tool set in protocol.TOOLS_BY_TYPE")
    with _lock:
        if spec.key in _TYPES and not replace:
            raise ValueError(f"agent type {spec.key!r} is already registered")
        _TYPES[spec.key] = spec
    return spec


def get_type(key: str) -> AgentTypeSpec:
    try:
        return _TYPES[key]
    except KeyError:
        raise UnknownAgentType(key) from None


def has_type(key: str) -> bool:
    return key in _TYPES


def type_keys() -> tuple[str, ...]:
    return tuple(sorted(_TYPES))


def register_pm_app(adapter: PmAppAdapter, *, replace: bool = False) -> PmAppAdapter:
    with _lock:
        if adapter.platform in _PM_APPS and not replace:
            raise ValueError(f"PM app {adapter.platform!r} is already registered")
        _PM_APPS[adapter.platform] = adapter
    return adapter


def get_pm_app(platform: str) -> PmAppAdapter:
    try:
        return _PM_APPS[platform]
    except KeyError:
        raise UnknownAgentType(platform) from None


def pm_app_platforms() -> tuple[str, ...]:
    return tuple(sorted(_PM_APPS))


# ── Built-ins ─────────────────────────────────────────────────────────────────


def _register_builtins() -> None:
    """canary (types/canary.py), rfi_from_email (types/rfi_from_email.py:
    the claim-time short-circuits of SPEC.md 5.2) and the procore adapter
    (pm_apps/procore.py: seed and capture validation)."""
    from app.services.browser_agents.pm_apps import procore as procore_app
    from app.services.browser_agents.types import canary, rfi_from_email, user_browser_read

    register_type(canary.SPEC, replace=True)
    register_type(rfi_from_email.SPEC, replace=True)
    register_type(user_browser_read.SPEC, replace=True)
    register_pm_app(procore_app.ADAPTER, replace=True)


_register_builtins()

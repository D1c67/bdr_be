"""Due-date reminder preferences — palettes, document schema, role defaults.

Single source of truth shared by the prefs API (routers/users.py) and the
reminder poller (services/due_reminders.py). The two hard business rules live
here exactly once:

- actual-bid alerts are PA-only AND mandatory for the PA: non-PA users can
  never hold them (stripped on write and on read; the poller re-filters at
  fire time), and the PA can tune which offsets fire but cannot disable the
  kind (`ActualBidPref` has no `enabled` field and requires >= 1 offset).
- the external estimator never resolves through prefs: the prefs endpoints are
  require_internal, and the poller reaches estimators only via active
  estimator_assignments with the fixed full palette.

Storage: one notification_prefs row per user, `prefs` jsonb holding a full
NotificationPrefsDoc. Absent row = role defaults. Reads are lenient per kind —
a missing or invalid kind falls back to that kind's default without discarding
the user's other customizations (schema evolution / partial corruption safe).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.roles import Role

TaskOffset = Literal["2w", "1w", "2d", "1d", "1h", "expired"]
ActualBidOffset = Literal["24h", "8h", "1h"]

# Canonical (descending-duration) order. Pinned by tests: these strings are
# also ledger keys in due_reminder_log and CHECK-constrained in migration 0032.
TASK_OFFSETS: tuple[str, ...] = ("2w", "1w", "2d", "1d", "1h", "expired")
ACTUAL_BID_OFFSETS: tuple[str, ...] = ("24h", "8h", "1h")

TASK_KINDS: tuple[str, ...] = ("internal_bid", "due_from_estimator", "due_from_vendors")

# Default internal audiences (decided with G3). The estimator's inclusion for
# due_from_estimator is handled by the poller's assignment path, not prefs.
_DEFAULT_AUDIENCE: dict[str, frozenset[Role]] = {
    "internal_bid":       frozenset({Role.PM, Role.PE, Role.PA}),
    "due_from_estimator": frozenset({Role.PM, Role.PE, Role.PA}),
    "due_from_vendors":   frozenset({Role.PE}),
}


def _canonical(offsets: list[str], palette: tuple[str, ...]) -> list[str]:
    chosen = set(offsets)
    return [o for o in palette if o in chosen]


class TaskKindPref(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    # max_length tolerates client-sent duplicates (deduped below) while still
    # failing fast on absurd payloads.
    offsets: list[TaskOffset] = Field(max_length=2 * len(TASK_OFFSETS))

    @field_validator("offsets")
    @classmethod
    def _dedupe_and_order(cls, v: list[str]) -> list[str]:
        # Canonical order makes BE/FE round-trips byte-stable, so the frontend
        # can dirty-track with plain JSON equality.
        return _canonical(v, TASK_OFFSETS)


class ActualBidPref(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No `enabled`: presence of the key = on, always (mandatory for the PA).
    offsets: list[ActualBidOffset] = Field(
        min_length=1, max_length=2 * len(ACTUAL_BID_OFFSETS)
    )

    @field_validator("offsets")
    @classmethod
    def _dedupe_and_order(cls, v: list[str]) -> list[str]:
        return _canonical(v, ACTUAL_BID_OFFSETS)


class NotificationPrefsDoc(BaseModel):
    """Full prefs document — the PUT body and the stored jsonb shape."""

    model_config = ConfigDict(extra="forbid")

    internal_bid: TaskKindPref
    due_from_estimator: TaskKindPref
    due_from_vendors: TaskKindPref
    actual_bid: ActualBidPref | None = None  # PA-only; omitted for everyone else


def default_prefs(role: Role) -> NotificationPrefsDoc:
    """Role presets. Disabled kinds still carry the full palette so opting in
    later is a single toggle, not a rebuild."""
    return NotificationPrefsDoc(
        **{
            kind: TaskKindPref(
                enabled=role in _DEFAULT_AUDIENCE[kind], offsets=list(TASK_OFFSETS)
            )
            for kind in TASK_KINDS
        },
        actual_bid=(
            ActualBidPref(offsets=list(ACTUAL_BID_OFFSETS)) if role == Role.PA else None
        ),
    )


def effective_prefs(role: Role, stored: dict | None) -> NotificationPrefsDoc:
    """Resolve a user's working prefs: stored jsonb merged over role defaults.

    Per-kind lenient: each kind is validated independently and falls back to
    its default on any error, so one corrupt/missing kind never discards the
    rest. actual_bid is forced off for non-PA regardless of stored content
    (covers dev role switches and PA reassignment after a row was saved).
    """
    defaults = default_prefs(role)
    stored = stored or {}

    kinds: dict[str, TaskKindPref | ActualBidPref | None] = {}
    for kind in TASK_KINDS:
        try:
            kinds[kind] = TaskKindPref.model_validate(stored[kind])
        except (KeyError, TypeError, ValidationError):
            kinds[kind] = getattr(defaults, kind)

    if role != Role.PA:
        kinds["actual_bid"] = None
    else:
        try:
            kinds["actual_bid"] = ActualBidPref.model_validate(stored["actual_bid"])
        except (KeyError, TypeError, ValidationError):
            kinds["actual_bid"] = defaults.actual_bid

    return NotificationPrefsDoc(**kinds)

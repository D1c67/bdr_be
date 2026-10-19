"""Launcher state and the pure rules for choosing, promoting and rolling back
versions (spec 8 "Update" step 4, D16). No I/O except load/save.

state/launcher.json (atomic write + .bak):
    current             last version that proved healthy here
    previous            the one before it (rollback target)
    trial               {"version", "from_version", "attempts", "started_at"} while a
                        newly staged version has not proved healthy yet
    bad_versions        never launched or staged again; reported on /hello
    crashes             {version: [unix ts, ...]} within the crash window
    min_version_floor   highest signed min_version of any version promoted here
    events              the last few decisions, for support

Health is local only: the worker process starts, loads its local config and
starts Chromium, then writes the health marker the launcher named. Nothing
here depends on reaching BDR, so an outage never causes a rollback.

Rules:
  - A trial that does not write its health marker within HEALTH_DEADLINE_SECONDS,
    or exits before writing it, or is retried more than TRIAL_MAX_ATTEMPTS times
    (power loss mid trial), is marked bad and the launcher returns to `current`.
  - A healthy trial is promoted: previous = current, current = trial, and the
    floor rises to the trial's signed min_version.
  - CRASH_LIMIT crashes of `current` within CRASH_WINDOW_SECONDS roll back to
    `previous` (when it is installed, not bad and not below the floor) and mark
    `current` bad. Without a usable previous the launcher keeps restarting with
    a backoff.
  - Exit codes that are not the version's fault never count: 0 (stopped),
    2 (local setup: not enrolled), 3 (stale boot), 4 (revoked), 5 (update staged).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic
from .release import EMBEDDED_MIN_VERSION, is_version, max_version, version_lt

HEALTH_DEADLINE_SECONDS = 180.0
CRASH_LIMIT = 3
CRASH_WINDOW_SECONDS = 600.0
TRIAL_MAX_ATTEMPTS = 3
MAX_EVENTS = 40

EXIT_OK = 0
EXIT_SETUP = 2
EXIT_STALE_BOOT = 3
EXIT_REVOKED = 4
EXIT_UPDATE = 5
EXIT_HEALTH_TIMEOUT = -1000   # synthetic: the launcher killed a child that never got healthy

BACKOFF_RESTART = 2.0
BACKOFF_CLEAN_EXIT = 5.0
BACKOFF_CRASH = 15.0
BACKOFF_SETUP = 60.0
BACKOFF_REVOKED = 600.0
BACKOFF_NOTHING_TO_RUN = 60.0


@dataclass
class LauncherState:
    current: str | None = None
    previous: str | None = None
    trial: dict[str, Any] | None = None
    bad_versions: list[str] = field(default_factory=list)
    crashes: dict[str, list[float]] = field(default_factory=dict)
    min_version_floor: str = EMBEDDED_MIN_VERSION
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"current": self.current, "previous": self.previous, "trial": self.trial,
                "bad_versions": list(self.bad_versions), "crashes": self.crashes,
                "min_version_floor": self.min_version_floor, "events": self.events[-MAX_EVENTS:]}

    @classmethod
    def from_json(cls, data: Any) -> "LauncherState":
        if not isinstance(data, dict):
            return cls()
        trial = data.get("trial")
        if not (isinstance(trial, dict) and is_version(trial.get("version"))):
            trial = None
        crashes = data.get("crashes") if isinstance(data.get("crashes"), dict) else {}
        return cls(
            current=data.get("current") if is_version(data.get("current")) else None,
            previous=data.get("previous") if is_version(data.get("previous")) else None,
            trial=trial,
            bad_versions=[v for v in (data.get("bad_versions") or []) if is_version(v)],
            crashes={k: [float(t) for t in v if isinstance(t, (int, float))]
                     for k, v in crashes.items() if is_version(k) and isinstance(v, list)},
            min_version_floor=max_version(EMBEDDED_MIN_VERSION, data.get("min_version_floor")),
            events=[e for e in (data.get("events") or []) if isinstance(e, dict)][-MAX_EVENTS:],
        )

    def note(self, now: float, event: str, version: str | None = None, detail: str = "") -> None:
        self.events.append({"at": round(now, 3), "event": event, "version": version, "detail": detail})
        del self.events[:-MAX_EVENTS]

    def mark_bad(self, version: str) -> None:
        if version not in self.bad_versions:
            self.bad_versions.append(version)


def load_state(path: Path) -> LauncherState:
    return LauncherState.from_json(atomic.read_json(path))


def save_state(path: Path, state: LauncherState) -> None:
    atomic.write_json(path, state.to_json())


def read_pending(path: Path) -> str | None:
    """pending.txt holds one version line. Its .bak is never consulted: a
    consumed pending must not come back."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if is_version(text) else None


def write_pending(path: Path, version: str) -> None:
    atomic.write_bytes(path, (version + "\n").encode("utf-8"), keep_bak=False)


def clear_pending(path: Path) -> None:
    atomic.remove(path)


# Pure rules ---------------------------------------------------------------------------------

# is_launchable(version) -> bool: installed and complete on disk.
Launchable = Callable[[str], bool]
# min_version_of(version) -> the signed min_version recorded at install.
MinVersionOf = Callable[[str], str | None]


def usable(state: LauncherState, version: str | None, launchable: Launchable) -> bool:
    return bool(version and version not in state.bad_versions
                and not version_lt(version, state.min_version_floor) and launchable(version))


@dataclass(frozen=True)
class Choice:
    version: str | None
    trial: bool
    reason: str


def choose(state: LauncherState, pending: str | None, launchable: Launchable, now: float) -> Choice:
    """Pick what to start next. Mutates `state` (trial bookkeeping). The caller
    clears pending.txt after this returns."""
    if pending is not None and pending != state.current:
        if usable(state, pending, launchable):
            state.trial = {"version": pending, "from_version": state.current, "attempts": 1,
                           "started_at": now}
            state.note(now, "trial_start", pending, f"from {state.current}")
            return Choice(pending, True, "pending")
        state.note(now, "pending_refused", pending, "bad, below floor or not installed")
    if state.trial is not None:
        tv = state.trial["version"]
        attempts = int(state.trial.get("attempts", 1)) + 1
        if attempts > TRIAL_MAX_ATTEMPTS or not usable(state, tv, launchable):
            fail_trial(state, now, "trial never proved healthy")
        else:
            # The launcher restarted mid trial (power loss, service restart).
            state.trial["attempts"] = attempts
            state.note(now, "trial_retry", tv, f"attempt {attempts}")
            return Choice(tv, True, "trial_retry")
    if usable(state, state.current, launchable):
        return Choice(state.current, False, "current")
    if usable(state, state.previous, launchable):
        bad = state.current
        state.note(now, "current_unusable", bad, f"falling back to {state.previous}")
        state.current, state.previous = state.previous, None
        return Choice(state.current, False, "previous")
    return Choice(None, False, "nothing_installed")


def fail_trial(state: LauncherState, now: float, why: str) -> None:
    if state.trial is None:
        return
    tv = state.trial["version"]
    state.mark_bad(tv)
    state.crashes.pop(tv, None)
    state.note(now, "trial_failed", tv, why)
    state.trial = None


def on_healthy(state: LauncherState, version: str, now: float, min_version_of: MinVersionOf) -> bool:
    """The child wrote its health marker. Promotes a trial; returns True when
    the state changed."""
    if state.trial is None or state.trial.get("version") != version:
        return False
    old = state.current
    if old != version:
        state.previous = old
    state.current = version
    state.trial = None
    state.crashes.pop(version, None)
    state.min_version_floor = max_version(state.min_version_floor, min_version_of(version))
    state.note(now, "promoted", version, f"previous {old}; floor {state.min_version_floor}")
    return True


@dataclass(frozen=True)
class ExitDecision:
    delay: float
    reason: str


def on_exit(state: LauncherState, version: str, code: int, healthy: bool, now: float,
            launchable: Launchable) -> ExitDecision:
    """The child exited (or was killed for missing its health deadline)."""
    in_trial = state.trial is not None and state.trial.get("version") == version
    if code == EXIT_UPDATE:
        state.note(now, "update_staged", version)
        return ExitDecision(0.0, "update")
    if in_trial and not healthy:
        why = "health deadline missed" if code == EXIT_HEALTH_TIMEOUT else f"exited {code} before healthy"
        fail_trial(state, now, why)
        return ExitDecision(BACKOFF_RESTART, "trial_failed")
    if code == EXIT_STALE_BOOT:
        return ExitDecision(BACKOFF_RESTART, "stale_boot")
    if code == EXIT_REVOKED:
        state.note(now, "revoked", version, "re-enroll with the installer")
        return ExitDecision(BACKOFF_REVOKED, "revoked")
    if code == EXIT_SETUP:
        state.note(now, "setup_problem", version)
        return ExitDecision(BACKOFF_SETUP, "setup")
    if code == EXIT_OK:
        return ExitDecision(BACKOFF_CLEAN_EXIT, "clean_exit")
    # A crash (including a missed health deadline of an established version).
    window = [t for t in state.crashes.get(version, []) if now - t < CRASH_WINDOW_SECONDS]
    window.append(now)
    state.crashes[version] = window
    state.note(now, "crash", version, f"code {code}; {len(window)} in window")
    if len(window) >= CRASH_LIMIT and version == state.current:
        if usable(state, state.previous, launchable) and state.previous != version:
            target = state.previous
            state.mark_bad(version)
            state.crashes.pop(version, None)
            state.note(now, "rolled_back", version, f"to {target}")
            state.current, state.previous = target, None
            return ExitDecision(BACKOFF_RESTART, "rolled_back")
        state.note(now, "no_rollback_target", version)
    return ExitDecision(BACKOFF_CRASH, "crash")


def default_clock() -> float:
    return time.time()

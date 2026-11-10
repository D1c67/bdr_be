"""The launcher's run loop: start the chosen worker version, watch its local
health, apply the rules in state.py, repeat until the service stops.

The child is `versions/<v>/venv/python -I -X utf8 -m bdr_agent --home <home> run`
with:
    BDR_AGENT_HOME            the data folder
    BDR_AGENT_HEALTH_FILE     where the worker writes its health marker
    BDR_AGENT_BAD_VERSIONS    comma list, reported on /hello and never staged
    BDR_AGENT_MIN_VERSION     the local floor, so the updater refuses early
    BDR_AGENT_LAUNCHER        path of launcher.py (the updater calls `stage`)
    BDR_AGENT_RUNTIME         the base Python that runs the launcher
    PLAYWRIGHT_BROWSERS_PATH  <home>/ms-playwright

Stop (WinSW sends Ctrl+C to the console group, so the worker gets it too): the
launcher waits STOP_GRACE_SECONDS for the worker's own shutdown, then kills it.
"""

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import release
from .paths import LauncherPaths, venv_python
from .state import (
    BACKOFF_NOTHING_TO_RUN,
    EXIT_HEALTH_TIMEOUT,
    HEALTH_DEADLINE_SECONDS,
    LauncherState,
    choose,
    clear_pending,
    load_state,
    on_exit,
    on_healthy,
    read_pending,
    save_state,
)

log = logging.getLogger("bdr_launcher")

STOP_GRACE_SECONDS = 45.0
POLL_SECONDS = 1.0


class Proc(Protocol):
    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


Spawner = Callable[[list[str], dict[str, str], Path], Proc]


def popen_spawner(cmd: list[str], env: dict[str, str], cwd: Path) -> Proc:
    # Same console process group as the launcher: WinSW's Ctrl+C reaches both.
    return subprocess.Popen(cmd, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL)


@dataclass
class Clock:
    now: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


class Launcher:
    def __init__(self, paths: LauncherPaths, *, launcher_script: Path, runtime: str | None = None,
                 spawner: Spawner = popen_spawner, clock: Clock | None = None,
                 health_deadline: float = HEALTH_DEADLINE_SECONDS, stop_grace: float = STOP_GRACE_SECONDS,
                 poll: float = POLL_SECONDS, launchable: Callable[[str], bool] | None = None):
        self.paths = paths
        self.launcher_script = Path(launcher_script)
        self.runtime = runtime or sys.executable
        self.spawner = spawner
        self.clock = clock or Clock()
        self.health_deadline = health_deadline
        self.stop_grace = stop_grace
        self.poll = poll
        self._launchable = launchable or (lambda v: release.is_installed(self.paths, v))
        self._stop = threading.Event()
        self.runs: list[tuple[str, int, bool]] = []   # (version, exit code, healthy), for tests and logs

    # Control -------------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _sleep(self, seconds: float) -> None:
        end = self.clock.monotonic() + seconds
        while not self._stop.is_set():
            left = end - self.clock.monotonic()
            if left <= 0:
                return
            self.clock.sleep(min(self.poll, left))

    # State ----------------------------------------------------------------------------

    def load(self) -> LauncherState:
        return load_state(self.paths.state_file)

    def save(self, state: LauncherState) -> None:
        save_state(self.paths.state_file, state)

    def boot_cleanup(self) -> None:
        self.paths.ensure()
        removed = release.remove_partials(self.paths)
        if removed:
            log.info("removed %d incomplete version folder(s)", removed)
        for marker in self.paths.health_dir.glob("*"):
            try:
                marker.unlink()
            except OSError:
                pass

    def child_env(self, state: LauncherState, health_file: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update({
            "BDR_AGENT_HOME": str(self.paths.home),
            "BDR_AGENT_HEALTH_FILE": str(health_file),
            "BDR_AGENT_BAD_VERSIONS": ",".join(state.bad_versions),
            "BDR_AGENT_MIN_VERSION": state.min_version_floor,
            "BDR_AGENT_LAUNCHER": str(self.launcher_script),
            "BDR_AGENT_RUNTIME": self.runtime,
            "PLAYWRIGHT_BROWSERS_PATH": str(self.paths.browsers_dir),
        })
        return env

    def child_cmd(self, version: str) -> list[str]:
        py = venv_python(self.paths.version_dir(version) / "venv")
        return [str(py), "-I", "-X", "utf8", "-m", "bdr_agent", "--home", str(self.paths.home), "run"]

    # One launch -----------------------------------------------------------------------

    def _stop_child(self, proc: Proc) -> int:
        if sys.platform != "win32":
            # Posix (dev): forward the stop. On Windows the console Ctrl+C from
            # WinSW already reached the worker.
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            return proc.wait(timeout=self.stop_grace)
        except subprocess.TimeoutExpired:
            log.warning("worker did not stop within %ss; killing it", self.stop_grace)
            proc.kill()
            return proc.wait(timeout=10)

    def run_once(self, state: LauncherState, version: str) -> tuple[int, bool]:
        """Start `version`, return (exit code, healthy)."""
        health = self.paths.health_dir / f"{version}-{uuid.uuid4().hex}.ok"
        # Personal browser pool: copy this version's node and Chromium to the
        # fixed runtime paths the firewall rules name. Never fatal; a stale
        # runtime only makes the worker's self-test refuse personal jobs.
        release.sync_stable_runtime_if_wanted(self.paths, version)
        proc = self.spawner(self.child_cmd(version), self.child_env(state, health), self.paths.home)
        started = self.clock.monotonic()
        healthy = False
        code: int | None = None
        while True:
            if not healthy and health.exists():
                healthy = True
                if on_healthy(state, version, self.clock.now(), self._min_version_of):
                    self.save(state)
                    self._prune(state)
                log.info("worker %s is healthy", version)
            code = proc.poll()
            if code is not None:
                break
            if self._stop.is_set():
                code = self._stop_child(proc)
                break
            if not healthy and self.clock.monotonic() - started > self.health_deadline:
                log.error("worker %s not healthy within %ss; killing it", version, self.health_deadline)
                proc.kill()
                proc.wait(timeout=10)
                code = EXIT_HEALTH_TIMEOUT
                break
            self.clock.sleep(self.poll)
        if not healthy and code != EXIT_HEALTH_TIMEOUT and health.exists():
            healthy = True
            if on_healthy(state, version, self.clock.now(), self._min_version_of):
                self.save(state)
        try:
            health.unlink()
        except OSError:
            pass
        return code, healthy

    def _min_version_of(self, version: str) -> str | None:
        return release.installed_min_version(self.paths, version)

    def _prune(self, state: LauncherState) -> None:
        keep = [state.current, state.previous]
        pending = read_pending(self.paths.pending_file)
        if pending:
            keep.append(pending)
        try:
            removed = release.prune_versions(self.paths, [v for v in keep if v])
        except OSError as exc:
            log.warning("could not prune old versions: %s", exc)
            return
        if removed:
            log.info("removed old versions %s", ", ".join(removed))

    # Loop -----------------------------------------------------------------------------

    def step(self) -> float:
        """One choose-run-decide cycle. Returns the delay before the next one."""
        state = self.load()
        pending = read_pending(self.paths.pending_file)
        choice = choose(state, pending, self._launchable, self.clock.now())
        if pending is not None:
            clear_pending(self.paths.pending_file)
        self.save(state)
        if choice.version is None:
            log.error("no installed worker version can run (state: current=%s previous=%s bad=%s); "
                      "rerun the installer", state.current, state.previous, state.bad_versions)
            return BACKOFF_NOTHING_TO_RUN
        log.info("starting worker %s (%s)", choice.version, choice.reason)
        code, healthy = self.run_once(state, choice.version)
        self.runs.append((choice.version, code, healthy))
        log.info("worker %s exited with %s (healthy=%s)", choice.version, code, healthy)
        if self._stop.is_set():
            self.save(state)
            return 0.0
        decision = on_exit(state, choice.version, code, healthy, self.clock.now(), self._launchable)
        self.save(state)
        if decision.reason in ("trial_failed", "rolled_back"):
            log.error("version %s marked bad (%s); running %s", choice.version, decision.reason,
                      state.current)
        return decision.delay

    def run_forever(self, *, max_cycles: int | None = None) -> int:
        self.boot_cleanup()
        cycles = 0
        while not self._stop.is_set():
            delay = self.step()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            self._sleep(delay)
        return 0


def install_signal_handlers(launcher: Launcher) -> None:
    import signal

    def _handler(*_: Any) -> None:
        launcher.request_stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass

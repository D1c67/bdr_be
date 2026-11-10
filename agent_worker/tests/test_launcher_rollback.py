"""Launcher version choice, promotion and local-health rollback (spec 8 step 4):
the pure rules in state.py and the run loop in runner.py with fake processes
and a fake clock (no Windows, no real children)."""

import subprocess

import pytest

from bdr_launcher import atomic
from bdr_launcher.paths import LauncherPaths
from bdr_launcher.runner import Clock, Launcher
from bdr_launcher.state import (
    CRASH_LIMIT,
    EXIT_HEALTH_TIMEOUT,
    EXIT_REVOKED,
    EXIT_SETUP,
    EXIT_STALE_BOOT,
    EXIT_UPDATE,
    TRIAL_MAX_ATTEMPTS,
    LauncherState,
    choose,
    load_state,
    on_exit,
    on_healthy,
    read_pending,
    save_state,
    write_pending,
)

ALL = lambda v: True  # noqa: E731


def _state(**kw) -> LauncherState:
    return LauncherState(**kw)


# Pure rules -------------------------------------------------------------------------------


def test_pending_starts_a_trial_and_health_promotes_it():
    st = _state(current="1.0.0")
    c = choose(st, "1.1.0", ALL, 100.0)
    assert (c.version, c.trial) == ("1.1.0", True)
    assert st.trial["from_version"] == "1.0.0"
    assert on_healthy(st, "1.1.0", 110.0, lambda v: "1.1.0")
    assert (st.current, st.previous, st.trial, st.min_version_floor) == ("1.1.0", "1.0.0", None, "1.1.0")
    assert not on_healthy(st, "1.1.0", 111.0, lambda v: "1.1.0")   # already promoted


@pytest.mark.parametrize("code", [1, EXIT_HEALTH_TIMEOUT, EXIT_SETUP, EXIT_STALE_BOOT, 0])
def test_trial_that_never_gets_healthy_is_bad_and_current_resumes(code):
    st = _state(current="1.0.0")
    choose(st, "1.1.0", ALL, 0.0)
    d = on_exit(st, "1.1.0", code, False, 5.0, ALL)
    assert d.reason == "trial_failed"
    assert st.bad_versions == ["1.1.0"] and st.trial is None and st.current == "1.0.0"
    assert choose(st, None, ALL, 6.0).version == "1.0.0"
    # A bad version is never tried again, even if staged again.
    assert choose(st, "1.1.0", ALL, 7.0).version == "1.0.0"


def test_trial_survives_a_power_loss_but_not_forever():
    st = _state(current="1.0.0")
    choose(st, "1.1.0", ALL, 0.0)
    for attempt in range(2, TRIAL_MAX_ATTEMPTS + 1):
        c = choose(st, None, ALL, float(attempt))    # launcher restarted mid trial
        assert (c.version, c.trial, st.trial["attempts"]) == ("1.1.0", True, attempt)
    c = choose(st, None, ALL, 9.0)
    assert c.version == "1.0.0" and "1.1.0" in st.bad_versions


def test_crash_loop_of_current_rolls_back_to_previous():
    st = _state(current="1.1.0", previous="1.0.0")
    for i in range(CRASH_LIMIT - 1):
        assert on_exit(st, "1.1.0", 1, True, 100.0 + i, ALL).reason == "crash"
    d = on_exit(st, "1.1.0", 1, True, 103.0, ALL)
    assert d.reason == "rolled_back"
    assert (st.current, st.previous, st.bad_versions) == ("1.0.0", None, ["1.1.0"])


def test_crashes_outside_the_window_do_not_roll_back():
    st = _state(current="1.1.0", previous="1.0.0")
    for t in (0.0, 400.0, 800.0, 1200.0):
        assert on_exit(st, "1.1.0", 1, True, t, ALL).reason == "crash"
    assert st.current == "1.1.0" and not st.bad_versions


@pytest.mark.parametrize("code,reason", [(0, "clean_exit"), (EXIT_SETUP, "setup"), (EXIT_STALE_BOOT, "stale_boot"),
                                         (EXIT_REVOKED, "revoked"), (EXIT_UPDATE, "update")])
def test_exit_codes_that_are_not_the_versions_fault_never_count(code, reason):
    st = _state(current="1.1.0", previous="1.0.0")
    for i in range(10):
        assert on_exit(st, "1.1.0", code, True, float(i), ALL).reason == reason
    assert st.current == "1.1.0" and not st.bad_versions and not st.crashes


def test_no_rollback_below_the_signed_floor_or_to_a_missing_version():
    st = _state(current="1.1.0", previous="1.0.0", min_version_floor="1.1.0")
    for i in range(CRASH_LIMIT + 2):
        on_exit(st, "1.1.0", 1, True, float(i), ALL)
    assert st.current == "1.1.0" and not st.bad_versions
    st2 = _state(current="1.1.0", previous="1.0.0")
    for i in range(CRASH_LIMIT):
        on_exit(st2, "1.1.0", 1, True, float(i), lambda v: v != "1.0.0")
    assert st2.current == "1.1.0"
    assert any(e["event"] == "no_rollback_target" for e in st2.events)


def test_choose_falls_back_when_current_is_unusable():
    st = _state(current="1.1.0", previous="1.0.0")
    c = choose(st, None, lambda v: v == "1.0.0", 0.0)
    assert c.version == "1.0.0" and st.current == "1.0.0"
    assert choose(_state(), None, ALL, 0.0).version is None
    st3 = _state(current="1.0.0", min_version_floor="1.1.0")
    assert choose(st3, None, ALL, 0.0).version is None   # below the floor: never launched
    st4 = _state(current="1.0.0")
    assert choose(st4, "1.1.0", lambda v: v == "1.0.0", 0.0).version == "1.0.0"   # pending not installed


def test_state_file_round_trip_and_bak_recovery(tmp_path):
    p = tmp_path / "launcher.json"
    st = _state(current="1.1.0", previous="1.0.0", bad_versions=["1.2.0"], min_version_floor="1.1.0")
    st.crashes["1.1.0"] = [1.0]
    save_state(p, st)
    st.current = "1.2.1"
    save_state(p, st)
    p.write_text("{ torn")                        # power loss mid write of the primary
    back = load_state(p)
    assert (back.current, back.previous, back.bad_versions, back.min_version_floor) == \
        ("1.1.0", "1.0.0", ["1.2.0"], "1.1.0")
    p.write_text('{"current": "nope", "bad_versions": ["x", "1.3.0"], "min_version_floor": "0.1.0"}')
    weird = load_state(p)
    assert weird.current is None and weird.bad_versions == ["1.3.0"] and weird.min_version_floor == "1.0.0"


def test_pending_never_comes_back_from_a_bak(tmp_path):
    p = tmp_path / "pending.txt"
    write_pending(p, "1.1.0")
    write_pending(p, "1.2.0")
    assert read_pending(p) == "1.2.0"
    assert not atomic.bak_path(p).exists()
    atomic.remove(p)
    assert read_pending(p) is None
    p.write_text("garbage")
    assert read_pending(p) is None


# Run loop with fake processes ---------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.001)


class FakeProc:
    """Exits with `code` after `runtime` seconds; writes the health file after
    `health_at` seconds (None: never)."""

    def __init__(self, clock, env, *, code, runtime, health_at=None, ignore_stop=False):
        self.clock = clock
        self.env = env
        self.code = code
        self.started = clock.t
        self.runtime = runtime
        self.health_at = health_at
        self.ignore_stop = ignore_stop
        self.terminated = False
        self.killed = False
        self.result = None

    def _tick(self):
        from pathlib import Path

        age = self.clock.t - self.started
        if self.health_at is not None and age >= self.health_at and self.result is None:
            Path(self.env["BDR_AGENT_HEALTH_FILE"]).write_text("ok")
        if self.result is None and age >= self.runtime:
            self.result = self.code

    def poll(self):
        self._tick()
        return self.result

    def wait(self, timeout=None):
        if self.result is None and self.terminated and not self.ignore_stop:
            self.result = 0
        if self.result is None and self.killed:
            self.result = -9
        if self.result is None:
            raise subprocess.TimeoutExpired("worker", timeout)
        return self.result

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class Script:
    """Spawner that hands out FakeProcs from a per-version script."""

    def __init__(self, clock, plan):
        self.clock = clock
        self.plan = {k: list(v) for k, v in plan.items()}
        self.spawned: list[tuple[str, dict]] = []
        self.procs: list[FakeProc] = []

    def __call__(self, cmd, env, cwd):
        version = cmd[0].split("versions")[1].strip("/\\").split("/")[0].split("\\")[0]
        spec = self.plan[version].pop(0) if len(self.plan[version]) > 1 else self.plan[version][0]
        self.spawned.append((version, env))
        proc = FakeProc(self.clock, env, **spec)
        self.procs.append(proc)
        return proc


def _launcher(tmp_path, plan, installed=("1.0.0", "1.1.0", "1.2.0"), **kw):
    paths = LauncherPaths.for_home(tmp_path / "home")
    paths.ensure()
    clock = FakeClock()
    script = Script(clock, plan)
    lch = Launcher(paths, launcher_script=tmp_path / "launcher.py", runtime="/rt/python", spawner=script,
                   clock=Clock(now=clock.now, monotonic=clock.monotonic, sleep=clock.sleep),
                   launchable=lambda v: v in installed, **kw)
    return lch, paths, script, clock


def test_loop_updates_promotes_and_reports_bad_versions(tmp_path):
    lch, paths, script, _clock = _launcher(tmp_path, {
        "1.0.0": [{"code": EXIT_UPDATE, "runtime": 30, "health_at": 5}],
        "1.1.0": [{"code": EXIT_UPDATE, "runtime": 40, "health_at": 8}],
        "1.2.0": [{"code": 1, "runtime": 3}],             # crashes before healthy
    })
    save_state(paths.state_file, _state(current="1.0.0"))
    write_pending(paths.pending_file, "1.1.0")
    lch.step()                                         # 1.1.0 trial: healthy, then stages 1.2.0
    st = load_state(paths.state_file)
    assert (st.current, st.previous) == ("1.1.0", "1.0.0")
    write_pending(paths.pending_file, "1.2.0")
    lch.step()                                         # 1.2.0 trial crashes
    st = load_state(paths.state_file)
    assert st.current == "1.1.0" and st.bad_versions == ["1.2.0"] and st.trial is None
    lch.step()                                         # back on 1.1.0
    assert [r[0] for r in lch.runs] == ["1.1.0", "1.2.0", "1.1.0"]
    env = script.spawned[-1][1]
    assert env["BDR_AGENT_BAD_VERSIONS"] == "1.2.0"
    assert env["BDR_AGENT_MIN_VERSION"] == "1.0.0"
    assert env["BDR_AGENT_RUNTIME"] == "/rt/python"
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(paths.browsers_dir)
    assert read_pending(paths.pending_file) is None
    assert not list(paths.health_dir.iterdir())


def test_loop_kills_a_trial_that_misses_the_health_deadline(tmp_path):
    lch, paths, script, clock = _launcher(tmp_path, {
        "1.0.0": [{"code": 0, "runtime": 10**9, "health_at": 1}],
        "1.1.0": [{"code": 0, "runtime": 10**9, "health_at": None}],   # hangs, never healthy
    }, health_deadline=180.0)
    save_state(paths.state_file, _state(current="1.0.0"))
    write_pending(paths.pending_file, "1.1.0")
    start = clock.t
    lch.step()
    assert script.procs[0].killed and lch.runs[0] == ("1.1.0", EXIT_HEALTH_TIMEOUT, False)
    assert 180 <= clock.t - start < 200
    st = load_state(paths.state_file)
    assert st.bad_versions == ["1.1.0"] and st.current == "1.0.0"


def test_loop_rolls_back_an_established_version_after_a_crash_loop(tmp_path):
    lch, paths, _script, _clock = _launcher(tmp_path, {
        "1.1.0": [{"code": 1, "runtime": 60, "health_at": 2}],
        "1.0.0": [{"code": 0, "runtime": 5, "health_at": 1}],
    })
    save_state(paths.state_file, _state(current="1.1.0", previous="1.0.0"))
    delays = [lch.step() for _ in range(CRASH_LIMIT)]
    st = load_state(paths.state_file)
    assert (st.current, st.bad_versions) == ("1.0.0", ["1.1.0"])
    assert delays[-1] < delays[0]           # rollback restarts promptly
    lch.step()
    assert lch.runs[-1][0] == "1.0.0"


def test_bdr_outage_is_not_a_health_failure(tmp_path):
    # A worker that cannot reach BDR keeps running (it retries /hello): it is
    # healthy locally, so nothing is rolled back however long the outage lasts.
    lch, paths, _script, clock = _launcher(tmp_path, {
        "1.1.0": [{"code": 0, "runtime": 6 * 3600, "health_at": 20}],
    })
    save_state(paths.state_file, _state(current="1.0.0"))
    write_pending(paths.pending_file, "1.1.0")
    lch.step()
    st = load_state(paths.state_file)
    assert st.current == "1.1.0" and not st.bad_versions
    assert clock.t - 1000.0 >= 6 * 3600


def test_stop_request_forwards_and_waits_then_kills(tmp_path, monkeypatch):
    import bdr_launcher.runner as runner_mod

    monkeypatch.setattr(runner_mod.sys, "platform", "linux")
    lch, paths, script, clock = _launcher(tmp_path, {
        "1.0.0": [{"code": 0, "runtime": 10**9, "health_at": 1, "ignore_stop": True}],
    }, stop_grace=5.0)
    save_state(paths.state_file, _state(current="1.0.0"))
    orig_sleep = clock.sleep

    def sleep_then_stop(s):
        orig_sleep(s)
        if clock.t - 1000.0 > 30:
            lch.request_stop()

    lch.clock.sleep = sleep_then_stop
    assert lch.step() == 0.0
    proc = script.procs[0]
    assert proc.terminated and proc.killed
    st = load_state(paths.state_file)
    assert st.current == "1.0.0" and not st.crashes


def test_nothing_installed_waits_instead_of_spinning(tmp_path):
    lch, paths, script, _clock = _launcher(tmp_path, {}, installed=())
    assert lch.step() == 60.0
    assert script.spawned == []


def test_child_command_is_isolated_and_names_the_home(tmp_path):
    lch, paths, _script, _clock = _launcher(tmp_path, {})
    cmd = lch.child_cmd("1.1.0")
    assert cmd[1:] == ["-I", "-X", "utf8", "-m", "bdr_agent", "--home", str(paths.home), "run"]
    assert "1.1.0" in cmd[0] and "venv" in cmd[0]


def test_run_forever_cleans_partials_and_stale_health_markers(tmp_path):
    lch, paths, _script, _clock = _launcher(tmp_path, {"1.0.0": [{"code": 0, "runtime": 1, "health_at": 0}]})
    (paths.versions_dir / "1.3.0.partial-7").mkdir(parents=True)
    (paths.health_dir / "old.ok").write_text("x")
    save_state(paths.state_file, _state(current="1.0.0"))
    lch.run_forever(max_cycles=1)
    assert not (paths.versions_dir / "1.3.0.partial-7").exists()
    assert not (paths.health_dir / "old.ok").exists()


def test_every_start_syncs_the_stable_runtime_first(tmp_path, monkeypatch):
    from bdr_launcher import release
    calls = []
    monkeypatch.setattr(release, "sync_stable_runtime_if_wanted", lambda p, v: calls.append(v))
    lch, paths, _script, _clock = _launcher(tmp_path, {"1.0.0": [{"code": 0, "runtime": 3, "health_at": 1}]})
    save_state(paths.state_file, _state(current="1.0.0"))
    lch.step()
    assert calls == ["1.0.0"] and [r[0] for r in lch.runs] == ["1.0.0"]


def test_a_failed_runtime_sync_never_stops_the_worker(tmp_path):
    # Real hook on an org-pool home: not wanted, so nothing is copied and the worker starts.
    lch, paths, _script, _clock = _launcher(tmp_path, {"1.0.0": [{"code": 0, "runtime": 3, "health_at": 1}]})
    save_state(paths.state_file, _state(current="1.0.0"))
    lch.step()
    assert [r[0] for r in lch.runs] == ["1.0.0"] and not (paths.home / "runtime").exists()

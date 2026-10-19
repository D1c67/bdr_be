"""Launcher commands (stdlib only).

    python launcher.py [--home DIR] run
        The service entry (WinSW). Runs the current worker version forever.
    python launcher.py [--home DIR] stage --zip Z --release-json J
        Called by the worker's updater: verify (signature, sha256, min_version,
        floor, bad_versions), install side by side, write state/pending.txt.
    python launcher.py [--home DIR] install-bundle --zip Z --release-json J
        Called by install.ps1 as Administrator: same checks, then make it the
        current version when nothing newer is installed. An explicit admin
        install clears that version from bad_versions.
    python launcher.py [--home DIR] fingerprint [--key PATH]
    python launcher.py [--home DIR] status [--python-path]

`stage` and `install-bundle` print one JSON line: {"ok": true, "version": ...}
or {"ok": false, "reason": "<code>", "detail": "..."}. Exit codes: 0 ok,
10 the release was rejected, 1 anything else.
"""

import argparse
import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

from . import release
from .paths import LauncherPaths, default_home, venv_python
from .runner import Launcher, install_signal_handlers
from .state import load_state, save_state, usable, write_pending

EXIT_REJECTED = 10
log = logging.getLogger("bdr_launcher")


def _setup_logging(paths: LauncherPaths, *, to_file: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        try:
            paths.logs_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                paths.logs_dir / "launcher.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"))
        except OSError:
            pass
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


def _paths(args: argparse.Namespace) -> LauncherPaths:
    home = Path(args.home) if args.home else default_home()
    launcher_dir = Path(args.launcher_dir) if args.launcher_dir else Path(__file__).resolve().parent.parent
    return LauncherPaths.for_home(home, launcher_dir)


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, sort_keys=True), flush=True)


def _staging_inputs(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    return release.load_release_json(Path(args.release_json))


def cmd_stage(args: argparse.Namespace) -> int:
    paths = _paths(args)
    _setup_logging(paths, to_file=True)
    try:
        manifest, signature = _staging_inputs(args)
        state = load_state(paths.state_file)
        res = release.stage_bundle(paths, Path(args.zip), manifest, signature, runtime=sys.executable,
                                   floor=state.min_version_floor, bad_versions=state.bad_versions)
        if res.version == state.current:
            _emit({"ok": True, "version": res.version, "pending": False, "reused": res.reused})
            return 0
        write_pending(paths.pending_file, res.version)
    except release.ReleaseRejected as exc:
        log.error("release rejected: %s", exc)
        _emit({"ok": False, "reason": exc.reason, "detail": exc.detail})
        return EXIT_REJECTED
    except Exception as exc:  # noqa: BLE001 - reported to the worker as one line
        log.exception("stage failed")
        _emit({"ok": False, "reason": "error", "detail": str(exc)[:300]})
        return 1
    _emit({"ok": True, "version": res.version, "pending": True, "reused": res.reused})
    return 0


def cmd_install_bundle(args: argparse.Namespace) -> int:
    paths = _paths(args)
    _setup_logging(paths, to_file=True)
    try:
        manifest, signature = _staging_inputs(args)
        state = load_state(paths.state_file)
        res = release.stage_bundle(paths, Path(args.zip), manifest, signature, runtime=sys.executable,
                                   floor=state.min_version_floor, bad_versions=())
        v = res.version
        if v in state.bad_versions:
            state.bad_versions.remove(v)
        launchable = lambda x: release.is_installed(paths, x)  # noqa: E731
        activate = (not usable(state, state.current, launchable)
                    or release.version_lt(state.current, v))  # type: ignore[arg-type]
        now = time.time()
        if activate and state.current != v:
            if usable(state, state.current, launchable):
                state.previous = state.current
            state.current = v
            state.trial = None
            state.min_version_floor = release.max_version(state.min_version_floor, res.min_version)
            state.note(now, "installed", v, "by the installer")
        else:
            state.note(now, "installed_kept_current", v, f"current {state.current} is newer")
        save_state(paths.state_file, state)
    except release.ReleaseRejected as exc:
        log.error("release rejected: %s", exc)
        _emit({"ok": False, "reason": exc.reason, "detail": exc.detail})
        return EXIT_REJECTED
    except Exception as exc:  # noqa: BLE001
        log.exception("install failed")
        _emit({"ok": False, "reason": "error", "detail": str(exc)[:300]})
        return 1
    _emit({"ok": True, "version": v, "current": state.current, "reused": res.reused})
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    paths = _paths(args)
    try:
        key = release.load_public_key(Path(args.key) if args.key else paths.release_pubkey)
    except release.ReleaseRejected as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(release.fingerprint(key))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    paths = _paths(args)
    state = load_state(paths.state_file)
    if args.python_path:
        if not state.current or not release.is_installed(paths, state.current):
            print("no current version installed", file=sys.stderr)
            return 1
        print(venv_python(paths.version_dir(state.current) / "venv"))
        return 0
    data = state.to_json()
    data["installed"] = sorted(
        (e.name for e in paths.versions_dir.iterdir() if release.is_version(e.name)),
        key=release.parse_version) if paths.versions_dir.exists() else []
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    paths = _paths(args)
    _setup_logging(paths, to_file=True)
    launcher = Launcher(paths, launcher_script=Path(args.launcher_script or sys.argv[0]).resolve())
    install_signal_handlers(launcher)
    log.info("BDR agent launcher starting (home %s)", paths.home)
    return launcher.run_forever()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="launcher.py", description="BDR agent launcher")
    p.add_argument("--home", help="data folder (default C:\\ProgramData\\BDRAgent)")
    p.add_argument("--launcher-dir", help=argparse.SUPPRESS)
    p.add_argument("--launcher-script", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the worker (service entry)")
    for name in ("stage", "install-bundle"):
        s = sub.add_parser(name)
        s.add_argument("--zip", required=True)
        s.add_argument("--release-json", required=True)
    f = sub.add_parser("fingerprint")
    f.add_argument("--key")
    st = sub.add_parser("status")
    st.add_argument("--python-path", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"run": cmd_run, "stage": cmd_stage, "install-bundle": cmd_install_bundle,
                "fingerprint": cmd_fingerprint, "status": cmd_status}
    return handlers[args.cmd](args)

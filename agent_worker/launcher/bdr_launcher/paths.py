"""Launcher folder layout (stdlib only).

    C:\\ProgramData\\BDRAgent\\                 home (BDR_AGENT_HOME overrides)
      launcher\\                                admin-owned, read-only to bdr-agent
        launcher.py, bdr_launcher\\, release_key.pub (the baked-in public key)
      python\\                                  the Python 3.12 runtime (admin-owned)
      service\\BDRAgent.exe, BDRAgent.xml       WinSW (admin-owned)
      versions\\<x.y.z>\\                        side-by-side worker versions
        app\\bdr_agent\\  venv\\  bundle.json  INSTALLED.json (written last)
      state\\
        launcher.json (+ .bak)                 current / previous / trial / bad_versions / floor
        pending.txt                            version staged by the updater, tried next
        health\\                                per-launch health markers
      ms-playwright\\                           Playwright's Chromium
      worker.json, token.bin, release_key.pub, spool\\, tmp\\, logs\\   (the worker's own files)
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "BDR_AGENT_HOME"


def default_home() -> Path:
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "BDRAgent"
    return Path.home() / ".bdr-agent"


@dataclass(frozen=True)
class LauncherPaths:
    home: Path
    launcher_dir: Path

    @classmethod
    def for_home(cls, home: Path, launcher_dir: Path | None = None) -> "LauncherPaths":
        home = Path(home)
        return cls(home=home, launcher_dir=Path(launcher_dir) if launcher_dir else home / "launcher")

    @property
    def release_pubkey(self) -> Path:
        return self.launcher_dir / "release_key.pub"

    @property
    def versions_dir(self) -> Path:
        return self.home / "versions"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "launcher.json"

    @property
    def pending_file(self) -> Path:
        return self.state_dir / "pending.txt"

    @property
    def health_dir(self) -> Path:
        return self.state_dir / "health"

    @property
    def browsers_dir(self) -> Path:
        return self.home / "ms-playwright"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.home / "tmp"

    def version_dir(self, version: str) -> Path:
        return self.versions_dir / version

    def ensure(self) -> None:
        for d in (self.versions_dir, self.state_dir, self.health_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"

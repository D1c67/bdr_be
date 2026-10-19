"""Shared helpers for the W5 tests: a throwaway release key, signed manifests,
real bundle zips (tools/build_release_bundle.py) and a fake command runner
that imitates venv/pip/playwright without touching the network."""

import base64
import hashlib
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bdr_agent.protocol import canonical_manifest_bytes
from bdr_launcher.paths import LauncherPaths

CREATED_AT = "2026-10-18T20:00:00Z"


class ReleaseKey:
    def __init__(self) -> None:
        self.priv = Ed25519PrivateKey.generate()
        self.raw = self.priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def b64(self) -> str:
        return base64.b64encode(self.raw).decode()

    def pem(self) -> bytes:
        return self.priv.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.b64() + "\n")
        return path

    def sign(self, manifest: dict[str, Any]) -> str:
        return base64.b64encode(self.priv.sign(canonical_manifest_bytes(manifest))).decode()


def manifest(version: str, sha: str = "ab" * 32, min_version: str = "1.0.0",
             created_at: str = CREATED_AT) -> dict[str, Any]:
    return {"version": version, "sha256": sha, "min_version": min_version, "created_at": created_at}


def fake_package(root: Path, version: str) -> Path:
    pkg = root / f"pkg-{version}" / "bdr_agent"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'"""fake"""\n\n__version__ = "{version}"\n')
    (pkg / "__main__.py").write_text("print('fake worker')\n")
    (pkg / "__pycache__").mkdir(exist_ok=True)
    (pkg / "__pycache__" / "junk.pyc").write_bytes(b"x")
    return pkg


def fake_wheels(root: Path) -> Path:
    wheels = root / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    (wheels / "httpx-0.28.1-py3-none-any.whl").write_bytes(b"httpx wheel")
    (wheels / "pydantic_core-2.41.5-cp312-cp312-win_amd64.whl").write_bytes(b"core wheel")
    return wheels


def build_signed_bundle(tmp: Path, key: ReleaseKey, version: str, *, min_version: str = "1.0.0",
                        chromium: bool = True) -> tuple[Path, dict[str, Any], str]:
    from build_release_bundle import build_bundle

    out = build_bundle(version, tmp / "dist", fake_wheels(tmp), package=fake_package(tmp, version),
                       chromium=chromium)
    m = manifest(version, out["sha256"], min_version)
    return Path(out["path"]), m, key.sign(m)


def launcher_paths(tmp: Path, key: ReleaseKey | None = None) -> LauncherPaths:
    paths = LauncherPaths.for_home(tmp / "home")
    paths.ensure()
    if key is not None:
        key.write(paths.release_pubkey)
    return paths


class FakeInstall:
    """CommandRunner for stage_bundle: creates the venv layout on `-m venv`,
    succeeds or fails per step, records every command."""

    def __init__(self, *, fail: str | None = None):
        self.fail = fail
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, cmd: list[str], env: dict[str, str], timeout: float) -> int:
        self.calls.append(cmd)
        self.envs.append(env)
        step = self.step_of(cmd)
        if step == self.fail:
            return 1
        if step == "venv":
            venv = Path(cmd[-1])
            if sys.platform == "win32":
                (venv / "Scripts").mkdir(parents=True)
                (venv / "Scripts" / "python.exe").write_text("")
                (venv / "Lib" / "site-packages").mkdir(parents=True)
            else:
                (venv / "bin").mkdir(parents=True)
                (venv / "bin" / "python").write_text("")
                (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
        return 0

    @staticmethod
    def step_of(cmd: list[str]) -> str:
        joined = " ".join(cmd)
        if "-m venv" in joined:
            return "venv"
        if "-m pip install" in joined:
            return "pip"
        if "-m playwright install" in joined:
            return "chromium"
        if "bdr_agent.__version__" in joined:
            return "smoke"
        return "other"

    def steps(self) -> list[str]:
        return [self.step_of(c) for c in self.calls]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

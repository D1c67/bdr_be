"""Build the worker release bundle zip (the thing the release key signs).

    python tools/build_release_bundle.py --version 1.0.1 --out dist/
    python tools/build_release_bundle.py --version 1.0.1 --out dist/ --wheels-dir prebuilt/

Without --wheels-dir it runs `pip download` for win_amd64 / CPython 3.12 from
pyproject.toml's exact pins (and their dependencies), so the PC installs
offline with --require-hashes and never resolves anything itself.

Layout (see launcher/bdr_launcher/release.py):
    bundle.json, requirements.lock, wheels/*.whl, app/bdr_agent/...

The zip is deterministic (sorted entries, fixed timestamps), so the same
inputs give the same sha256. It prints one JSON line {version, sha256, path,
size}. Signing and upload are the publish script's job
(bdr_be/scripts/publish_agent_worker_release.py): it signs
{version, sha256, min_version, created_at} with the offline key.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "bdr_agent"
FIXED_DATE = (1980, 1, 1, 0, 0, 0)
WHEEL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.]+?)-(?P<ver>[^-]+)-.+\.whl$")
PY_TARGET = "3.12"


def package_version(package: Path = PACKAGE) -> str:
    text = (package / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("bdr_agent/__init__.py has no __version__")
    return m.group(1)


def pinned_requirements(pyproject: Path = ROOT / "pyproject.toml") -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = list(data["project"]["dependencies"])
    for d in deps:
        if "==" not in d:
            raise SystemExit(f"dependency is not pinned exactly: {d}")
    return deps


def download_wheels(dest: Path, requirements: list[str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "--dest", str(dest), "--only-binary=:all:",
           "--platform", "win_amd64", "--python-version", PY_TARGET, "--implementation", "cp",
           "--abi", "cp312", *requirements]
    subprocess.run(cmd, check=True)


def lock_lines(wheels: Path) -> list[str]:
    lines = []
    seen: dict[str, str] = {}
    for whl in sorted(wheels.glob("*.whl")):
        m = WHEEL_RE.match(whl.name)
        if not m:
            raise SystemExit(f"unrecognized wheel name: {whl.name}")
        name = m.group("name").replace("_", "-").lower()
        if name in seen:
            raise SystemExit(f"two wheels for {name}: {seen[name]} and {whl.name}")
        seen[name] = whl.name
        digest = hashlib.sha256(whl.read_bytes()).hexdigest()
        lines.append(f"{name}=={m.group('ver')} --hash=sha256:{digest}")
    if not lines:
        raise SystemExit("no wheels")
    return lines


def _add_tree(zf: zipfile.ZipFile, src: Path, arc_prefix: str) -> None:
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if "__pycache__" in rel.parts or path.suffix in (".pyc", ".pyo") or path.is_dir():
            continue
        _add_bytes(zf, f"{arc_prefix}/{rel.as_posix()}", path.read_bytes())


def _add_bytes(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE)
    info.external_attr = 0o644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def build_bundle(version: str, out_dir: Path, wheels_dir: Path, *, package: Path = PACKAGE,
                 chromium: bool = True) -> dict[str, Any]:
    if package_version(package) != version:
        raise SystemExit(f"bdr_agent.__version__ is {package_version(package)}, not {version}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"bdr-agent-{version}.zip"
    meta = {"format": 1, "version": version, "python": PY_TARGET, "chromium": chromium}
    lock = "\n".join(lock_lines(wheels_dir)) + "\n"
    tmp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(tmp, "w") as zf:
        _add_tree(zf, package, "app/bdr_agent")
        _add_bytes(zf, "bundle.json", (json.dumps(meta, sort_keys=True, indent=2) + "\n").encode())
        _add_bytes(zf, "requirements.lock", lock.encode())
        for whl in sorted(wheels_dir.glob("*.whl")):
            _add_bytes(zf, f"wheels/{whl.name}", whl.read_bytes())
    tmp.replace(out)
    data = out.read_bytes()
    return {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "path": str(out),
            "size": len(data)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--wheels-dir", help="use these wheels instead of pip download")
    p.add_argument("--no-chromium", action="store_true", help="skip playwright install at stage time")
    args = p.parse_args(argv)
    if args.wheels_dir:
        result = build_bundle(args.version, Path(args.out), Path(args.wheels_dir), chromium=not args.no_chromium)
    else:
        with tempfile.TemporaryDirectory() as d:
            wheels = Path(d) / "wheels"
            download_wheels(wheels, pinned_requirements())
            result = build_bundle(args.version, Path(args.out), wheels, chromium=not args.no_chromium)
            shutil.rmtree(wheels, ignore_errors=True)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

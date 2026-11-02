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
from email.parser import BytesParser
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
    try:
        import importlib.util
        if importlib.util.find_spec("pip") is None:
            raise SystemExit("Build requires pip: run with uv run --with pip --with packaging.")
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        raise SystemExit("Windows dependency download failed; no bundle was built.") from None


def validate_wheels(wheels: Path, requirements: list[str]) -> None:
    """Validate the complete dependency closure for the actual Windows target.

    Supplying --wheels-dir must not silently omit cryptography (or a transitive
    dependency), accept a Mac wheel, or replace an exact pyproject pin.
    Packaging is a build-time dependency, never shipped to the worker for this.
    """
    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
        from packaging.tags import compatible_tags, cpython_tags
        from packaging.utils import canonicalize_name, parse_wheel_filename
        from packaging.version import Version
    except ImportError:
        raise SystemExit("Build requires packaging: run with uv run --with pip --with packaging.") from None
    target = set(cpython_tags((3, 12), platforms=["win_amd64"]))
    target.update(compatible_tags((3, 12), interpreter="cp312", platforms=["win_amd64"]))
    packages = {}
    for path in sorted(wheels.glob("*.whl")):
        name, version, _, tags = parse_wheel_filename(path.name)
        if not target.intersection(tags):
            raise SystemExit(f"Wheel is incompatible with Windows CPython 3.12: {path.name}")
        if name in packages:
            raise SystemExit(f"Duplicate wheel distribution: {name}")
        try:
            with zipfile.ZipFile(path) as zf:
                members = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
                if len(members) != 1:
                    raise ValueError("metadata")
                meta = BytesParser().parsebytes(zf.read(members[0]))
                if canonicalize_name(meta["Name"] or "") != name or Version(meta["Version"]) != version:
                    raise ValueError("identity")
                if meta.get("Requires-Python") and Version("3.12.10") not in SpecifierSet(meta["Requires-Python"]):
                    raise ValueError("python")
        except (ValueError, TypeError, KeyError, zipfile.BadZipFile):
            raise SystemExit(f"Invalid wheel metadata: {path.name}") from None
        packages[name] = (version, meta.get_all("Requires-Dist", []))
    env = default_environment()
    env.update(os_name="nt", sys_platform="win32", platform_system="Windows", platform_machine="AMD64",
               python_version="3.12", python_full_version="3.12.10", implementation_name="cpython",
               implementation_version="3.12.10", platform_python_implementation="CPython")
    pending = [Requirement(value) for value in requirements]
    visited = set()
    checked = set()
    while pending:
        req = pending.pop()
        name = canonicalize_name(req.name)
        if req.url or name not in packages or packages[name][0] not in req.specifier:
            raise SystemExit(f"Missing or incorrect Windows dependency: {req}")
        visited.add(name)
        for extra in {"", *req.extras}:
            key = (name, extra)
            if key in checked:
                continue
            checked.add(key)
            for raw in packages[name][1]:
                dependency = Requirement(raw)
                if dependency.marker is None or dependency.marker.evaluate({**env, "extra": extra}):
                    pending.append(dependency)
    if set(packages) != visited:
        raise SystemExit("Unexpected wheels outside dependency closure: " + ", ".join(sorted(set(packages)-visited)))


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
    if package.resolve() == PACKAGE.resolve():
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        if declared != version:
            raise SystemExit(f"pyproject version is {declared}, not {version}")
        validate_wheels(wheels_dir, pinned_requirements())
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
    p.add_argument("--check-wheels", action="store_true", help="validate supplied Windows wheels without building")
    p.add_argument("--no-chromium", action="store_true", help="skip playwright install at stage time")
    args = p.parse_args(argv)
    if args.check_wheels:
        if not args.wheels_dir:
            p.error("--check-wheels requires --wheels-dir")
        validate_wheels(Path(args.wheels_dir), pinned_requirements())
        print(json.dumps({"validated": True, "target": "cp312-win_amd64"}))
        return 0
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

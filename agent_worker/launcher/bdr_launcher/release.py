"""Release checks and side-by-side version installs (stdlib only; spec 8, O3).

Trust: the Ed25519 release public key in the admin-owned launcher folder is
the only root. A bundle is installed only when
  - the manifest signature verifies under that key,
  - the zip's sha256 equals the signed manifest's sha256,
  - its version is not below the signed manifest's own `min_version`,
  - its version is not below the local floor (the highest `min_version` of any
    version that ever ran healthy here, and the embedded floor), and
  - its version is not in `bad_versions`.
BDR can choose which signed version a worker runs; it cannot make a worker
run unsigned code or go back below the signed floor.

Bundle zip layout (built by tools/build_release_bundle.py):
    bundle.json          {"format": 1, "version": "x.y.z", "python": "3.12", "chromium": true}
    requirements.lock    pinned requirements, every line with --hash=sha256:...
    wheels/*.whl         every dependency for win_amd64 / cp312 (installed offline)
    app/bdr_agent/...    the worker package source

Install of version V (into versions/V.partial-<pid>, renamed at the end):
    runtime -m venv venv
    venv python -m pip install --no-index --find-links wheels --require-hashes
        --no-deps --only-binary :all: -r requirements.lock
    a relative .pth in the venv's site-packages that points at app/
    venv python -m playwright install chromium   (PLAYWRIGHT_BROWSERS_PATH=<home>/ms-playwright)
    smoke: venv python -I -c "import bdr_agent" reports version V
    INSTALLED.json written last: a version folder without it is incomplete.
"""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import atomic, ed25519
from .paths import LauncherPaths, venv_python

log = logging.getLogger("bdr_launcher.release")

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Never run anything older than this, whatever BDR or a manifest says.
EMBEDDED_MIN_VERSION = "1.0.0"
BUNDLE_FORMAT = 1
MANIFEST_KEYS = ("version", "sha256", "min_version", "created_at")
INSTALLED_FILE = "INSTALLED.json"
PTH_NAME = "bdr_agent_app.pth"

MAX_BUNDLE_BYTES = 400 * 1024 * 1024
MAX_EXTRACT_BYTES = 1536 * 1024 * 1024
MAX_EXTRACT_FILES = 20000

_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


class ReleaseRejected(ValueError):
    """A release failed a check. `reason` is a stable short code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# Versions ------------------------------------------------------------------------


def parse_version(v: str) -> tuple[int, int, int]:
    if not isinstance(v, str) or not SEMVER_RE.match(v):
        raise ValueError(f"not a version: {v!r}")
    a, b, c = (int(x) for x in v.split("."))
    return (a, b, c)


def is_version(v: Any) -> bool:
    return isinstance(v, str) and bool(SEMVER_RE.match(v))


def version_lt(a: str, b: str) -> bool:
    return parse_version(a) < parse_version(b)


def max_version(*versions: str | None) -> str:
    vs = [v for v in versions if is_version(v)]
    if not vs:
        return EMBEDDED_MIN_VERSION
    return max(vs, key=parse_version)  # type: ignore[arg-type]


# Keys ---------------------------------------------------------------------------------


def load_public_key(path: Path) -> bytes:
    """32 raw Ed25519 public key bytes from base64 raw or a PEM SPKI block."""
    try:
        raw = Path(path).read_bytes().strip()
    except FileNotFoundError as exc:
        raise ReleaseRejected("no_release_key", str(path)) from exc
    if raw.startswith(b"-----BEGIN"):
        body = b"".join(line.strip() for line in raw.splitlines() if not line.startswith(b"-----"))
        try:
            der = base64.b64decode(body, validate=True)
        except ValueError as exc:
            raise ReleaseRejected("bad_release_key", "PEM body is not base64") from exc
        if len(der) != 44 or not der.startswith(_ED25519_SPKI_PREFIX):
            raise ReleaseRejected("bad_release_key", "PEM is not an Ed25519 public key")
        return der[12:]
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise ReleaseRejected("bad_release_key", "not base64") from exc
    if len(data) != 32:
        raise ReleaseRejected("bad_release_key", "must be 32 raw Ed25519 bytes")
    return data


def fingerprint(key: bytes) -> str:
    """SHA-256 of the raw key, upper hex in groups of four (same format as
    bdr_agent.release_key.fingerprint and the installer)."""
    digest = hashlib.sha256(key).hexdigest().upper()
    return " ".join(digest[i:i + 4] for i in range(0, len(digest), 4))


# Manifests ------------------------------------------------------------------------------


def _created_at_spellings(value: Any) -> list[Any]:
    """The same instant as written by the publisher, by pydantic v2 ('Z',
    microseconds only when non-zero) and by datetime.isoformat ('+00:00').
    The worker only holds a pydantic-parsed manifest, so a signature over any
    of these spellings of the signed instant is accepted."""
    out = [value]
    if not isinstance(value, str):
        return out
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return out
    if dt.tzinfo is None:
        return out
    iso = dt.isoformat()
    if dt.utcoffset() is not None and dt.utcoffset().total_seconds() == 0:  # type: ignore[union-attr]
        z = iso[: -len("+00:00")] + "Z"
        out += [z, iso]
    else:
        out.append(iso)
    return list(dict.fromkeys(out))


def canonical_manifest_bytes(manifest: dict[str, Any], *, created_at: Any = None) -> bytes:
    """Mirror of protocol.canonical_manifest_bytes (a drift test pins them):
    the four keys, sorted, no spaces, UTF-8."""
    data = {k: manifest[k] for k in MANIFEST_KEYS}
    if created_at is not None:
        data["created_at"] = created_at
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def signature_valid(key: bytes, manifest: dict[str, Any], signature_b64: str) -> bool:
    try:
        sig = base64.b64decode(signature_b64 or "", validate=True)
    except ValueError:
        return False
    try:
        spellings = _created_at_spellings(manifest["created_at"])
        candidates = [canonical_manifest_bytes(manifest, created_at=c) for c in spellings]
    except (KeyError, TypeError):
        return False
    return any(ed25519.verify(key, c, sig) for c in candidates)


def check_release(manifest: Any, signature_b64: str, key: bytes, *, expected_version: str | None = None,
                  floor: str | None = None, bad_versions: Iterable[str] = ()) -> dict[str, Any]:
    """Every check except the zip hash. Returns the manifest (4 keys) or raises."""
    if not isinstance(manifest, dict) or any(k not in manifest for k in MANIFEST_KEYS):
        raise ReleaseRejected("manifest_invalid", "missing keys")
    version, sha, min_v = manifest["version"], manifest["sha256"], manifest["min_version"]
    if not (is_version(version) and is_version(min_v) and isinstance(sha, str) and SHA256_RE.match(sha)):
        raise ReleaseRejected("manifest_invalid", "bad version or sha256")
    if not signature_valid(key, manifest, signature_b64):
        raise ReleaseRejected("bad_signature", version)
    if expected_version is not None and version != expected_version:
        raise ReleaseRejected("version_mismatch", f"{version} != {expected_version}")
    if version_lt(version, min_v):
        raise ReleaseRejected("below_min_version", f"{version} < signed min_version {min_v}")
    effective_floor = max_version(EMBEDDED_MIN_VERSION, floor)
    if version_lt(version, effective_floor):
        raise ReleaseRejected("below_floor", f"{version} < local floor {effective_floor}")
    if version in set(bad_versions):
        raise ReleaseRejected("bad_version", version)
    return {k: manifest[k] for k in MANIFEST_KEYS}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_release_json(path: Path) -> tuple[dict[str, Any], str]:
    """{"manifest": {...}, "signature": "..."} (extra keys such as url ignored)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseRejected("manifest_invalid", f"cannot read {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("manifest"), dict) \
            or not isinstance(data.get("signature"), str):
        raise ReleaseRejected("manifest_invalid", "expected {manifest, signature}")
    return data["manifest"], data["signature"]


# Extraction ------------------------------------------------------------------------------


def safe_extract(zip_path: Path, dest: Path, *, max_bytes: int = MAX_EXTRACT_BYTES,
                 max_files: int = MAX_EXTRACT_FILES) -> int:
    """Extract refusing absolute paths, '..', drive letters, symlinks and
    oversized archives. Returns the number of files written."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    written = 0
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ReleaseRejected("bundle_invalid", "not a zip") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise ReleaseRejected("bundle_invalid", "too many files")
        if sum(i.file_size for i in infos) > max_bytes:
            raise ReleaseRejected("bundle_invalid", "too large when extracted")
        for info in infos:
            name = info.filename
            pure = PurePosixPath(name.replace("\\", "/"))
            if name.startswith(("/", "\\")) or ":" in name or ".." in pure.parts or not pure.parts:
                raise ReleaseRejected("bundle_invalid", f"unsafe path {name!r}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ReleaseRejected("bundle_invalid", f"symlink {name!r}")
            target = (root / Path(*pure.parts)).resolve()
            if root != target and root not in target.parents:
                raise ReleaseRejected("bundle_invalid", f"escapes the folder: {name!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            written += 1
    return written


# Installed versions -------------------------------------------------------------------------


def read_installed(vdir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(vdir) / INSTALLED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and is_version(data.get("version")) else None


def is_installed(paths: LauncherPaths, version: str) -> bool:
    info = read_installed(paths.version_dir(version))
    return bool(info and info.get("version") == version and venv_python(paths.version_dir(version) / "venv").exists())


def installed_min_version(paths: LauncherPaths, version: str) -> str | None:
    info = read_installed(paths.version_dir(version)) or {}
    mv = (info.get("manifest") or {}).get("min_version")
    return mv if is_version(mv) else None


def _rmtree(path: Path) -> None:
    def _onerror(func: Callable[..., Any], p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if Path(path).exists():
        shutil.rmtree(path, onexc=_onerror)


# Commands --------------------------------------------------------------------------------------

# run(cmd, env, timeout) -> exit code. Output goes to the launcher log.
CommandRunner = Callable[[list[str], dict[str, str], float], int]


def subprocess_runner(cmd: list[str], env: dict[str, str], timeout: float) -> int:
    log.info("run: %s", " ".join(_quote_for_log(c) for c in cmd))
    try:
        proc = subprocess.run(cmd, env=env, timeout=timeout, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        log.error("timed out after %ss", timeout)
        return 124
    except OSError as exc:
        log.error("could not start: %s", exc)
        return 127
    for line in (proc.stdout or "").splitlines()[-40:]:
        log.info("  %s", line)
    for line in (proc.stderr or "").splitlines()[-40:]:
        log.info("  ! %s", line)
    return proc.returncode


def _quote_for_log(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _child_env(paths: LauncherPaths) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PIP_"))}
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(paths.browsers_dir)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    return env


@dataclass(frozen=True)
class StageResult:
    version: str
    min_version: str
    sha256: str
    reused: bool


def _site_packages(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Lib" / "site-packages"
    found = sorted(venv.glob("lib/python3*/site-packages"))
    if not found:
        raise ReleaseRejected("install_failed", "venv has no site-packages")
    return found[0]


def _validate_bundle(root: Path, version: str) -> dict[str, Any]:
    try:
        meta = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseRejected("bundle_invalid", "bundle.json missing or unreadable") from exc
    if not isinstance(meta, dict) or meta.get("format") != BUNDLE_FORMAT:
        raise ReleaseRejected("bundle_invalid", "unknown bundle format")
    if meta.get("version") != version:
        raise ReleaseRejected("bundle_invalid", f"bundle.json version {meta.get('version')!r} != {version}")
    if not (root / "app" / "bdr_agent" / "__init__.py").is_file():
        raise ReleaseRejected("bundle_invalid", "app/bdr_agent missing")
    lock = root / "requirements.lock"
    if not lock.is_file():
        raise ReleaseRejected("bundle_invalid", "requirements.lock missing")
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "--hash=sha256:" not in line:
            raise ReleaseRejected("bundle_invalid", f"unhashed requirement: {line[:60]}")
    if not (root / "wheels").is_dir():
        raise ReleaseRejected("bundle_invalid", "wheels/ missing")
    return meta


def stage_bundle(paths: LauncherPaths, zip_path: Path, manifest: dict[str, Any], signature: str, *,
                 runtime: str, floor: str | None, bad_versions: Iterable[str],
                 run: CommandRunner | None = None,
                 timeouts: tuple[float, float, float, float] = (300, 1200, 1800, 120)) -> StageResult:
    """Verify a downloaded bundle and install it side by side. Does not touch
    which version runs; the caller writes pending.txt or the launcher state."""
    run = run or subprocess_runner
    key = load_public_key(paths.release_pubkey)
    m = check_release(manifest, signature, key, floor=floor, bad_versions=bad_versions)
    version = m["version"]
    zip_path = Path(zip_path)
    try:
        size = zip_path.stat().st_size
    except OSError as exc:
        raise ReleaseRejected("bundle_invalid", "zip not found") from exc
    if size > MAX_BUNDLE_BYTES:
        raise ReleaseRejected("bundle_invalid", "zip too large")
    sha = sha256_file(zip_path)
    if sha != m["sha256"]:
        raise ReleaseRejected("sha_mismatch", f"{sha[:12]} != {m['sha256'][:12]}")

    paths.ensure()
    vdir = paths.version_dir(version)
    existing = read_installed(vdir)
    if existing and existing.get("sha256") == sha and is_installed(paths, version):
        log.info("version %s already installed", version)
        return StageResult(version, m["min_version"], sha, reused=True)

    partial = paths.versions_dir / f"{version}.partial-{os.getpid()}"
    _rmtree(partial)
    try:
        safe_extract(zip_path, partial)
        meta = _validate_bundle(partial, version)
        env = _child_env(paths)
        venv = partial / "venv"
        t_venv, t_pip, t_chromium, t_smoke = timeouts
        if run([runtime, "-m", "venv", str(venv)], env, t_venv) != 0:
            raise ReleaseRejected("install_failed", "venv creation failed")
        py = str(venv_python(venv))
        pip = [py, "-m", "pip", "install", "--no-index", "--find-links", str(partial / "wheels"),
               "--require-hashes", "--no-deps", "--only-binary", ":all:", "--no-cache-dir",
               "-r", str(partial / "requirements.lock")]
        if run(pip, env, t_pip) != 0:
            raise ReleaseRejected("install_failed", "pip install failed")
        site = _site_packages(venv)
        rel = os.path.relpath(partial / "app", site)
        (site / PTH_NAME).write_text(rel + "\n", encoding="utf-8")
        if meta.get("chromium", True):
            paths.browsers_dir.mkdir(parents=True, exist_ok=True)
            if run([py, "-m", "playwright", "install", "chromium"], env, t_chromium) != 0:
                raise ReleaseRejected("install_failed", "playwright install chromium failed")
        smoke = [py, "-I", "-c",
                 "import sys, bdr_agent; sys.exit(0 if bdr_agent.__version__ == sys.argv[1] else 7)", version]
        if run(smoke, env, t_smoke) != 0:
            raise ReleaseRejected("install_failed", "the installed package does not report its version")
        installed = {"version": version, "sha256": sha, "manifest": m, "signature": signature,
                     "installed_at": datetime.now(UTC).isoformat(), "bundle": meta}
        atomic.write_json(partial / INSTALLED_FILE, installed)
        if vdir.exists():
            _rmtree(vdir)
        os.replace(partial, vdir)
    except BaseException:
        _rmtree(partial)
        raise
    log.info("version %s installed side by side", version)
    return StageResult(version, m["min_version"], sha, reused=False)


def remove_partials(paths: LauncherPaths) -> int:
    removed = 0
    try:
        entries = list(paths.versions_dir.iterdir())
    except FileNotFoundError:
        return 0
    for entry in entries:
        if ".partial-" in entry.name or (entry.is_dir() and is_version(entry.name)
                                         and read_installed(entry) is None):
            _rmtree(entry)
            removed += 1
    return removed


def prune_versions(paths: LauncherPaths, keep: Iterable[str], *, max_keep: int = 3) -> list[str]:
    """Delete installed versions not in `keep`, newest kept first, so at most
    `max_keep` remain. Returns the removed versions."""
    keep_set = {v for v in keep if v}
    try:
        installed = [e.name for e in paths.versions_dir.iterdir() if e.is_dir() and is_version(e.name)]
    except FileNotFoundError:
        return []
    others = sorted((v for v in installed if v not in keep_set), key=parse_version, reverse=True)
    room = max(0, max_keep - len(keep_set & set(installed)))
    removed = []
    for v in others[room:]:
        _rmtree(paths.version_dir(v))
        removed.append(v)
    return removed



# Stable runtime for the user pool (docs/ASSISTANT_BROWSER_V2_PLAN.md 10.1 layer 2) ----------
#
# Windows Firewall rules name program paths, and the paths of a version change
# on every release (versions/<v>/venv) and every Playwright bump
# (ms-playwright/chromium-<revision>). So on each version switch, before the
# worker starts, the launcher copies the Playwright driver's node.exe and the
# full Chromium of that version into fixed folders:
#
#     <home>\runtime\current\driver\node.exe
#     <home>\runtime\current\chrome\chrome.exe   (the whole chrome-win* folder)
#     <home>\runtime\current\runtime.json        what was copied, from which version
#
# The worker starts its driver with PLAYWRIGHT_NODEJS_PATH and every per-job
# Chromium with executable_path pointing there, so the firewall rules never go
# stale. Python needs no copy: on Windows a venv python.exe is a redirector
# that starts the base runtime's python.exe (pyvenv.cfg `home`), which is the
# process the firewall sees and which does not move between releases.
# Only the user pool does this (its home is BDRAgentUser, or a runtime folder
# exists); the org pool never pays for the copy.

RUNTIME_MANIFEST = "runtime.json"
RUNTIME_FORMAT = 1
_CHROME_DIRS = ("chrome-win64", "chrome-win", "chrome-linux64", "chrome-linux", "chrome-mac-arm64",
                "chrome-mac-x64", "chrome-mac")
_CHROME_EXES = ("chrome.exe", "chrome", "Chromium.app/Contents/MacOS/Chromium",
                "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")


def stable_runtime_dir(paths: LauncherPaths) -> Path:
    return paths.home / "runtime" / "current"


def wants_stable_runtime(paths: LauncherPaths) -> bool:
    return paths.home.name.lower() == "bdragentuser" or (paths.home / "runtime").is_dir()


def _playwright_package(vdir: Path) -> Path:
    pkg = _site_packages(vdir / "venv") / "playwright"
    if not (pkg / "driver").is_dir():
        raise ReleaseRejected("runtime_sync_failed", "playwright driver missing in the version")
    return pkg


def _chromium_revision(pkg: Path) -> str:
    try:
        data = json.loads((pkg / "driver" / "package" / "browsers.json").read_text(encoding="utf-8"))
        rev = next(b["revision"] for b in data["browsers"] if b.get("name") == "chromium")
    except (OSError, ValueError, KeyError, StopIteration, TypeError) as exc:
        raise ReleaseRejected("runtime_sync_failed", "cannot read the Chromium revision") from exc
    if not isinstance(rev, str) or not rev.isdigit():
        raise ReleaseRejected("runtime_sync_failed", "bad Chromium revision")
    return rev


def _source_chrome(paths: LauncherPaths, revision: str) -> tuple[Path, str]:
    base = paths.browsers_dir / f"chromium-{revision}"
    for name in _CHROME_DIRS:
        folder = base / name
        for exe in _CHROME_EXES:
            if (folder / exe).is_file():
                return folder, exe
    raise ReleaseRejected("runtime_sync_failed", f"Chromium {revision} is not installed")


def _read_runtime_manifest(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / RUNTIME_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sync_stable_runtime(paths: LauncherPaths, version: str) -> dict[str, Any]:
    """Make <home>/runtime/current hold `version`'s driver node and Chromium.
    Call it before starting the worker, never while one runs (Windows cannot
    replace a running executable). Returns the runtime manifest; raises
    ReleaseRejected and leaves the previous runtime in place on failure."""
    vdir = paths.version_dir(version)
    pkg = _playwright_package(vdir)
    node_name = "node.exe" if sys.platform == "win32" else "node"
    node_src = pkg / "driver" / node_name
    if not node_src.is_file():
        raise ReleaseRejected("runtime_sync_failed", "driver node missing")
    revision = _chromium_revision(pkg)
    chrome_src, chrome_exe = _source_chrome(paths, revision)
    node_sha = sha256_file(node_src)
    chrome_sha = sha256_file(chrome_src / chrome_exe)
    current = stable_runtime_dir(paths)
    have = _read_runtime_manifest(current)
    if (have and have.get("format") == RUNTIME_FORMAT and have.get("version") == version
            and have.get("chromium_revision") == revision and have.get("node_sha256") == node_sha
            and have.get("chrome_sha256") == chrome_sha
            and (current / str(have.get("node_exe", ""))).is_file()
            and (current / str(have.get("chrome_exe", ""))).is_file()):
        return have
    root = current.parent
    root.mkdir(parents=True, exist_ok=True)
    nxt = root / f"next-{os.getpid()}"
    old = root / f"old-{os.getpid()}"
    _rmtree(nxt)
    _rmtree(old)
    manifest = {"format": RUNTIME_FORMAT, "version": version, "chromium_revision": revision,
                "node_exe": f"driver/{node_name}", "chrome_exe": f"chrome/{chrome_exe}",
                "node_sha256": node_sha, "chrome_sha256": chrome_sha,
                "synced_at": datetime.now(UTC).isoformat()}
    try:
        (nxt / "driver").mkdir(parents=True)
        shutil.copy2(node_src, nxt / "driver" / node_name)
        shutil.copytree(chrome_src, nxt / "chrome", symlinks=True)
        if sha256_file(nxt / manifest["node_exe"]) != node_sha \
                or sha256_file(nxt / manifest["chrome_exe"]) != chrome_sha:
            raise ReleaseRejected("runtime_sync_failed", "copy does not match the source")
        atomic.write_json(nxt / RUNTIME_MANIFEST, manifest)
        moved = False
        if current.exists():
            os.replace(current, old)
            moved = True
        try:
            os.replace(nxt, current)
        except OSError:
            if moved:
                os.replace(old, current)
            raise
    except OSError as exc:
        _rmtree(nxt)
        raise ReleaseRejected("runtime_sync_failed", str(exc)) from exc
    except BaseException:
        _rmtree(nxt)
        raise
    _rmtree(old)
    log.info("stable runtime now holds version %s (Chromium %s)", version, revision)
    return manifest


def sync_stable_runtime_if_wanted(paths: LauncherPaths, version: str) -> dict[str, Any] | None:
    """The launcher's pre-start hook: sync for the user pool, never fatal
    (a stale runtime only makes the worker's self-test refuse personal jobs)."""
    if not wants_stable_runtime(paths):
        return None
    try:
        return sync_stable_runtime(paths, version)
    except (ReleaseRejected, OSError) as exc:
        log.error("stable runtime sync for %s failed: %s", version, exc)
        return None

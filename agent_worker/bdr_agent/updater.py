"""Signed self-update, worker side (spec 8 "Update", O3, D16).

IT Admin picks the desired version in BDR; GET /config carries it as `release`
(version, sha256, min_version, manifest, signature). The worker:

  1. checks it before anything else (`decide`): signature under the release key
     baked in at install, manifest fields agree with the offer, the version is
     not below the signed min_version, not below the local floor (embedded and
     the launcher's), and not in bad_versions;
  2. drains (the supervisor stops claiming; running jobs finish, or are
     requeued `drain` after 10 min) while it
  3. fetches GET /releases/{version} (manifest, signature, a 900 s signed URL),
     checks that manifest again, downloads the zip with a separate client that
     never sends the worker token, and checks its sha256;
  4. hands the zip to the launcher (`launcher.py stage`), which verifies it
     again with its own stdlib Ed25519 and the admin-owned key, installs it
     side by side and writes state/pending.txt;
  5. exits with EXIT_UPDATE; the launcher starts the new version as a trial
     and rolls back on local health only.

The signed URL carries a token in its query: it is never logged.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .client import BdrClient
from .local_config import Paths
from .protocol import SEMVER_RE, ReleaseInfo, ReleaseManifest
from .release_key import ReleaseKeyError, load_public_key, verify_manifest

log = logging.getLogger("bdr_agent.updater")

EXIT_UPDATE = 5
EMBEDDED_MIN_VERSION = "1.0.0"
RETRY_AFTER_FAILURE_SECONDS = 1800.0
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
STAGE_TIMEOUT_SECONDS = 3600.0
EXIT_STAGE_REJECTED = 10

ENV_LAUNCHER = "BDR_AGENT_LAUNCHER"
ENV_RUNTIME = "BDR_AGENT_RUNTIME"
ENV_BAD_VERSIONS = "BDR_AGENT_BAD_VERSIONS"
ENV_MIN_VERSION = "BDR_AGENT_MIN_VERSION"
ENV_HEALTH_FILE = "BDR_AGENT_HEALTH_FILE"
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class UpdateError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def vtuple(v: str) -> tuple[int, int, int]:
    if not SEMVER_RE.match(v or ""):
        raise ValueError(f"not a version: {v!r}")
    a, b, c = (int(x) for x in v.split("."))
    return (a, b, c)


def max_version(*vs: str | None) -> str:
    good = [v for v in vs if v and SEMVER_RE.match(v)]
    return max(good, key=vtuple) if good else EMBEDDED_MIN_VERSION


def parse_bad_versions(text: str | None) -> list[str]:
    return [v.strip() for v in (text or "").split(",") if SEMVER_RE.match(v.strip())]


@dataclass(frozen=True)
class UpdateDecision:
    action: Literal["none", "stage", "refuse"]
    reason: str
    version: str | None = None


def offer_signature_valid(key: Ed25519PublicKey, manifest: ReleaseManifest, signature: str) -> bool:
    """verify_manifest over the pydantic form ('Z') and the isoformat form
    ('+00:00') of created_at: the publisher may have signed either spelling."""
    data = manifest.model_dump(mode="json")
    spellings = [data["created_at"]]
    if data["created_at"].endswith("Z"):
        spellings.append(data["created_at"][:-1] + "+00:00")
    return any(verify_manifest(key, {**data, "created_at": c}, signature) for c in spellings)


def decide(current: str, offer: ReleaseInfo | None, *, key: Ed25519PublicKey, bad_versions: list[str],
           floor: str | None) -> UpdateDecision:
    """Pure: should this worker move to the offered release?"""
    if offer is None:
        return UpdateDecision("none", "no_release")
    v = offer.version
    if v == current:
        return UpdateDecision("none", "current", v)
    m = offer.manifest
    if (m.version, m.sha256, m.min_version) != (offer.version, offer.sha256, offer.min_version):
        return UpdateDecision("refuse", "manifest_mismatch", v)
    if not offer_signature_valid(key, m, offer.signature):
        return UpdateDecision("refuse", "bad_signature", v)
    if vtuple(v) < vtuple(m.min_version):
        return UpdateDecision("refuse", "below_min_version", v)
    eff_floor = max_version(EMBEDDED_MIN_VERSION, floor)
    if vtuple(v) < vtuple(eff_floor):
        return UpdateDecision("refuse", "below_floor", v)
    if v in bad_versions:
        return UpdateDecision("refuse", "bad_version", v)
    return UpdateDecision("stage", "ok", v)


def _check_download_url(url: str) -> None:
    parts = urlsplit(url or "")
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return
    raise UpdateError("bad_url", "release URL must be https")


# stage(argv, timeout) -> (exit code, stdout)
StageRunner = Callable[[list[str], float], tuple[int, str]]


def subprocess_stage(argv: list[str], timeout: float) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    # Node (playwright install) does not fall back when TMP is missing: make
    # sure the inherited temp dir exists before the stage child starts.
    for var in ("TMPDIR", "TMP", "TEMP"):
        if env.get(var):
            with contextlib.suppress(OSError):
                os.makedirs(env[var], exist_ok=True)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError as exc:
        return 127, json.dumps({"ok": False, "reason": "launcher_missing", "detail": str(exc)})
    return proc.returncode, proc.stdout or ""


class Updater:
    def __init__(self, *, paths: Paths, key: Ed25519PublicKey, current: str, launcher: Path, runtime: str,
                 bad_versions: list[str] | None = None, floor: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, stage: StageRunner = subprocess_stage,
                 clock: Callable[[], float] = time.monotonic, max_bytes: int = MAX_DOWNLOAD_BYTES):
        self.paths = paths
        self.key = key
        self.current = current
        self.launcher = Path(launcher)
        self.runtime = runtime
        self.bad_versions = list(bad_versions or [])
        self.floor = max_version(EMBEDDED_MIN_VERSION, floor)
        self._transport = transport
        self._stage = stage
        self._clock = clock
        self.max_bytes = max_bytes
        self._failed: dict[str, float] = {}
        self._refused_logged: set[tuple[str, str]] = set()

    @classmethod
    def from_env(cls, paths: Paths, current: str) -> "Updater | None":
        """Only when started by the launcher (it sets these). A dev `run` has no updater."""
        launcher = os.environ.get(ENV_LAUNCHER)
        runtime = os.environ.get(ENV_RUNTIME)
        if not launcher or not runtime:
            return None
        try:
            key = load_public_key(paths.release_pubkey)
        except ReleaseKeyError as exc:
            log.error("self-update disabled: %s", exc)
            return None
        return cls(paths=paths, key=key, current=current, launcher=Path(launcher), runtime=runtime,
                   bad_versions=parse_bad_versions(os.environ.get(ENV_BAD_VERSIONS)),
                   floor=os.environ.get(ENV_MIN_VERSION))

    # Decisions ---------------------------------------------------------------------

    def check(self, offer: ReleaseInfo | None) -> UpdateDecision:
        d = decide(self.current, offer, key=self.key, bad_versions=self.bad_versions, floor=self.floor)
        if d.action == "refuse":
            if (d.version or "", d.reason) not in self._refused_logged:
                self._refused_logged.add((d.version or "", d.reason))
                log.error("refusing release %s: %s", d.version, d.reason)
            return d
        if d.action == "stage" and d.version in self._failed:
            if self._clock() - self._failed[d.version] < RETRY_AFTER_FAILURE_SECONDS:
                return UpdateDecision("none", "recently_failed", d.version)
            del self._failed[d.version]
        return d

    def mark_failed(self, version: str) -> None:
        self._failed[version] = self._clock()

    # Download + stage ----------------------------------------------------------------

    async def prepare(self, client: BdrClient, offer: ReleaseInfo) -> str:
        """Fetch, verify and stage `offer`. Returns the staged version; raises
        UpdateError. Runs while the supervisor drains."""
        version = offer.version
        resp = await client.release(version)
        m: ReleaseManifest = resp.manifest
        if m.model_dump(mode="json") != offer.manifest.model_dump(mode="json"):
            raise UpdateError("manifest_mismatch", "/releases differs from /config")
        if not offer_signature_valid(self.key, m, resp.signature):
            raise UpdateError("bad_signature", version)
        _check_download_url(resp.url)
        work = self.paths.tmp_dir / "update"
        work.mkdir(parents=True, exist_ok=True)
        zip_path = work / f"bdr-agent-{version}.zip"
        sha = await self._download(resp.url, zip_path)
        if sha != m.sha256:
            zip_path.unlink(missing_ok=True)
            raise UpdateError("sha_mismatch", f"{sha[:12]} != {m.sha256[:12]}")
        rel_json = work / f"bdr-agent-{version}.release.json"
        rel_json.write_text(json.dumps({"manifest": m.model_dump(mode="json"), "signature": resp.signature}),
                            encoding="utf-8")
        argv = [self.runtime, "-I", str(self.launcher), "--home", str(self.paths.home), "stage",
                "--zip", str(zip_path), "--release-json", str(rel_json)]
        code, out = await asyncio.to_thread(self._stage, argv, STAGE_TIMEOUT_SECONDS)
        result = _last_json_line(out)
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        if code != 0 or not result.get("ok"):
            raise UpdateError(str(result.get("reason") or f"stage_exit_{code}"), str(result.get("detail") or ""))
        log.info("release %s staged; restarting into it", version)
        return version

    async def _download(self, url: str, dest: Path) -> str:
        h = hashlib.sha256()
        total = 0
        tmp = dest.with_name(dest.name + ".part")
        # A separate client: the worker token and headers never go to storage,
        # and redirects are not followed off the signed URL.
        async with httpx.AsyncClient(transport=self._transport, follow_redirects=False,
                                     timeout=httpx.Timeout(60.0, connect=15.0)) as http:
            try:
                async with http.stream("GET", url) as r:
                    if r.status_code != 200:
                        raise UpdateError("download_failed", f"HTTP {r.status_code}")
                    with open(tmp, "wb") as fh:
                        async for chunk in r.aiter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise UpdateError("download_failed", "bundle too large")
                            h.update(chunk)
                            fh.write(chunk)
                        fh.flush()
                        os.fsync(fh.fileno())
            except httpx.HTTPError as exc:
                tmp.unlink(missing_ok=True)
                # str(exc) can include the URL; keep only the class name.
                raise UpdateError("download_failed", type(exc).__name__) from None
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        os.replace(tmp, dest)
        return h.hexdigest()


def _last_json_line(text: str) -> dict[str, Any]:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return {}


def write_health_marker() -> None:
    """Tell the launcher this process is locally healthy (config loaded,
    Chromium up). No-op when not started by the launcher."""
    target = os.environ.get(ENV_HEALTH_FILE)
    if not target:
        return
    try:
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{os.getpid()} {time.time():.0f}\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not write the health marker: %s", exc)

"""The only configuration that lives on the PC (spec 8, "The owner never edits
config on the PC").

Locally: the BDR API base URL, the worker id from enrollment, and where files
go. Everything else (slots, allowed types, enablement, caps, drain, release,
host narrowing) is pulled from GET /config and never written to disk.

Layout under the data home (Windows: C:\\ProgramData\\BDRAgent):
    worker.json        {"api_base_url", "worker_id"}  atomic write + .bak
    token.bin          worker token (DPAPI on Windows, 0600 elsewhere)
    release_key.pub    Ed25519 release public key baked in by the installer
    spool/             per-job step spools (never secrets)
    tmp/               wiped at every boot
    logs/
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .atomicfile import atomic_write_json, bak_path, read_json_with_bak
from .protocol import API_PREFIX

ENV_HOME = "BDR_AGENT_HOME"
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class LocalConfigError(ValueError):
    pass


def default_home() -> Path:
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "BDRAgent"
    return Path.home() / ".bdr-agent"


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def worker_json(self) -> Path:
        return self.home / "worker.json"

    @property
    def token_file(self) -> Path:
        return self.home / "token.bin"

    @property
    def release_pubkey(self) -> Path:
        return self.home / "release_key.pub"

    @property
    def spool_dir(self) -> Path:
        return self.home / "spool"

    @property
    def tmp_dir(self) -> Path:
        return self.home / "tmp"

    @property
    def chromium_tmp_dir(self) -> Path:
        """Chromium's temp dir (throwaway profile, driver artifacts). The browser
        provider wipes it before launch; boot hygiene must not touch it, since
        it runs after Chromium is already up."""
        return self.tmp_dir / CHROMIUM_TMP_NAME

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    def ensure(self) -> None:
        for d in (self.home, self.spool_dir, self.tmp_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


CHROMIUM_TMP_NAME = "chromium"


def normalize_base_url(url: str) -> str:
    """https://host[:port][/base] with no trailing slash and no API prefix.
    Plain http is allowed only for localhost (dev and tests)."""
    raw = (url or "").strip().rstrip("/")
    if raw.endswith(API_PREFIX):
        raw = raw[: -len(API_PREFIX)].rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise LocalConfigError("api_base_url must be an absolute https URL")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
        raise LocalConfigError("api_base_url must use https")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise LocalConfigError("api_base_url must not carry a query, fragment or credentials")
    return raw


@dataclass(frozen=True)
class LocalConfig:
    api_base_url: str
    worker_id: UUID | None = None

    @property
    def api_host(self) -> str:
        return urlsplit(self.api_base_url).hostname or ""

    def to_json(self) -> dict[str, str | None]:
        return {
            "api_base_url": self.api_base_url,
            "worker_id": str(self.worker_id) if self.worker_id else None,
        }

    @classmethod
    def from_json(cls, data: object) -> "LocalConfig":
        if not isinstance(data, dict):
            raise LocalConfigError("worker.json is not an object")
        unknown = set(data) - {"api_base_url", "worker_id"}
        if unknown:
            # Anything else on disk would be config the owner is not supposed
            # to edit on the PC; refuse rather than silently honour it.
            raise LocalConfigError(f"worker.json has unexpected keys: {sorted(unknown)}")
        wid = data.get("worker_id")
        return cls(
            api_base_url=normalize_base_url(str(data.get("api_base_url") or "")),
            worker_id=UUID(str(wid)) if wid else None,
        )


def load_local_config(paths: Paths) -> LocalConfig | None:
    data = read_json_with_bak(paths.worker_json)
    if data is None:
        return None
    try:
        return LocalConfig.from_json(data)
    except (LocalConfigError, ValueError):
        # The primary parsed as JSON but is invalid: try the .bak explicitly.
        bak = read_json_with_bak(bak_path(paths.worker_json))
        if bak is None:
            raise
        return LocalConfig.from_json(bak)


def save_local_config(paths: Paths, cfg: LocalConfig) -> None:
    atomic_write_json(paths.worker_json, cfg.to_json(), mode=0o644)

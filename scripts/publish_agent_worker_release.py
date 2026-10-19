"""Build, sign and publish one browser agent worker release (O3, D16; SPEC.md 8 "Update").

Run on the owner's Mac, where the offline Ed25519 private key lives
(scripts/agent_release_keygen.py). The private key is read locally to sign
and is never uploaded, printed or logged.

    cd bdr_be
    uv run python scripts/publish_agent_worker_release.py --version 1.0.1 --min-version 1.0.0 \\
        --key ~/.bdr/agent_release_ed25519.pem [--bundle dist/bdr-agent-1.0.1.zip] \\
        [--wheels-dir prebuilt/] [--chromium-version 141.0.7390.54] [--notes "..."] \\
        [--installer-api https://<bdr-api>] [--dry-run]

Steps:
  1. Build the worker zip with agent_worker/tools/build_release_bundle.py
     (deterministic; skipped when --bundle names a built zip).
  2. sha256 of the zip; the manifest {version, sha256, min_version,
     created_at}; Ed25519 signature over protocol.canonical_manifest_bytes.
  3. Verify the signature with the public half, and against
     BROWSER_AGENT_RELEASE_PUBKEY when the env has one (a mismatch stops:
     BDR would never advertise the release).
  4. Upload the zip to the private `browser-agent` bucket at
     releases/<version>/bdr-agent-<version>.zip and insert the
     browser_agent_worker_releases row (service role; there is no UI upload).
  5. With --installer-api: render agent_worker/install/install.ps1 with the
     API base, the public key, the launcher and the WinSW XML, leaving the
     release placeholder for BDR to fill per download, and upload it to
     installer/bdr-install-template.ps1. GET /agent-worker/v1/installer.ps1
     serves it with the desired release (and a fresh 900 s link) filled in.

Then IT Admin picks the version in Settings > Browser agents. Safety: the
target database comes from the .env (SUPABASE_URL); the production project
is refused unless --allow-prod is given on purpose.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey  # noqa: E402

from app.services.browser_agents import protocol  # noqa: E402

WORKER = ROOT / "agent_worker"
BUNDLE_TOOL = WORKER / "tools" / "build_release_bundle.py"
INSTALLER_TOOL = WORKER / "tools" / "render_installer.py"
BUCKET = "browser-agent"
INSTALLER_TEMPLATE_PATH = "installer/bdr-install-template.ps1"
RELEASE_PLACEHOLDER = "'__RELEASE_JSON_B64__'"
PROD_REF = "zxqkqotlcgbalwoqeeca"
PASSPHRASE_ENV = "BDR_RELEASE_KEY_PASSPHRASE"


class PublishError(SystemExit):
    pass


def version_tuple(v: str) -> tuple[int, ...]:
    if not protocol.SEMVER_RE.match(v or ""):
        raise PublishError(f"not a MAJOR.MINOR.PATCH version: {v!r}")
    return tuple(int(p) for p in v.split("."))


def load_private_key(path: Path, passphrase: bytes | None) -> Ed25519PrivateKey:
    data = Path(path).expanduser().read_bytes()
    try:
        key = serialization.load_pem_private_key(data, password=passphrase)
    except TypeError:
        if passphrase is not None:
            raise
        env = os.environ.get(PASSPHRASE_ENV)
        pw = env.encode() if env else getpass.getpass("Release key passphrase: ").encode()
        key = serialization.load_pem_private_key(data, password=pw)
    if not isinstance(key, Ed25519PrivateKey):
        raise PublishError("the release key is not an Ed25519 key")
    return key


def public_raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(version: str, sha256: str, min_version: str, created_at: datetime | None = None) -> dict[str, Any]:
    """The manifest exactly as BDR stores and re-serializes it (pydantic
    JSON: created_at in UTC with a Z), so the signed bytes match on both
    sides (protocol.canonical_manifest_bytes)."""
    if version_tuple(min_version) > version_tuple(version):
        raise PublishError("min_version cannot be above the version")
    created = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    return protocol.ReleaseManifest(version=version, sha256=sha256, min_version=min_version,
                                    created_at=created).model_dump(mode="json")


def sign(key: Ed25519PrivateKey, manifest: dict[str, Any]) -> str:
    return base64.b64encode(key.sign(protocol.canonical_manifest_bytes(manifest))).decode("ascii")


def verify(pub_raw: bytes, manifest: dict[str, Any], signature: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(
            base64.b64decode(signature), protocol.canonical_manifest_bytes(protocol.ReleaseManifest(**manifest)))
        return True
    except (InvalidSignature, ValueError):
        return False


def build_bundle(version: str, out_dir: Path, wheels_dir: Path | None) -> Path:
    cmd = [sys.executable, str(BUNDLE_TOOL), "--version", version, "--out", str(out_dir)]
    if wheels_dir:
        cmd += ["--wheels-dir", str(wheels_dir)]
    done = subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=str(WORKER))
    line = [ln for ln in done.stdout.splitlines() if ln.strip().startswith("{")][-1]
    return Path(json.loads(line)["path"])


def _load_tool(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def render_installer_template(api_base: str, pub_raw: bytes) -> bytes:
    """install.ps1 with every placeholder filled except the release, which
    GET /agent-worker/v1/installer.ps1 fills per download. UTF-8 with BOM
    and CRLF, like tools/render_installer.py writes it."""
    tool = _load_tool(INSTALLER_TOOL, "bdr_render_installer")
    api = tool._check_api(api_base)
    values = {
        "api": api,
        "pubkey": base64.b64encode(pub_raw).decode(),
        "launcher": base64.b64encode(tool.launcher_zip_bytes()).decode(),
        "winsw": base64.b64encode(tool.WINSW_XML.read_bytes()).decode(),
    }
    text = tool.TEMPLATE.read_text(encoding="utf-8")
    for name, token in tool.PLACEHOLDERS.items():
        if text.count(token) != 1:
            raise PublishError(f"install.ps1 must contain {token} exactly once")
        if name != "release":
            text = text.replace(token, f"'{values[name]}'")
    if text.count(RELEASE_PLACEHOLDER) != 1:
        raise PublishError("the rendered template lost its release placeholder")
    return b"\xef\xbb\xbf" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def guard_target(supabase_url: str, allow_prod: bool) -> None:
    if not supabase_url:
        raise PublishError("SUPABASE_URL is not set")
    if PROD_REF in supabase_url and not allow_prod:
        raise PublishError("refusing to publish to BDR_Prod without --allow-prod (explicit owner approval)")


def publish(sb: Any, *, version: str, min_version: str, bundle: Path, key: Ed25519PrivateKey,
            env_pubkey: str = "", chromium_version: str | None = None, notes: str | None = None,
            dry_run: bool = False, created_at: datetime | None = None) -> dict[str, Any]:
    """Sign and register one release. Returns the row (without uploading on dry_run)."""
    pub = public_raw(key)
    if env_pubkey.strip():
        configured = base64.b64decode(env_pubkey.strip() + "=" * (-len(env_pubkey.strip()) % 4))
        if configured != pub:
            raise PublishError("this key is not BROWSER_AGENT_RELEASE_PUBKEY: BDR would refuse the release")
    sha = sha256_file(bundle)
    manifest = build_manifest(version, sha, min_version, created_at)
    signature = sign(key, manifest)
    if not verify(pub, manifest, signature):
        raise PublishError("the signature does not verify")
    path = f"releases/{version}/bdr-agent-{version}.zip"
    row = {"version": version, "storage_path": path, "sha256": sha, "min_version": min_version,
           "manifest": manifest, "signature": signature, "chromium_version": chromium_version,
           "notes": (notes or None) and notes[:1000]}
    if dry_run:
        return row
    existing = sb.table("browser_agent_worker_releases").select("version").eq("version", version).limit(1).execute().data
    if existing:
        raise PublishError(f"release {version} is already published (versions are immutable)")
    sb.storage.from_(BUCKET).upload(path, bundle.read_bytes(),
                                    {"content-type": "application/zip", "upsert": "false"})
    sb.table("browser_agent_worker_releases").insert(row).execute()
    return row


def upload_installer_template(sb: Any, template: bytes) -> None:
    sb.storage.from_(BUCKET).upload(INSTALLER_TEMPLATE_PATH, template,
                                    {"content-type": "text/plain; charset=utf-8", "upsert": "true"})


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build, sign and publish a browser agent worker release.")
    p.add_argument("--version", required=True)
    p.add_argument("--min-version")
    p.add_argument("--key", type=Path, required=True, help="the offline Ed25519 private key PEM")
    p.add_argument("--bundle", type=Path, help="an already built bdr-agent-<version>.zip")
    p.add_argument("--wheels-dir", type=Path)
    p.add_argument("--chromium-version")
    p.add_argument("--notes")
    p.add_argument("--installer-api", help="also render and upload the installer template for this https API base")
    p.add_argument("--dry-run", action="store_true", help="build, sign and verify only; upload nothing")
    p.add_argument("--allow-prod", action="store_true", help="publish to BDR_Prod (needs the owner's approval)")
    args = p.parse_args(argv)
    version_tuple(args.version)
    min_version = args.min_version or args.version

    from app.core.config import get_settings
    from app.core.supabase_client import get_supabase

    settings = get_settings()
    if not args.dry_run:
        guard_target(settings.supabase_url, args.allow_prod)
    key = load_private_key(args.key, None)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = args.bundle or build_bundle(args.version, Path(tmp), args.wheels_dir)
        sb = None if args.dry_run else get_supabase()
        row = publish(sb, version=args.version, min_version=min_version, bundle=bundle, key=key,
                      env_pubkey=settings.browser_agent_release_pubkey, chromium_version=args.chromium_version,
                      notes=args.notes, dry_run=args.dry_run)
        if args.installer_api:
            template = render_installer_template(args.installer_api, public_raw(key))
            if not args.dry_run:
                upload_installer_template(sb, template)
    print(json.dumps({"version": row["version"], "sha256": row["sha256"], "min_version": row["min_version"],
                      "storage_path": row["storage_path"], "dry_run": args.dry_run}, sort_keys=True))
    if not args.dry_run:
        print("Published. Pick it in Settings > Browser agents > desired worker version.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey  # noqa: E402

from app.services.browser_agents import protocol  # noqa: E402
import httpx  # noqa: E402
from storage3.exceptions import StorageApiError  # noqa: E402

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
    # The builder runs from WORKER, so resolve CLI paths in the caller's cwd
    # before crossing that subprocess boundary.
    out_dir = out_dir.expanduser().resolve()
    cmd = [sys.executable, str(BUNDLE_TOOL), "--version", version, "--out", str(out_dir)]
    if wheels_dir:
        cmd += ["--wheels-dir", str(wheels_dir.expanduser().resolve())]
    done = subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=str(WORKER))
    line = [ln for ln in done.stdout.splitlines() if ln.strip().startswith("{")][-1]
    path = Path(json.loads(line)["path"])
    return path if path.is_absolute() else (WORKER / path).resolve()


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


def guard_target(supabase_url: str, allow_prod: bool, expected_project: str | None = None) -> None:
    if not supabase_url:
        raise PublishError("SUPABASE_URL is not set")
    if expected_project and urlsplit(supabase_url).hostname != f"{expected_project}.supabase.co":
        raise PublishError("SUPABASE_URL does not match --expected-project; no release was published")
    if PROD_REF in supabase_url and not allow_prod:
        raise PublishError("refusing to publish to BDR_Prod without --allow-prod (explicit owner approval)")


def upload_release_bundle(bucket: Any, path: str, data: bytes, expected_sha: str) -> None:
    """Recover an ambiguous upload without replacing any existing object.

    Only an explicit 404 permits retrying an upload. Unknown read failures
    stop publication; a duplicate or accepted-but-disconnected upload must
    download and match the exact signed bytes before registration.
    """
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise PublishError("Bundle changed after signing; no upload attempted")
    for attempt in range(3):
        try:
            # storage3 mutates file_options, so each attempt gets a new dict.
            bucket.upload(path, data, {"content-type": "application/zip", "upsert": "false"})
            return
        except (httpx.TransportError, StorageApiError) as exc:
            if isinstance(exc, StorageApiError) and not (
                str(exc.status) in {"409", "429", "500", "502", "503", "504"}
                or exc.code in {"Duplicate", "ResourceAlreadyExists", "KeyAlreadyExists"}
            ):
                raise PublishError("Storage refused the bundle upload; no release registered") from None
        except Exception:
            raise PublishError("Unexpected storage response during upload; no release registered. Retry the same command.") from None
        print("Upload was interrupted or already exists; checking stored bundle...", flush=True)
        try:
            remote = bucket.download(path)
        except StorageApiError as exc:
            if str(exc.status) != "404":
                raise PublishError("Could not verify stored bundle; no release registered. Retry the same command.") from None
        except httpx.TransportError:
            raise PublishError("Connection failed while checking stored bundle; no release registered. Retry the same command.") from None
        except Exception:
            raise PublishError("Unexpected storage response while checking bundle; no release registered. Retry the same command.") from None
        else:
            if len(remote) != len(data) or hashlib.sha256(remote).hexdigest() != expected_sha:
                raise PublishError("Stored bundle differs from the signed bundle; refusing to overwrite or register it")
            print("Stored bundle matches the signed SHA-256; continuing publication.", flush=True)
            return
        if attempt < 2:
            print(f"Bundle is absent; retrying upload ({attempt + 2}/3)...", flush=True)
            time.sleep(attempt + 1)
    raise PublishError("Upload connection failed after 3 attempts; no release registered. Retry the same command.")


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
    register_release(sb, row, bundle)
    return row


def register_release(sb: Any, row: dict, bundle: Path, uploader=None, *, resume: bool = False) -> dict:
    """Register only verified bytes. Resume never modifies an existing release."""
    version, path, sha = row['version'], row['storage_path'], row['sha256']
    if sha256_file(bundle) != sha:
        raise PublishError('Bundle changed after signing; no release registered')
    existing = sb.table("browser_agent_worker_releases").select("*").eq("version", version).limit(1).execute().data
    if existing:
        if resume and all(existing[0].get(k) == v for k, v in row.items()):
            verify_stored_bundle(sb.storage.from_(BUCKET), path, bundle, sha, required=True)
            return row
        raise PublishError(f"release {version} is already published (versions are immutable)")
    bucket = sb.storage.from_(BUCKET)
    if uploader is None:
        upload_release_bundle(bucket, path, bundle.read_bytes(), sha)
    else:
        if not verify_stored_bundle(bucket, path, bundle, sha, required=False):
            uploader(path, sha)
        verify_stored_bundle(bucket, path, bundle, sha, required=True)
    sb.table("browser_agent_worker_releases").insert(row).execute()
    return row


def verify_stored_bundle(bucket, path: str, bundle: Path, sha: str, *, required: bool) -> bool:
    try:
        data = bucket.download(path)
    except StorageApiError as exc:
        if str(exc.status) == '404' and not required:
            return False
        raise PublishError('Cannot verify stored release bundle; publication stopped') from None
    except Exception:
        raise PublishError('Connection failed verifying stored release bundle; signed checkpoint retained') from None
    if len(data) != bundle.stat().st_size or hashlib.sha256(data).hexdigest() != sha:
        raise PublishError('Stored bundle differs from signed bundle; refusing publication')
    return True


def cached_release(path: Path, *, pub: bytes, bundle: Path, version: str, min_version: str,
                   chromium_version: str | None, notes: str | None) -> dict:
    try:
        row = json.loads(path.read_text())
        manifest = protocol.ReleaseManifest(**row['manifest']).model_dump(mode='json')
        expected = {'version': version, 'min_version': min_version, 'sha256': sha256_file(bundle),
                    'storage_path': f'releases/{version}/bdr-agent-{version}.zip',
                    'chromium_version': chromium_version, 'notes': (notes or None) and notes[:1000]}
        if (any(row.get(k) != v for k, v in expected.items())
                or set(row) != {*expected, 'manifest', 'signature'}
                or any(manifest[k] != expected[k] for k in ('version', 'min_version', 'sha256'))
                or not verify(pub, manifest, row['signature'])):
            raise ValueError
        return row
    except Exception:
        raise PublishError('Saved signed release does not match this bundle, key or command; publication stopped') from None


def save_signed_release(path: Path, row: dict) -> None:
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.signed-release-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(row, sort_keys=True, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


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
    installer_source = p.add_mutually_exclusive_group()
    installer_source.add_argument("--installer-api", help="also render and upload the installer template for this https API base")
    installer_source.add_argument("--installer-from-settings", action="store_true",
                                  help="use BROWSER_AGENT_API_BASE_URL for the installer template")
    p.add_argument("--dry-run", action="store_true", help="build, sign and verify only; upload nothing")
    p.add_argument("--resumable", action="store_true", help="use direct-storage TUS chunks and retain signed progress")
    p.add_argument("--expected-project", help="refuse a different Supabase project before building or signing")
    p.add_argument("--output-dir", type=Path, help="retain bundle, signed release JSON and installer template locally")
    p.add_argument("--allow-prod", action="store_true", help="publish to BDR_Prod (needs the owner's approval)")
    args = p.parse_args(argv)
    if args.resumable and not args.output_dir:
        raise PublishError('--resumable requires --output-dir for recovery checkpoints')
    version_tuple(args.version)
    min_version = args.min_version or args.version

    from app.core.config import get_settings
    from app.core.supabase_client import get_supabase

    settings = get_settings()
    if not args.dry_run or args.expected_project:
        guard_target(settings.supabase_url, args.allow_prod, args.expected_project)
    installer_api = settings.browser_agent_api_base_url if args.installer_from_settings else args.installer_api
    if args.installer_from_settings and not installer_api:
        raise PublishError("BROWSER_AGENT_API_BASE_URL is required for --installer-from-settings")
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    workspace = nullcontext(args.output_dir) if args.output_dir else tempfile.TemporaryDirectory()
    with workspace as tmp:
        print("Preparing Windows worker bundle...", flush=True)
        bundle = args.bundle or build_bundle(args.version, Path(tmp), args.wheels_dir)
        bundle = bundle.expanduser().resolve()
        if not bundle.is_file():
            raise PublishError("Worker bundle was not found; no signing key was opened or release published")
        checkpoint = args.output_dir / f'release-{args.version}.json' if args.output_dir else None
        if args.resumable and checkpoint.exists():
            try:
                encoded_pub = settings.browser_agent_release_pubkey.strip()
                pub = base64.b64decode(encoded_pub + '=' * (-len(encoded_pub) % 4), validate=True)
            except Exception:
                raise PublishError('Configured release public key is required for resume') from None
            row = cached_release(checkpoint, pub=pub, bundle=bundle, version=args.version,
                                 min_version=min_version, chromium_version=args.chromium_version, notes=args.notes)
            print('Verified saved signature; resuming without opening the release key.', flush=True)
        else:
            print("Bundle ready. Unlock the release key locally to sign it.", flush=True)
            key = load_private_key(args.key, None)
            pub = public_raw(key)
            row = publish(None, version=args.version, min_version=min_version, bundle=bundle, key=key,
                          env_pubkey=settings.browser_agent_release_pubkey, chromium_version=args.chromium_version,
                          notes=args.notes, dry_run=True)
            if checkpoint:
                save_signed_release(checkpoint, row)
        template = render_installer_template(installer_api, pub) if installer_api else None
        sb = None if args.dry_run else get_supabase()
        if not args.dry_run:
            uploader = None
            if args.resumable:
                from scripts.agent_release_upload import UploadError, upload_resumable

                def uploader(path, sha):
                    try:
                        upload_resumable(supabase_url=settings.supabase_url,
                                         token=settings.supabase_service_role_key, bucket=BUCKET,
                                         object_name=path, bundle=bundle, expected_sha=sha,
                                         state_path=args.output_dir / 'upload-state.json',
                                         progress=lambda done, total: print(f'Uploaded {done}/{total} bytes ({done * 100 // total}%)', flush=True))
                    except UploadError as exc:
                        # A final chunk can commit even if its acknowledgement
                        # or the TUS completion HEAD is lost. Only exact bytes
                        # permit continuing; otherwise preserve the checkpoint.
                        if verify_stored_bundle(sb.storage.from_(BUCKET), path, bundle, sha, required=False):
                            print('Completed object verified after interrupted acknowledgement.', flush=True)
                            return
                        raise PublishError(str(exc)) from None
            register_release(sb, row, bundle, uploader, resume=args.resumable)
        if template is not None:
            if args.output_dir:
                (args.output_dir / "bdr-install-template.ps1").write_bytes(template)
            if not args.dry_run:
                upload_installer_template(sb, template)
    print(json.dumps({"version": row["version"], "sha256": row["sha256"], "min_version": row["min_version"],
                      "storage_path": row["storage_path"], "dry_run": args.dry_run}, sort_keys=True))
    if not args.dry_run:
        print("Published. The selected worker version is unchanged; activation is a separate step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

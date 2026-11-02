"""Prepare the development-only personal worker install, then keep its tunnel open.

Run locally: uv run python scripts/prepare_phase6_windows_install.py
Uses the configured public release key only. Publishes public installer scripts;
does not enroll workers, select releases, enable flags, or touch credentials.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
import publish_agent_worker_release as publisher  # noqa: E402
from app.services.browser_agents.artifacts import verify_release  # noqa: E402

DEV_URL = "https://bpidntbyvoooqvaispup.supabase.co"
VERSION = "1.0.1"
PYTHON_VERSION = "3.12.10"
PACKAGE_URL = f"https://api.nuget.org/v3-flatcontainer/python/{PYTHON_VERSION}/python.{PYTHON_VERSION}.nupkg"
REGISTRATION_URL = f"https://api.nuget.org/v3/registration5-semver1/python/{PYTHON_VERSION}.json"
RUNTIME_PATH = "installer/setup-python-runtime.ps1"
TOKEN = "__PYTHON_NUGET_SHA256__"
OUTPUT = ROOT / "dist" / "phase6-worker-1.0.1"
TUNNEL_URL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com(?![a-z0-9.-])")


class PreparationError(ValueError):
    pass


class DownloadUnavailable(PreparationError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = status in {None, 408, 425, 429, 500, 502, 503, 504}


def endpoint_label(url: str) -> str:
    """A diagnostic location without userinfo, query, fragment, or body."""
    parsed = urlsplit(url)
    host = parsed.hostname or "unknown-host"
    if parsed.port is not None:
        host += f":{parsed.port}"
    location = host + (parsed.path or "/")
    return re.sub(r"[^A-Za-z0-9/._:-]", "?", location)[:300]


def transport_label(exc: httpx.TransportError) -> str:
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        if isinstance(current, socket.gaierror):
            return "DNS resolution failure"
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return type(exc).__name__


def guard_target(url: str) -> None:
    if url.rstrip("/") != DEV_URL:
        raise PreparationError("Refusing any Supabase target except the exact BDR development project.")


def public_key(value: str) -> bytes:
    try:
        result = base64.b64decode(value.strip(), validate=True)
    except (ValueError, AttributeError):
        raise PreparationError("A valid BROWSER_AGENT_RELEASE_PUBKEY is required.") from None
    if len(result) != 32:
        raise PreparationError("The configured release public key must contain 32 bytes.")
    return result


def verified_release(sb, key: bytes) -> tuple[dict, bytes]:
    rows = sb.table("browser_agent_worker_releases").select("*").eq("version", VERSION).limit(1).execute().data
    if not rows or not verify_release(rows[0], base64.b64encode(key).decode()):
        raise PreparationError("Published release 1.0.1 is missing or its signature is invalid.")
    row = rows[0]
    path = f"releases/{VERSION}/bdr-agent-{VERSION}.zip"
    if row.get("version") != VERSION or row.get("storage_path") != path:
        raise PreparationError("Release storage metadata does not match version 1.0.1.")
    bundle = sb.storage.from_(publisher.BUCKET).download(path)
    if hashlib.sha256(bundle).hexdigest() != row["sha256"]:
        raise PreparationError("Published worker bundle hash does not match its signed manifest.")
    return row, bundle


def fetch(client, url: str, *, maximum: int = 100_000_000, timeout: float | None = None) -> bytes:
    label = endpoint_label(url)
    try:
        options = {"follow_redirects": False}
        if timeout is not None:
            options["timeout"] = timeout
        response = client.get(url, **options)
    except httpx.TransportError as exc:
        raise DownloadUnavailable(f"{label}: {transport_label(exc)}.") from None
    if 300 <= response.status_code < 400 or str(response.url) != url:
        raise PreparationError(f"{label}: download redirected or returned a different URL (HTTP {response.status_code}).")
    if response.status_code != 200:
        raise DownloadUnavailable(f"{label}: HTTP {response.status_code}.", response.status_code)
    if len(response.content) > maximum:
        raise PreparationError(f"{label}: download exceeded its size limit.")
    return response.content


def nuget_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.nuget.org"
            or parsed.query or parsed.fragment or not parsed.path.startswith("/v3/")):
        raise PreparationError("NuGet catalog URL must use https://api.nuget.org/v3/.")
    return url


def verified_python_package(client) -> tuple[bytes, dict]:
    try:
        registration = json.loads(fetch(client, REGISTRATION_URL, maximum=2_000_000))
        entry = registration["catalogEntry"]
        if not isinstance(entry, str):
            raise PreparationError("NuGet registration must point to an official catalog document.")
        catalog = json.loads(fetch(client, nuget_url(entry), maximum=2_000_000))
        if (catalog.get("id", "").lower() != "python" or catalog.get("version") != PYTHON_VERSION
                or catalog.get("packageHashAlgorithm") != "SHA512"
                or type(catalog.get("packageSize")) is not int
                or not 0 < catalog["packageSize"] <= 100_000_000):
            raise PreparationError("NuGet package identity, version, hash algorithm, or size is invalid.")
        digest = base64.b64decode(catalog["packageHash"], validate=True)
        if len(digest) != 64:
            raise PreparationError("NuGet SHA512 digest is invalid.")
        package = fetch(client, PACKAGE_URL)
        if len(package) != catalog["packageSize"] or hashlib.sha512(package).digest() != digest:
            raise PreparationError("Python package failed the official NuGet hash or size check.")
        return package, catalog
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PreparationError("NuGet metadata is malformed.") from exc


def render_runtime(template: bytes, package: bytes) -> bytes:
    if template.count(TOKEN.encode()) != 1:
        raise PreparationError("Runtime template must contain exactly one archive hash placeholder.")
    return template.replace(TOKEN.encode(), hashlib.sha256(package).hexdigest().encode())


def stop_tunnel(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    if process.stdout:
        process.stdout.close()


def start_tunnel(*, popen=subprocess.Popen, timeout: float = 45, origin_port: int = 5051) -> tuple[object, str]:
    if type(origin_port) is not int or origin_port not in (5051, 5071):
        raise PreparationError("Only the development worker or isolated acceptance port is allowed.")
    process = popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{origin_port}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    start_new_session=True, bufsize=1)
    lines: queue.Queue[str] = queue.Queue(maxsize=200)

    def read_output():
        try:
            for line in process.stdout:
                try:
                    lines.put_nowait(line)
                except queue.Full:
                    pass
        except (OSError, ValueError):
            pass

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout
    url, registered = None, False
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise PreparationError("The new development tunnel exited before becoming ready.")
            try:
                line = lines.get(timeout=min(0.2, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            match = TUNNEL_URL.search(line)
            if match:
                url = match.group(0)
            registered = registered or "Registered tunnel connection" in line
            if url and registered:
                return process, url
        raise PreparationError("The new development tunnel did not register within 45 seconds.")
    except BaseException:
        stop_tunnel(process)
        raise


def changed_environment(original: str, api: str) -> str:
    if TUNNEL_URL.fullmatch(api) is None:
        raise PreparationError("Only the newly allocated trycloudflare URL may be saved.")
    pattern = re.compile(r"^(?:export[ \t]+)?BROWSER_AGENT_API_BASE_URL[ \t]*=[^\r\n]*", re.MULTILINE)
    matches = list(pattern.finditer(original))
    if len(matches) > 1:
        raise PreparationError("The environment contains duplicate browser API URL entries.")
    replacement = f'BROWSER_AGENT_API_BASE_URL="{api}"'
    if matches:
        return pattern.sub(lambda _: replacement, original, count=1)
    newline = "\r\n" if "\r\n" in original else "\n"
    return original + (newline if original and not original.endswith("\n") else "") + replacement + newline


def write_environment(path: Path, original: bytes, changed: str) -> None:
    if path.is_symlink() or path.read_bytes() != original:
        raise PreparationError("The environment changed during preparation; its URL was not updated.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), path.stat().st_mode & 0o777)
            file.write(changed.encode())
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def verify_endpoints(client, api: str, runtime: bytes, installer: bytes, key: bytes) -> None:
    try:
        served_runtime = fetch(client, api + "/agent-worker/v1/python-runtime.ps1", maximum=2_000_000, timeout=20)
    except DownloadUnavailable as exc:
        if exc.status == 404:
            raise PreparationError(f"{exc} The Python runtime endpoint is missing. Reload the local BDR backend with updated code, then run preparation again.") from None
        raise
    if served_runtime != runtime:
        raise PreparationError("The runtime endpoint does not serve the newly verified script.")
    downloaded = fetch(client, api + "/agent-worker/v1/installer.ps1", maximum=4_000_000, timeout=20)
    match = re.search(rb"(\$EmbeddedReleaseB64\s*=\s*)'([A-Za-z0-9+/=]+)'", downloaded)
    try:
        release = json.loads(base64.b64decode(match.group(2), validate=True)) if match else {}
        manifest = release["manifest"]
        if manifest["version"] != VERSION or not publisher.verify(key, manifest, release["signature"]):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise PreparationError("Select the verified worker release 1.0.1 before preparing installation.") from None
    normalized = downloaded[:match.start(2)] + b"__RELEASE_JSON_B64__" + downloaded[match.end(2):]
    if normalized != installer:
        raise PreparationError("The installer endpoint does not serve the newly prepared template.")


def verify_live_endpoints(client, api: str, runtime: bytes, installer: bytes, key: bytes,
                          process, *, timeout: float = 180, sleep=time.sleep,
                          clock=time.monotonic) -> None:
    """Three-minute retry window; each in-flight endpoint request may take 20s."""
    deadline = clock() + timeout
    next_report = None
    while True:
        if process.poll() is not None:
            raise PreparationError("The new development tunnel stopped during endpoint verification.")
        try:
            verify_endpoints(client, api, runtime, installer, key)
            return
        except DownloadUnavailable as exc:
            now = clock()
            remaining = deadline - now
            if not exc.retryable:
                raise
            if remaining <= 0:
                raise PreparationError(f"The tunnel registered but its install endpoints did not become reachable within the startup window. Last check: {exc}") from None
            if next_report is None or now >= next_report:
                print(f"Waiting for public tunnel endpoints ({int(remaining)} seconds left). Last check: {exc}", flush=True)
                next_report = now + 15
            sleep(min(1, remaining))


def prepare(settings, sb, client, *, output: Path = OUTPUT, env_path: Path = ROOT / ".env",
            tunnel_factory=start_tunnel) -> tuple[object, str]:
    guard_target(settings.supabase_url)
    key = public_key(settings.browser_agent_release_pubkey)
    print("Verifying published development worker release 1.0.1...", flush=True)
    row, bundle = verified_release(sb, key)
    print("Checking the local BDR API on port 5051...", flush=True)
    if json.loads(fetch(client, "http://127.0.0.1:5051/health", maximum=10_000, timeout=5)) != {"status": "ok"}:
        raise PreparationError("Start the local BDR API on port 5051 before preparing installation.")
    if env_path.is_symlink():
        raise PreparationError("The development .env must be a regular file.")
    original = env_path.read_bytes()
    print("Downloading Python 3.12.10 and verifying official NuGet catalog hash and size...", flush=True)
    package, catalog = verified_python_package(client)
    runtime = render_runtime((ROOT / "agent_worker/install/setup_python_runtime.ps1").read_bytes(), package)
    output.mkdir(parents=True, exist_ok=True)
    (output / f"bdr-agent-{VERSION}.zip").write_bytes(bundle)
    (output / f"release-{VERSION}.json").write_text(json.dumps(row, indent=2) + "\n")
    (output / f"python.{PYTHON_VERSION}.nupkg").write_bytes(package)
    (output / "python-catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
    (output / "setup-python-runtime.ps1").write_bytes(runtime)
    print("Starting a new development tunnel; existing tunnels are unchanged...", flush=True)
    process, api = tunnel_factory()
    try:
        print(f"New development tunnel: {api}", flush=True)
        changed = changed_environment(original.decode(), api)
        installer = publisher.render_installer_template(api, key)
        (output / "bdr-install-template.ps1").write_bytes(installer)
        print("Publishing verified runtime setup and refreshed installer templates...", flush=True)
        sb.storage.from_(publisher.BUCKET).upload(RUNTIME_PATH, runtime,
            {"content-type": "text/plain; charset=utf-8", "upsert": "true"})
        publisher.upload_installer_template(sb, installer)
        print("Verifying the uploaded install scripts through the local BDR API...", flush=True)
        try:
            verify_endpoints(client, "http://127.0.0.1:5051", runtime, installer, key)
        except PreparationError as exc:
            raise PreparationError(f"Local BDR install endpoint verification failed before the public tunnel check. {exc}") from None
        print("Verifying the public install endpoints; waiting for tunnel connectivity if needed...", flush=True)
        verify_live_endpoints(client, api, runtime, installer, key, process)
        if process.poll() is not None:
            raise PreparationError("The new development tunnel stopped before preparation completed.")
        write_environment(env_path, original, changed)
        print("Updated only BROWSER_AGENT_API_BASE_URL in the development .env.", flush=True)
        return process, api
    except BaseException:
        stop_tunnel(process)
        raise


def windows_commands(api: str) -> str:
    if TUNNEL_URL.fullmatch(api) is None:
        raise PreparationError("Invalid tunnel URL.")
    return (
        "& {\n$ErrorActionPreference = 'Stop'\n"
        f"$api = '{api}'\n"
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12\n"
        'Invoke-WebRequest -UseBasicParsing "$api/agent-worker/v1/python-runtime.ps1" -OutFile "$env:TEMP\\bdr-python-runtime.ps1"\n'
        'powershell -ExecutionPolicy Bypass -File "$env:TEMP\\bdr-python-runtime.ps1"\n'
        'if ($LASTEXITCODE -ne 0) { throw "Python runtime setup failed" }\n'
        'Invoke-WebRequest -UseBasicParsing "$api/agent-worker/v1/installer.ps1" -OutFile "$env:TEMP\\bdr-personal-install.ps1"\n'
        'powershell -ExecutionPolicy Bypass -File "$env:TEMP\\bdr-personal-install.ps1" -Pool user '
        "-PythonPath 'C:\\Program Files\\BDR Python 3.12.10\\tools\\python.exe' -ApiBase $api\n"
        'if ($LASTEXITCODE -ne 0) { throw "BDR personal installer failed" }\n}'
    )


def main() -> int:
    from app.core.config import get_settings
    from app.core.supabase_client import get_supabase

    process = None
    try:
        settings = get_settings()
        guard_target(settings.supabase_url)
        public_key(settings.browser_agent_release_pubkey)
        with httpx.Client(timeout=180, follow_redirects=False) as client:
            process, api = prepare(settings, get_supabase(), client)
        print("Prepared development install. Keep this Mac terminal open; Ctrl+C stops only its tunnel.")
        print("Before installation, create a fresh Personal tasks enrollment code in BDR Settings > Browser agents > Add worker. Do not reuse an organization code.")
        print("Compare the release key fingerprint with the independently stored trusted fingerprint when prompted.")
        print("Run the following in elevated 64-bit Windows PowerShell:")
        print(windows_commands(api), flush=True)
        process.wait()
        raise PreparationError("The development tunnel stopped. Run preparation again for a fresh URL.")
    except KeyboardInterrupt:
        return 0
    except PreparationError as exc:
        print(f"Preparation stopped: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Never print service-role headers, environment contents, signed URLs,
        # or provider exception bodies. Individual helpers remain testable.
        print(f"Preparation failed ({type(exc).__name__}). No installation is ready; check the local API, selected release, and development configuration.", file=sys.stderr)
        return 1
    finally:
        if process is not None:
            stop_tunnel(process)


if __name__ == "__main__":
    raise SystemExit(main())

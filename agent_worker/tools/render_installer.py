"""Render install/install.ps1 into the single file the owner runs on the PC.

    python tools/render_installer.py --api https://<bdr-api> --pubkey release_key.pub \\
        [--release-json release.json] --out bdr-install.ps1

Fills the five placeholders in install.ps1:
    API base URL; the Ed25519 release public key (base64 of the 32 raw bytes);
    the launcher (zip of launcher/ plus install/uninstall.ps1, base64); the
    WinSW XML template (base64);
    optionally the release to install ({manifest, signature, url}, base64 JSON).

The release URL is a 900 s signed URL, so a rendered file that carries one
must be run within 15 minutes (the installer downloads it first thing). Without
a release, run the installer with -BundlePath and -ReleaseJsonPath.

The script, the key in it and the printed fingerprint are the install's trust
root: the owner compares the fingerprint with the password manager copy.
BDR's GET /agent-worker/v1/installer.ps1 must produce exactly this output
(see `render_installer`).
"""

import argparse
import base64
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "install" / "install.ps1"
WINSW_XML = ROOT / "install" / "winsw" / "BDRAgent.xml"
LAUNCHER = ROOT / "launcher"
UNINSTALL = ROOT / "install" / "uninstall.ps1"
FIXED_DATE = (1980, 1, 1, 0, 0, 0)

sys.path.insert(0, str(LAUNCHER))
from bdr_launcher import release as launcher_release  # noqa: E402

PLACEHOLDERS = {
    "api": "'__BDR_API_BASE__'",
    "pubkey": "'__RELEASE_PUBKEY_B64__'",
    "launcher": "'__LAUNCHER_ZIP_B64__'",
    "winsw": "'__WINSW_XML_B64__'",
    "release": "'__RELEASE_JSON_B64__'",
}


def launcher_zip_bytes(launcher_dir: Path = LAUNCHER, uninstall: Path = UNINSTALL) -> bytes:
    """The launcher files plus uninstall.ps1 at the zip root (install.ps1 moves
    it to <home>\\service, so the PC always has its own uninstaller)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        files = [launcher_dir / "launcher.py", *sorted((launcher_dir / "bdr_launcher").glob("*.py"))]
        entries = [(f.relative_to(launcher_dir).as_posix(), f) for f in files]
        if uninstall.is_file():
            entries.append(("uninstall.ps1", uninstall))
        for name, f in entries:
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, f.read_bytes())
    return buf.getvalue()


def _check_api(api: str) -> str:
    api = (api or "").strip().rstrip("/")
    parts = urlsplit(api)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise ValueError("api must be an https base URL")
    if "'" in api:
        raise ValueError("api must not contain quotes")
    return api


def render_installer(api_base: str, pubkey_path: Path, release: dict[str, Any] | None = None, *,
                     template: Path = TEMPLATE) -> str:
    api = _check_api(api_base)
    key = launcher_release.load_public_key(pubkey_path)
    release_b64 = ""
    if release is not None:
        manifest, signature = release.get("manifest"), release.get("signature")
        launcher_release.check_release(manifest, signature, key)
        url = str(release.get("url") or "")
        if urlsplit(url).scheme != "https":
            raise ValueError("release url must be https")
        payload = {"manifest": manifest, "signature": signature, "url": url}
        release_b64 = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    values = {
        "api": api,
        "pubkey": base64.b64encode(key).decode(),
        "launcher": base64.b64encode(launcher_zip_bytes()).decode(),
        "winsw": base64.b64encode(WINSW_XML.read_bytes()).decode(),
        "release": release_b64,
    }
    text = template.read_text(encoding="utf-8")
    for name, token in PLACEHOLDERS.items():
        if text.count(token) != 1:
            raise ValueError(f"template must contain {token} exactly once")
        text = text.replace(token, f"'{values[name]}'")
    return text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render the BDR agent installer")
    p.add_argument("--api", required=True)
    p.add_argument("--pubkey", required=True)
    p.add_argument("--release-json")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    rel = json.loads(Path(args.release_json).read_text(encoding="utf-8")) if args.release_json else None
    text = render_installer(args.api, Path(args.pubkey), rel)
    # UTF-8 with BOM so Windows PowerShell 5.1 reads it as UTF-8.
    Path(args.out).write_bytes(b"\xef\xbb\xbf" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode())
    key = launcher_release.load_public_key(Path(args.pubkey))
    print(f"wrote {args.out}; release key fingerprint {launcher_release.fingerprint(key)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

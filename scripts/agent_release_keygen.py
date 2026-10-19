"""Generate the Ed25519 release signing key for the browser agent worker (O3, D16).

Run ONCE, on the owner's Mac. The private key never leaves the Mac: it is
written to a local file (mode 600, PKCS8 PEM, encrypted with a passphrase
unless --no-passphrase) and is only ever read again by
scripts/publish_agent_worker_release.py on the same Mac. Nothing here
talks to BDR, Supabase or the network.

    cd bdr_be
    uv run python scripts/agent_release_keygen.py \\
        --private ~/.bdr/agent_release_ed25519.pem \\
        --public agent_worker/install/release_key.pub

Prints:
  - the public key as BROWSER_AGENT_RELEASE_PUBKEY (base64 of the 32 raw
    bytes): set it in Railway, and keep the .pub file for the installer;
  - the fingerprint (SHA-256 of the raw key, upper hex in groups of four),
    the same text install.ps1 and Settings > Add worker show. Save it in the
    password manager entry "BDR agent release key fingerprint".

Refuses to overwrite an existing private key (a new key orphans every
installed worker until it is re-installed with the new public key).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_PRIVATE = Path.home() / ".bdr" / "agent_release_ed25519.pem"
PASSPHRASE_ENV = "BDR_RELEASE_KEY_PASSPHRASE"


def fingerprint(raw_public: bytes) -> str:
    digest = hashlib.sha256(raw_public).hexdigest().upper()
    return " ".join(digest[i:i + 4] for i in range(0, len(digest), 4))


def public_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def generate(private_path: Path, public_path: Path | None, passphrase: bytes | None) -> dict[str, str]:
    """Write the key pair; returns {public_b64, fingerprint, private_path}."""
    private_path = private_path.expanduser()
    if private_path.exists():
        raise SystemExit(f"refusing to overwrite {private_path}: a new key orphans every installed worker")
    key = Ed25519PrivateKey.generate()
    encryption = (serialization.BestAvailableEncryption(passphrase) if passphrase
                  else serialization.NoEncryption())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(private_path.parent, 0o700)
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    pub = public_b64(key)
    if public_path is not None:
        public_path = public_path.expanduser()
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text(pub + "\n", encoding="ascii")
    return {"public_b64": pub, "fingerprint": fingerprint(base64.b64decode(pub)),
            "private_path": str(private_path)}


def _passphrase(no_passphrase: bool) -> bytes | None:
    if no_passphrase:
        return None
    env = os.environ.get(PASSPHRASE_ENV)
    if env:
        return env.encode("utf-8")
    first = getpass.getpass("Passphrase for the release key (keep it in the password manager): ")
    if len(first) < 12:
        raise SystemExit("use a passphrase of at least 12 characters")
    if getpass.getpass("Again: ") != first:
        raise SystemExit("the passphrases differ")
    return first.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate the browser agent release signing key (offline).")
    p.add_argument("--private", type=Path, default=DEFAULT_PRIVATE, help="private key PEM path (stays on this Mac)")
    p.add_argument("--public", type=Path, help="also write the public key (base64 raw) here, e.g. release_key.pub")
    p.add_argument("--no-passphrase", action="store_true", help="store the private key unencrypted (not advised)")
    args = p.parse_args(argv)
    out = generate(args.private, args.public, _passphrase(args.no_passphrase))
    print(f"private key: {out['private_path']} (never copy it off this Mac)")
    print(f"BROWSER_AGENT_RELEASE_PUBKEY={out['public_b64']}")
    print(f"fingerprint: {out['fingerprint']}")
    print("Save the fingerprint in the password manager entry 'BDR agent release key fingerprint'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The Ed25519 release public key the installer bakes in (spec 8, O3).

`release_key.pub` holds the 32 raw key bytes, base64 encoded (one line), or a
PEM SubjectPublicKeyInfo block. Its fingerprint is what the owner compares at
install and enrollment against the copy saved in the password manager at keygen.

The updater (W5) verifies release manifests with `verify_manifest`.
"""

import base64
import hashlib
import hmac
import re
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .protocol import ReleaseManifest, canonical_manifest_bytes


class ReleaseKeyError(ValueError):
    pass


def load_public_key(path: Path) -> Ed25519PublicKey:
    try:
        raw = Path(path).read_bytes().strip()
    except FileNotFoundError as exc:
        raise ReleaseKeyError(f"release public key not found at {path}") from exc
    if raw.startswith(b"-----BEGIN"):
        key = serialization.load_pem_public_key(raw)
        if not isinstance(key, Ed25519PublicKey):
            raise ReleaseKeyError("release key is not an Ed25519 key")
        return key
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise ReleaseKeyError("release key is not base64") from exc
    if len(data) != 32:
        raise ReleaseKeyError("release key must be 32 raw Ed25519 bytes")
    return Ed25519PublicKey.from_public_bytes(data)


def raw_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(key: Ed25519PublicKey) -> str:
    """SHA-256 of the raw key, upper hex in groups of four: 'AB12 CD34 ...'."""
    digest = hashlib.sha256(raw_bytes(key)).hexdigest().upper()
    return " ".join(digest[i:i + 4] for i in range(0, len(digest), 4))


def normalize_fingerprint(text: str) -> str:
    return re.sub(r"[^0-9a-f]", "", (text or "").lower())


def fingerprint_matches(key: Ed25519PublicKey, expected: str) -> bool:
    want = normalize_fingerprint(expected)
    have = normalize_fingerprint(fingerprint(key))
    return len(want) == 64 and hmac.compare_digest(want, have)


def verify_manifest(key: Ed25519PublicKey, manifest: ReleaseManifest | dict[str, Any],
                    signature_b64: str) -> bool:
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        return False
    try:
        key.verify(sig, canonical_manifest_bytes(manifest))
    except InvalidSignature:
        return False
    return True

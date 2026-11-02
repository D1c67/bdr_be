"""Personal browser envelope encryption, separate from organization secrets.

Each object has a random DEK wrapped by USER_CRED_KEK. Both envelopes bind
owner, object, generation and purpose. No org key or plaintext fallback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings


class VaultUnavailable(ValueError):
    pass


def _decode(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _key(key_id: str | None = None) -> tuple[str, bytes]:
    settings = get_settings()
    current_id = getattr(settings, "user_cred_kek_id", "u1")
    raw = getattr(settings, "user_cred_kek", "")
    if key_id is not None and key_id != current_id:
        raw = dict(part.split(":", 1) for part in
                   getattr(settings, "user_cred_old_keys", "").split(",") if ":" in part).get(key_id, "")
    try:
        key = _decode(raw)
        if len(key) != 32:
            raise ValueError
    except (ValueError, TypeError):
        raise VaultUnavailable("Personal browser vault is unavailable.") from None
    return key_id or current_id, key


def _aad(user_id: str, object_id: str, version: int, purpose: str) -> bytes:
    if purpose not in {"credential", "session", "artifact", "trace", "result", "help_secret"} or int(version) < 1:
        raise VaultUnavailable("Personal browser vault is unavailable.")
    return f"bdr-user-cred|{user_id}|{object_id}|{int(version)}|{purpose}".encode()


def encrypt_payload(user_id: str, object_id: str, version: int, purpose: str, plaintext: bytes) -> dict:
    aad = _aad(user_id, object_id, version, purpose)
    key_id, kek = _key()
    dek, nonce, wrap_nonce = os.urandom(32), os.urandom(12), os.urandom(12)
    def encode(value):
        return base64.b64encode(value).decode("ascii")
    return {"wrapped_dek": encode(wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, aad + b"|dek")),
            "ciphertext": encode(AESGCM(dek).encrypt(nonce, plaintext, aad)),
            "nonce": encode(nonce), "key_id": key_id}


def decrypt_payload(user_id: str, object_id: str, version: int, purpose: str, envelope: dict) -> bytes:
    try:
        aad = _aad(user_id, object_id, version, purpose)
        _, kek = _key(envelope["key_id"])
        wrapped = _decode(envelope["wrapped_dek"])
        dek = AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], aad + b"|dek")
        return AESGCM(dek).decrypt(_decode(envelope["nonce"]), _decode(envelope["ciphertext"]), aad)
    except Exception:
        raise VaultUnavailable("Personal browser vault is unavailable.") from None


def password_fingerprint(user_id: str, password: str) -> str:
    _, key = _key()
    return hmac.new(key, f"fingerprint|{user_id}|{password}".encode(), hashlib.sha256).hexdigest()[:12]


def decrypt_credential(row: dict) -> str:
    if row.get("status") != "active":
        raise VaultUnavailable("Personal browser vault is unavailable.")
    return decrypt_payload(str(row["user_id"]), str(row["id"]), int(row["version"]),
                           "credential", row).decode("utf-8")


def job_credential(job: dict) -> dict:
    from app.assistant import access, repo
    settings = get_settings()
    owner = str(job.get("principal_user_id") or "")
    if (not getattr(settings, "assistant_browser_enabled", False)
            or job.get("principal_kind") != "user" or not owner
            or access.owner_check(owner, require_writer=True).problem):
        raise VaultUnavailable("Personal browser vault is unavailable.")
    row = repo.browser_credential_by_id(owner, str(job.get("user_credential_id") or ""))
    if (not row or row.get("status") != "active"
            or row.get("version") != job.get("user_credential_version")
            or row.get("service") != (job.get("input") or {}).get("service")):
        raise VaultUnavailable("Personal browser vault is unavailable.")
    from app.services import assistant_browser_sites as sites
    seed = job.get("input") or {}
    try:
        allowed = sites.credential_allows_url(row, seed.get("service"), seed.get("url"))
    except (ValueError, TypeError):
        allowed = False
    if not allowed:
        raise VaultUnavailable("Personal browser vault is unavailable.")
    return row


def encode_envelope(envelope: dict) -> bytes:
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()

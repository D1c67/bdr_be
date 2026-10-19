"""Encrypted secret store for the browser agent platform (SPEC.md 2.2, O2).

Two kinds of secret live here, both under AES-256-GCM keyed by
BROWSER_AGENT_SECRETS_KEY (Railway env, never in the database):

  identity passwords  one row per `<identity>_password` in
                      browser_agent_secrets (today only `procore_password`,
                      the shared Procore account the daily sweep, the RFP
                      harvest and the agents' login/begin all use). AAD is
                      'bdr-agent-secret|<name>|<version>', so a ciphertext
                      moved to another name or version never decrypts.
  helper one-time     the help chat's secret messages (an MFA code). AAD is
  codes               'help-secret|<secret_id>'; `encrypt_with_aad` /
                      `decrypt_with_aad` are the primitives the help phase uses.

Write-only from the UI: nothing in this module returns a stored value to a
caller that renders it. `secret_rows()` gives the Settings view
({name, set, set_at, set_by_label, fingerprint}); the fingerprint is
HMAC-SHA256 under the SEPARATE BROWSER_AGENT_FINGERPRINT_KEY, first 8 hex
characters, enough to tell "the value changed" without revealing it.

Rotation: a row keeps the key_id it was written with. Retired keys stay
readable through BROWSER_AGENT_SECRETS_OLD_KEYS ("k0:<b64>,...") while
`reencrypt_all()` (scripts/rotate_agent_secrets_key.py) rewrites every row
under the current key.

Every function is plain `def` (the Supabase SDK is synchronous).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core import supabase_client
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

TABLE = "browser_agent_secrets"
SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}_password$")
SECRET_VALUE_MAX = 200
NONCE_BYTES = 12
FINGERPRINT_CHARS = 8


class SecretsKeyMissing(RuntimeError):
    """BROWSER_AGENT_SECRETS_KEY (or the key a row names) is not configured."""


class SecretDecryptError(RuntimeError):
    """A stored ciphertext failed authentication (wrong key, AAD or bytes).
    The message never carries any part of the value."""


class SecretInvalid(ValueError):
    """A name or value the store refuses (shape, length)."""


# ── Keys ────────────────────────────────────────────────────────────────────


def _b64_32(value: str, what: str) -> bytes:
    try:
        raw = base64.b64decode((value or "").strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SecretsKeyMissing(f"{what} is not valid base64") from None
    if len(raw) != 32:
        raise SecretsKeyMissing(f"{what} must decode to 32 bytes")
    return raw


def keys_configured(settings: Settings | None = None) -> bool:
    """The store is usable: a current key is set (the boot validator has
    already checked its shape when agents are on)."""
    s = settings or get_settings()
    return bool((getattr(s, "browser_agent_secrets_key", "") or "").strip())


def current_key(settings: Settings | None = None) -> tuple[str, bytes]:
    """(key_id, 32 raw bytes) of the key new ciphertexts are written under."""
    s = settings or get_settings()
    if not keys_configured(s):
        raise SecretsKeyMissing("BROWSER_AGENT_SECRETS_KEY is not set")
    return s.browser_agent_secrets_key_id, _b64_32(s.browser_agent_secrets_key, "BROWSER_AGENT_SECRETS_KEY")


def key_for(key_id: str, settings: Settings | None = None) -> bytes:
    """The key a stored row names: the current one or a retired one."""
    s = settings or get_settings()
    cur_id, cur = current_key(s)
    if key_id == cur_id:
        return cur
    old = s.browser_agent_old_keys
    if key_id in old:
        return old[key_id]
    raise SecretsKeyMissing(f"no key configured for key id {key_id!r}")


def _fingerprint_key(settings: Settings | None = None) -> bytes:
    s = settings or get_settings()
    return _b64_32(getattr(s, "browser_agent_fingerprint_key", "") or "", "BROWSER_AGENT_FINGERPRINT_KEY")


def fingerprint(value: str, settings: Settings | None = None) -> str:
    """HMAC-SHA256(fingerprint key, value) hex, first 8 characters."""
    digest = hmac.new(_fingerprint_key(settings), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:FINGERPRINT_CHARS]


# ── AES-GCM primitives ────────────────────────────────────────────────────────


def secret_aad(name: str, version: int) -> bytes:
    return f"bdr-agent-secret|{name}|{int(version)}".encode("utf-8")


def helper_secret_aad(secret_id: str) -> bytes:
    return f"help-secret|{secret_id}".encode("utf-8")


def encrypt_with_aad(plaintext: str, aad: bytes, settings: Settings | None = None) -> tuple[bytes, bytes, str]:
    """(ciphertext||tag, nonce, key_id) under the current key. A fresh
    96-bit random nonce per call (never reused with the same key)."""
    key_id, key = current_key(settings)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
    return ciphertext, nonce, key_id


def decrypt_with_aad(ciphertext: bytes, nonce: bytes, aad: bytes, key_id: str,
                     settings: Settings | None = None) -> str:
    key = key_for(key_id, settings)
    if len(nonce) != NONCE_BYTES:
        raise SecretDecryptError("stored nonce has the wrong length")
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad).decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError):
        raise SecretDecryptError("stored secret failed authentication") from None


# ── bytea over PostgREST ────────────────────────────────────────────────────


def _bytea(data: bytes) -> str:
    return "\\x" + data.hex()


def _unbytea(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    text = str(value or "")
    if text.startswith("\\x"):
        try:
            return bytes.fromhex(text[2:])
        except ValueError:
            pass
    raise SecretDecryptError("stored secret is not a bytea value")


# ── The table ──────────────────────────────────────────────────────────────────


def _sb():
    return supabase_client.get_supabase()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity_secret_name(identity: str) -> str:
    return f"{identity}_password"


def _check_name(name: str) -> None:
    if not SECRET_NAME_RE.match(name or ""):
        raise SecretInvalid("secret names look like <identity>_password")


def load_row(name: str) -> dict | None:
    _check_name(name)
    rows = _sb().table(TABLE).select("*").eq("name", name).limit(1).execute().data or []
    return rows[0] if rows else None


def decrypt_row(row: dict, settings: Settings | None = None) -> str:
    return decrypt_with_aad(
        _unbytea(row.get("ciphertext")), _unbytea(row.get("nonce")),
        secret_aad(row["name"], int(row.get("version") or 1)), str(row.get("key_id") or ""), settings,
    )


def get_secret(name: str, settings: Settings | None = None) -> str | None:
    """The decrypted value, read fresh from the database on every call (a
    password changed in Settings applies to the very next login). None when
    the row is absent. Raises SecretsKeyMissing / SecretDecryptError."""
    row = load_row(name)
    if row is None:
        return None
    return decrypt_row(row, settings)


def get_secret_version(name: str) -> int | None:
    row = load_row(name)
    return int(row.get("version") or 1) if row else None


def set_secret(name: str, value: str, *, actor_id: str | None, actor_label: str,
               settings: Settings | None = None) -> dict:
    """Store (or replace) one secret: version + 1, encrypted before the
    single write, plaintext never reaches a database call. Writes the
    settings change log ({set, fingerprint} only) and an audit row. For an
    identity password, also clears that identity's login lock, failure
    count and spacing stamp (the new password deserves an attempt now).
    Returns the Settings view row (never the value)."""
    from app.services import notifications

    s = settings or get_settings()
    _check_name(name)
    if not isinstance(value, str) or not value or len(value) > SECRET_VALUE_MAX:
        raise SecretInvalid(f"a secret is 1 to {SECRET_VALUE_MAX} characters")
    if value != value.strip() or any(ch in value for ch in "\r\n\x00"):
        raise SecretInvalid("a secret has no leading or trailing spaces, line breaks or NUL")
    current = load_row(name)
    version = int((current or {}).get("version") or 0) + 1
    ciphertext, nonce, key_id = encrypt_with_aad(value, secret_aad(name, version), s)
    fp = fingerprint(value, s)
    row = {
        "name": name,
        "ciphertext": _bytea(ciphertext),
        "nonce": _bytea(nonce),
        "key_id": key_id,
        "version": version,
        "fingerprint": fp,
        "set_by": actor_id,
        "set_at": _now_iso(),
    }
    _sb().table(TABLE).upsert(row, on_conflict="name").execute()
    _log_change(name, current, {"set": True, "fingerprint": fp}, actor_id, actor_label)
    try:
        # audit_log.entity_id is a uuid column: the secret's name rides in the payload.
        notifications.audit(actor_id, "browser_agent.secret_set", TABLE, None,
                            {"name": name, "version": version, "fingerprint": fp, "actor_label": actor_label})
    except Exception:  # noqa: BLE001 - the change log row above is the record
        logger.exception("browser agents: audit row for secret %s failed", name)
    if name.endswith("_password"):
        reset_login_lock(name[: -len("_password")])
    return secret_view({**row, "set_by_label": actor_label})


def delete_secret(name: str, *, actor_id: str | None, actor_label: str) -> bool:
    """Remove a secret (logins fall back to the env value, if any)."""
    from app.services import notifications

    _check_name(name)
    current = load_row(name)
    if current is None:
        return False
    _sb().table(TABLE).delete().eq("name", name).execute()
    _log_change(name, current, {"set": False}, actor_id, actor_label)
    try:
        notifications.audit(actor_id, "browser_agent.secret_cleared", TABLE, None,
                            {"name": name, "actor_label": actor_label})
    except Exception:  # noqa: BLE001
        logger.exception("browser agents: audit row for secret %s failed", name)
    return True


def _log_change(name: str, current: dict | None, new_value: dict, actor_id: str | None, actor_label: str) -> None:
    old = {"set": True, "fingerprint": current.get("fingerprint")} if current else {"set": False}
    try:
        _sb().table("browser_agent_settings_changes").insert({
            "scope": "secret", "target": name, "field": "value", "old_value": old, "new_value": new_value,
            "changed_by": actor_id, "changed_by_label": actor_label or "unknown",
        }).execute()
    except Exception:  # noqa: BLE001 - the audit row is still written
        logger.exception("browser agents: change log row for secret %s failed", name)


def reset_login_lock(identity: str) -> None:
    """A new password for `identity`: its shared rfp_harvest_sessions row
    forgets the failures, the lock and the last attempt stamp."""
    try:
        _sb().table("rfp_harvest_sessions").update({
            "login_failures": 0, "locked_until": None, "last_login_attempt_at": None, "last_error": None,
        }).eq("provider", identity).execute()
    except Exception:  # noqa: BLE001 - a lock that stays simply expires on its own
        logger.exception("browser agents: resetting the %s login lock failed", identity)


def secret_view(row: dict) -> dict:
    """The Settings row: never the value."""
    return {
        "name": row["name"],
        "set": True,
        "set_at": row.get("set_at"),
        "set_by_label": row.get("set_by_label"),
        "fingerprint": row.get("fingerprint"),
    }


def secret_rows(names: tuple[str, ...] = ("procore_password",)) -> list[dict]:
    """C2 SecretRow per known secret name, set or not."""
    rows = {
        r["name"]: r for r in (
            _sb().table(TABLE).select("name, set_at, set_by, fingerprint, version, key_id").execute().data or []
        )
    }
    labels: dict[str, str] = {}
    ids = sorted({str(r["set_by"]) for r in rows.values() if r.get("set_by")})
    if ids:
        try:
            for p in _sb().table("profiles").select("id, full_name, email").in_("id", ids).execute().data or []:
                labels[str(p["id"])] = p.get("full_name") or p.get("email") or ""
        except Exception:  # noqa: BLE001 - labels are cosmetic
            logger.warning("browser agents: secret setter labels unreadable")
    out = []
    for name in sorted(set(names) | set(rows)):
        row = rows.get(name)
        if row is None:
            out.append({"name": name, "set": False, "set_at": None, "set_by_label": None, "fingerprint": None})
        else:
            out.append(secret_view({**row, "set_by_label": labels.get(str(row.get("set_by")))}))
    return out


def reencrypt_all(settings: Settings | None = None) -> int:
    """Rewrite every stored secret under the current key (rotation). Same
    name and version (the AAD), new nonce. Returns rows rewritten."""
    s = settings or get_settings()
    cur_id, _ = current_key(s)
    done = 0
    for row in _sb().table(TABLE).select("*").execute().data or []:
        if row.get("key_id") == cur_id:
            continue
        value = decrypt_row(row, s)
        ciphertext, nonce, key_id = encrypt_with_aad(value, secret_aad(row["name"], int(row.get("version") or 1)), s)
        _sb().table(TABLE).update({
            "ciphertext": _bytea(ciphertext), "nonce": _bytea(nonce), "key_id": key_id,
        }).eq("name", row["name"]).eq("key_id", row.get("key_id")).execute()
        done += 1
    return done

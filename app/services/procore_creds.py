"""The shared Procore account's credentials (docs/browser_agents/SPEC.md 5.5, O2).

One account (PROCORE_LOGIN_EMAIL) is used by everything that logs in to
Procore: the PM RFI daily sweep, the RFP harvest and the browser agents'
login/begin. Its password comes from the encrypted secret store
(`browser_agent_secrets.procore_password`, set by IT Admin in Settings >
Browser agents) and falls back to the env PROCORE_LOGIN_PASSWORD, so an
existing deployment keeps working before anyone sets it in Settings.

  password(settings)    the password, read FRESH from the database on every
                        call (a change in Settings applies to the very next
                        login). Called only when a login is about to happen.
  configured(settings)  the account can log in at all: email set and a
                        password in the store or the env. Cached 60 s per
                        process (the RFP harvest asks it per email row).
  config(settings)      the ProcoreConfig a ProcoreSession is built with.

Every `settings.procore_configured` read in app/ goes through `configured`
instead (tests/test_browser_agents_secrets.py greps for strays): the
property only knows the env password.

The store is consulted only when BROWSER_AGENT_SECRETS_KEY is set; without
the key nothing could be decrypted, and nothing extra is read.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.core.config import Settings, get_settings
from app.services import procore_client as pc

logger = logging.getLogger(__name__)

IDENTITY = "procore"
CONFIGURED_CACHE_SECONDS = 60.0

_cache_lock = threading.Lock()
# (monotonic stamp, has_db_secret) per identity
_db_secret_cache: dict[str, tuple[float, bool]] = {}


def _email(settings: Any) -> str:
    return (getattr(settings, "procore_login_email", "") or "").strip()


def _env_password(settings: Any) -> str:
    return getattr(settings, "procore_login_password", "") or ""


def _store_enabled(settings: Any) -> bool:
    return bool((getattr(settings, "browser_agent_secrets_key", "") or "").strip()) and bool(
        getattr(settings, "supabase_url", "")
    )


def reset_cache() -> None:
    with _cache_lock:
        _db_secret_cache.clear()


def db_password(settings: Settings | None = None, identity: str = IDENTITY) -> str | None:
    """The decrypted DB secret, or None (no key, no row, or unreadable: an
    unreadable row is logged, never raised, so the env fallback still works)."""
    s = settings or get_settings()
    if not _store_enabled(s):
        return None
    from app.services.browser_agents import secrets

    try:
        return secrets.get_secret(secrets.identity_secret_name(identity), s)
    except (secrets.SecretsKeyMissing, secrets.SecretDecryptError) as exc:
        logger.error("procore creds: the stored %s password is unreadable (%s); using the env value",
                     identity, type(exc).__name__)
        return None
    except Exception:  # noqa: BLE001 - a database hiccup falls back to the env value
        logger.exception("procore creds: reading the stored %s password failed", identity)
        return None


def password(settings: Settings | None = None, identity: str = IDENTITY) -> str:
    """The password for a login about to happen: the DB secret, else the env."""
    s = settings or get_settings()
    return db_password(s, identity) or _env_password(s)


def password_version(settings: Settings | None = None, identity: str = IDENTITY) -> int | None:
    """The stored secret's version (None when the env value is in use)."""
    s = settings or get_settings()
    if not _store_enabled(s):
        return None
    from app.services.browser_agents import secrets

    try:
        return secrets.get_secret_version(secrets.identity_secret_name(identity))
    except Exception:  # noqa: BLE001 - bookkeeping only
        return None


def _has_db_secret(settings: Any, identity: str) -> bool:
    if not _store_enabled(settings):
        return False
    now = time.monotonic()
    with _cache_lock:
        hit = _db_secret_cache.get(identity)
        if hit is not None and now - hit[0] < CONFIGURED_CACHE_SECONDS:
            return hit[1]
    from app.services.browser_agents import secrets

    try:
        has = secrets.load_row(secrets.identity_secret_name(identity)) is not None
    except Exception:  # noqa: BLE001 - unknown counts as absent; the env decides
        logger.warning("procore creds: the secret store is unreadable; using the env password only")
        has = False
    with _cache_lock:
        _db_secret_cache[identity] = (now, has)
    return has


def configured(settings: Settings | Any | None = None, identity: str = IDENTITY) -> bool:
    """The Procore account can log in: the email is set and a password is
    in the env or the encrypted store."""
    s = settings or get_settings()
    if not _email(s):
        return False
    if _env_password(s):
        return True
    return _has_db_secret(s, identity)


def config(settings: Settings | None = None, host: str | None = None) -> pc.ProcoreConfig:
    """The ProcoreConfig for a session on `host` (all hosts share the one
    account; the host only picks the cookie row, see pm_rfi_sync.open_session).
    The password is read when the session is opened."""
    s = settings or get_settings()
    return pc.ProcoreConfig(
        email=_email(s),
        password=password(s),
        min_request_interval=s.procore_min_request_interval_seconds,
        login_min_interval=s.procore_login_min_interval_seconds,
        timeout=s.procore_request_timeout_seconds,
    )


def _redaction_source() -> tuple[str, ...]:
    """Live Procore password for the agent turn scrub (redaction.load_secrets):
    the DB secret, so a password changed in Settings is redacted too."""
    value = db_password(get_settings())
    return (value,) if value else ()


def register_redaction_source() -> None:
    from app.services.browser_agents import redaction

    redaction.register_secret_source(_redaction_source)


register_redaction_source()

"""BDR-side scrub of worker observations and model I/O (SPEC.md 1 step 4, O5).

Nothing the worker sends to /turns is persisted, forwarded to a provider or
logged before it passes through here. O5 cut the n-gram tripwire; what stays
is exact-value redaction of KNOWN secrets, in plain, URL-encoded and base64
forms, plus the URL query redaction and the untrusted-text hygiene:

  known secret                       how BDR knows it
  ---------------------------------  -------------------------------------------
  the Procore password               PROCORE_LOGIN_PASSWORD, plus every source
                                     registered with register_secret_source()
                                     (the encrypted DB secret)
  this worker's own token            the Authorization header of the request
  stored Procore cookie values       rfp_harvest_sessions (provider 'procore'
                                     and every 'procore:<host>' row)
  helper one-time secrets of the job secret_hmac on browser_agent_help_messages;
                                     the plaintext is gone after use, so
                                     candidate tokens in the text are HMACed
                                     with the fingerprint key and compared

A hit on a password, a helper secret or the worker token is SEVERE: the
turn service stores only {"redacted": true} and fails the job
`secret_leak_blocked` (the value reached the PC's page or tool output where
it never should be). A cookie value is replaced and the turn continues.

URL hygiene: inside free text, query parameters whose NAME looks like a
credential (protocol.TOKEN_QUERY_KEY_RE: token, sig, key, code, session,
passport, x-amz-...) get the value "[redacted]", and userinfo is dropped.
The rest of the URL stays, because the model navigates by it.

`secret_hmac(value)` is THE helper-secret HMAC: HMAC-SHA256 under
BROWSER_AGENT_FINGERPRINT_KEY over the UTF-8 value, lowercase hex. The help
phase must store browser_agent_help_messages.secret_hmac with this function.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from app.core import supabase_client
from app.core.config import get_settings
from app.services.browser_agents import protocol

logger = logging.getLogger(__name__)

REDACTED = "[redacted]"
# Exact-value matching ignores values this short (a 3-char "password" would
# shred ordinary text). Helper codes are matched by HMAC, any length >= 4.
MIN_SECRET_LEN = 6
MIN_COOKIE_LEN = 12
HELPER_CODE_RE = re.compile(r"[A-Za-z0-9]{4,12}")
COOKIE_CACHE_SECONDS = 60

# Zero-width, bidi control and BOM characters (SPEC.md 1 step 6): stripped
# from every untrusted string before the model sees it.
INVISIBLE_RE = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")

_sources: list[Callable[[], Iterable[str]]] = []
_sources_lock = threading.Lock()
_cookie_cache: tuple[float, tuple[str, ...]] | None = None


def register_secret_source(fn: Callable[[], Iterable[str]]) -> None:
    """Add a provider of live secret values (the login phase registers the
    decrypted Procore password from the encrypted store). Values are read
    on every turn and never stored."""
    with _sources_lock:
        if fn not in _sources:
            _sources.append(fn)


def reset_for_tests() -> None:
    global _cookie_cache
    with _sources_lock:
        _sources.clear()
    _cookie_cache = None


def secret_hmac(value: str) -> str:
    """HMAC-SHA256(fingerprint key, value) hex. See the module docstring."""
    key = base64.b64decode(get_settings().browser_agent_fingerprint_key or "")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def strip_invisible(text: str) -> str:
    return INVISIBLE_RE.sub("", text)


def _redact_url_queries(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return REDACTED
    if not parts.scheme or not host:
        return url
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(protocol.TOKEN_QUERY_KEY_RE.search(k) for k, _ in pairs):
            query = urlencode([(k, REDACTED if protocol.TOKEN_QUERY_KEY_RE.search(k) else v) for k, v in pairs])
    fragment = parts.fragment
    if fragment and protocol.TOKEN_QUERY_KEY_RE.search(fragment):
        fragment = ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def redact_token_queries(text: str) -> str:
    """Every URL in `text` without userinfo and without credential-like
    query values (see the module docstring)."""
    return _URL_RE.sub(lambda m: _redact_url_queries(m.group(0)), text)


def _variants(value: str) -> set[str]:
    raw = value.encode("utf-8")
    out = {value, quote(value, safe=""), quote_plus(value), quote(value)}
    for enc in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        text = enc.decode("ascii")
        out.add(text)
        out.add(text.rstrip("="))
    return {v for v in out if v}


@dataclass
class ScrubReport:
    hits: list[str] = field(default_factory=list)       # labels, never values
    severe: bool = False

    def add(self, label: str, severe: bool) -> None:
        if label not in self.hits:
            self.hits.append(label)
        self.severe = self.severe or severe


@dataclass
class SecretSet:
    """The known secrets for one turn. `exact` holds (label, variant, severe)
    sorted longest first, so a base64 form is replaced before a shorter
    plain form inside it."""
    exact: list[tuple[str, str, bool]] = field(default_factory=list)
    hmacs: set[str] = field(default_factory=set)
    hmac_key: bytes = b""

    def add(self, label: str, value: str | None, *, severe: bool, min_len: int = MIN_SECRET_LEN) -> None:
        if not value or len(value) < min_len:
            return
        for v in _variants(value):
            if len(v) >= min_len:
                self.exact.append((label, v, severe))
        self.exact.sort(key=lambda item: len(item[1]), reverse=True)

    def scrub_text(self, text: str, report: ScrubReport) -> str:
        for label, value, severe in self.exact:
            if value in text:
                text = text.replace(value, REDACTED)
                report.add(label, severe)
        if self.hmacs and self.hmac_key:
            def _check(m: re.Match) -> str:
                digest = hmac.new(self.hmac_key, m.group(0).encode("utf-8"), hashlib.sha256).hexdigest()
                if digest in self.hmacs:
                    report.add("helper_secret", True)
                    return REDACTED
                return m.group(0)

            text = HELPER_CODE_RE.sub(_check, text)
        return text


def scrub(value: Any, secrets: SecretSet, report: ScrubReport, *, untrusted: bool = True) -> Any:
    """Deep scrub of a JSON-like value: strings lose known secrets and
    credential-like URL query values; untrusted strings also lose invisible
    characters. Dict keys are scrubbed too."""
    if isinstance(value, str):
        text = strip_invisible(value) if untrusted else value
        text = secrets.scrub_text(text, report)
        return redact_token_queries(text)
    if isinstance(value, list):
        return [scrub(v, secrets, report, untrusted=untrusted) for v in value]
    if isinstance(value, tuple):
        return [scrub(v, secrets, report, untrusted=untrusted) for v in value]
    if isinstance(value, dict):
        return {scrub(str(k), secrets, report, untrusted=untrusted): scrub(v, secrets, report, untrusted=untrusted)
                for k, v in value.items()}
    return value


# ── Loading the known secrets ──────────────────────────────────────────────────


def _cookie_values() -> tuple[str, ...]:
    """Values of every stored Procore jar (the `procore` row for
    app.procore.com and each `procore:<host>` row, such as us02), cached a
    minute per process."""
    global _cookie_cache
    now = time.monotonic()
    if _cookie_cache is not None and now - _cookie_cache[0] < COOKIE_CACHE_SECONDS:
        return _cookie_cache[1]
    values: tuple[str, ...] = ()
    try:
        rows = (
            supabase_client.get_supabase().table("rfp_harvest_sessions").select("provider, cookies")
            .like("provider", "procore%").execute()
        ).data or []
        found: dict[str, None] = {}
        for row in rows:
            provider = str(row.get("provider") or "")
            if provider != "procore" and not provider.startswith("procore:"):
                continue
            for c in row.get("cookies") or []:
                if isinstance(c, dict) and c.get("value"):
                    found[str(c["value"])] = None
        values = tuple(found)
    except Exception:  # noqa: BLE001 - redaction falls back to the other secrets
        logger.warning("browser agents: stored cookie jar unreadable for redaction")
    _cookie_cache = (now, values)
    return values


def _helper_secret_hmacs(job_id: str) -> set[str]:
    """secret_hmac of every helper secret message on a help request this
    job waits (or waited) on."""
    sb = supabase_client.get_supabase()
    waiters = sb.table("browser_agent_help_waiters").select("help_id").eq("job_id", job_id).execute().data or []
    help_ids = sorted({str(w["help_id"]) for w in waiters if w.get("help_id")})
    if not help_ids:
        return set()
    rows = (
        sb.table("browser_agent_help_messages").select("secret_hmac, is_secret")
        .in_("help_id", help_ids).eq("is_secret", True).execute()
    ).data or []
    return {str(r["secret_hmac"]) for r in rows if r.get("secret_hmac")}


def load_secrets(job_id: str, *, worker_token: str | None) -> SecretSet:
    """Every known secret for one turn of `job_id`."""
    settings = get_settings()
    secrets = SecretSet()
    secrets.add("procore_password", settings.procore_login_password, severe=True)
    with _sources_lock:
        sources = list(_sources)
    for fn in sources:
        try:
            for value in fn() or ():
                secrets.add("procore_password", value, severe=True)
        except Exception:  # noqa: BLE001 - one broken source must not disable the rest
            logger.exception("browser agents: secret source failed")
    secrets.add("worker_token", worker_token, severe=True)
    for value in _cookie_values():
        secrets.add("cookie", value, severe=False, min_len=MIN_COOKIE_LEN)
    try:
        secrets.hmacs = _helper_secret_hmacs(job_id)
    except Exception:  # noqa: BLE001
        logger.exception("browser agents: helper secret hmacs unreadable")
    if secrets.hmacs:
        try:
            secrets.hmac_key = base64.b64decode(settings.browser_agent_fingerprint_key or "")
        except (binascii.Error, ValueError):
            secrets.hmacs = set()
    return secrets

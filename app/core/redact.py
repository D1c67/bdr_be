"""URL redaction for logs and stored error text.

Upstream URLs can carry credentials: Graph upload sessions embed a
pre-authenticated `tempauth` token in the query string, share links carry
access keys, and a URL can hold `user:password@`. Anything that writes an
upstream URL, or an exception message that quotes one (httpx does: "... for
url 'https://host/path?query'"), to a log line or to an error column goes
through these helpers first. They keep scheme, host, port and path, which is
enough to tell which call failed, and drop userinfo, query and fragment.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit

# A scheme followed by `://` and everything up to whitespace or a quote or
# angle bracket (httpx quotes the URL in single quotes).
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")


def redact_url(url: object) -> str:
    """`url` with userinfo, query string and fragment removed."""
    text = str(url or "")
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "<redacted url>"
    if not parts.scheme or not host:
        # Not an absolute URL: keep only what precedes a query or fragment.
        return re.split(r"[?#]", text, maxsplit=1)[0]
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def redact_text(text: object) -> str:
    """`text` with every URL inside it passed through `redact_url`."""
    return _URL_RE.sub(lambda m: redact_url(m.group(0)), str(text or ""))


# ── Global httpx log filter (browser agents, SPEC.md 1 "Logging") ─────────────
#
# httpx logs every request at INFO as 'HTTP Request: GET <url> "HTTP/1.1 200"'
# with the full URL, query string included (signed storage links, Procore
# attachment signatures, a provider key passed as ?key=). The filter rewrites
# each record from these loggers through redact_text before any handler sees
# it. Installed once from the main.py lifespan (and by the worker's own
# __main__); installing twice is a no-op.

_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "openai",
    "openai._base_client",
    "anthropic",
    "anthropic._base_client",
    "google_genai",
    "google_genai._api_client",
)


def _redact_log_url(url: str) -> str:
    """redact_url, but only when the URL carries something to drop (query,
    fragment or userinfo): a URL another filter already cut down (SmartBid's
    "...?<redacted>") is left exactly as it is."""
    try:
        parts = urlsplit(url)
        has_secret_part = bool(parts.query or parts.fragment or parts.username or parts.password)
    except ValueError:
        return "<redacted url>"
    return redact_url(url) if has_secret_part else url


class RedactUrlFilter(logging.Filter):
    """Replaces a record's message with its URL-redacted rendering."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string still gets logged, redacted
            message = str(record.msg)
        redacted = _URL_RE.sub(lambda m: _redact_log_url(m.group(0)), message)
        if redacted != message or record.args:
            record.msg = redacted
            record.args = None
        return True


_installed_filter: RedactUrlFilter | None = None


def install_httpx_filter() -> RedactUrlFilter:
    """Attach one RedactUrlFilter to every HTTP client logger. Idempotent."""
    global _installed_filter
    if _installed_filter is None:
        _installed_filter = RedactUrlFilter()
    for name in _HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if _installed_filter not in logger.filters:
            logger.addFilter(_installed_filter)
    return _installed_filter

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

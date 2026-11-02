"""Phase 5 outbound destination policy, independent of conversation taint.

Only pass the current and previous user messages, in chronological order.
Trusted addresses must be resolved server-side from active internal profiles
or explicit GC/vendor contact IDs. Never pass model output or mail results as
trusted input. These pure helpers do not send, fetch, render HTML, or authorize
network destinations. URL fetches still need the vetted API's SSRF controls.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

# ASCII mailbox syntax intentionally excludes display names and address lists.
_ADDRESS = re.compile(
    r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[a-zA-Z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,63}"
)
_ADDRESS_TOKEN = re.compile(r"(?<![\w.!#$%&'*+/=?^`{|}~@-])(" + _ADDRESS.pattern
                            + r")(?![\w@-]|\.[\w.-])")
_FORWARD = re.compile(
    r"^\s*(?:-+\s*(?:original|forwarded) message\s*-+|"
    r"begin forwarded message:|on .+wrote:|"
    r"(?:from|sent|to|cc|bcc|subject|date):)", re.IGNORECASE,
)
_URL_TOKEN = re.compile(r"https?://[^\s<>\"`]+", re.IGNORECASE)


def typed_texts(user_texts: Iterable[str]) -> tuple[str, ...]:
    """Keep two recent user messages, excluding recognizable quoted content.

    Forward headers end the trusted part of a message. Blockquotes, fenced
    snippets and HTML are data, not typed destinations. This cannot identify
    an unmarked paste; callers must also exclude structured pasted/attached
    content, and approval is still required for every send.
    """
    out = []
    for text in list(user_texts)[-2:]:
        lines = []
        fence = None
        for line in str(text).splitlines():
            stripped = line.lstrip()
            if _FORWARD.match(line):
                break
            if stripped.startswith(("```", "~~~")):
                marker = stripped[:3]
                if fence is None:
                    fence = marker
                elif marker == fence:
                    fence = None
                continue
            if fence or stripped.startswith(">") or line.startswith(("    ", "\t")):
                continue
            # HTML pasted into a prompt is not an authenticated composition.
            if re.search(r"</?[a-zA-Z][^>]*>|<!|<\?", line):
                break
            lines.append(line)
        out.append("\n".join(lines))
    return tuple(out)


def normalize_address(value: str) -> str | None:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    value = value.strip(" ")
    if len(value) > 254 or not _ADDRESS.fullmatch(value):
        return None
    local, _domain = value.rsplit("@", 1)
    return value.casefold() if len(local) <= 64 else None


def bind_recipients(
    candidates: Iterable[str], *, allowed_emails: Iterable[str], user_texts: Iterable[str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Return normalized, deduplicated recipients and explicit refusals.

    Apply to To and Cc independently before hashing the approval payload,
    whether or not the conversation is tainted. Reject display-name strings,
    header injection and substring matches against a different address.
    """
    allowed = {address for value in allowed_emails
               if (address := normalize_address(value)) is not None}
    for text in typed_texts(user_texts):
        for match in _ADDRESS_TOKEN.finditer(text):
            if (address := normalize_address(match.group(1))) is not None:
                allowed.add(address)
    kept, stripped = [], []
    for candidate in candidates:
        address = normalize_address(candidate)
        if address is not None and address in allowed:
            if address not in kept:
                kept.append(address)
        else:
            stripped.append({"field": str(candidate), "reason": "recipient_not_bound"})
    return kept, stripped


def normalize_url(value: str) -> str | None:
    """Exact HTTP(S) destination identity; path/query case stays significant."""
    value = str(value)
    if not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme.casefold() not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return None
        # Access validates malformed ports. Do not normalize different ports,
        # paths, percent encodings or fragments into a trusted destination.
        _ = parts.port
        return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                           parts.path, parts.query, parts.fragment))
    except ValueError:
        return None


def url_is_bound(
    candidate: str, *, user_texts: Iterable[str], signed_urls: Iterable[str] = (),
) -> bool:
    """Bind an href or fetch URL to exact typed text or a server-minted link.

    signed_urls must contain complete links minted for this user and action,
    never an allowed host or a URL supplied by the model. A link inside mail,
    a forward or a code block cannot authorize a fetch or an outgoing href.
    """
    destination = normalize_url(candidate)
    if destination is None:
        return False
    allowed = {url for value in signed_urls if (url := normalize_url(value)) is not None}
    for text in typed_texts(user_texts):
        for match in _URL_TOKEN.finditer(text):
            # Natural-language punctuation is not a URL suffix. Exact URLs
            # with these suffixes can still be supplied as server-minted links.
            value = match.group().rstrip(".,;!?)']}")
            if (url := normalize_url(value)) is not None:
                allowed.add(url)
    return destination in allowed

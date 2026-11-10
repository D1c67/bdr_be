"""Owner/job-scoped matching for consumed verification codes, and URL
redaction for stored browser traces (docs/ASSISTANT_BROWSER_V2_PLAN.md 8.1,
10.7).

Codes: a consumed code leaves only an owner/job-scoped HMAC. The HMAC is over
`normalize_code(code)` (separators removed, upper case), so a digits-only
code hashes exactly as it did before v2 and an alphanumeric code matches in
any letter case.

URLs: every stored or traced URL keeps its origin and path only. Query,
fragment and userinfo are removed, and a path segment that looks like a
token (20 or more characters of base64url or hex) becomes `:token`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from urllib.parse import unquote, urlsplit

from app.services.browser_agents.redaction import REDACTED

_DIGITS = re.compile(r"[0-9]{4,12}")
# A whole alphanumeric token (not inside a longer run) of a code's length.
_ALNUM = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{4,12}(?![A-Za-z0-9])")
_CODE = re.compile(r"^(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{4,12}$")
_SEPARATORS = re.compile(r"[ \-]")
_URL_CODE = re.compile(r"(?:%3[0-9]|[0-9]){4,12}", re.IGNORECASE)
_BASE64_CODE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{6,16}={0,2}(?![A-Za-z0-9+/_=-])")

# 10.7: URLs inside free text, and path segments that look like tokens.
_URL_IN_TEXT = re.compile(r"(?i)\bhttps?://[^\s\"'<>\\]+")
_TOKEN_SEGMENT = re.compile(r"^(?:[A-Za-z0-9_-]{20,}|[0-9a-fA-F]{20,})$")
_WORDY_SEGMENT = re.compile(r"^[a-z]+(?:[-_][a-z]+)+$")
TOKEN_PLACEHOLDER = ":token"


def normalize_code(value: str) -> str:
    """The form a code is hashed in: single spaces and hyphens removed,
    upper case. Raises nothing; the caller checks the shape."""
    return _SEPARATORS.sub("", value or "").upper()


def is_code(value: str) -> bool:
    """4 to 12 letters and digits with at least one digit (8.1), after
    normalize_code."""
    return bool(_CODE.fullmatch(normalize_code(value)))


def code_digest(key: bytes, code: str) -> str:
    """The durable HMAC of one consumed code."""
    return hmac.new(key, normalize_code(code).encode("ascii"), hashlib.sha256).hexdigest()


def scrub_codes(payload, key: bytes, hashes):
    """Compare bounded candidates to durable hashes without retaining codes.

    Exact numeric candidates also match within labels such as otp012345.
    Alphanumeric candidates match as whole tokens in any letter case.
    Encoded candidates are bounded to the supported 4 to 12 character codes;
    unmatched project IDs and ordinary base64 remain intact.
    """
    expected = tuple(value for value in hashes if isinstance(value, str) and len(value) == 64)
    if not expected:
        return payload

    def matches(value: str) -> bool:
        if not _CODE.fullmatch(value):
            return False
        digest = code_digest(key, value)
        return any(hmac.compare_digest(digest, target) for target in expected)

    def encoded(match):
        candidate = match.group(0)
        if "%" in candidate:
            decoded = unquote(candidate)
        else:
            try:
                decoded = base64.b64decode(candidate + "=" * (-len(candidate) % 4), altchars=b"-_", validate=True).decode("ascii")
            except (ValueError, UnicodeError):
                return candidate
        return REDACTED if matches(decoded) else candidate

    def visit(value):
        if isinstance(value, str):
            value = _URL_CODE.sub(lambda m: encoded(m) if "%" in m.group(0) else m.group(0), value)
            value = _BASE64_CODE.sub(encoded, value)
            value = _DIGITS.sub(lambda m: REDACTED if matches(m.group(0)) else m.group(0), value)
            return _ALNUM.sub(lambda m: REDACTED if matches(m.group(0)) else m.group(0), value)
        if isinstance(value, int) and not isinstance(value, bool):
            return REDACTED if matches(str(value)) else value
        if isinstance(value, dict):
            return {visit(str(k)): visit(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [visit(item) for item in value]
        return value

    return visit(payload)


# ── 10.7 URL redaction ───────────────────────────────────────────────────────


def _segment(value: str) -> str:
    if _TOKEN_SEGMENT.fullmatch(value) and not _WORDY_SEGMENT.fullmatch(value):
        return TOKEN_PLACEHOLDER
    return value


def redact_url(url: str) -> str:
    """origin + path of an http(s) URL; query, fragment and userinfo are
    dropped and token-like path segments become :token. Anything that is not
    an http(s) URL with a host comes back unchanged."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return url
    origin = f"{scheme}://{host}"
    if port is not None and (scheme, port) not in (("https", 443), ("http", 80)):
        origin += f":{port}"
    path = "/".join(_segment(part) for part in (parts.path or "").split("/"))
    return origin + (path or "")


def redact_urls(payload):
    """Deep copy of a JSON-like value with every URL inside a string (and
    every string that is a URL) reduced by redact_url. Dict keys are kept."""
    if isinstance(payload, str):
        return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), payload)
    if isinstance(payload, dict):
        return {key: redact_urls(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [redact_urls(item) for item in payload]
    return payload

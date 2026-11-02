"""Owner/job-scoped matching for consumed numeric verification codes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from urllib.parse import unquote

from app.services.browser_agents.redaction import REDACTED

_DIGITS = re.compile(r"[0-9]{4,12}")
_URL_CODE = re.compile(r"(?:%3[0-9]|[0-9]){4,12}", re.IGNORECASE)
_BASE64_CODE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{6,16}={0,2}(?![A-Za-z0-9+/_=-])")


def scrub_codes(payload, key: bytes, hashes):
    """Compare bounded candidates to durable hashes without retaining codes.

    Exact numeric candidates also match within labels such as otp012345.
    Encoded candidates are bounded to the supported 4 to 12 digit codes;
    unmatched project IDs and ordinary base64 remain intact.
    """
    expected = tuple(value for value in hashes if isinstance(value, str) and len(value) == 64)
    if not expected:
        return payload

    def matches(value: str) -> bool:
        if not _DIGITS.fullmatch(value):
            return False
        digest = hmac.new(key, value.encode("ascii"), hashlib.sha256).hexdigest()
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
            return _DIGITS.sub(lambda m: REDACTED if matches(m.group(0)) else m.group(0), value)
        if isinstance(value, int) and not isinstance(value, bool):
            return REDACTED if matches(str(value)) else value
        if isinstance(value, dict):
            return {visit(str(k)): visit(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [visit(item) for item in value]
        return value

    return visit(payload)

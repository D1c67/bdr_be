"""Exact-value secret scrubbing on the worker side (O5).

Every known live secret (worker token, login password and email, consumed
helper secrets, cookie values) is registered here and removed from anything
that is logged, spooled, posted as a step or sent to BDR as a model
observation: in plain, URL-encoded and base64 forms. URL query strings are
stripped from step URLs (they can carry signed tokens).

Secrets learned for one job (the password a login/begin released, an MFA
code, the job's cookie values) are registered with `owner=<job id>` and
dropped when that job ends (`discard_owner`), so the worker keeps them in
memory for scrubbing only, for the life of the job (spec 4.2). Process-wide
secrets (the worker token) have no owner and live as long as the process.

No n-gram tripwire in v1 (O5).
"""

import base64
import threading
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

MASK = "[redacted]"
MIN_SECRET_LEN = 6   # shorter values would mangle ordinary text


def _variants(secret: str) -> set[str]:
    raw = secret.encode("utf-8")
    out = {secret, quote(secret, safe=""), quote_plus(secret)}
    for enc in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        text = enc.decode("ascii")
        out.add(text)
        out.add(text.rstrip("="))
    return {v for v in out if len(v) >= MIN_SECRET_LEN}


class LiveSecrets:
    """Thread-safe registry; the heartbeat thread scrubs too.

    Every value is scrubbed from steps, logs and summaries. Only `tripwire`
    values (real credentials: the password, helper codes, session cookies,
    the worker token) make `contains` true, which fails a job when one
    reaches a model observation. Identifying but non-secret values (the
    login email, ordinary cookie values) are scrub-only: page text that
    shows them is normal and must not kill the job."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_owner: dict[str | None, set[str]] = {}
        self._trip_by_owner: dict[str | None, set[str]] = {}
        self._values: list[str] = []
        self._trip_values: list[str] = []

    def _rebuild_locked(self) -> None:
        merged: set[str] = set()
        for values in self._by_owner.values():
            merged |= values
        trip: set[str] = set()
        for values in self._trip_by_owner.values():
            trip |= values
        # Longest first so a value never leaves a partial tail behind.
        self._values = sorted(merged, key=len, reverse=True)
        self._trip_values = sorted(trip, key=len, reverse=True)

    def add(self, secret: str | None, *, owner: str | None = None, tripwire: bool = True) -> None:
        if not secret or len(secret) < MIN_SECRET_LEN:
            return
        variants = _variants(secret)
        with self._lock:
            self._by_owner.setdefault(owner, set()).update(variants)
            if tripwire:
                self._trip_by_owner.setdefault(owner, set()).update(variants)
            self._rebuild_locked()

    def discard_owner(self, owner: str) -> None:
        """Forget every secret registered for one job (called when it ends)."""
        with self._lock:
            had = self._by_owner.pop(owner, None) is not None
            had = (self._trip_by_owner.pop(owner, None) is not None) or had
            if had:
                self._rebuild_locked()

    def discard_all(self) -> None:
        with self._lock:
            self._by_owner = {}
            self._trip_by_owner = {}
            self._values = []
            self._trip_values = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def contains(self, text: str | None) -> bool:
        """True when `text` carries any live tripwire secret in any registered form."""
        if not text:
            return False
        with self._lock:
            values = list(self._trip_values)
        return any(v in text for v in values)

    def scrub_text(self, text: str) -> str:
        with self._lock:
            values = list(self._values)
        for v in values:
            if v in text:
                text = text.replace(v, MASK)
        return text

    def scrub(self, obj: Any) -> Any:
        """Deep-scrub str values (and dict keys) of JSON-like data."""
        if isinstance(obj, str):
            return self.scrub_text(obj)
        if isinstance(obj, dict):
            return {self.scrub_text(k) if isinstance(k, str) else k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        return obj


def strip_query(url: str | None) -> str | None:
    """Drop query and fragment from a URL (step url_before/url_after)."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme:
        return url.split("?", 1)[0].split("#", 1)[0]
    netloc = parts.netloc.rsplit("@", 1)[-1]   # never keep user:pass@
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))

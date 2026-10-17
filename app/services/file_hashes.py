"""SHA-256 of stored project files, filled lazily (docs/PROJECT_MERGE.md 4.4).

Merge Projects decides "the other project already has this document" by the
SHA-256 of the bytes. `project_files.sha256` (migration 0149) caches it: a
row is hashed once, the first time a comparison needs it, and every later
comparison reads the column.

The bytes are streamed through a short-lived signed URL with httpx, 1 MB at
a time, so a 450 MB drawing set never sits in memory (`storage.download_file`
returns the whole object as bytes and is not used here).

`fill` hashes a list of rows within a time budget checked between files:
each call does what fits and stops, the hashes persist, and the next call
resumes where it stopped. A row whose object is missing (or cannot be read)
gets no hash and is reported, never retried within the same call.

Event-loop rule: everything here is sync; callers are plain `def` handlers.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from storage3.exceptions import StorageApiError

from app.services import storage

logger = logging.getLogger(__name__)

TABLE = "project_files"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# One streamed GET per object, on a client of its own (the SDK's pooled one
# must not be held for minutes by a large object).
HASH_CHUNK = 1024 * 1024
SIGNED_URL_TTL_S = 600
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0)
_ATTEMPTS = 2
_ERROR_BODY_CAP = 64 * 1024


class ObjectMissing(Exception):
    """The stored object does not exist (storage answered not found)."""


class ObjectUnreadable(Exception):
    """The object could not be read for another reason (an HTTP error, a
    connection that kept dropping)."""


def _not_found_text(text: str) -> bool:
    low = (text or "").lower()
    return "not_found" in low or "not found" in low or '"404"' in low


def storage_error_missing(exc: Exception) -> bool:
    """storage-api's "Object not found" on signing: HTTP 404, or 400 with a
    not-found body (older releases)."""
    status_code = str(getattr(exc, "status", "") or getattr(exc, "status_code", "") or "")
    if status_code == "404":
        return True
    if str(getattr(exc, "code", "") or "").lower() in ("not_found", "nosuchkey"):
        return True
    message = str(getattr(exc, "message", "") or exc)
    return status_code in ("", "400") and _not_found_text(message)


def _response_missing(resp: httpx.Response) -> bool:
    if resp.status_code == 404:
        return True
    if resp.status_code != 400:
        return False
    try:
        body = resp.read()[:_ERROR_BODY_CAP].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - an unreadable error body proves nothing
        return False
    return _not_found_text(body)


def _client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT, follow_redirects=False)


def sha256_of_object(path: str, *, client_factory: Callable[[], httpx.Client] = _client) -> str:
    """The lowercase hex SHA-256 of the object at `path`, streamed in 1 MB
    chunks. Raises ObjectMissing when storage says the object is not there,
    ObjectUnreadable for any other failure (a dropped connection is retried
    once on a fresh client first)."""
    if not path:
        raise ObjectMissing("no storage path")
    try:
        url = storage.signed_url(path, SIGNED_URL_TTL_S)
    except StorageApiError as exc:
        if storage_error_missing(exc):
            raise ObjectMissing(path) from exc
        raise ObjectUnreadable(str(exc)) from exc
    for attempt in range(1, _ATTEMPTS + 1):
        digest = hashlib.sha256()
        try:
            with client_factory() as client, client.stream("GET", url) as resp:
                if _response_missing(resp):
                    raise ObjectMissing(path)
                if not resp.is_success:
                    raise ObjectUnreadable(f"storage returned HTTP {resp.status_code}")
                for chunk in resp.iter_bytes(HASH_CHUNK):
                    digest.update(chunk)
            return digest.hexdigest()
        except httpx.TransportError as exc:
            if attempt == _ATTEMPTS:
                raise ObjectUnreadable(f"connection failed: {exc}") from exc
            logger.warning("file hash: stream of %s dropped (attempt %d); retrying", path, attempt)
    raise AssertionError("unreachable")


@dataclass
class HashRun:
    """What one `fill` call did: `hashed` {file_id: sha256} newly stored,
    `missing` / `unreadable` the ids that could not be hashed this call, and
    `remaining` the ids the budget did not reach."""

    hashed: dict[str, str] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    unreadable: set[str] = field(default_factory=set)
    remaining: list[str] = field(default_factory=list)

    @property
    def failed(self) -> set[str]:
        return self.missing | self.unreadable


def needs_hash(row: dict) -> bool:
    return not (isinstance(row.get("sha256"), str) and SHA256_RE.match(row["sha256"]))


def fill(
    sb,
    rows: list[dict],
    *,
    budget_s: float,
    clock: Callable[[], float] = time.monotonic,
    hasher: Callable[[str], str] | None = None,
) -> HashRun:
    """Hash every row of `rows` that has no `sha256` yet, in order, until the
    budget runs out (checked BETWEEN files: the file in progress always
    finishes, so one call may overrun by one file, and the first file is
    always attempted, so every call makes progress). Each hash is written
    back conditionally (`sha256 is null`, so a concurrent writer of the same
    value is harmless) and set on the row dict in place. Never raises for a
    single file: a missing or unreadable object is recorded and the run moves
    on."""
    hash_one = hasher or sha256_of_object
    run = HashRun()
    deadline = clock() + max(float(budget_s), 0.0)
    attempted = False
    for row in rows:
        if not needs_hash(row):
            continue
        file_id = str(row.get("id"))
        # The first file is always attempted, so every call makes progress.
        if attempted and clock() >= deadline:
            run.remaining.append(file_id)
            continue
        attempted = True
        try:
            digest = hash_one(row.get("storage_path") or "")
        except ObjectMissing:
            run.missing.add(file_id)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad object never stops the run
            logger.warning("file hash: %s could not be read: %s", file_id, exc)
            run.unreadable.add(file_id)
            continue
        row["sha256"] = digest
        run.hashed[file_id] = digest
        try:
            sb.table(TABLE).update({"sha256": digest}).eq("id", file_id).is_("sha256", "null").execute()
        except Exception:  # noqa: BLE001 - the hash is still good for this call
            logger.exception("file hash: could not store the hash of %s", file_id)
    return run

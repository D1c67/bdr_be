"""Supabase Storage for the RFP Ingestion sandbox: two private buckets, the
path builders, and the transfer helpers the runner and the router share.

Why this is not storage.py: that module closes over ONE bucket
(`project-files`), keys its signed-URL memo by path alone, and walks prefixes
exactly two levels deep. The sandbox needs two buckets with opposite rules,
paths three and four levels deep, and one bucket that must never be signed at
all. Mixing that into storage.py (a bucket keyword on every helper plus a
re-keyed cache) was judged riskier than a parallel module, so nothing here
touches storage.py's cache or constants and nothing there knows about these
buckets (migration 0119 creates them).

Buckets (both private, no storage policies, service-role only):

- QUARANTINE_BUCKET `rfp-quarantine`: the raw, attacker-controlled bytes at
  `{run_id}/{file_id}/source.<ext>`, where `<ext>` is the file's
  `source_format` (`pdf`, or one of the office formats of section 2.1).
  Written once, read only by the runner (streamed to scratch disk). There is
  deliberately NO way to mint a signed URL for it: `signed_url` raises for
  any bucket but the derived one.
- DERIVED_BUCKET `rfp-derived`: what the sandbox produced and the parent
  re-validated, under `{run_id}/{file_id}/`: `thumb/NNNN.jpg`,
  `full/NNNN.jpg`, `text.json`, `manifest.json`, `images-NNN.pdf`, and for
  an office file `converted.pdf`, the converter's output that the child
  was then run over (kept across a re-run so the file is not converted
  twice; `delete_prefix(..., keep=...)` is how the re-run cleanup spares
  it). Page names come from `protocol.page_file`, the same function the
  runner uses to derive output names, so a path here is never built from
  anything the child wrote.

Transfers:

- `upload_bytes` / `upload_file` upsert (keys are deterministic, so a retry
  after a dropped connection must not 400 on "exists") and retry
  `httpx.TransportError` three times with a `2 s x attempt` backoff, the same
  policy as storage.download_file (large TLS transfers have died mid-stream
  in prod). HTTP-level errors from the SDK are raised as-is on the first
  attempt: they are not transient.
- `upload_many` fans page objects over a small ThreadPoolExecutor on the
  shared HTTP/1.1 client (0.25 s per object sequentially, measured); every
  item is attempted and the FIRST failure in item order is raised afterwards.
- `download_to_file` streams the Storage REST object endpoint with the
  service headers on its own short-lived client (the SDK's download buffers
  the whole object in memory), writing 1 MB chunks into a file opened
  O_EXCL, with a running byte cap. The partial file is removed on every
  failure so a retry's O_EXCL open succeeds.
- `delete_prefix` is a RECURSIVE list-and-remove (folders come back from the
  list API as entries with a null `id`), because the derived layout is three
  and four levels deep and the two-level walkers in storage.py would leave
  orphans behind.
- `signed_url` memoizes per (bucket, path, download) with the same 60 s
  refresh margin as storage.py, in its own dict.

Every error message raised from here is app-authored (a status code at most,
never a response body, an object path or an exception text), because the
runner records some of them in `rfp_ingest_files.error`.

Sync module: callers run it in the queue worker thread or under
`run_in_threadpool`, never directly inside `async def`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.sandbox import protocol

logger = logging.getLogger(__name__)

QUARANTINE_BUCKET = "rfp-quarantine"
DERIVED_BUCKET = "rfp-derived"
BUCKETS = frozenset({QUARANTINE_BUCKET, DERIVED_BUCKET})

QUARANTINE_OBJECT = "source.pdf"                 # the PDF case of QUARANTINE_TEMPLATE
QUARANTINE_TEMPLATE = "source.{ext}"
CONVERTED_OBJECT = "converted.pdf"
TEXT_OBJECT = "text.json"
MANIFEST_OBJECT = "manifest.json"
IMAGES_PDF_TEMPLATE = "images-{part:03d}.pdf"

# Content type recorded on the quarantine object per source format. The
# converter is not told these (it goes by the extension of the part name).
SOURCE_CONTENT_TYPES: dict[str, str] = {
    protocol.SOURCE_FORMAT_PDF: "application/pdf",
    protocol.SOURCE_FORMAT_DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    protocol.SOURCE_FORMAT_XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    protocol.SOURCE_FORMAT_DOC: "application/msword",
    protocol.SOURCE_FORMAT_XLS: "application/vnd.ms-excel",
}

# Retry policy for transfers (mirrors storage.download_file).
_TRANSFER_ATTEMPTS = 3
_TRANSFER_BACKOFF_S = 2.0
_DOWNLOAD_CHUNK = 1024 * 1024
# The download client is short-lived and separate from the SDK's shared one:
# a 300 MB stream must not hold a pooled connection for minutes, and the
# stream API needs the response object rather than the SDK's bytes.
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0)
# Bounded read of an error body (only to recognise "object not found").
_ERROR_BODY_CAP = 64 * 1024

_LIST_PAGE = 1000
_REMOVE_BATCH = 500
# The derived layout is 4 levels deep at most; anything deeper is not ours.
_MAX_LIST_DEPTH = 8

# Signed-URL memo, keyed by (bucket, path, download). Separate from
# storage.py's on purpose (that one is keyed by path alone).
_REFRESH_MARGIN_S = 60
_CACHE_SWEEP_SIZE = 500
_signed_url_cache: dict[tuple[str, str, str | None], tuple[str, float]] = {}

# Indirections so tests can freeze time and skip the backoff without
# patching the global `time` module.
_now = time.time
_sleep = time.sleep

# Segments are uuids from our own rows; anything else is refused before it
# can become part of an object key.
_SEGMENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


class RfpStorageError(RuntimeError):
    """A Storage operation failed. The message is app-authored and safe to
    surface; the runner maps it to `failed/storage`."""


class RfpStorageNotFound(RfpStorageError):
    """The object does not exist (the quarantine copy was pruned or never
    landed)."""


class RfpStorageTooLarge(RfpStorageError):
    """The object exceeded the caller's byte cap while streaming; the partial
    file was removed."""


# ── Path builders ────────────────────────────────────────────────────────


def _segment(value: str, what: str) -> str:
    if not isinstance(value, str) or not value or any(ch not in _SEGMENT_CHARS for ch in value):
        raise ValueError(f"{what} must be a single safe path segment")
    return value


def _index(index: int) -> int:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("page index must be a non-negative int")
    return index


def _file_prefix(run_id: str, file_id: str) -> str:
    return f"{_segment(run_id, 'run_id')}/{_segment(file_id, 'file_id')}"


def _source_format(value: str) -> str:
    if value not in protocol.SOURCE_FORMATS:
        raise ValueError("source_format must be one of the sandbox's source formats")
    return value


def quarantine_path(run_id: str, file_id: str, source_format: str = protocol.SOURCE_FORMAT_PDF) -> str:
    """`{run_id}/{file_id}/source.<ext>` in QUARANTINE_BUCKET; the extension
    is the file's `source_format` (`pdf` by default) and nothing else is
    accepted, so the object name can never come from the declared
    filename."""
    ext = _source_format(source_format)
    return f"{_file_prefix(run_id, file_id)}/{QUARANTINE_TEMPLATE.format(ext=ext)}"


def source_content_type(source_format: str) -> str:
    """The content type the quarantine object is stored with."""
    return SOURCE_CONTENT_TYPES[_source_format(source_format)]


def converted_path(run_id: str, file_id: str) -> str:
    """`{run_id}/{file_id}/converted.pdf` in DERIVED_BUCKET: the PDF the
    converter produced from an office file (section 2.1)."""
    return f"{_file_prefix(run_id, file_id)}/{CONVERTED_OBJECT}"


def derived_prefix(run_id: str, file_id: str) -> str:
    """`{run_id}/{file_id}`: the prefix every derived object of a file lives
    under in DERIVED_BUCKET, and what `delete_prefix` sweeps."""
    return _file_prefix(run_id, file_id)


def thumb_path(run_id: str, file_id: str, index: int) -> str:
    """`{prefix}/thumb/NNNN.jpg` (classification tier), name derived from
    the page index exactly as the runner derives the child's output name."""
    name = protocol.page_file(protocol.THUMB_DIR, _index(index), "jpg")
    return f"{_file_prefix(run_id, file_id)}/{name}"


def full_path(run_id: str, file_id: str, index: int) -> str:
    """`{prefix}/full/NNNN.jpg` (reading tier)."""
    name = protocol.page_file(protocol.FULL_DIR, _index(index), "jpg")
    return f"{_file_prefix(run_id, file_id)}/{name}"


def text_path(run_id: str, file_id: str) -> str:
    return f"{_file_prefix(run_id, file_id)}/{TEXT_OBJECT}"


def manifest_path(run_id: str, file_id: str) -> str:
    return f"{_file_prefix(run_id, file_id)}/{MANIFEST_OBJECT}"


def images_pdf_path(run_id: str, file_id: str, part: int) -> str:
    """`{prefix}/images-NNN.pdf`; `part` is 1-based (the streaming writer
    splits the thumb-tier PDF at the configured part cap)."""
    if isinstance(part, bool) or not isinstance(part, int) or part < 1:
        raise ValueError("images PDF part must be a positive int")
    return f"{_file_prefix(run_id, file_id)}/{IMAGES_PDF_TEMPLATE.format(part=part)}"


# ── Uploads ──────────────────────────────────────────────────────────────


def _store(bucket: str):
    if bucket not in BUCKETS:
        raise ValueError("not an RFP ingestion bucket")
    return get_supabase().storage.from_(bucket)


def _with_transfer_retry(op: Callable[[], None], what: str) -> None:
    """Run `op`, retrying only on httpx.TransportError (a dropped connection;
    httpx discards it and the retry gets a fresh one). Any other error is
    raised on the first attempt: HTTP-level failures are not transient."""
    for attempt in range(1, _TRANSFER_ATTEMPTS + 1):
        try:
            op()
            return
        except httpx.TransportError:
            if attempt == _TRANSFER_ATTEMPTS:
                raise
            logger.warning(
                "rfp storage %s dropped (attempt %d/%d); retrying",
                what,
                attempt,
                _TRANSFER_ATTEMPTS,
            )
            _sleep(_TRANSFER_BACKOFF_S * attempt)
    raise AssertionError("unreachable")


def _upload_options(content_type: str) -> dict:
    # A fresh dict per attempt: the SDK pops keys out of the one it is given.
    return {"content-type": content_type, "upsert": "true"}


def upload_bytes(bucket: str, path: str, data: bytes, content_type: str) -> None:
    """Upsert `data` at `path`. Three attempts on a dropped connection with a
    2 s x attempt backoff; the last error is raised."""
    store = _store(bucket)

    def _op() -> None:
        store.upload(path, data, _upload_options(content_type))

    _with_transfer_retry(_op, "upload")


def upload_file(bucket: str, path: str, file_path: Path | str, content_type: str) -> None:
    """Upsert the file at `file_path`, streamed from disk (the SDK hands an
    open file object to httpx, which streams a multipart body). The file is
    reopened on every attempt so a retry starts from byte 0."""
    store = _store(bucket)
    src = Path(file_path)

    def _op() -> None:
        with open(src, "rb") as fh:
            store.upload(path, fh, _upload_options(content_type))

    _with_transfer_retry(_op, "upload")


def upload_many(bucket: str, items: list[tuple[str, bytes, str]], *, max_workers: int = 4) -> None:
    """Upload `(path, data, content_type)` items in parallel through
    `upload_bytes` (each with its own retry). Every item is attempted; once
    all have completed the first failure in item order is raised, so a
    caller that catches it knows nothing is still in flight."""
    if not items:
        return
    if bucket not in BUCKETS:
        raise ValueError("not an RFP ingestion bucket")
    workers = max(1, min(max_workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rfp-upload") as pool:
        futures = [pool.submit(upload_bytes, bucket, path, data, ct) for path, data, ct in items]
    # The `with` block has joined every future; surface the first failure.
    for future in futures:
        exc = future.exception()
        if exc is not None:
            raise exc


# ── Streaming download ───────────────────────────────────────────────────


def _download_client() -> httpx.Client:
    """Short-lived client for one streamed object GET. Redirects are never
    followed (the object endpoint does not redirect; following one would
    forward the service key)."""
    return httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=False)


def _object_url(bucket: str, path: str) -> str:
    base = get_settings().supabase_url.rstrip("/")
    if not base:
        raise RfpStorageError("storage is not configured")
    return f"{base}/storage/v1/object/{bucket}/{path}"


def _service_headers() -> dict[str, str]:
    key = get_settings().supabase_service_role_key
    if not key:
        raise RfpStorageError("storage is not configured")
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def _read_capped(resp: httpx.Response, cap: int) -> bytes:
    buf = bytearray()
    for chunk in resp.iter_bytes(cap):
        buf += chunk
        if len(buf) >= cap:
            break
    return bytes(buf[:cap])


def _is_not_found(resp: httpx.Response) -> bool:
    """storage-api reports a missing object as HTTP 404 (current) or as HTTP
    400 with `statusCode: "404"` / `error: "not_found"` in the JSON body
    (older releases). Recognise both without trusting anything else."""
    if resp.status_code == 404:
        return True
    if resp.status_code != 400:
        return False
    try:
        body = json.loads(_read_capped(resp, _ERROR_BODY_CAP))
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    return str(body.get("statusCode")) == "404" or body.get("error") == "not_found"


def _open_excl(dest: Path):
    """Create `dest` for writing, refusing an existing path (FileExistsError)
    and never following a symlink at it. Returns a buffered writer."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dest, flags, 0o644)
    try:
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def download_to_file(bucket: str, path: str, dest: Path, *, max_bytes: int) -> int:
    """Stream the object at `bucket/path` into `dest` (opened O_EXCL), 1 MB at
    a time, refusing to write past `max_bytes`. Returns the bytes written.

    Raises RfpStorageNotFound (404), RfpStorageTooLarge (cap exceeded),
    RfpStorageError (any other HTTP status), FileExistsError (`dest` already
    exists; it is left untouched), and after three attempts the last
    httpx.TransportError. On every failure the partial `dest` this call
    created is removed."""
    if bucket not in BUCKETS:
        raise ValueError("not an RFP ingestion bucket")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")
    dest = Path(dest)
    url = _object_url(bucket, path)
    headers = _service_headers()

    for attempt in range(1, _TRANSFER_ATTEMPTS + 1):
        out = _open_excl(dest)
        written = 0
        try:
            with out, _download_client() as client, client.stream(
                "GET", url, headers=headers
            ) as resp:
                if _is_not_found(resp):
                    raise RfpStorageNotFound("The stored file was not found.")
                if not resp.is_success:
                    raise RfpStorageError(f"Storage returned HTTP {resp.status_code}.")
                for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise RfpStorageTooLarge("The stored file is larger than the cap.")
                    out.write(chunk)
            return written
        except httpx.TransportError:
            _unlink_quietly(dest)
            if attempt == _TRANSFER_ATTEMPTS:
                raise
            logger.warning(
                "rfp storage download dropped (attempt %d/%d); retrying",
                attempt,
                _TRANSFER_ATTEMPTS,
            )
            _sleep(_TRANSFER_BACKOFF_S * attempt)
        except BaseException:
            _unlink_quietly(dest)
            raise
    raise AssertionError("unreachable")


# ── Recursive delete ─────────────────────────────────────────────────────


def _list_all(store, path: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        page = store.list(path, {"limit": _LIST_PAGE, "offset": offset}) or []
        out.extend(page)
        if len(page) < _LIST_PAGE:
            return out
        offset += _LIST_PAGE


def _walk(store, path: str, depth: int) -> list[str]:
    """Every object path under `path`, recursing into folder entries (the
    list API returns a folder as an entry with a null `id` and no metadata;
    objects always carry an id)."""
    paths: list[str] = []
    for entry in _list_all(store, path):
        name = entry.get("name")
        if not name:
            continue
        child = f"{path}/{name}"
        if entry.get("id") is None:
            if depth + 1 > _MAX_LIST_DEPTH:
                raise RfpStorageError("The storage prefix is nested deeper than expected.")
            paths.extend(_walk(store, child, depth + 1))
        else:
            paths.append(child)
    return paths


def _forget_signed(bucket: str, paths: set[str]) -> None:
    for key in [k for k in _signed_url_cache if k[0] == bucket and k[1] in paths]:
        _signed_url_cache.pop(key, None)


def delete_prefix(bucket: str, prefix: str, *, keep: tuple[str, ...] = ()) -> int:
    """Remove EVERY object under `prefix` (recursively) and return how many
    were removed. A prefix with nothing under it is fine (returns 0); a
    blank prefix is refused because it would name the whole bucket.
    `keep` names objects DIRECTLY under the prefix that are left in place
    (the re-run cleanup keeps `converted.pdf`, which the next attempt reuses
    rather than converting the office file again).

    Lists objects rather than trusting DB rows: a runner racing a delete can
    land an object after the rows were read, and nothing else would ever
    reclaim it."""
    prefix = (prefix or "").strip().strip("/")
    if not prefix:
        raise ValueError("delete_prefix refuses an empty prefix")
    store = _store(bucket)
    paths = _walk(store, prefix, 0)
    if keep:
        spared = {f"{prefix}/{name}" for name in keep}
        paths = [path for path in paths if path not in spared]
    removed = 0
    for start in range(0, len(paths), _REMOVE_BATCH):
        batch = paths[start : start + _REMOVE_BATCH]
        store.remove(batch)
        removed += len(batch)
    if paths:
        _forget_signed(bucket, set(paths))
    return removed


# ── Signed URLs (derived bucket only) ────────────────────────────────────


def signed_url(bucket: str, path: str, *, download: str | None = None) -> str:
    """Mint (or reuse) a signed URL for a DERIVED object. The quarantine
    bucket is refused outright: nothing ever serves the raw bytes.

    `download` (a filename) makes the object serve with
    `Content-Disposition: attachment`; the memo key includes it, so an inline
    URL and a download URL for the same object are cached separately. TTL is
    settings.signed_url_ttl_seconds; a cached URL is reused until 60 s before
    it expires."""
    if bucket != DERIVED_BUCKET:
        raise ValueError("signed URLs are only issued for the derived bucket")
    ttl = get_settings().signed_url_ttl_seconds
    now = _now()
    key = (bucket, path, download)
    cached = _signed_url_cache.get(key)
    if cached and now < cached[1] - _REFRESH_MARGIN_S:
        return cached[0]

    options = {"download": download} if download else None
    res = get_supabase().storage.from_(bucket).create_signed_url(path, ttl, options)
    url = res.get("signedURL") if isinstance(res, dict) else None
    if not url:
        raise RfpStorageError("Storage did not return a signed URL.")

    if len(_signed_url_cache) > _CACHE_SWEEP_SIZE:
        for k in [k for k, (_, exp) in _signed_url_cache.items() if exp <= now]:
            del _signed_url_cache[k]
    _signed_url_cache[key] = (url, now + ttl)
    return url

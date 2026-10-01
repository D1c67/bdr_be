"""Supabase Storage helpers for project files.

All objects live in the private `project-files` bucket. Downloads are served via
short-TTL signed URLs (critical for the hardened estimator). Object paths are
namespaced by project: `{project_id}/{category}/{uuid}-{filename}`.
"""

import logging
import string
import time
import uuid

import httpx
from storage3.exceptions import StorageApiError

from app.core.config import get_settings
from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

BUCKET = "project-files"

# Large transfers to Supabase Storage occasionally die at the TLS layer (seen
# on a ~180 MB bid-split upload as ReadError "SSLV3_ALERT_BAD_RECORD_MAC");
# httpx discards the broken connection, so a retry gets a fresh one.
_TRANSFER_ATTEMPTS = 3
_TRANSFER_BACKOFF_S = 2.0

# Characters Supabase Storage accepts in object keys (storage-api's isValidKey
# allowlist). Anything else is rejected with a 400 InvalidKey at upload time:
# seen in prod with the "~" in Windows 8.3 short names ("E002-E~1.PDF", which
# Windows substitutes for the real name when a file sits beyond the 260-char
# path limit), and equally true of accented/unicode letters. "/" is excluded
# on purpose: inside a filename it would mint bogus path segments.
_KEY_SAFE_CHARS = frozenset(string.ascii_letters + string.digits + "_!-.*'() &$@=;:+,?")


def safe_key_component(filename: str) -> str:
    """Map a user-supplied filename onto Storage's allowed key charset.

    Only the object key is sanitized; the display name shown to users comes
    from the DB row, which keeps the filename exactly as uploaded. The mapping
    is deterministic (each bad char becomes "_") so callers that rely on
    stable, re-derivable paths (email ingest upserts) stay idempotent.
    """
    return "".join(ch if ch in _KEY_SAFE_CHARS else "_" for ch in filename)

# Signed-URL memoization: a fresh token per request defeats every cache layer
# (browser and Supabase Smart CDN key on the token), so repeat previews would
# re-download the bytes. Reuse the same URL until shortly before it expires.
# GIL-atomic dict ops → no locking (callers now run in the threadpool); a lost
# race just re-mints, and each worker process keeps its own cache.
_REFRESH_MARGIN_S = 60
_CACHE_SWEEP_SIZE = 500
# path -> (url, expires_at_epoch)
_signed_url_cache: dict[str, tuple[str, float]] = {}


def build_object_path(project_id: str, category: str, filename: str) -> str:
    return f"{project_id}/{category}/{uuid.uuid4().hex}-{safe_key_component(filename)}"


def build_submittal_object_path(filename: str) -> str:
    """Object path for a Submittal Bank PDF. The bank is company-global (not
    project-scoped), so these live under a reserved `submittal-bank/` prefix in
    the same private bucket. A file may cover materials across categories, so the
    path is not category-namespaced."""
    return f"submittal-bank/{uuid.uuid4().hex}-{safe_key_component(filename)}"


def _upload_options(content_type: str, upsert: bool) -> dict:
    # A fresh dict per attempt: storage3 pops keys out of the one it is given.
    return {"content-type": content_type, "upsert": "true" if upsert else "false"}


def upload_file(path: str, content: bytes, content_type: str, *, upsert: bool = False) -> None:
    """Upload `content` at `path`. A dropped connection (httpx.TransportError,
    e.g. ReadError / WriteError "[SSL: SSLV3_ALERT_BAD_RECORD_MAC]") is
    retried on a fresh one, up to `_TRANSFER_ATTEMPTS` with the same backoff
    as `download_file`; the last error is raised. storage3 (2.30) does not
    wrap transport errors (its `_request` converts only HTTPStatusError into
    StorageApiError), so httpx.TransportError is what reaches us.

    The first attempt keeps the caller's `upsert`; every retry upserts: the
    failed attempt may have landed the object server side before the
    connection died, and the path is ours (every caller mints it, most with
    a fresh uuid), so overwriting it with the same bytes is the right answer
    where a non-upsert retry would 409 "The resource already exists".

    Only in-memory bytes are retried: a file object would resume from
    wherever the failed attempt left its position."""
    retryable = isinstance(content, (bytes, bytearray, memoryview))
    attempts = _TRANSFER_ATTEMPTS if retryable else 1
    for attempt in range(1, attempts + 1):
        try:
            get_supabase().storage.from_(BUCKET).upload(
                path, content, _upload_options(content_type, upsert or attempt > 1)
            )
            return
        except httpx.TransportError:
            if attempt == attempts:
                raise
            logger.warning(
                "storage upload of %s (%d bytes) dropped (attempt %d/%d); retrying",
                path,
                len(content),
                attempt,
                attempts,
            )
            time.sleep(_TRANSFER_BACKOFF_S * attempt)
    raise AssertionError("unreachable")


def signed_url(
    path: str,
    ttl_seconds: int | None = None,
    *,
    use_cache: bool = True,
    download: str | bool | None = None,
) -> str:
    """Mint (or reuse) a signed URL for `path`.

    Only the default-TTL flow is memoized; explicit TTLs and `use_cache=False`
    always mint fresh (e.g. links embedded in emails must carry the full TTL).

    `download` (a filename, or True) makes the object serve with
    `Content-Disposition: attachment`, so a top-level navigation downloads it
    instead of rendering it inline — defusing a stored HTML/SVG masquerading as
    another type. Download URLs are never cached (they differ from inline URLs).
    """
    ttl = ttl_seconds or get_settings().signed_url_ttl_seconds
    now = time.time()

    cacheable = use_cache and ttl_seconds is None and download is None
    if cacheable:
        cached = _signed_url_cache.get(path)
        if cached and now < cached[1] - _REFRESH_MARGIN_S:
            return cached[0]

    options = {"download": download} if download else None
    res = get_supabase().storage.from_(BUCKET).create_signed_url(path, ttl, options)
    url = res["signedURL"]

    if cacheable:
        if len(_signed_url_cache) > _CACHE_SWEEP_SIZE:
            for k in [k for k, (_, exp) in _signed_url_cache.items() if exp <= now]:
                del _signed_url_cache[k]
        _signed_url_cache[path] = (url, now + ttl)
    return url


def download_file(path: str) -> bytes:
    """Download an object. A download is idempotent, so a dropped connection
    (httpx.TransportError) is retried on a fresh one before giving up."""
    for attempt in range(1, _TRANSFER_ATTEMPTS + 1):
        try:
            return get_supabase().storage.from_(BUCKET).download(path)
        except httpx.TransportError:
            if attempt == _TRANSFER_ATTEMPTS:
                raise
            logger.warning(
                "storage download of %s dropped (attempt %d/%d); retrying",
                path,
                attempt,
                _TRANSFER_ATTEMPTS,
            )
            time.sleep(_TRANSFER_BACKOFF_S * attempt)
    raise AssertionError("unreachable")


def delete_file(path: str) -> None:
    get_supabase().storage.from_(BUCKET).remove([path])
    _signed_url_cache.pop(path, None)


_LIST_PAGE = 1000


def _held_by_deleted_project(project_id: str) -> bool:
    """Whether an open (not restored) deleted_projects archive names this
    project. A failed read counts as held: keeping objects is the safe side."""
    try:
        rows = (
            get_supabase().table("deleted_projects").select("id")
            .eq("project_id", project_id).is_("restored_at", "null").limit(1).execute()
        ).data or []
    except Exception:  # noqa: BLE001
        logger.exception("storage: deleted-project check failed for %s", project_id)
        return True
    return bool(rows)


def delete_project_prefix(project_id: str) -> None:
    """Remove EVERY object under `{project_id}/` — uploads and preview
    derivatives alike.

    Used by the project-discard cleanup, which must not trust project_files
    rows alone: an upload racing the discard can land its object after the rows
    were read (or its row can cascade away with the project), and nothing else
    would ever reclaim the orphan.

    ONLY correct for discard-eligible (early-intake) projects, whose objects
    all live exactly two levels deep: `{project_id}/{category}/{object}` and
    `{project_id}/previews/{file_id}.pdf`. PM-era paths are THREE levels
    (`{project_id}/pm/{category}/{object}`) and would be missed — don't reuse
    this for projects past intake without making the walk recursive.

    Never sweeps a project that sits in the Deleted Projects archive awaiting
    a restore (docs/PROJECT_DELETE.md 8): a delete keeps every object under
    `{project_id}/` so the restore gets its files back, and nothing may reclaim
    them while that archive is open."""
    if _held_by_deleted_project(project_id):
        logger.warning("storage: not sweeping %s, a deleted-project archive holds it", project_id)
        return
    store = get_supabase().storage.from_(BUCKET)

    def _list_all(prefix: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            page = store.list(prefix, {"limit": _LIST_PAGE, "offset": offset}) or []
            out.extend(page)
            if len(page) < _LIST_PAGE:
                return out
            offset += _LIST_PAGE

    paths: list[str] = []
    for folder in _list_all(project_id):
        name = folder.get("name")
        if not name:
            continue
        paths.extend(
            f"{project_id}/{name}/{entry['name']}"
            for entry in _list_all(f"{project_id}/{name}")
            if entry.get("name")
        )
    if paths:
        store.remove(paths)
        for p in paths:
            _signed_url_cache.pop(p, None)


# ── Saved New Bid draft files (0109) ─────────────────────────────────────────


def build_draft_object_path(draft_id: str, category: str, filename: str) -> str:
    """Object path for a file attached to a saved New Bid draft.

    Draft objects live in the SAME bucket as project files, under a reserved
    `drafts/{draft_id}/` prefix, with the identical `{category}/{uuid}-{name}`
    key scheme as `build_object_path`. The transfer endpoint MOVES the object by
    swapping the prefix for `{project_id}/`, so the landed key is exactly what a
    fresh project upload of that file would have produced."""
    return f"drafts/{draft_id}/{category}/{uuid.uuid4().hex}-{safe_key_component(filename)}"


def move_object(from_path: str, to_path: str) -> None:
    """Move (rename) an object within the bucket - used by the draft-to-project
    transfer so the bytes are never copied or re-uploaded."""
    get_supabase().storage.from_(BUCKET).move(from_path, to_path)
    _signed_url_cache.pop(from_path, None)


def copy_file(from_path: str, to_path: str) -> None:
    """Copy an object within the bucket (server-side; the bytes never travel
    through the app). Used by the bid-split training capture to preserve a
    source PDF under `bid-splits/training/{file_id}/` so the example outlives
    the job's deletion sweep, and by the RFP split promotion to file each
    segment on its project.

    A dropped connection is retried like `upload_file`. A copy has no upsert,
    so a retry that answers "already exists" (409 Duplicate) means the failed
    attempt landed the copy before the connection died: that is success. A
    409 on the FIRST attempt is still raised (the destination was taken
    before we started)."""
    for attempt in range(1, _TRANSFER_ATTEMPTS + 1):
        try:
            get_supabase().storage.from_(BUCKET).copy(from_path, to_path)
            return
        except httpx.TransportError:
            if attempt == _TRANSFER_ATTEMPTS:
                raise
            logger.warning(
                "storage copy of %s to %s dropped (attempt %d/%d); retrying",
                from_path,
                to_path,
                attempt,
                _TRANSFER_ATTEMPTS,
            )
            time.sleep(_TRANSFER_BACKOFF_S * attempt)
        except StorageApiError as exc:
            if attempt > 1 and _is_duplicate(exc):
                logger.info("storage copy to %s had landed before the drop; done", to_path)
                return
            raise
    raise AssertionError("unreachable")


def _is_duplicate(exc: Exception) -> bool:
    """storage-api's "The resource already exists" (409, code Duplicate)."""
    status_code = str(getattr(exc, "status", "") or "")
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    return status_code == "409" or code.lower() == "duplicate" or "already exists" in message.lower()


def object_exists(path: str) -> bool:
    """Whether an object exists at `path`. Used to disambiguate a failed move
    during a transfer retry (destination present + source gone means a previous
    attempt already moved it). Errors report False, which callers treat as "not
    provably moved" - the safe direction."""
    try:
        return bool(get_supabase().storage.from_(BUCKET).exists(path))
    except Exception:  # noqa: BLE001
        return False


# ── Bid File Splitter objects (0111) ─────────────────────────────────────────


def build_bid_split_source_path(job_id: str, filename: str) -> str:
    """Object path for an uploaded Bid File Splitter source PDF. Splitter data
    is standalone (no project), so it lives under a reserved `bid-splits/`
    prefix in the same private bucket, keyed by job."""
    return f"bid-splits/{job_id}/source/{uuid.uuid4().hex}-{safe_key_component(filename)}"


def build_bid_split_output_path(job_id: str, filename: str) -> str:
    """Object path for one split output PDF (a segment of a source file)."""
    return f"bid-splits/{job_id}/output/{uuid.uuid4().hex}-{safe_key_component(filename)}"


def delete_bid_split_prefix(job_id: str) -> None:
    """Remove EVERY object under `bid-splits/{job_id}/` (sources and outputs).

    Mirror of delete_draft_prefix for splitter jobs: the job-delete cleanup must
    not trust the DB rows alone (a worker racing the delete can land an output
    object after the rows were read). Splitter objects all live exactly two
    levels deep — `bid-splits/{job_id}/{source|output}/{object}` — so a
    two-level walk is complete."""
    store = get_supabase().storage.from_(BUCKET)
    prefix = f"bid-splits/{job_id}"

    def _list_all(path: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            page = store.list(path, {"limit": _LIST_PAGE, "offset": offset}) or []
            out.extend(page)
            if len(page) < _LIST_PAGE:
                return out
            offset += _LIST_PAGE

    paths: list[str] = []
    for folder in _list_all(prefix):
        name = folder.get("name")
        if not name:
            continue
        paths.extend(
            f"{prefix}/{name}/{entry['name']}"
            for entry in _list_all(f"{prefix}/{name}")
            if entry.get("name")
        )
    if paths:
        store.remove(paths)
        for p in paths:
            _signed_url_cache.pop(p, None)


def delete_draft_prefix(draft_id: str) -> None:
    """Remove EVERY object under `drafts/{draft_id}/`.

    Mirror of `delete_project_prefix` for saved New Bid drafts: the draft-delete
    and transfer cleanups must not trust bid_draft_files rows alone (an upload
    racing the delete can land its object after the rows were read, and nothing
    else would ever reclaim the orphan). Draft objects all live exactly two
    levels deep - `drafts/{draft_id}/{category}/{object}` - so a two-level walk
    is complete."""
    store = get_supabase().storage.from_(BUCKET)
    prefix = f"drafts/{draft_id}"

    def _list_all(path: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            page = store.list(path, {"limit": _LIST_PAGE, "offset": offset}) or []
            out.extend(page)
            if len(page) < _LIST_PAGE:
                return out
            offset += _LIST_PAGE

    paths: list[str] = []
    for folder in _list_all(prefix):
        name = folder.get("name")
        if not name:
            continue
        paths.extend(
            f"{prefix}/{name}/{entry['name']}"
            for entry in _list_all(f"{prefix}/{name}")
            if entry.get("name")
        )
    if paths:
        store.remove(paths)
        for p in paths:
            _signed_url_cache.pop(p, None)

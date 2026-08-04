"""Supabase Storage helpers for project files.

All objects live in the private `project-files` bucket. Downloads are served via
short-TTL signed URLs (critical for the hardened estimator). Object paths are
namespaced by project: `{project_id}/{category}/{uuid}-{filename}`.
"""

import string
import time
import uuid

from app.core.config import get_settings
from app.core.supabase_client import get_supabase

BUCKET = "project-files"

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


def upload_file(path: str, content: bytes, content_type: str, *, upsert: bool = False) -> None:
    get_supabase().storage.from_(BUCKET).upload(
        path, content, {"content-type": content_type, "upsert": "true" if upsert else "false"}
    )


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
    return get_supabase().storage.from_(BUCKET).download(path)


def delete_file(path: str) -> None:
    get_supabase().storage.from_(BUCKET).remove([path])
    _signed_url_cache.pop(path, None)

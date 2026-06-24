"""Supabase Storage helpers for project files.

All objects live in the private `project-files` bucket. Downloads are served via
short-TTL signed URLs (critical for the hardened estimator). Object paths are
namespaced by project: `{project_id}/{category}/{uuid}-{filename}`.
"""

import time
import uuid

from app.core.config import get_settings
from app.core.supabase_client import get_supabase

BUCKET = "project-files"

# Signed-URL memoization: a fresh token per request defeats every cache layer
# (browser and Supabase Smart CDN key on the token), so repeat previews would
# re-download the bytes. Reuse the same URL until shortly before it expires.
# Single process + GIL → no locking; a lost race just re-mints.
_REFRESH_MARGIN_S = 60
_CACHE_SWEEP_SIZE = 500
# path -> (url, expires_at_epoch)
_signed_url_cache: dict[str, tuple[str, float]] = {}


def build_object_path(project_id: str, category: str, filename: str) -> str:
    safe = filename.replace("/", "_")
    return f"{project_id}/{category}/{uuid.uuid4().hex}-{safe}"


def upload_file(path: str, content: bytes, content_type: str, *, upsert: bool = False) -> None:
    get_supabase().storage.from_(BUCKET).upload(
        path, content, {"content-type": content_type, "upsert": "true" if upsert else "false"}
    )


def signed_url(path: str, ttl_seconds: int | None = None, *, use_cache: bool = True) -> str:
    """Mint (or reuse) a signed URL for `path`.

    Only the default-TTL flow is memoized; explicit TTLs and `use_cache=False`
    always mint fresh (e.g. links embedded in emails must carry the full TTL).
    """
    ttl = ttl_seconds or get_settings().signed_url_ttl_seconds
    now = time.time()

    cacheable = use_cache and ttl_seconds is None
    if cacheable:
        cached = _signed_url_cache.get(path)
        if cached and now < cached[1] - _REFRESH_MARGIN_S:
            return cached[0]

    res = get_supabase().storage.from_(BUCKET).create_signed_url(path, ttl)
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

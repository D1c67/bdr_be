"""Staging for PDFs dropped in the assistant panel
(docs/ASSISTANT_PHASE2_PLAN.md 3.1).

One PDF per request (POST /assistant/files): the name is normalized, the
bytes pass the Bid File Splitter's own per-file check (so a file the
splitter would refuse is refused here, not after the card is approved), the
sha256 dedupes inside the person's unlinked live staging, and the object
lands in the private `project-files` bucket under `assistant-files/` so
split-drop can copy it into a splitter job server-side. The staged row is
the record that a message carried a file; rows are never deleted.

The 7-day purge has no scheduler in phase 2: `purge_expired` runs in
bounded steps from the staging routes and from the end of every
assistant_continue run, and never touches the objects of an owner on legal
hold.

Sync throughout (the Supabase SDK and the PDF parse are blocking): the
async upload route calls `stage` through run_in_threadpool. Every
assistant_* row is read and written through app/assistant/repo.py.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import Any

from app.services import bid_split, notifications, storage

logger = logging.getLogger(__name__)

PDF_MIME = "application/pdf"
PDF_SUFFIX = ".pdf"
FILENAME_MAX = 255                          # assistant_files.filename check (0170)
REUSE_MIN_TTL_SECONDS = 4 * 3600            # a 4-hour card built on a reused row cannot outlive it
SWEEP_ON_STAGE = 20
SWEEP_ON_LIST = 20

# Error codes (docs/ASSISTANT_PHASE2_PLAN.md 5.3).
BAD_FILENAME = "bad_filename"
NOT_PDF = "not_pdf"
UNREADABLE_PDF = "unreadable_pdf"
TOO_MANY_PAGES = "too_many_pages"
STAGING_FULL = "staging_full"
FILE_NOT_FOUND = "file_not_found"
FILE_CONSUMED = "file_consumed"

_SEPARATORS = re.compile(r"[/\\]")


class StagingRefused(Exception):
    """A staged file the assistant will not take. The route answers
    `status` with {"code", "message", **detail} and the code in
    X-Error-Code."""

    def __init__(self, code: str, message: str, status: int, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


def _repo():
    """The assistant repo, imported on first use (the only module allowed
    to touch assistant_* tables)."""
    from app.assistant import repo

    return repo


def normalize_filename(raw: str | None) -> str:
    """The name every later surface shows (cards, chips, the task card and
    the bid_split_files and project_files rows). Drops every character of
    Unicode category Cc or Cf (newlines, tabs, NUL and the bidi controls
    U+202A to U+202E and U+2066 to U+2069 included), turns path separators
    into "-", collapses whitespace, keeps or adds a ".pdf" suffix
    (case-insensitive) and trims the stem so the whole name fits 255
    characters. Returns "" when nothing is left (the caller refuses)."""
    text = "".join(ch for ch in str(raw or "") if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = " ".join(_SEPARATORS.sub("-", text).split())
    if text.lower().endswith(PDF_SUFFIX):
        stem, suffix = text[: -len(PDF_SUFFIX)], text[-len(PDF_SUFFIX):]
    else:
        stem, suffix = text, PDF_SUFFIX
    stem = stem.strip()[: FILENAME_MAX - len(suffix)].rstrip()
    if not stem.strip(" .-"):
        return ""
    return f"{stem}{suffix}"


def _refused_upload(exc: bid_split.UploadRefused) -> StagingRefused:
    """check_upload's refusal as a coded one: 413 is the page cap; any 415
    left after the is_pdf_upload check is a PDF that does not open."""
    if exc.status_code == 413:
        return StagingRefused(TOO_MANY_PAGES, str(exc), 413, {"pages": exc.pages})
    return StagingRefused(UNREADABLE_PDF, str(exc), 415)


def _is_cap_violation(exc: Exception) -> bool:
    return "file_cap_user" in str(exc)


def stage(user: Any, filename: str | None, content: bytes) -> tuple[dict, bool]:
    """Stage one PDF for `user`; returns (row, reused). Order (3.1.2): name,
    the splitter's check (extension, magic, must open, page cap), sha256, a
    bounded system sweep, reuse of the same unlinked bytes (expires_at
    refreshed, no new object), else the object and the row. Raises
    StagingRefused: bad_filename 400, not_pdf / unreadable_pdf 415,
    too_many_pages 413 (detail.pages), staging_full 409 (the object is
    deleted again)."""
    repo = _repo()
    name = normalize_filename(filename)
    if not name:
        raise StagingRefused(BAD_FILENAME, "The file needs a name.", 400)
    if not bid_split.is_pdf_upload(name, content):
        raise StagingRefused(NOT_PDF, "The assistant takes PDF files only.", 415)
    try:
        pages = bid_split.check_upload(name, content)
    except bid_split.UploadRefused as exc:
        raise _refused_upload(exc) from exc
    sha = hashlib.sha256(content).hexdigest()

    purge_expired(user_id=None, limit=SWEEP_ON_STAGE)
    reused = repo.reusable_staged_file_by_sha(user.id, sha, min_ttl_seconds=REUSE_MIN_TTL_SECONDS)
    if reused is not None:
        return reused, True

    path = storage.build_assistant_staging_path(user.id, name)
    storage.upload_file(path, content, PDF_MIME)
    try:
        row = repo.insert_staged_file(user.id, {
            "filename": name,
            "storage_path": path,
            "size_bytes": len(content),
            "sha256": sha,
            "mime": PDF_MIME,
            "page_count": pages,
        })
    except Exception as exc:
        _delete_object(path)
        if _is_cap_violation(exc):
            raise StagingRefused(
                STAGING_FULL,
                "You have 2 GB of files waiting in the assistant. Send or remove some first.",
                409,
            ) from exc
        raise
    _audit(user.id, "assistant.file_staged", row.get("id"),
           {"file_id": row.get("id"), "size_bytes": len(content), "pages": pages})
    return row, False


def list_tray(user: Any) -> list[dict]:
    """GET /assistant/files: the caller's own sweep first (bounded), then
    the live, unlinked, unclaimed rows, newest first."""
    purge_expired(user_id=user.id, limit=SWEEP_ON_LIST)
    return _repo().list_staged_files(user.id, limit=50)


def remove(user: Any, file_id: str) -> None:
    """DELETE /assistant/files/{id}: the caller's own unclaimed row, including
    one sent with a message that no open split card names. The row
    is retired first (expires_at = now(), fenced on unclaimed), so no claim
    can take it while its object goes; the object is then deleted unless
    the owner is on legal hold, and the row marked purged. Already retired:
    nothing to do (idempotent). Raises StagingRefused file_not_found 404 or
    file_consumed 409."""
    repo = _repo()
    rows = repo.staged_files_by_ids(user.id, [file_id])
    row = rows[0] if rows else None
    if row is None:
        raise StagingRefused(FILE_NOT_FOUND, "That file is not staged.", 404)
    if row.get("consumed_at") or row.get("consumed_task_id"):
        raise StagingRefused(FILE_CONSUMED, "That file is already being split.", 409)
    if row.get("purged_at"):
        return
    linked = bool(row.get("message_id"))
    if linked and str(row["id"]) in repo.open_split_card_file_ids(user.id, include_decided=True):
        # Sent with a message whose split card is still open (pending, or
        # approved and not yet executed): that card needs the object. A file
        # sent but never split (refused, rejected or expired card) is still
        # the person's to remove (plan 3.1.3: only consumed or claimed rows
        # are refused), so it stops holding the 2 GB staging cap.
        raise StagingRefused(FILE_CONSUMED, "That file is on a split card that is still open.", 409)
    if not repo.retire_staged_file(user.id, str(row["id"]), allow_linked=linked):
        raise StagingRefused(FILE_CONSUMED, "That file is already being split.", 409)
    if repo.legal_hold(user.id):
        return
    if _delete_object(str(row.get("storage_path") or "")):
        repo.mark_staged_purged(user.id, str(row["id"]))


def purge_expired(user_id: str | None, limit: int) -> int:
    """Delete the storage objects of consumed or expired staged rows and
    mark them purged (3.1.4); returns how many were purged. `user_id` None
    is the SYSTEM SWEEP across every user (called only from `stage` here
    and from continue_job.run_continue, pinned by
    tests/test_assistant_repo_isolation); otherwise only that person's rows.
    Owners on legal hold are skipped in both modes (the repo query excludes
    them). Best effort: never raises, and rows are never deleted."""
    try:
        repo = _repo()
        rows = repo.staged_objects_to_purge(user_id, limit=limit)
    except Exception:  # noqa: BLE001 - the sweep is best effort; the next call retries
        logger.exception("assistant staging: purge query failed")
        return 0
    done = 0
    for row in rows:
        try:
            if not _delete_object(str(row.get("storage_path") or "")):
                continue
            if repo.mark_staged_purged(str(row.get("user_id")), str(row.get("id"))):
                done += 1
        except Exception:  # noqa: BLE001 - one bad row never stops the sweep
            logger.exception("assistant staging: purge of %s failed", row.get("id"))
    return done


def _delete_object(path: str) -> bool:
    """Delete one staging object; a missing object counts as deleted. False
    (logged) when storage refused, so the row stays unpurged for the next
    sweep."""
    if not path:
        return True
    try:
        storage.delete_file(path)
    except Exception:  # noqa: BLE001 - logged; the purge retries this object later
        logger.exception("assistant staging: could not delete object %s", path)
        return False
    return True


def _audit(actor_id: str, action: str, entity_id: Any, payload: dict) -> None:
    """An audit row with ids and sizes only (a file name is tainted text)."""
    try:
        notifications.audit(actor_id, action, "assistant_files", str(entity_id) if entity_id else None, payload)
    except Exception:  # noqa: BLE001 - a missing audit row never fails the upload
        logger.exception("assistant staging: audit %s failed", action)

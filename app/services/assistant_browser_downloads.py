"""Browser downloads into the BDR project (docs/ASSISTANT_BROWSER_V2_PLAN.md 11.2 to 11.4).

The server half of POST /agent-worker/v1/jobs/{job_id}/user/downloads. The
worker has already allowlisted the file by magic bytes and scanned it with
Microsoft Defender; it never trusts that, and neither does this module. Order
of `accept`:

  1. read the part under the per-file cap (a stream past the cap is refused);
  2. hash and size it; a retry of the same bytes returns the first answer;
  3. re-sniff by magic bytes (never the extension) and compare with the type
     the worker declared; macro-enabled Office files, executables, scripts
     and anything not on the allowlist are refused;
  4. check the task caps (files and bytes) and the owner's right to upload;
  5. write the bytes to the project the task was admitted for (category
     `other`, provenance assistant, the file-add hooks run), or, with no
     project (or no right to write to it), to the owner's staging under the
     kind `browser_download` (assistant_staging.py);
  6. record the download (assistant_browser_download_record), fenced on the
     lease. A refusal is recorded too (status rejected, a short reason) so a
     retry answers the same way and the result card can say what happened.

Nothing is stored before steps 1 to 4 pass, so a refused file is never
visible anywhere (the quarantine rule of 11.1 on the server side). The file
name is cleaned and given the extension of the sniffed type, so a name that
lies about the type never reaches the project. A download is data: nothing
here reads the text of a file, and no model sees it.

`add_to_project` is the human half of 11.4: the owner picks a project for a
staged file from the result block. No model tool reaches it.

Sync throughout (the Supabase SDK and storage calls are blocking); the worker
route is a `def` route, so it runs in the threadpool. Every assistant_* row is
read and written through app/assistant/repo.py.
"""
from __future__ import annotations

import codecs
import hashlib
import io
import logging
import re
import threading
import unicodedata
import uuid
import zipfile
import zlib
from typing import Any, BinaryIO

from app.core import provenance
from app.services import assistant_staging as staging
from app.services.browser_agents import protocol

logger = logging.getLogger(__name__)

ALLOWED_TYPES = ("pdf", "png", "jpeg", "tiff", "gif", "zip", "ooxml", "ole", "csv", "text", "dwg", "dxf", "rtf")
PROJECT_CATEGORY = "other"
NOTE_PREFIX = "Downloaded by the assistant browser from "
AUDIT_VIA = "assistant_browser"
HEAD_BYTES = 65536
NAME_MAX = 200

# The extensions a name may keep, per sniffed type. A name with any other
# extension gets the first one appended, so the last extension always agrees
# with the bytes ("x.exe" holding a PDF becomes "x.exe.pdf"). Macro-enabled
# Office extensions are not listed and macro-bearing files are refused.
ALLOWED_EXT: dict[str, tuple[str, ...]] = {
    "pdf": (".pdf",),
    "png": (".png",),
    "jpeg": (".jpg", ".jpeg", ".jpe"),
    "tiff": (".tif", ".tiff"),
    "gif": (".gif",),
    "zip": (".zip",),
    "ooxml": (".docx", ".xlsx", ".pptx"),
    "ole": (".doc", ".xls", ".ppt", ".msg"),
    "csv": (".csv", ".tsv"),
    "text": (".txt", ".log", ".md"),
    "dwg": (".dwg",),
    "dxf": (".dxf",),
    "rtf": (".rtf",),
}

# Types that are one family for the "declared versus sniffed" comparison: the
# worker cannot tell a zip from an Office file, or a csv from a text file,
# without the same extra checks, and the server decides the final type.
_FAMILY = {"zip": "zip", "ooxml": "zip", "csv": "text", "text": "text", "dxf": "text"}

# Reason codes recorded on a refused download (assistant_browser_downloads.reason).
TOO_LARGE = "too_large"
EMPTY = "empty"
SIZE_MISMATCH = "size_mismatch"
SHA_MISMATCH = "sha_mismatch"
TYPE_NOT_ALLOWED = "type_not_allowed"
TYPE_MISMATCH = "type_mismatch"
MACRO_ENABLED = "macro_enabled"
TOO_MANY_FILES = "too_many_files"
TASK_QUOTA = "task_quota"
STAGING_FULL = "staging_full"
OWNER_UNAVAILABLE = "owner_unavailable"
# Reasons recorded on a file that was kept but not written to the project.
NO_PROJECT_RIGHT = "no_upload_right"
PROJECT_WRITE_FAILED = "project_write_failed"

# add_to_project error codes.
BAD_REQUEST = "bad_request"
NOT_FOUND = "download_not_found"
NOT_STAGED = "download_not_staged"
EXPIRED = "download_expired"
CHANGED = "download_changed"
PROJECT_UNAVAILABLE = "project_unavailable"
CLAIMED = "download_claimed"

_SEPARATORS = re.compile(r"[/\\]")
_DWG = re.compile(rb"^AC1[0-9]{3}")
_DXF_ASCII = re.compile(rb"^(?:\xef\xbb\xbf)?[ \t]*0[ \t]*\r?\n[ \t]*SECTION[ \t]*\r?\n[ \t]*2[ \t]*\r?\n[ \t]*[A-Z]+")
_DXF_BINARY = b"AutoCAD Binary DXF\r\n\x1a\x00"
_OOXML_FIRST_ENTRIES = ("[Content_Types].xml", "_rels/", "docProps/", "word/", "xl/", "ppt/")


class DownloadRefused(Exception):
    """A human-route refusal (add_to_project). The route answers `status`
    with {"code", "message"}."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class _Reject(Exception):
    """Internal: this file is refused for `code` (recorded, answered 200 rejected)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _repo():
    """The assistant repo, imported on first use (the only module allowed to
    touch assistant_* tables)."""
    from app.assistant import repo

    return repo


# ── Type by magic bytes ────────────────────────────────────────────────────


def _looks_text(head: bytes, *, final: bool) -> bool:
    if not head or b"\x00" in head:
        return False
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(head, final=final)
    except UnicodeDecodeError:
        return False
    return True


def _zip_first_entry(head: bytes) -> str | None:
    """The name in the first local file header of a zip, when the head holds it."""
    if len(head) < 30:
        return None
    name_len = int.from_bytes(head[26:28], "little")
    raw = head[30:30 + name_len]
    if len(raw) < name_len:
        return None
    return raw.decode("utf-8", errors="replace")


def sniff(head: bytes, *, filename: str | None = None) -> str | None:
    """Type by magic bytes (never the extension); None when not allowed.
    `head` is the start of the file (up to 64 KB is looked at). `filename`
    only separates csv from plain text, both of which are UTF-8 with no NUL;
    it never turns an unrecognized file into an allowed one. A zip whose first
    entry is an Office part is `ooxml`; `classify` confirms it on the whole
    file."""
    head = bytes(head or b"")[:HEAD_BYTES]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "tiff"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head.startswith(b"PK\x03\x04"):
        first = _zip_first_entry(head) or ""
        return "ooxml" if first.startswith(_OOXML_FIRST_ENTRIES) else "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    if _DWG.match(head):
        return "dwg"
    if head.startswith(_DXF_BINARY) or _DXF_ASCII.match(head):
        return "dxf"
    if _looks_text(head, final=len(head) < HEAD_BYTES):
        return "csv" if str(filename or "").lower().endswith(".csv") else "text"
    return None


def _utf8_clean(data: bytes) -> bool:
    """The whole file is UTF-8 with no NUL (a text type must hold that for all of it)."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        for start in range(0, len(data), 1 << 20):
            chunk = data[start:start + (1 << 20)]
            if b"\x00" in chunk:
                return False
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


_CONTENT_TYPES_MAX = 1 << 20
_OOXML_MACRO_MARKERS = (b"vnd.ms-office.vbaproject", b"macroenabled", b"vnd.ms-excel.sheet.binary.macroenabled",
                        b"vnd.ms-office.activex", b"vnd.ms-excel.intl.macrosheet", b"vnd.ms-excel.macrosheet")
# Compound file directory entry names (UTF-16LE, NUL-terminated) of a VBA
# project: Excel's _VBA_PROJECT_CUR, Word's Macros, and their VBA and
# _VBA_PROJECT children.
_OLE_MACRO_MARKERS = tuple(name.encode("utf-16-le") + b"\x00\x00"
                           for name in ("_VBA_PROJECT_CUR", "_VBA_PROJECT", "Macros", "VBA"))
_OLE_ENTRY = 128


def _ole_has_macros(data: bytes) -> bool:
    """A directory entry (128-byte aligned, after the 512 or 4096 byte
    header) named like a VBA storage. Matching only at entry starts keeps
    document text that merely says "Macros" from counting."""
    for marker in _OLE_MACRO_MARKERS:
        start = data.find(marker)
        while start != -1:
            if start % _OLE_ENTRY == 0 and start >= 512:
                return True
            start = data.find(marker, start + 1)
    return False


def _content_types(archive: zipfile.ZipFile, names: list[str]) -> bytes | None:
    """[Content_Types].xml lowercased (at most 1 MB declared and read), or
    None when the archive has none. A larger one is refused as not allowed."""
    if "[Content_Types].xml" not in names:
        return None
    info = archive.getinfo("[Content_Types].xml")
    if info.file_size > _CONTENT_TYPES_MAX:
        raise ValueError("content types too large")
    with archive.open(info) as handle:
        return handle.read(_CONTENT_TYPES_MAX + 1)[:_CONTENT_TYPES_MAX].lower()


def classify(data: bytes, filename: str | None = None) -> str:
    """The final type of a whole file, or `_Reject`. The head sniff, then the
    checks that need the whole file: a zip is opened for its directory only
    (never extracted) to tell an Office file from a plain archive and to refuse
    a macro project; a text type must be UTF-8 end to end."""
    kind = sniff(data[:HEAD_BYTES], filename=filename)
    if kind is None:
        raise _Reject(TYPE_NOT_ALLOWED)
    if kind in ("zip", "ooxml"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                content_types = _content_types(archive, names)
        except (zipfile.BadZipFile, ValueError, OSError, RuntimeError, EOFError, zlib.error):
            raise _Reject(TYPE_NOT_ALLOWED) from None
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise _Reject(MACRO_ENABLED)
        # The VBA part may have any name: Office finds it through the content
        # types (and the relationship), so the declared type is what counts.
        if content_types is not None and any(marker in content_types for marker in _OOXML_MACRO_MARKERS):
            raise _Reject(MACRO_ENABLED)
        office = "[Content_Types].xml" in names and any(n.startswith(("word/", "xl/", "ppt/")) for n in names)
        return "ooxml" if office else "zip"
    if kind == "ole" and _ole_has_macros(data):
        # A legacy .doc, .xls or .ppt carrying a VBA project (its storage
        # directory names are UTF-16LE in the compound file directory).
        raise _Reject(MACRO_ENABLED)
    if kind in ("csv", "text") and not _utf8_clean(data):
        raise _Reject(TYPE_NOT_ALLOWED)
    return kind


def clean_filename(raw: str | None, kind: str | None) -> str:
    """The name the project shows: control and format characters dropped,
    separators made "-", whitespace collapsed, trailing dots and spaces
    trimmed, at most 200 characters, and an extension that agrees with `kind`
    (appended when the name's own is not one of the type's)."""
    text = "".join(ch for ch in str(raw or "") if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = " ".join(_SEPARATORS.sub("-", text).split()).strip(" .")
    if not text.strip(" .-"):
        text = "download"
    ext = ""
    if kind in ALLOWED_EXT:
        allowed = ALLOWED_EXT[kind]
        lowered = text.lower()
        ext = next((e for e in allowed if lowered.endswith(e)), None) or ""
        if ext:
            text = text[: -len(ext)]
            ext = lowered[-len(ext):]
        else:
            ext = allowed[0]
    return f"{text[: NAME_MAX - len(ext)].rstrip(' .') or 'download'}{ext}"


# ── The worker route ───────────────────────────────────────────────────────


def _read_capped(stream: BinaryIO, limit: int) -> tuple[bytes, bool]:
    """(bytes, over): at most limit + 1 bytes are read, so a stream past the
    cap is `over` and never held whole."""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = stream.read(min(1 << 20, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), total > limit


def _response(row: dict) -> dict:
    status = {"stored": "saved_to_project", "staged": "staged"}.get(str(row.get("status")), "rejected")
    return {
        "download_id": str(row["id"]),
        "status": status,
        "file_id": str(row["file_id"]) if row.get("file_id") else None,
        "reason": (str(row["reason"])[:200] if row.get("reason") else None),
    }


def _quota_problem(owner: str, job_id: str, size: int, caps: Any) -> str | None:
    rows = [r for r in _repo().browser_downloads(owner, job_id, limit=200) if r.get("status") != "rejected"]
    if len(rows) >= int(caps.download_files):
        return TOO_MANY_FILES
    if sum(int(r.get("size_bytes") or 0) for r in rows) + size > int(caps.download_task_bytes):
        return TASK_QUOTA
    return None


def _current_user(human: dict) -> Any:
    from app.core.deps import CurrentUser
    from app.core.roles import Role

    return CurrentUser(id=str(human["id"]), email=str(human.get("email") or ""), role=Role(human["role"]),
                       is_active=True)


class _Later:
    """The BackgroundTasks stand-in files.add_team_file takes: the preview
    conversions it queues run after the download is recorded."""

    def __init__(self) -> None:
        self.tasks: list[tuple[Any, tuple, dict]] = []

    def add_task(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((fn, args, kwargs))


def _spawn(later: _Later | None) -> None:
    """Run the queued preview conversions off the request (best effort; the
    converter records its own failures)."""
    if later is None or not later.tasks:
        return

    def run() -> None:
        for fn, args, kwargs in later.tasks:
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 - a preview never matters to the download
                logger.exception("assistant browser downloads: background task failed")

    threading.Thread(target=run, name="browser-download-preview", daemon=True).start()


def _write_project_file(human: dict, project_id: str, job_id: str, name: str, data: bytes, origin: str,
                        later: _Later) -> dict:
    """The project_files row for a download, through the upload route's own
    policy (files.add_team_file: writer gate, safe content type, the object,
    the row, the audit row and every file-add hook), inside the assistant
    provenance scope of the job owner so the row is stamped assistant. The
    scope id is the job id: a browser job has no chat run of its own."""
    from app.routers import files

    owner = str(human["id"])
    with provenance.acting_as_assistant(owner, job_id):
        row, _resolved = files.add_team_file(
            project_id, PROJECT_CATEGORY, name, data, _current_user(human),
            note=f"{NOTE_PREFIX}{origin}", background=later,  # type: ignore[arg-type]
            audit_extra={"via": AUDIT_VIA, "job_id": job_id},
        )
    return row


def _undo_project_file(row: dict) -> None:
    """Remove a project file this module just wrote (the record failed or lost a race)."""
    from app.core.supabase_client import get_supabase
    from app.services import storage

    try:
        get_supabase().table("project_files").delete().eq("id", row["id"]).execute()
    except Exception:  # noqa: BLE001 - logged; the object delete below still runs
        logger.exception("assistant browser downloads: could not remove project file %s", row.get("id"))
    try:
        if row.get("storage_path"):
            storage.delete_file(str(row["storage_path"]))
    except Exception:  # noqa: BLE001
        logger.exception("assistant browser downloads: could not remove object %s", row.get("storage_path"))


def accept(job: dict[str, Any], worker_id: str, boot_id: str, epoch: int, *, stream: BinaryIO,
           meta: dict[str, Any]) -> dict[str, Any]:
    """Re-sniff, re-check size and sha256, then write to project_files
    (category other, provenance assistant, owner's upload right rechecked) or
    to the owner's staging (kind browser_download); record the download.
    Returns the UserDownloadResponse fields {download_id, status, file_id,
    reason}; a refused file answers status "rejected" (200), never an error.
    Raises only for a failure the worker should retry (storage, the record
    RPC, a lost lease)."""
    from app.assistant import access

    repo = _repo()
    owner = str(job["principal_user_id"])
    job_id = str(job["id"])
    caps = protocol.UserPolicyCaps()
    limit = min(int(caps.download_file_bytes), protocol.USER_DOWNLOAD_FILE_BYTES_FLOOR)
    origin = str(meta["source_origin"])

    data, over = _read_capped(stream, limit)
    sha = hashlib.sha256(data).hexdigest()
    prior = repo.browser_download_by_sha(owner, job_id, sha)
    if prior is not None:
        return _response(prior)
    staging.purge_browser_downloads(owner, 5)

    kind: str | None = None
    reason: str | None = None
    if over:
        reason = TOO_LARGE
    elif not data:
        reason = EMPTY
    elif len(data) != int(meta["size_bytes"]):
        reason = SIZE_MISMATCH
    elif sha != str(meta["sha256"]).lower():
        reason = SHA_MISMATCH
    else:
        try:
            kind = classify(data, meta.get("filename"))
            declared = str(meta.get("sniffed_kind") or "")
            if _FAMILY.get(declared, declared) != _FAMILY.get(kind, kind):
                kind, reason = None, TYPE_MISMATCH
        except _Reject as refusal:
            reason = refusal.code
    if reason is None:
        reason = _quota_problem(owner, job_id, len(data), caps)
    name = clean_filename(meta.get("filename"), kind)
    record: dict[str, Any] = {
        "origin": origin, "filename": name, "sha256": sha, "size_bytes": max(len(data), 0),
        "sniffed_type": kind or "unknown", "scan_verdict": "clean",
    }

    project_row: dict | None = None
    staged_path: str | None = None
    note: str | None = None
    later = _Later()
    if reason is None:
        project_id = str(job["project_id"]) if job.get("project_id") else None
        if project_id:
            check = access.owner_check(owner, sub_app=None, require_writer=True)
            if check.problem is None and check.human:
                try:
                    project_row = _write_project_file(check.human, project_id, job_id, name, data, origin, later)
                except Exception:  # noqa: BLE001 - the file is kept in staging below
                    logger.exception("assistant browser downloads: project write failed for job %s", job_id)
                    note = PROJECT_WRITE_FAILED
            elif check.detail == access.DETAIL_NOT_WRITER:
                note = NO_PROJECT_RIGHT
            else:
                reason = OWNER_UNAVAILABLE
        if reason is None and project_row is None:
            if staging.browser_downloads_room(owner, len(data)):
                staged_path, expires = staging.put_browser_download(owner, name, data)
                record["storage_path"], record["expires_at"] = staged_path, expires
            else:
                reason = STAGING_FULL

    if reason is not None:
        record.update({"status": "rejected", "reason": reason})
    elif project_row is not None:
        record.update({"status": "stored", "project_id": str(job["project_id"]), "file_id": str(project_row["id"])})
    else:
        record.update({"status": "staged", "reason": note})

    try:
        result = repo.browser_download_record(owner, job_id, worker_id, boot_id, epoch, record)
    except Exception:
        _cleanup(project_row, staged_path)
        raise
    if result.get("ok") is not True:
        _cleanup(project_row, staged_path)
        raise RuntimeError(f"download record refused: {result.get('code')}")
    saved = result.get("download")
    if not isinstance(saved, dict) or not saved.get("id"):
        # Lost a race with the same bytes: the first row stands.
        _cleanup(project_row, staged_path)
        existing = repo.browser_download_by_sha(owner, job_id, sha)
        if existing is None:
            raise RuntimeError("download record vanished")
        return _response(existing)
    if reason is None and project_row is not None:
        _spawn(later)
    return _response(saved)


def _cleanup(project_row: dict | None, staged_path: str | None) -> None:
    if project_row is not None:
        _undo_project_file(project_row)
    if staged_path:
        staging.discard_browser_download(staged_path)


# ── The result block ───────────────────────────────────────────────────────


def file_entries(user_id: str, job_id: str) -> list[dict[str, Any]]:
    """The files of one of the user's jobs for the result block (11.3):
    [{download_id, filename, size_bytes, kind, status, project_id, file_id,
    reason, can_add_to_project}] oldest first. `status` is saved_to_project,
    staged or rejected; the project file link is built from project_id and
    file_id by the card. Metadata only, never the bytes."""
    out = []
    for row in _repo().browser_downloads(user_id, job_id, limit=50):
        response = _response(row)
        out.append({
            "download_id": response["download_id"], "filename": row.get("filename"),
            "size_bytes": row.get("size_bytes"), "kind": row.get("sniffed_type"), "status": response["status"],
            "project_id": str(row["project_id"]) if row.get("project_id") else None,
            "file_id": response["file_id"], "reason": response["reason"],
            "can_add_to_project": response["status"] == "staged" and not row.get("purged_at"),
        })
    return out


# ── "Add to project" (11.4, human route only) ──────────────────────────────


def add_to_project(user: Any, download_id: str, project_id: str, *, background: Any = None) -> dict[str, Any]:
    """POST /assistant/browser-results/downloads/{id}/add-to-project: the
    owner picks a project for a staged download. The row is claimed first (one
    click wins), the bytes are read back and re-verified (hash, size, type),
    the file goes in through the upload route's own policy as the person (the
    person chose it, so human provenance), then the staged object is deleted.
    Returns {"file_id", "project_id", "filename"}. Raises DownloadRefused
    (400 bad_request, 404 download_not_found / project_unavailable, 409
    download_not_staged / download_claimed / download_changed, 410
    download_expired); an HTTPException from the upload policy (a role that
    may not upload) passes through after the claim is undone."""
    repo = _repo()
    try:
        download_uuid, project_uuid = str(uuid.UUID(str(download_id))), str(uuid.UUID(str(project_id)))
    except ValueError:
        raise DownloadRefused(BAD_REQUEST, "That is not a valid download or project.", 400) from None
    row = repo.browser_download_get(user.id, download_uuid)
    if row is None:
        raise DownloadRefused(NOT_FOUND, "That download is not yours or no longer exists.", 404)
    if row.get("status") != "staged":
        raise DownloadRefused(NOT_STAGED, "That download is not waiting to be added.", 409)
    if row.get("purged_at") or not row.get("storage_path") or _past(row.get("expires_at")):
        raise DownloadRefused(EXPIRED, "That download has expired. Ask the assistant to fetch it again.", 410)
    if not _project_exists(project_uuid):
        raise DownloadRefused(PROJECT_UNAVAILABLE, "That project does not exist.", 404)
    if repo.browser_download_claim(user.id, download_uuid, project_uuid) is None:
        raise DownloadRefused(CLAIMED, "That download was just added or has expired.", 409)
    try:
        data = staging.read_browser_download(str(row["storage_path"]))
        if (len(data) != int(row["size_bytes"]) or hashlib.sha256(data).hexdigest() != row["sha256"]
                or classify(data, row.get("filename")) != _stored_family(row)):
            raise _Reject(CHANGED)
        later = background if background is not None else _Later()
        from app.routers import files

        created, _resolved = files.add_team_file(
            project_uuid, PROJECT_CATEGORY, str(row["filename"]), data, user,
            note=f"{NOTE_PREFIX}{row['origin']}", background=later,
            audit_extra={"via": "assistant_browser_download", "download_id": download_uuid},
        )
    except _Reject:
        repo.browser_download_unclaim(user.id, download_uuid)
        raise DownloadRefused(CHANGED, "That download changed in storage and was not added.", 409) from None
    except Exception:
        repo.browser_download_unclaim(user.id, download_uuid)
        raise
    repo.browser_download_finish(user.id, download_uuid, str(created["id"]))
    staging.discard_browser_download(str(row["storage_path"]))
    if background is None:
        _spawn(later)
    return {"file_id": str(created["id"]), "project_id": project_uuid, "filename": str(row["filename"])}


def _stored_family(row: dict) -> str:
    """The recorded type; a re-classification of the same bytes must give it back."""
    return str(row.get("sniffed_type") or "")


def _past(stamp: Any) -> bool:
    from datetime import datetime, timezone

    if not stamp:
        return True
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def _project_exists(project_id: str) -> bool:
    from app.core.supabase_client import get_supabase

    rows = get_supabase().table("projects").select("id").eq("id", project_id).limit(1).execute().data or []
    return bool(rows)

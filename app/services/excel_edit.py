"""Edit in Excel (docs/EXCEL_EDIT.md, migration 0193).

An internal user opens an .xlsx/.xlsm project file in Excel for the web:

1. `start` copies the file's current object to the MS_DRIVE_OWNER OneDrive
   (under settings.excel_edit_drive_folder, one folder per session), grants
   the user write access on that copy only (driveItem invite, no email, sign
   in required) and returns the copy's webUrl, which Excel for the web opens.
   A second person who opens the same file joins the open session.
2. Excel autosaves to the copy. `poll_once` (every excel_edit_poll_seconds,
   every worker, one lease per session) compares the copy's content tag with
   the last one pulled and, when it moved, writes the new bytes back.
3. One session is one new version (`project_file_versions`). The first
   change of a session records the file's prior content as version 1 when the
   file has no versions yet, writes the new bytes to a fresh object and
   points `project_files.storage_path` at it; later changes in the same
   session replace that version's object. Earlier objects are never touched.
4. `finish` (the Finish editing button) or `excel_edit_idle_minutes` with no
   change pulls a last time, deletes the OneDrive copy and closes the session.

Every write lands on a fresh storage path (version object and PDF preview
alike), so memoized signed URLs and CDN caches can never serve stale bytes.

Event-loop rule: everything here is sync; routes are plain `def` handlers and
the poller runs `poll_once` in a thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from postgrest.exceptions import APIError

from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.services import office_preview, storage
from app.services.notifications import audit

logger = logging.getLogger(__name__)

EDITABLE_EXTS = (".xlsx", ".xlsm")
SESSIONS = "file_edit_sessions"
VERSIONS = "project_file_versions"
FILES = "project_files"

# A tick's claim on one session; long enough for a download, an upload and a
# preview conversion, short enough that a crashed worker's claim lapses soon.
LEASE_SECONDS = 180
# How long Finish editing waits for the poller to let go of the session.
FINISH_LEASE_WAIT_SECONDS = 20
# Every xlsx/xlsm is a zip container.
_ZIP_MAGIC = b"PK\x03\x04"

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()-]")


class ExcelEditError(Exception):
    """A user-facing refusal or failure; the message is the sentence to show."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_editable_name(filename: str | None) -> bool:
    return (filename or "").lower().endswith(EDITABLE_EXTS)


def feature_on() -> bool:
    s = get_settings()
    return bool(s.excel_edit_enabled and s.ms_client_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Microsoft Graph (the drive owner's OneDrive) ─────────────────────────────


def _owner() -> str:
    s = get_settings()
    # Same resolution as graph_email.drive_upload, so every call hits one drive.
    return s.ms_drive_owner or s.ms_sender


def _graph(method: str, path: str, **kwargs) -> httpx.Response:
    from app.services import graph_email

    return graph_email.graph_request(method, f"/users/{_owner()}/drive{path}", **kwargs)


_ITEM_SELECT = "id,webUrl,cTag,size,lastModifiedDateTime,lastModifiedBy"


def _drive_item(item_id: str) -> dict | None:
    """The copy's metadata, or None once it is gone from the drive."""
    try:
        return _graph("GET", f"/items/{item_id}", params={"$select": _ITEM_SELECT}).json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _drive_content(item_id: str) -> bytes:
    return _graph("GET", f"/items/{item_id}/content", follow_redirects=True, timeout=120).content


def _drive_delete(item_id: str) -> None:
    try:
        _graph("DELETE", f"/items/{item_id}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise


def _drive_grant(item_id: str, email: str) -> None:
    """Write access on this one copy for this one person. No invitation email;
    sign-in required, so the grant is useless to anyone who is not them."""
    _graph(
        "POST",
        f"/items/{item_id}/invite",
        json={
            "recipients": [{"email": email}],
            "roles": ["write"],
            "requireSignIn": True,
            "sendInvitation": False,
        },
    )


def _drive_name(filename: str) -> str:
    return _NAME_UNSAFE.sub("_", filename or "workbook.xlsx").strip() or "workbook.xlsx"


def _modified_by(item: dict) -> str | None:
    user = (item.get("lastModifiedBy") or {}).get("user") or {}
    return user.get("displayName") or user.get("email") or None


# ── Eligibility ──────────────────────────────────────────────────────────────


def block_reason(rec: dict, *, writer: bool, locked: bool, sent: bool) -> str | None:
    """Why this person cannot edit this file in Excel, or None when they can.
    `locked`: the file is in the initial package and the hand-off lock is on.
    `sent`: the file is an update or addendum already emailed to estimators.
    Both mirror the delete route's evidence rules in routers/files.py."""
    if not feature_on():
        return "Edit in Excel is turned off."
    if not writer:
        return "Only team members who can change project files can edit in Excel."
    if not is_editable_name(rec.get("filename")):
        return "Only .xlsx and .xlsm files can be edited in Excel."
    if rec.get("category") == "proposal":
        return "Proposals can't be edited in Excel."
    if locked:
        return (
            "This file is part of the package already sent to the estimators, so it "
            "can't change. Upload a revision instead."
        )
    if sent:
        return "This file was already sent to the estimators, so it can't change."
    size = rec.get("size_bytes") or 0
    if size > get_settings().upload_max_bytes:
        return "This file is too large to edit in Excel."
    return None


# ── Rows ─────────────────────────────────────────────────────────────────────


def open_session(file_id: str) -> dict | None:
    rows = (
        get_supabase().table(SESSIONS).select("*").eq("file_id", file_id)
        .eq("status", "open").limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def _session(session_id: str) -> dict | None:
    rows = get_supabase().table(SESSIONS).select("*").eq("id", session_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _file(file_id: str) -> dict | None:
    rows = get_supabase().table(FILES).select("*").eq("id", file_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _version(version_id: str) -> dict | None:
    rows = get_supabase().table(VERSIONS).select("*").eq("id", version_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _names(user_ids) -> dict[str, str]:
    ids = sorted({str(u) for u in user_ids if u})
    if not ids:
        return {}
    rows = get_supabase().table("profiles").select("id, full_name").in_("id", ids).execute().data or []
    return {str(r["id"]): r.get("full_name") for r in rows}


def current_version_no(file_id: str) -> int:
    rows = (
        get_supabase().table(VERSIONS).select("version_no").eq("file_id", file_id)
        .order("version_no", desc=True).limit(1).execute()
    ).data or []
    return int(rows[0]["version_no"]) if rows else 1


def session_out(row: dict | None, user=None) -> dict | None:
    """The API shape of a session (docs/EXCEL_EDIT.md 3). `last_synced_at`
    is the last time new content was saved into BDR, not the last check."""
    if not row:
        return None
    version = _version(row["version_id"]) if row.get("version_id") else None
    names = _names([row.get("started_by")])
    idle_from = _parse(row.get("last_change_at")) or _parse(row.get("created_at"))
    idle_close_at = None
    if row.get("status") == "open" and idle_from:
        idle_close_at = _iso(idle_from + timedelta(minutes=get_settings().excel_edit_idle_minutes))
    return {
        "id": row["id"],
        "status": row["status"],
        "started_by_name": names.get(str(row.get("started_by"))),
        "started_at": row.get("created_at"),
        "last_synced_at": row.get("last_synced_at"),
        "last_change_at": row.get("last_change_at"),
        "last_modified_by": row.get("last_modified_by"),
        "version_no": version.get("version_no") if version else None,
        "edit_url": row.get("edit_url"),
        "idle_close_at": idle_close_at,
        "started_by_me": bool(user) and str(row.get("started_by")) == str(user.id),
        "granted_to_me": bool(user) and str(user.id) in {str(u) for u in row.get("granted_user_ids") or []},
    }


def list_versions(file_rec: dict) -> list[dict]:
    rows = (
        get_supabase().table(VERSIONS).select("*").eq("file_id", file_rec["id"])
        .order("version_no", desc=True).execute()
    ).data or []
    names = _names(r.get("created_by") for r in rows)
    return [
        {
            "id": r["id"],
            "version_no": r["version_no"],
            "filename": r.get("filename"),
            "size_bytes": r.get("size_bytes"),
            "created_at": r.get("created_at"),
            "created_by_name": names.get(str(r.get("created_by"))),
            "last_modified_by": r.get("last_modified_by"),
            "source": r.get("source"),
            "current": r.get("storage_path") == file_rec.get("storage_path"),
        }
        for r in rows
    ]


def get_version(file_id: str, version_id: str) -> dict | None:
    rows = (
        get_supabase().table(VERSIONS).select("*").eq("id", version_id).eq("file_id", file_id)
        .limit(1).execute()
    ).data or []
    return rows[0] if rows else None


def version_paths(file_id: str) -> list[str]:
    """Every version object of a file, for the delete route's cleanup."""
    rows = get_supabase().table(VERSIONS).select("storage_path").eq("file_id", file_id).execute().data or []
    return [r["storage_path"] for r in rows if r.get("storage_path")]


# ── Leases ───────────────────────────────────────────────────────────────────


def _claim(session_id: str) -> bool:
    now = _now()
    rows = (
        get_supabase().table(SESSIONS)
        .update({"lease_until": _iso(now + timedelta(seconds=LEASE_SECONDS))})
        .eq("id", session_id).eq("status", "open")
        .or_(f"lease_until.is.null,lease_until.lt.{_iso(now)}")
        .execute()
    ).data or []
    return bool(rows)


def _release(session_id: str) -> None:
    try:
        get_supabase().table(SESSIONS).update({"lease_until": None}).eq("id", session_id).execute()
    except Exception:  # noqa: BLE001 - the lease lapses on its own
        logger.warning("excel edit: could not release the lease of %s", session_id)


# ── Start / join ─────────────────────────────────────────────────────────────


def _grant_or_fail(item_id: str, email: str) -> None:
    if (email or "").strip().lower() == _owner().strip().lower():
        return  # the drive owner already owns the copy
    try:
        _drive_grant(item_id, email)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "excel edit: Microsoft refused to share %s with %s (%s)",
            item_id, email, exc.response.status_code,
        )
        raise ExcelEditError(
            f"Microsoft 365 would not share the workbook with {email}. Your BDR sign-in "
            "email must be your G3 Microsoft 365 account.",
            502,
        ) from exc


def _join(row: dict, user) -> dict:
    granted = [str(u) for u in (row.get("granted_user_ids") or [])]
    if str(user.id) not in granted:
        _grant_or_fail(row["drive_item_id"], user.email)
        get_supabase().table(SESSIONS).update(
            {"granted_user_ids": [*granted, str(user.id)]}
        ).eq("id", row["id"]).execute()
        row = {**row, "granted_user_ids": [*granted, str(user.id)]}
    return row


def start(rec: dict, user) -> dict:
    """Open (or join) the file's editing session; returns the session row."""
    existing = open_session(rec["id"])
    if existing:
        return _join(existing, user)

    content = storage.download_file(rec["storage_path"])
    session_id = str(uuid.uuid4())
    folder = get_settings().excel_edit_drive_folder.strip("/")
    drive_path = f"{folder}/{session_id}/{_drive_name(rec.get('filename'))}"
    from app.services import graph_email

    item_id = graph_email.drive_upload(drive_path, content)
    try:
        item = _drive_item(item_id) or {}
        if not item.get("webUrl"):
            raise ExcelEditError("OneDrive did not return a link for the workbook.", 502)
        _grant_or_fail(item_id, user.email)
        try:
            row = (
                get_supabase().table(SESSIONS).insert({
                    "id": session_id,
                    "project_id": rec["project_id"],
                    "file_id": rec["id"],
                    "started_by": user.id,
                    "drive_item_id": item_id,
                    "drive_path": drive_path,
                    "edit_url": item["webUrl"],
                    "last_ctag": item.get("cTag"),
                    "last_change_at": item.get("lastModifiedDateTime"),
                    "granted_user_ids": [str(user.id)],
                }).execute()
            ).data[0]
        except APIError as exc:
            if getattr(exc, "code", None) != "23505":
                raise
            # Someone else opened it a moment earlier: drop our copy, join theirs.
            _safe_drive_delete(item_id)
            winner = open_session(rec["id"])
            if not winner:
                raise ExcelEditError("Someone else is opening this file. Try again.") from exc
            return _join(winner, user)
    except Exception:
        _safe_drive_delete(item_id)
        raise
    audit(
        user.id, "file.excel_edit_started", "project_file", rec["id"],
        {"project_id": rec["project_id"], "session_id": session_id},
    )
    return row


def _safe_drive_delete(item_id: str) -> bool:
    try:
        _drive_delete(item_id)
        return True
    except Exception:  # noqa: BLE001 - the copy leaks; logged for cleanup
        logger.exception("excel edit: could not delete OneDrive item %s", item_id)
        return False


# ── Sync ─────────────────────────────────────────────────────────────────────


@dataclass
class SyncResult:
    changed: bool = False
    closed: bool = False


def _close(row: dict, reason: str, *, actor_id: str | None = None) -> None:
    deleted = _safe_drive_delete(row["drive_item_id"])
    for path in row.get("superseded_paths") or []:
        try:
            storage.delete_file(path)
        except Exception:  # noqa: BLE001 - an orphan object is cosmetic
            logger.warning("excel edit: could not delete superseded object %s", path)
    get_supabase().table(SESSIONS).update({
        "status": "closed",
        "closed_at": _iso(_now()),
        "close_reason": reason,
        "lease_until": None,
        **({} if deleted else {"last_error": "OneDrive copy could not be deleted"}),
    }).eq("id", row["id"]).execute()
    if row.get("file_id"):
        audit(
            actor_id or row.get("started_by"), "file.excel_edit_finished", "project_file",
            row["file_id"], {"project_id": row["project_id"], "session_id": row["id"], "reason": reason},
        )


def _write_version(row: dict, rec: dict, content: bytes, digest: str, modified_by: str | None) -> dict:
    """Store `content` as this session's version and make it the file's
    current content. Returns the updated session row."""
    sb = get_supabase()
    now = _iso(_now())
    new_path = storage.build_object_path(rec["project_id"], rec["category"], rec.get("filename") or "workbook.xlsx")
    storage.upload_file(new_path, content, rec.get("mime_type") or "application/octet-stream")
    version = _version(row["version_id"]) if row.get("version_id") else None
    old_session_object: str | None = None
    try:
        if version is None:
            if not sb.table(VERSIONS).select("id").eq("file_id", rec["id"]).limit(1).execute().data:
                sb.table(VERSIONS).insert({
                    "file_id": rec["id"],
                    "project_id": rec["project_id"],
                    "version_no": 1,
                    "storage_path": rec["storage_path"],
                    "filename": rec.get("filename") or "",
                    "size_bytes": rec.get("size_bytes"),
                    "sha256": rec.get("sha256"),
                    "source": "original",
                    "created_by": rec.get("uploaded_by"),
                    "created_at": rec.get("created_at"),
                }).execute()
            version = sb.table(VERSIONS).insert({
                "file_id": rec["id"],
                "project_id": rec["project_id"],
                "version_no": current_version_no(rec["id"]) + 1,
                "storage_path": new_path,
                "filename": rec.get("filename") or "",
                "size_bytes": len(content),
                "sha256": digest,
                "source": "excel_edit",
                "edit_session_id": row["id"],
                "created_by": row.get("started_by"),
                "last_modified_by": modified_by,
            }).execute().data[0]
            sb.table(SESSIONS).update({"version_id": version["id"]}).eq("id", row["id"]).execute()
            row = {**row, "version_id": version["id"]}
            audit(
                row.get("started_by"), "file.excel_edit_version", "project_file", rec["id"],
                {"project_id": rec["project_id"], "session_id": row["id"], "version_no": version["version_no"]},
            )
        else:
            old_session_object = version["storage_path"]
            sb.table(VERSIONS).update({
                "storage_path": new_path,
                "size_bytes": len(content),
                "sha256": digest,
                "last_modified_by": modified_by,
                "updated_at": now,
            }).eq("id", version["id"]).execute()
        convertible = office_preview.is_convertible(rec.get("filename"), rec["category"])
        sb.table(FILES).update({
            "storage_path": new_path,
            "size_bytes": len(content),
            "sha256": digest,
            **({"preview_status": "pending", "preview_error": None} if convertible else {}),
        }).eq("id", rec["id"]).execute()
    except Exception:
        try:
            storage.delete_file(new_path)
        except Exception:  # noqa: BLE001
            pass
        raise
    # The session's previous object: only ever this session's own write, never
    # an earlier version (those are kept forever). Deleted when the session
    # closes, so a link signed in the meantime keeps working until then.
    if old_session_object and old_session_object != new_path:
        superseded = [*(row.get("superseded_paths") or []), old_session_object]
        sb.table(SESSIONS).update({"superseded_paths": superseded}).eq("id", row["id"]).execute()
        row = {**row, "superseded_paths": superseded}
    if convertible:
        office_preview.generate_preview(rec["id"], fresh_path=True)
    return row


def sync(row: dict) -> SyncResult:
    """Pull the copy once. The caller holds the session's lease."""
    sb = get_supabase()
    if not row.get("file_id"):
        _close(row, "file_deleted")
        return SyncResult(closed=True)
    item = _drive_item(row["drive_item_id"])
    if item is None:
        _close(row, "drive_missing")
        return SyncResult(closed=True)
    updates = {
        "last_change_at": item.get("lastModifiedDateTime") or row.get("last_change_at"),
        "last_modified_by": _modified_by(item) or row.get("last_modified_by"),
        "last_error": None,
    }
    changed = False
    ctag = item.get("cTag")
    if ctag and ctag != row.get("last_ctag"):
        rec = _file(row["file_id"])
        if rec is None:
            _close(row, "file_deleted")
            return SyncResult(closed=True)
        if (item.get("size") or 0) > get_settings().upload_max_bytes:
            raise ExcelEditError("The workbook grew past the upload size limit; changes not saved.", 413)
        content = _drive_content(row["drive_item_id"])
        if not content.startswith(_ZIP_MAGIC):
            raise ExcelEditError("OneDrive returned something that is not a workbook.", 502)
        digest = hashlib.sha256(content).hexdigest()
        if digest != rec.get("sha256"):
            _write_version(row, rec, content, digest, updates["last_modified_by"])
            updates["last_synced_at"] = _iso(_now())
            changed = True
        updates["last_ctag"] = ctag
    sb.table(SESSIONS).update(updates).eq("id", row["id"]).execute()
    return SyncResult(changed=changed)


def _idle(row: dict) -> bool:
    last = _parse(row.get("last_change_at")) or _parse(row.get("created_at"))
    if last is None:
        return False
    return _now() - last > timedelta(minutes=get_settings().excel_edit_idle_minutes)


def _record_error(session_id: str, exc: Exception) -> None:
    try:
        get_supabase().table(SESSIONS).update({"last_error": str(exc)[:500]}).eq("id", session_id).execute()
    except Exception:  # noqa: BLE001
        pass


def tick_session(session_id: str) -> SyncResult | None:
    """One poller pass over one session; None when another worker holds it."""
    if not _claim(session_id):
        return None
    try:
        row = _session(session_id)
        if not row or row.get("status") != "open":
            return None
        result = sync(row)
        if not result.closed:
            row = _session(session_id) or row
            if _idle(row):
                _close(row, "idle")
                result.closed = True
        return result
    except Exception as exc:  # noqa: BLE001 - one bad session never stops the tick
        logger.exception("excel edit: sync failed for session %s", session_id)
        _record_error(session_id, exc)
        return None
    finally:
        _release(session_id)


def finish(row: dict, user) -> SyncResult:
    """Finish editing: pull a last time and close. Waits briefly for a poller
    tick that holds the session to let go."""
    deadline = time.monotonic() + FINISH_LEASE_WAIT_SECONDS
    while not _claim(row["id"]):
        current = _session(row["id"])
        if not current or current.get("status") != "open":
            return SyncResult(closed=True)
        if time.monotonic() >= deadline:
            raise ExcelEditError("The workbook is saving right now. Try again in a moment.")
        time.sleep(1)
    try:
        current = _session(row["id"]) or row
        result = sync(current)
        if not result.closed:
            _close(_session(row["id"]) or current, "finished", actor_id=user.id)
            result.closed = True
        return result
    except ExcelEditError:
        raise
    except Exception as exc:
        logger.exception("excel edit: finish failed for session %s", row["id"])
        _record_error(row["id"], exc)
        raise ExcelEditError("Could not pull the last changes from Excel. Try again.", 502) from exc
    finally:
        _release(row["id"])


def poll_once() -> dict[str, int]:
    stats = {"sessions": 0, "changed": 0, "closed": 0}
    if not feature_on():
        return stats
    rows = get_supabase().table(SESSIONS).select("id").eq("status", "open").execute().data or []
    for r in rows:
        result = tick_session(r["id"])
        stats["sessions"] += 1
        if result and result.changed:
            stats["changed"] += 1
        if result and result.closed:
            stats["closed"] += 1
    return stats


async def polling_loop() -> None:
    while True:
        try:
            stats = await asyncio.to_thread(poll_once)
            if stats["changed"] or stats["closed"]:
                logger.info("excel edit poll: %s", stats)
        except Exception:  # noqa: BLE001 - the loop must survive any tick failure
            logger.exception("Excel edit poll failed")
        await asyncio.sleep(get_settings().excel_edit_poll_seconds)

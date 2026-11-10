"""Edit in Excel and file versions (docs/EXCEL_EDIT.md, migration 0193).

Internal only: the external estimator never reaches these routes, and the
read-only accountant may see the status and versions but never edit. The
work itself lives in app/services/excel_edit.py.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_project_assignment, require_writer
from app.core.file_categories import INITIAL_CATEGORIES, SENT_GATED_CATEGORIES
from app.core.ratelimit import rate_limit
from app.core.error_codes import RateLimitScope
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES
from app.routers.files import _get_file_checked, _record_access, handoff_locked
from app.services import excel_edit, storage
from app.services.notifications import audit

router = APIRouter(prefix="/projects/{project_id}/files", tags=["files"])

excel_edit_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)


def _internal_file(project_id: str, file_id: str, user: CurrentUser) -> dict:
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return _get_file_checked(project_id, file_id, user)


def _block_reason(rec: dict, user: CurrentUser) -> str | None:
    writer = user.role in WRITER_ROLES
    # The cheap checks first, so a plain read never queries the lock tables
    # for a file nobody could edit anyway.
    reason = excel_edit.block_reason(rec, writer=writer, locked=False, sent=False)
    if reason:
        return reason
    locked = rec["category"] in INITIAL_CATEGORIES and handoff_locked(rec["project_id"])
    sent = rec["category"] in SENT_GATED_CATEGORIES and bool(rec.get("sent_to_estimators_at"))
    return excel_edit.block_reason(rec, writer=writer, locked=locked, sent=sent)


def _raise(exc: excel_edit.ExcelEditError):
    raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/{file_id}/excel-edit")
def excel_edit_status(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    rec = _internal_file(project_id, file_id, user)
    reason = _block_reason(rec, user)
    session = excel_edit.open_session(file_id) if excel_edit.feature_on() else None
    return {
        "editable": reason is None,
        "reason": reason,
        "current_version_no": excel_edit.current_version_no(file_id),
        "session": excel_edit.session_out(session, user),
    }


@router.post("/{file_id}/excel-edit", dependencies=[Depends(require_writer), Depends(excel_edit_rate_limit)])
def excel_edit_start(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    rec = _internal_file(project_id, file_id, user)
    reason = _block_reason(rec, user)
    if reason:
        code = status.HTTP_403_FORBIDDEN if user.role not in WRITER_ROLES else status.HTTP_409_CONFLICT
        if not excel_edit.feature_on():
            code = status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(code, reason)
    try:
        row = excel_edit.start(rec, user)
    except excel_edit.ExcelEditError as exc:
        _raise(exc)
    _record_access(user, rec, "view", {"excel_edit": True})
    return {"edit_url": row["edit_url"], "session": excel_edit.session_out(row, user)}


@router.post("/{file_id}/excel-edit/finish", dependencies=[Depends(require_writer), Depends(excel_edit_rate_limit)])
def excel_edit_finish(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    _internal_file(project_id, file_id, user)
    if user.role not in WRITER_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    row = excel_edit.open_session(file_id)
    if not row:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nobody is editing this file in Excel.")
    try:
        result = excel_edit.finish(row, user)
    except excel_edit.ExcelEditError as exc:
        _raise(exc)
    closed = excel_edit._session(row["id"]) or row
    out = excel_edit.session_out(closed, user)
    return {
        "changed": result.changed or bool(closed.get("version_id")),
        "version_no": out["version_no"] if out else None,
        "session": out,
    }


@router.get("/{file_id}/versions")
def file_versions(
    project_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_project_assignment),
):
    rec = _internal_file(project_id, file_id, user)
    return {"versions": excel_edit.list_versions(rec)}


@router.get("/{file_id}/versions/{version_id}/download")
def file_version_download(
    project_id: str,
    file_id: str,
    version_id: str,
    intent: Literal["view", "download", "prefetch"] = "download",
    user: CurrentUser = Depends(require_project_assignment),
):
    rec = _internal_file(project_id, file_id, user)
    version = excel_edit.get_version(file_id, version_id)
    if not version:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    filename = version.get("filename") or rec.get("filename") or "workbook.xlsx"
    url = storage.signed_url(version["storage_path"], download=filename)
    _record_access(user, rec, intent, {"version_no": version["version_no"]})
    if intent != "prefetch":
        audit(
            user.id, "file.download", "project_file", file_id,
            {"project_id": project_id, "filename": filename, "version_no": version["version_no"]},
        )
    return {"url": url, "filename": filename}

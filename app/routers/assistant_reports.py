"""Assistant report files (docs/ASSISTANT_PHASE4_PLAN.md 3.9.2, 3.9.3, D5).

Who may call what:

  assistant  POST /assistant/tools/reports (the `build_report` housekeeping
             tool): a turn principal only, trusted through the begun ledger
             row (routers.assistant._trusted_turn, phase 3 3.7.3); a human
             gets the bare 404. The workbook is built in process by
             services/assistant_reports.build with the caller's role,
             uploaded to assistant-reports/{user_id}/{id}.xlsx and recorded
             in assistant_reports. The answer never carries a URL;
  human      GET /assistant/reports/{report_id}/download mints a 60 second
             signed link with the filename as the download name, at the
             click, for the person's own ready, unexpired row whose spec the
             person's LIVE role may still read (a demoted Executive cannot
             fetch an earlier evidence workbook); GET /assistant/reports
             lists the person's own rows. Both call
             _refuse_assistant_principal and are on the firewall deny-list,
             so a signed link never reaches the model.

Spec roles come from services/assistant_reports.SPEC_ROLES (one table for
the build and the download). `person_evidence` is never built here: only the
approval-bound evidence route writes it (403 report_spec_forbidden).

Errors are `{"detail": {"code", "message"?}}` with the code in X-Error-Code:
403 report_spec_forbidden, 422 range_invalid, report_params_invalid and
report_spec_invalid, 404 report_not_found, 413 report_too_large, 410
report_expired. Plain def handlers (the Supabase SDK is synchronous).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.assistant import repo
from app.assistant.schemas import (
    ReportBuildIn,
    ReportDownloadOut,
    ReportListOut,
    ReportOut,
    ReportToolOut,
)
from app.assistant.tools._common import is_uuid
from app.core import supabase_client
from app.core.deps import CurrentUser, require_internal
from app.core.ratelimit import export_rate_limit
from app.routers.assistant import (
    _code_error,
    _refuse_assistant_principal,
    _require_assistant,
    _trusted_turn,
)
from app.services import assistant_reports as reports
from app.services import storage
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"], dependencies=[Depends(require_internal)])

# Route literals the WI-13b tool spec repeats.
TOOL_REPORTS_ROUTE = "/assistant/tools/reports"
DOWNLOAD_ROUTE = "/assistant/reports/{report_id}/download"
LIST_ROUTE = "/assistant/reports"
TOOL_NAME = "build_report"

REPORT_SPEC_FORBIDDEN = "report_spec_forbidden"
REPORT_NOT_FOUND = "report_not_found"
REPORT_EXPIRED = "report_expired"
REPORT_EXISTS = "report_exists"
RANGE_INVALID = "range_invalid"
MAILBOX_REVIEW = "mailbox_review"

DOWNLOAD_TTL_SECONDS = 60
LIST_LIMIT_MAX = 100
PERSON_EVIDENCE = "person_evidence"


def _sb():
    return supabase_client.get_supabase()


def _forbidden() -> Any:
    return _code_error(status.HTTP_403_FORBIDDEN, REPORT_SPEC_FORBIDDEN, "Your role cannot read that report.")


def _range_vocabulary() -> tuple[str, ...]:
    from app.services import bid_invitations

    return tuple(bid_invitations.RANGE_NAMES)


def _params(body: ReportBuildIn) -> dict:
    """The body fields as the service takes them: unset fields dropped,
    dates as ISO strings. A `range` outside the spec's vocabulary (only
    bid_invitations takes one) is 422 range_invalid, as is a custom range
    without both dates."""
    if body.range is not None:
        if body.spec != "bid_invitations" or body.range not in _range_vocabulary():
            raise _code_error(status.HTTP_422_UNPROCESSABLE_CONTENT, RANGE_INVALID,
                              f"range must be one of {', '.join(_range_vocabulary())} (bid_invitations only).")
        if body.range == "custom" and (body.date_from is None or body.date_to is None):
            raise _code_error(status.HTTP_422_UNPROCESSABLE_CONTENT, RANGE_INVALID,
                              "A custom range needs date_from and date_to.")
    out: dict[str, Any] = {}
    for key in ("range", "date_from", "date_to", "week", "period", "project_id"):
        value = getattr(body, key)
        if value is None:
            continue
        out[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


@router.post(TOOL_REPORTS_ROUTE.removeprefix("/assistant"), response_model=ReportToolOut,
             dependencies=[Depends(export_rate_limit)])
def tool_build_report(request: Request, raw: dict = Body(...),
                      user: CurrentUser = Depends(require_internal)) -> ReportToolOut:
    """build_report (3.9.2): build one .xlsx for the caller from in-process
    service calls with the caller's role. A human gets 404; the spec's role
    is checked first (403 report_spec_forbidden, always for
    person_evidence), then the body. Returns `{report_id, title, filename,
    spec, row_count, truncated, totals, preview_rows, expires_at_pt}`."""
    turn = _trusted_turn(request, user, TOOL_NAME)
    _require_assistant(user)
    if turn.get("mode") == "mailbox_review":
        raise _code_error(status.HTTP_409_CONFLICT, MAILBOX_REVIEW, "Report files are not built while reviewing a mailbox.")
    spec = raw.get("spec") if isinstance(raw, dict) else None
    if spec == PERSON_EVIDENCE:
        raise _forbidden()
    if isinstance(spec, str) and spec in reports.SPEC_ROLES and user.role not in reports.SPEC_ROLES[spec]:
        raise _forbidden()
    try:
        body = ReportBuildIn.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None
    params = _params(body)
    try:
        out = reports.build(_sb(), user, body.spec, params, conversation_id=turn.get("conversation_id"),
                            run_id=user.via_assistant_run_id)
    except reports.ReportError as exc:
        raise _code_error(exc.status_code, exc.code, str(exc)) from None
    except repo.ReportExists:
        raise _code_error(status.HTTP_409_CONFLICT, REPORT_EXISTS, "This report was already built.") from None
    return ReportToolOut(**out)


def _expired(row: dict) -> bool:
    if row.get("status") != "ready":
        return True
    try:
        when = datetime.fromisoformat(str(row.get("expires_at")).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


@router.get(DOWNLOAD_ROUTE.removeprefix("/assistant"), response_model=ReportDownloadOut,
            dependencies=[Depends(export_rate_limit)])
def download_report(report_id: str, user: CurrentUser = Depends(require_internal)) -> ReportDownloadOut:
    """A fresh 60 second signed link to the person's own report (3.9.3).
    410 report_expired for a purged or expired row; 403
    report_spec_forbidden when the person's live role may no longer read
    the spec. The link is never stored or audited."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    rid = (report_id or "").strip().lower()
    if not is_uuid(rid):
        raise _code_error(status.HTTP_404_NOT_FOUND, REPORT_NOT_FOUND, "That report was not found.")
    row = repo.get_report(user.id, rid)
    if row is None:
        raise _code_error(status.HTTP_404_NOT_FOUND, REPORT_NOT_FOUND, "That report was not found.")
    if _expired(row):
        raise _code_error(status.HTTP_410_GONE, REPORT_EXPIRED,
                          "This report file has expired. Ask the assistant to build it again.")
    if not reports.live_role_may_read(row, user.role):
        raise _forbidden()
    path = str(row.get("storage_path") or "")
    if path != f"{reports.STORAGE_PREFIX}/{user.id}/{row['id']}.xlsx":
        # 0180 A4 pins the path to the owner and the row; anything else is not signed.
        logger.error("assistant reports: report %s carries an unexpected storage path", row.get("id"))
        raise _code_error(status.HTTP_404_NOT_FOUND, REPORT_NOT_FOUND, "That report was not found.")
    filename = str(row.get("filename") or "report.xlsx")
    url = storage.signed_url(path, ttl_seconds=DOWNLOAD_TTL_SECONDS, use_cache=False, download=filename)
    expires = datetime.now(timezone.utc) + timedelta(seconds=DOWNLOAD_TTL_SECONDS)
    try:
        audit(user.id, "assistant.report_download", "assistant_report", str(row["id"]),
              {"spec": row.get("spec"), "filename": filename})
    except Exception:  # noqa: BLE001 - the link is minted; a lost audit row must not block it
        logger.exception("assistant reports: download audit for %s was not written", row.get("id"))
    return ReportDownloadOut(url=url, filename=filename, expires_at=expires.isoformat())


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


@router.get(LIST_ROUTE.removeprefix("/assistant"), response_model=ReportListOut)
def list_reports(limit: int = Query(default=20, ge=1, le=LIST_LIMIT_MAX),
                 user: CurrentUser = Depends(require_internal)) -> ReportListOut:
    """The person's own report files, newest first (no links)."""
    _refuse_assistant_principal(user)
    _require_assistant(user)
    rows = repo.list_reports(user.id, limit=limit)
    return ReportListOut(reports=[
        ReportOut(id=str(r["id"]), spec=str(r.get("spec") or ""), title=str(r.get("title") or ""),
                  filename=str(r.get("filename") or ""), row_count=int(r.get("row_count") or 0),
                  truncated=bool(r.get("truncated")), status=r.get("status") or "ready",
                  expires_at=_iso(r.get("expires_at")), created_at=_iso(r.get("created_at")))
        for r in rows
    ])

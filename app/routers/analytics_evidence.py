"""Evidence routes for Executive and IT Admin (docs/ASSISTANT_PHASE4_PLAN.md
3.2.3, 3.8 and 5.3): "why did X happen" reads under /analytics/evidence.

A separate router from app/routers/analytics.py, so no /analytics/evidence/...
path can collide with /analytics/projects/{project_id}. app/main.py mounts it
with `dependencies=_BIDDING` next to analytics.router; the router repeats the
Bidding gate itself, so it is never served with Bidding off whoever mounts
it. Every route is gated to EVIDENCE_ROLES through deps.require_role (never
analytics._gate, which admits the accountant).

Who may call what:

  both       the GET reads (project timeline, opens, change history, AI
             usage) and the two card-preview reads (one person, the
             estimators of a project): the person in a later UI and the
             assistant through the shim;
  assistant  the two sensitive POSTs (estimator timeline, person evidence),
             approval-bound only: the execute job of an approved
             sensitive_read card. A human, a chat turn principal and a
             header that differs from the token's approval id get the bare
             404 (app/core/approval_bound.py); a missing header is 400. The
             approval row must be `executing`, of this route's tool, about
             the path's ids, and carry the reason the human typed (409
             approval_mismatch otherwise). Each POST appends one
             assistant_evidence_access row (reason from the approval row,
             never from the body) and audits `assistant.evidence_read`
             without the reason; a replay of the same approval re-runs the
             read and writes nothing more.

Errors: `{"detail": {"code", "message"}}` with X-Error-Code: 422
view_invalid, window_invalid, assignment_required, sources_invalid,
field_invalid, group_by_invalid, cursor_invalid; 409 approval_mismatch. An
id that is not a uuid, an unknown project and an id that is not a profile
all answer the bare 404 a missing route gives (byte-identical).

Plain def handlers: the evidence service uses the synchronous Supabase SDK.
Every assistant_* row goes through app/assistant/repo.py.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.assistant import repo
from app.assistant.tools._common import is_uuid
from app.core import supabase_client
from app.core.approval_bound import require_approval_bound
from app.core.deps import CurrentUser, require_role
from app.core.features import SubApp, require_feature
from app.core.ratelimit import report_rate_limit
from app.core.roles import EVIDENCE_ROLES
from app.services import evidence

logger = logging.getLogger(__name__)

# One dependency object for the router gate and the handlers' `user`, so
# FastAPI resolves it once per request.
require_evidence_role = require_role(*EVIDENCE_ROLES)

router = APIRouter(
    prefix="/analytics/evidence",
    tags=["analytics-evidence"],
    dependencies=[Depends(require_feature(SubApp.BIDDING)), Depends(require_evidence_role)],
)

# Route literals the phase 4 evidence tools repeat (app/assistant/tools/evidence.py,
# pinned by a test there).
TIMELINE_ROUTE = "/analytics/evidence/projects/{project_id}/timeline"
OPENS_ROUTE = "/analytics/evidence/projects/{project_id}/opens"
CHANGES_ROUTE = "/analytics/evidence/projects/{project_id}/changes"
AI_USAGE_ROUTE = "/analytics/evidence/ai-usage"
USER_SUBJECT_ROUTE = "/analytics/evidence/subjects/users/{user_id}"
ESTIMATOR_SUBJECTS_ROUTE = "/analytics/evidence/projects/{project_id}/estimator-subjects"
ESTIMATOR_TIMELINE_ROUTE = "/analytics/evidence/projects/{project_id}/estimator-timeline"
PERSON_EVIDENCE_ROUTE = "/analytics/evidence/users/{user_id}"

# The tool names an approval-bound POST serves (assistant_approvals.action_kind
# and assistant_evidence_access.tool).
TOOL_ESTIMATOR_TIMELINE = "estimator_timeline"
TOOL_PERSON_EVIDENCE = "person_evidence"

APPROVAL_MISMATCH = "approval_mismatch"
ASSIGNMENT_REQUIRED = "assignment_required"
WINDOW_INVALID = "window_invalid"
EVIDENCE_READ_AUDIT = "assistant.evidence_read"

_MESSAGES = {
    "view_invalid": "view must be one of activity, file_access, notifications or ai_usage.",
    "window_invalid": "since and until are Pacific dates (YYYY-MM-DD), since first, at most 400 days apart.",
    "assignment_required": "Name the estimator assignment to read.",
    "sources_invalid": "One of the sources is not a timeline source.",
    "field_invalid": "That field is not a project field or a change group.",
    "group_by_invalid": "group_by must be feature, model or day.",
    "cursor_invalid": "That cursor is not one this read returned.",
    "approval_mismatch": "This read does not match the approved card.",
}
_CONFLICT_CODES = frozenset({APPROVAL_MISMATCH})


# ── Errors and parameters ────────────────────────────────────────────────────


def _not_found() -> HTTPException:
    """The bare 404 of a missing route: an unknown id reads like no route."""
    return HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def _code_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": _MESSAGES.get(code, "That read was refused.")},
                         headers={"X-Error-Code": code})


def _evidence_error(exc: evidence.EvidenceError) -> HTTPException:
    if exc.code == "not_found":
        return _not_found()
    if exc.code in _CONFLICT_CODES:
        return _code_error(status.HTTP_409_CONFLICT, exc.code)
    return _code_error(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code)


def _id(value: Any) -> str:
    """A path or filter id: a canonical uuid, or the bare 404."""
    text = str(value or "").strip()
    if not is_uuid(text):
        raise _not_found()
    return str(uuid.UUID(text))


def _optional_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return _id(text) if text else None


def _pt_date(value: str | None) -> date | None:
    """A Pacific date filter (YYYY-MM-DD); 422 window_invalid otherwise."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        if len(text) != 10:
            raise ValueError(text)
        return date.fromisoformat(text)
    except ValueError:
        raise _code_error(status.HTTP_422_UNPROCESSABLE_CONTENT, WINDOW_INVALID) from None


def _sb():
    return supabase_client.get_supabase()


def _role(user: CurrentUser) -> str:
    return str(getattr(user.role, "value", user.role))


# ── The plain reads ──────────────────────────────────────────────────────────


@router.get(TIMELINE_ROUTE.removeprefix("/analytics/evidence"), dependencies=[Depends(report_rate_limit)])
def project_timeline(
    project_id: str,
    since: str | None = Query(default=None, max_length=10),
    until: str | None = Query(default=None, max_length=10),
    sources: str | None = Query(default=None, max_length=300),
    limit: int = Query(default=50, ge=1, le=evidence.TIMELINE_LIMIT_MAX),
    cursor: str | None = Query(default=None, max_length=500),
    user: CurrentUser = Depends(require_evidence_role),
) -> dict:
    """3.8.1 (tool project_timeline): the project's events newest first,
    with `next_cursor`. Estimator subjects show no read time, email status
    or per-person opens here (that is the estimator_timeline card)."""
    pid = _id(project_id)
    wanted = [s.strip() for s in (sources or "").split(",") if s.strip()] or None
    try:
        return evidence.project_timeline(
            _sb(), pid, role=_role(user), since=_pt_date(since), until=_pt_date(until), sources=wanted,
            limit=limit, cursor=(cursor or "").strip() or None,
        )
    except evidence.EvidenceError as exc:
        raise _evidence_error(exc) from None


@router.get(OPENS_ROUTE.removeprefix("/analytics/evidence"), dependencies=[Depends(report_rate_limit)])
def project_opens(project_id: str, user: CurrentUser = Depends(require_evidence_role)) -> dict:
    """3.8.2 (tool file_access_report, second request): project page opens
    per person and kind, with the caveats of what is not recorded."""
    pid = _id(project_id)
    try:
        return evidence.project_opens(_sb(), pid)
    except evidence.EvidenceError as exc:
        raise _evidence_error(exc) from None


@router.get(CHANGES_ROUTE.removeprefix("/analytics/evidence"), dependencies=[Depends(report_rate_limit)])
def change_history(
    project_id: str,
    field: str | None = Query(default=None, max_length=63),
    since: str | None = Query(default=None, max_length=10),
    until: str | None = Query(default=None, max_length=10),
    limit: int = Query(default=50, ge=1, le=evidence.CHANGE_LIMIT_MAX),
    user: CurrentUser = Depends(require_evidence_role),
) -> dict:
    """3.8.3 (tool change_history): field changes with their before values
    (`before_known=false` when the first value predates the trail)."""
    pid = _id(project_id)
    try:
        return evidence.change_history(
            _sb(), pid, field=(field or "").strip() or None, since=_pt_date(since), until=_pt_date(until),
            limit=limit,
        )
    except evidence.EvidenceError as exc:
        raise _evidence_error(exc) from None


@router.get(AI_USAGE_ROUTE.removeprefix("/analytics/evidence"), dependencies=[Depends(report_rate_limit)])
def ai_usage(
    project_id: str | None = Query(default=None, max_length=64),
    since: str | None = Query(default=None, max_length=10),
    until: str | None = Query(default=None, max_length=10),
    group_by: str = Query(default="feature", max_length=20),
    user: CurrentUser = Depends(require_evidence_role),
) -> dict:
    """3.8.4 (tool ai_usage): the company's or one project's AI calls
    grouped by feature, model or day, plus the AE run costs. No per-person
    split and no user argument (that is person_evidence)."""
    pid = _optional_id(project_id)
    try:
        return evidence.ai_usage(_sb(), project_id=pid, since=_pt_date(since), until=_pt_date(until),
                                 group_by=group_by.strip())
    except evidence.EvidenceError as exc:
        raise _evidence_error(exc) from None


# ── Card previews (3.8.5) ────────────────────────────────────────────────────


@router.get(USER_SUBJECT_ROUTE.removeprefix("/analytics/evidence"), dependencies=[Depends(report_rate_limit)])
def user_subject(user_id: str, user: CurrentUser = Depends(require_evidence_role)) -> dict:
    """The person a person_evidence card names: {id, full_name, role,
    is_active}. An id that is not a profile is the bare 404."""
    subject = evidence.user_subject(_sb(), _id(user_id))
    if subject is None:
        raise _not_found()
    return subject


@router.get(ESTIMATOR_SUBJECTS_ROUTE.removeprefix("/analytics/evidence"),
            dependencies=[Depends(report_rate_limit)])
def estimator_subjects(project_id: str, user: CurrentUser = Depends(require_evidence_role)) -> dict:
    """The estimator assignments (active and past) an estimator_timeline
    card chooses from, plus the project {id, number, name}."""
    out = evidence.estimator_subjects(_sb(), _id(project_id))
    if out is None:
        raise _not_found()
    return out


# ── The sensitive POSTs (approval-bound, 3.8.6) ──────────────────────────────


class EstimatorTimelineIn(BaseModel):
    """POST .../estimator-timeline: one card reads one estimator, the one
    the card names. The reason is never in the body (it is on the approval
    row)."""

    assignment_id: str | None = Field(default=None, max_length=64)


class PersonEvidenceIn(BaseModel):
    """POST /analytics/evidence/users/{user_id}. `view` is validated by the
    service (422 view_invalid); `since` and `until` are Pacific dates."""

    view: str | None = Field(default=None, max_length=40)
    since: str | None = Field(default=None, max_length=10)
    until: str | None = Field(default=None, max_length=10)
    project_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=100, ge=1, le=evidence.PERSON_LIMIT_MAX)
    cursor: str | None = Field(default=None, max_length=500)
    xlsx: bool = False


def _same_id(a: Any, b: Any) -> bool:
    try:
        return uuid.UUID(str(a).strip()) == uuid.UUID(str(b).strip())
    except (ValueError, TypeError, AttributeError):
        return False


def _bound_approval(user: CurrentUser, approval_id: str, tool: str, ids: dict[str, str]) -> dict:
    """The approval an approval-bound evidence POST runs for (3.2.3): this
    person's, `executing`, of this route's tool, about the same ids as the
    approved arguments, and carrying the human's reason. Anything else is
    409 approval_mismatch."""
    row = repo.approval_for_route(str(user.id), approval_id)
    if not row or row.get("status") != "executing" or row.get("action_kind") != tool:
        raise _code_error(status.HTTP_409_CONFLICT, APPROVAL_MISMATCH)
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    for key, value in ids.items():
        if not _same_id(args.get(key), value):
            raise _code_error(status.HTTP_409_CONFLICT, APPROVAL_MISMATCH)
    reason = str(row.get("decision_reason") or "").strip()
    if not reason:
        raise _code_error(status.HTTP_409_CONFLICT, APPROVAL_MISMATCH)
    return {**row, "decision_reason": reason}


def _reader_label(user: CurrentUser) -> str:
    try:
        human = repo.human_profile(str(user.id)) or {}
    except Exception:  # noqa: BLE001 - the email still names the reader
        logger.exception("evidence: the reader's profile could not be read")
        human = {}
    return str((human.get("full_name") or "").strip() or human.get("email") or user.email or user.id)[:200]


def _log_access(user: CurrentUser, approval: dict, approval_id: str, *, tool: str, view: str, subject_kind: str,
                subject_id: str | None, subject_label: str | None, project_id: str | None, filters: dict,
                result: dict, xlsx_report_id: str | None) -> bool:
    """One append-only assistant_evidence_access row, then the audit row
    (without the reason). True when this call wrote the row; a replay of the
    same approval writes nothing (the unique key) and audits nothing."""
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    inserted = repo.insert_evidence_access(str(user.id), {
        "reader_label": _reader_label(user),
        "reader_role": _role(user),
        "subject_kind": subject_kind,
        "subject_user_id": subject_id,
        "subject_label": (str(subject_label)[:200] if subject_label else None),
        "project_id": project_id,
        "tool": tool,
        "view": view,
        "filters": filters,
        "row_count": len(rows),
        # One page of a paged view is partial too (person_evidence's next_cursor).
        "truncated": bool(result.get("truncated")) or bool(result.get("next_cursor")),
        "xlsx_report_id": xlsx_report_id,
        "reason": approval["decision_reason"],
        "approval_id": approval_id,
        "conversation_id": approval.get("conversation_id"),
        "run_id": getattr(user, "via_assistant_run_id", None),
    })
    if inserted is None:
        return False
    try:
        from app.services import notifications

        notifications.audit(str(user.id), EVIDENCE_READ_AUDIT, "profile", subject_id,
                            {"view": view, "approval_id": approval_id})
    except Exception:  # noqa: BLE001 - the access log row is the record; a lost audit row is logged
        logger.exception("evidence: audit row for approval %s was not written", approval_id)
    return True


@router.post(ESTIMATOR_TIMELINE_ROUTE.removeprefix("/analytics/evidence"),
             dependencies=[Depends(report_rate_limit)])
def estimator_timeline(
    project_id: str,
    body: EstimatorTimelineIn,
    approval_id: str = Depends(require_approval_bound),
    user: CurrentUser = Depends(require_evidence_role),
) -> dict:
    """3.8.6 (tool estimator_timeline): the one estimator of the approved
    assignment, oldest first, with the summary block (emailed, bell read,
    first open, rounds). 422 assignment_required without an assignment;
    409 approval_mismatch for an assignment the card did not name or of
    another project."""
    pid = _id(project_id)
    assignment = (body.assignment_id or "").strip()
    if not assignment:
        raise _code_error(status.HTTP_422_UNPROCESSABLE_CONTENT, ASSIGNMENT_REQUIRED)
    if not is_uuid(assignment):
        raise _code_error(status.HTTP_409_CONFLICT, APPROVAL_MISMATCH)
    assignment = str(uuid.UUID(assignment))
    approval = _bound_approval(user, approval_id, TOOL_ESTIMATOR_TIMELINE,
                               {"project_id": pid, "assignment_id": assignment})
    try:
        result = evidence.estimator_timeline(_sb(), pid, assignment)
    except evidence.EvidenceError as exc:
        raise _evidence_error(exc) from None
    subject = result.get("subject") if isinstance(result.get("subject"), dict) else {}
    _log_access(user, approval, approval_id, tool=TOOL_ESTIMATOR_TIMELINE, view="estimator_timeline",
                subject_kind="estimator", subject_id=subject.get("user_id"), subject_label=subject.get("label"),
                project_id=pid, filters={"assignment_id": assignment}, result=result, xlsx_report_id=None)
    return result


def _person_report(user: CurrentUser, approval: dict, approval_id: str, subject: dict, view: str,
                   result: dict, project_id: str | None) -> str | None:
    """The person_evidence workbook of this approval: the one an earlier
    attempt built, else a new one (assistant_reports.build_person_report,
    before the access row is written)."""
    prior = repo.report_for_approval(str(user.id), approval_id)
    if prior is not None:
        return str(prior["id"])
    from app.services import assistant_reports

    window = result.get("window") if isinstance(result.get("window"), dict) else {}
    try:
        built = assistant_reports.build_person_report(
            _sb(), user, subject, view,
            {**result, "since": window.get("since"), "until": window.get("until"), "project_id": project_id},
            conversation_id=approval.get("conversation_id"),
            run_id=getattr(user, "via_assistant_run_id", None), approval_id=approval_id,
        )
    except repo.ReportExists:
        prior = repo.report_for_approval(str(user.id), approval_id)
        if prior is None:
            raise
        return str(prior["id"])
    except assistant_reports.ReportError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)},
                            headers={"X-Error-Code": exc.code}) from None
    return str(built["report_id"])


@router.post(PERSON_EVIDENCE_ROUTE.removeprefix("/analytics/evidence"), dependencies=[Depends(report_rate_limit)])
def person_evidence(
    user_id: str,
    body: PersonEvidenceIn,
    approval_id: str = Depends(require_approval_bound),
    user: CurrentUser = Depends(require_evidence_role),
) -> dict:
    """3.8.6 (tool person_evidence): one person's own rows for `view`
    (activity, file_access, notifications, ai_usage), the assistant's rows
    only as `assistant_counts`. With `xlsx` the workbook is built first and
    its id is logged with the access row and returned as `xlsx_report_id`
    (never a URL). A replay re-runs the read and returns the first
    attempt's workbook id."""
    subject_id = _id(user_id)
    project_id = _optional_id(body.project_id)
    approval = _bound_approval(user, approval_id, TOOL_PERSON_EVIDENCE, {"user_id": subject_id})
    view = (body.view or "").strip()
    since, until = _pt_date(body.since), _pt_date(body.until)
    cursor = (body.cursor or "").strip() or None
    try:
        result = evidence.person_evidence(_sb(), subject_id, view, since=since, until=until,
                                          project_id=project_id, limit=body.limit, cursor=cursor)
    except evidence.EvidenceError as exc:
        raise _evidence_error(exc) from None
    subject = result.get("subject") if isinstance(result.get("subject"), dict) else {}
    window = result.get("window") if isinstance(result.get("window"), dict) else {}
    existing = repo.evidence_access_for_approval(str(user.id), approval_id)
    if existing is not None:
        report_id = existing.get("xlsx_report_id")
    elif body.xlsx:
        report_id = _person_report(user, approval, approval_id, subject, view, result, project_id)
    else:
        report_id = None
    if existing is None:
        _log_access(user, approval, approval_id, tool=TOOL_PERSON_EVIDENCE, view=view, subject_kind="user",
                    subject_id=subject_id, subject_label=subject.get("full_name"), project_id=project_id,
                    filters={"since": window.get("since"), "until": window.get("until"), "project_id": project_id,
                             "limit": body.limit, "cursor": bool(cursor), "xlsx": bool(body.xlsx)},
                    result=result, xlsx_report_id=report_id)
    return {**result, "xlsx_report_id": str(report_id) if report_id else None}

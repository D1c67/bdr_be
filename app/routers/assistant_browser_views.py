"""Owner-only encrypted browser content and explicit audited IT Admin reads."""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.core.deps import CurrentUser, require_internal
from app.core.roles import Role
from app.routers.assistant import _refuse_assistant_principal
from app.routers.assistant_browser import _enabled, _human
from app.services import assistant_browser_help as help_flow
from app.services import assistant_browser_records as records

router = APIRouter(prefix="/assistant", tags=["assistant-browser"])
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


class HelpReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_help_id: UUID
    client_msg_id: UUID
    action: Literal["message", "retry", "give_up"]
    body: str | None = Field(default=None, max_length=2000)
    secret_value: SecretStr | None = Field(default=None, max_length=64)


class InspectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    owner_id: UUID
    reason: str = Field(min_length=10, max_length=500)


def _body(model: type[BaseModel], value: Any) -> Any:
    try:
        return model.model_validate(value)
    except ValidationError:
        raise HTTPException(422, "Browser request fields are invalid.", headers=HEADERS) from None


def _run(call, *, status: int = 404):
    try:
        return call()
    except help_flow.BrowserTaskCapped:
        # A fixed, owner-safe refusal: the owner must stop the task and start a new read.
        raise HTTPException(409, help_flow.CAPPED, headers=HEADERS) from None
    except records.BrowserContentUnavailable:
        raise HTTPException(status, "Browser content is unavailable.", headers=HEADERS) from None
    except Exception:
        raise HTTPException(503, "Browser content is unavailable.", headers=HEADERS) from None


@router.get("/browser-tasks/{job_id}/trace")
def browser_trace(job_id: UUID, response: Response, before: str | None = Query(default=None, max_length=500),
                  user: CurrentUser = Depends(_human)):
    _enabled()
    response.headers.update(HEADERS)
    return _run(lambda: records.owner_trace(user.id, str(job_id), before=before))


@router.get("/browser-tasks/{job_id}/records/{record_id}/content")
def browser_record_content(job_id: UUID, record_id: UUID, user: CurrentUser = Depends(_human)):
    _enabled()
    blob, mime = _run(lambda: records.screenshot(user.id, str(job_id), str(record_id)))
    return Response(blob, media_type=mime, headers={**HEADERS, "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.get("/browser-tasks/{job_id}/help")
def browser_help(job_id: UUID, response: Response, user: CurrentUser = Depends(_human)):
    _enabled()
    response.headers.update(HEADERS)
    return _run(lambda: help_flow.owner_help(user.id, str(job_id)))


@router.post("/browser-tasks/{job_id}/help")
def browser_help_reply(job_id: UUID, response: Response, body: Any = Body(None),
                       user: CurrentUser = Depends(_human)):
    _enabled()
    payload: HelpReplyIn = _body(HelpReplyIn, body)
    response.headers.update(HEADERS)
    return _run(lambda: help_flow.owner_reply(user.id, str(job_id), expected_help_id=str(payload.expected_help_id),
        action=payload.action, client_msg_id=str(payload.client_msg_id), body=payload.body,
        secret_value=payload.secret_value.get_secret_value() if payload.secret_value is not None else None), status=409)


@router.get("/browser-tasks/{job_id}/views")
def browser_task_views(job_id: UUID, response: Response, user: CurrentUser = Depends(_human)):
    response.headers.update(HEADERS)
    return _run(lambda: records.owner_views(user.id, str(job_id)))


@router.post("/inspection/browser-tasks/{job_id}/read")
def inspect_browser_task(job_id: UUID, response: Response, body: Any = Body(None),
                         user: CurrentUser = Depends(require_internal)):
    _refuse_assistant_principal(user)
    if user.role != Role.IT_ADMIN or user.is_active is not True:
        raise HTTPException(403, "Not permitted", headers=HEADERS)
    payload: InspectionIn = _body(InspectionIn, body)
    response.headers.update(HEADERS)
    return _run(lambda: records.inspect(user.id, str(payload.owner_id), str(job_id), payload.reason), status=403)


@router.post("/inspection/browser-tasks/{job_id}/records/{record_id}/content")
def inspect_browser_screenshot(job_id: UUID, record_id: UUID, body: Any = Body(None),
                               user: CurrentUser = Depends(require_internal)):
    _refuse_assistant_principal(user)
    if user.role != Role.IT_ADMIN or user.is_active is not True:
        raise HTTPException(403, "Not permitted", headers=HEADERS)
    payload: InspectionIn = _body(InspectionIn, body)
    blob, mime = _run(lambda: records.inspect_screenshot(user.id, str(payload.owner_id), str(job_id),
                                                       str(record_id), payload.reason), status=403)
    return Response(blob, media_type=mime, headers={**HEADERS, "Content-Security-Policy": "default-src 'none'; sandbox"})

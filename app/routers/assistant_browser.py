"""Human-only personal browser settings and owner task metadata.

Credentials never enter a model tool, assistant transcript or validation
error. Task creation requires an executing owner approval.
"""
from __future__ import annotations

from uuid import UUID
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.core.config import get_settings
from app.core.approval_bound import require_approval_bound
from app.core.deps import CurrentUser, require_writer
from app.assistant import repo
from app.routers.assistant import _refuse_assistant_principal, _require_assistant
from app.services import assistant_browser as browser

router = APIRouter(prefix="/assistant", tags=["assistant-browser"])


class CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    label: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=1024)
    expected_version: int | None = Field(default=None, ge=0)
    acknowledge_personal_password: bool


def _human(response: Response, user: CurrentUser = Depends(require_writer)) -> CurrentUser:
    response.headers["Cache-Control"] = "no-store"
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return user


def _enabled() -> None:
    if not getattr(get_settings(), "assistant_browser_enabled", False):
        raise HTTPException(404, "Not Found", headers={"Cache-Control": "no-store"})


def _unavailable() -> HTTPException:
    return HTTPException(409, "Personal browser credentials are unavailable. Reload and try again.",
                         headers={"Cache-Control": "no-store"})


@router.get("/browser-sites")
def browser_sites(user: CurrentUser = Depends(_human)):
    return browser.site_status()


@router.get("/browser-credentials")
def browser_credentials(user: CurrentUser = Depends(_human)):
    _enabled()
    return browser.credentials(user.id)


@router.put("/browser-credentials/{service}")
def put_credential(service: str, body: Any = Body(None), user: CurrentUser = Depends(_human)):
    _enabled()
    try:
        payload = CredentialIn.model_validate(body)
    except ValidationError:
        # FastAPI's default validation includes raw input, including passwords.
        raise HTTPException(422, "Credential fields are invalid.", headers={"Cache-Control": "no-store"}) from None
    try:
        return browser.save_credential(user.id, service, label=payload.label, username=payload.username,
            password=payload.password.get_secret_value(), expected_version=payload.expected_version,
            acknowledge_personal_password=payload.acknowledge_personal_password)
    except Exception:
        raise _unavailable() from None


@router.delete("/browser-credentials/{service}", status_code=204)
def delete_credential(service: str, expected_version: int = Query(ge=1), user: CurrentUser = Depends(_human)):
    _enabled()
    try:
        browser.revoke_credential(user.id, service, expected_version)
    except Exception:
        raise _unavailable() from None
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/browser-tasks")
def browser_tasks(user: CurrentUser = Depends(_human)):
    _enabled()
    return browser.tasks(user.id)


def _reader(response: Response, user: CurrentUser = Depends(require_writer)) -> CurrentUser:
    response.headers["Cache-Control"] = "no-store"
    _require_assistant(user)
    if user.via_assistant_run_id:
        cid = repo.conversation_for_run(user.id, user.via_assistant_run_id)
        conversation = repo.get_conversation(user.id, cid) if cid else None
        if not conversation or conversation.get("mode") != "normal" or conversation.get("mailbox_tainted_at"):
            raise HTTPException(404, "Not Found")
    return user


@router.post("/browser-tasks/preview")
def preview_browser_task(body: Any = Body(None), user: CurrentUser = Depends(_reader)):
    _enabled()
    if not user.via_assistant_run_id:
        raise HTTPException(404, "Not Found")
    try:
        return browser.prepare(user.id, user.via_assistant_run_id, body)
    except Exception:
        raise HTTPException(409, "Personal browser request is unavailable.") from None


@router.get("/browser-tasks/by-approval/{approval_id}")
def recover_browser_task(approval_id: UUID, user: CurrentUser = Depends(_reader)):
    _enabled()
    row = repo.browser_job_by_approval(user.id, str(approval_id))
    if not row:
        raise HTTPException(404, "Not Found")
    return browser.task_metadata(row)


@router.get("/browser-tasks/{job_id}")
def browser_task(job_id: UUID, user: CurrentUser = Depends(_reader)):
    _enabled()
    result = browser.task(user.id, str(job_id))
    if result is None:
        raise HTTPException(404, "Not Found", headers={"Cache-Control": "no-store"})
    return result


@router.post("/browser-tasks")
def create_browser_task(body: Any = Body(None), user: CurrentUser = Depends(_reader),
                        approval_id: str = Depends(require_approval_bound)):
    _enabled()
    try:
        return browser.enqueue(user.id, user.via_assistant_run_id, approval_id, body)
    except Exception:
        raise HTTPException(409, "Personal browser approval is unavailable.",
                            headers={"Cache-Control": "no-store"}) from None

"""Model-facing browser routes under /assistant/browser-requests
(docs/ASSISTANT_BROWSER_V2_PLAN.md 4.3, 5.3).

Owner: WI-5. Included in app/main.py by WI-4. These routes only create
admissions, request rows and server-built cards, read the caller's own
tasks and stop them; they never grant, fill or approve anything (those are
human routes under the firewall's denied prefixes).

Who may call:
- POST /tasks, /site-access, /credential: the assistant principal only,
  inside a turn. The trusted turn (the begun ledger row named by the
  X-Assistant-Idempotency-Key header the executor sets) gives the
  conversation, its taint and the turn's message; nothing comes from model
  arguments. A human gets the bare 404.
- GET /tasks/{id}, POST /tasks/{id}/cancel, GET /sites: the assistant or the
  owner (the browser task card's Cancel button), own rows only.

Every response is no-store. Refusals are 409 with {code, message}; the
message is a fixed sentence, never the caller's input.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.assistant import repo
from app.core.deps import CurrentUser, require_writer
from app.routers.assistant import _require_assistant, _trusted_turn
from app.services import assistant_browser as browser

router = APIRouter(prefix="/assistant/browser-requests", tags=["assistant-browser-requests"])

ACCOUNT_LABEL = r"^[A-Za-z0-9 _.-]{1,40}$"
NO_STORE = {"Cache-Control": "no-store"}
# The tool names the executor begins the ledger row with (WI-12).
TOOL_TASK_START = "browser_task_start"
TOOL_SITE_ACCESS = "request_site_access"
TOOL_CREDENTIAL = "request_site_credential"


class TaskStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    site_key: str | None = Field(default=None, pattern=r"^[a-z0-9_.:-]{2,80}$")
    start_url: str | None = Field(default=None, max_length=2048)
    goal: str = Field(min_length=1, max_length=2000)
    project_id: str | None = None
    account_label: str | None = Field(default=None, pattern=ACCOUNT_LABEL)


class SiteAccessIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    site_key: str | None = Field(default=None, pattern=r"^[a-z0-9_.:-]{2,80}$")
    url: str | None = Field(default=None, max_length=2048)
    reason_code: Literal["task_needs_site", "user_asked", "link_from_email", "link_from_page"]


class CredentialRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    site_key: str = Field(pattern=r"^[a-z0-9_.:-]{2,80}$")
    account_label: str | None = Field(default=None, pattern=ACCOUNT_LABEL)
    reason_code: Literal["no_saved_login", "login_rejected", "user_asked"]


def _caller(response: Response, user: CurrentUser = Depends(require_writer)) -> CurrentUser:
    """Writer roles with a working assistant, while browser v2 is on."""
    response.headers["Cache-Control"] = "no-store"
    _require_assistant(user)
    if not browser.v2_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found", headers=NO_STORE)
    return user


def _refused(exc: browser.AdmissionRefused) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if exc.code == "not_found" else status.HTTP_409_CONFLICT
    return HTTPException(code, {"code": exc.code, "message": exc.message}, headers=NO_STORE)


def _turn(request: Request, user: CurrentUser, tool_name: str) -> dict:
    """The trusted turn of the calling assistant run; a normal conversation
    without mailbox content (4.2: never from a mailbox review)."""
    turn = _trusted_turn(request, user, tool_name)
    if turn.get("mode") != "normal" or turn.get("mailbox_tainted") or not turn.get("conversation_id"):
        raise _refused(browser.AdmissionRefused("conversation"))
    return turn


def _reader_checks(user: CurrentUser) -> None:
    """An assistant caller of a read or cancel must be in a normal,
    mailbox-free conversation (browser tools are never offered in a
    mailbox review); the owner reads directly."""
    run_id = getattr(user, "via_assistant_run_id", None)
    if not run_id:
        return
    cid = repo.conversation_for_run(user.id, run_id)
    conversation = repo.get_conversation(user.id, cid) if cid else None
    if not conversation or conversation.get("mode") != "normal" or conversation.get("mailbox_tainted_at"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found", headers=NO_STORE)


def _call(fn, *args: Any, **kwargs: Any) -> dict:
    try:
        return fn(*args, **kwargs)
    except browser.AdmissionRefused as exc:
        raise _refused(exc) from None


@router.post("/tasks")
def start_task(request: Request, body: TaskStartIn, user: CurrentUser = Depends(_caller)) -> dict:
    turn = _turn(request, user, TOOL_TASK_START)
    tainted, fresh = browser.turn_flags(user.id, turn)
    return _call(browser.admit, user.id, user.via_assistant_run_id, str(turn["conversation_id"]),
                 body.model_dump(exclude_none=True), tainted=tainted, fresh_user_message=fresh)


@router.get("/tasks/{job_id}")
def task_status(job_id: str, user: CurrentUser = Depends(_caller)) -> dict:
    _reader_checks(user)
    return _call(browser.task_status, user.id, job_id)


@router.post("/tasks/{job_id}/cancel")
def cancel_task(job_id: str, user: CurrentUser = Depends(_caller)) -> dict:
    _reader_checks(user)
    return _call(browser.cancel_task, user.id, job_id)


@router.get("/sites")
def sites(user: CurrentUser = Depends(_caller)) -> dict:
    _reader_checks(user)
    return _call(browser.sites_list, user.id)


@router.post("/site-access")
def request_site_access(request: Request, body: SiteAccessIn, user: CurrentUser = Depends(_caller)) -> dict:
    turn = _turn(request, user, TOOL_SITE_ACCESS)
    return _call(browser.request_site_access, user.id, user.via_assistant_run_id, str(turn["conversation_id"]),
                 body.model_dump(exclude_none=True))


@router.post("/credential")
def request_site_credential(request: Request, body: CredentialRequestIn,
                            user: CurrentUser = Depends(_caller)) -> dict:
    turn = _turn(request, user, TOOL_CREDENTIAL)
    return _call(browser.request_site_credential, user.id, user.via_assistant_run_id,
                 str(turn["conversation_id"]), body.model_dump(exclude_none=True))

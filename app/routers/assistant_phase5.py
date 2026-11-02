"""Phase 5 human settings/audit routes and assistant-only web tools."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.assistant import access, repo
from app.core.config import get_settings
from app.core.deps import CurrentUser, require_internal
from app.core.roles import Role
from app.routers.assistant import _refuse_assistant_principal, _require_assistant, _trusted_turn
from app.services import assistant_calendar, assistant_ms, assistant_web
from app.services.assistant_graph import GraphFailure

router = APIRouter(prefix="/assistant", tags=["assistant"])


class CalendarPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    mode: Literal["context", "all"] = "context"
    lead_minutes: int = Field(default=15, ge=5, le=180)


def _human(user: CurrentUser, response: Response) -> None:
    _refuse_assistant_principal(user)
    _require_assistant(user)
    response.headers["Cache-Control"] = "no-store"


@router.get("/calendar/preferences")
def calendar_preferences(response: Response, user: CurrentUser = Depends(require_internal)):
    _human(user, response)
    return assistant_calendar.preferences(user.id)


@router.patch("/calendar/preferences")
def calendar_preferences_patch(body: CalendarPreferences, response: Response,
                               user: CurrentUser = Depends(require_internal)):
    _human(user, response)
    try:
        if body.enabled:
            repo.ensure_delegation(user.id, user.aal)
        return assistant_calendar.update_preferences(user.id, **body.model_dump())
    except (ValueError, PermissionError, GraphFailure, assistant_ms.OutlookError):
        raise HTTPException(409, "Calendar access is unavailable.", headers={"Cache-Control": "no-store"}) from None


def _admin(user: CurrentUser, response: Response):
    _human(user, response)
    if user.role != Role.IT_ADMIN:
        raise HTTPException(403, "Access log is unavailable.")


@router.get("/mailbox-access")
def mailbox_access(response: Response, limit: int = Query(25, ge=1, le=50), before: str | None = None,
                   user: CurrentUser = Depends(require_internal)):
    _admin(user, response)
    try:
        rows = repo.mailbox_access_list(user.id, limit=limit + 1, before=before)
    except ValueError:
        raise HTTPException(422, "Access log cursor is unavailable.") from None
    items = [{"id": r["id"], "reader_label": r.get("reader_label") or r.get("user_id"),
              "target_upn": r.get("target_upn"), "reason": r.get("reason"),
              "created_at": r.get("created_at"), "outcome": r.get("outcome")} for r in rows[:limit]]
    return {"items": items, "next_before": f"{items[-1]['created_at']}|{items[-1]['id']}" if len(rows) > limit else None,
            "exclusions": sorted(get_settings().mailbox_exclusion_set)}


@router.get("/evidence-access")
def evidence_access(response: Response, limit: int = Query(25, ge=1, le=50), before: str | None = None,
                    user: CurrentUser = Depends(require_internal)):
    _admin(user, response)
    try:
        rows = repo.evidence_access_list(user.id, limit=limit + 1, before=before)
    except ValueError:
        raise HTTPException(422, "Access log cursor is unavailable.") from None
    items = [{"id": r["id"], "user_label": r.get("reader_label") or r.get("reader_user_id"),
              "target_label": r.get("subject_label") or r.get("subject_user_id"), "view": r.get("view"),
              "reason": r.get("reason"), "created_at": r.get("created_at")} for r in rows[:limit]]
    return {"items": items, "next_before": f"{items[-1]['created_at']}|{items[-1]['id']}" if len(rows) > limit else None}


def _web_turn(request: Request, user: CurrentUser, name: str) -> dict:
    _require_assistant(user)
    turn = _trusted_turn(request, user, name)
    if turn.get("mode") != "normal" or turn.get("mailbox_tainted") or access.owner_check(user.id).problem:
        raise HTTPException(409, "Web access is unavailable.")
    return turn


@router.get("/web/search")
def web_search(request: Request, q: str = Query(min_length=1, max_length=300),
               limit: int = Query(5, ge=1, le=10), user: CurrentUser = Depends(require_internal)):
    turn = _web_turn(request, user, "web_search")
    try:
        return assistant_web.search(q, limit, user_texts=turn.get("user_texts") or [turn.get("user_text", "")])
    except assistant_web.WebUnavailable:
        raise HTTPException(409, "Web access is unavailable.") from None


@router.get("/web/fetch")
def web_fetch(request: Request, url: str = Query(min_length=1, max_length=2048),
              user: CurrentUser = Depends(require_internal)):
    turn = _web_turn(request, user, "web_fetch")
    try:
        return assistant_web.fetch(url, user_texts=turn.get("user_texts") or [turn.get("user_text", "")])
    except assistant_web.WebUnavailable:
        raise HTTPException(409, "Web access is unavailable.") from None

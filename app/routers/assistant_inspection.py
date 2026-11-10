"""Audited human IT Admin inspection and owner-visible conversation view notices."""

import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, StringConstraints

from app.assistant import repo
from app.core.deps import CurrentUser, require_internal
from app.core.roles import Role
from app.routers.assistant import _refuse_assistant_principal, _require_assistant
from app.services import assistant_mail_send, storage

router = APIRouter(prefix="/assistant", tags=["assistant"])
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class InspectionIn(BaseModel):
    owner_id: UUID
    reason_kind: Literal["support", "legal", "offboarding", "other"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)]


def _error(status: int, message: str) -> HTTPException:
    return HTTPException(status, message, headers=HEADERS)


# Browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md 10.7): an IT Admin reading a
# conversation never sees what the owner approved or typed in a browser card.
# The cards carry ids and statuses only (secrets never enter chat), so this is
# a second fence: the cards that mirror a held request, a sign-in code, a
# credential or a live sign-in lose everything but their type and status, and
# a browser result loses its list of released actions (their labels are page
# text from the held request). Only browser blocks and the results of the
# browser tools are touched; every other block and result passes unchanged.
BROWSER_PRIVATE_CARDS = frozenset({
    "browser_final_action", "browser_code_request", "browser_credential_request", "browser_live_signin",
})
BROWSER_PRIVATE_KEYS = frozenset({
    "release_token", "body_summary", "body_sha256", "url_sha256", "secret", "password", "cookies",
})
BROWSER_TOOLS = frozenset({
    "browser_task_start", "browser_task_status", "browser_task_cancel", "browser_sites_list",
    "request_site_access", "request_site_credential",
})


def _scrub_browser_value(value: object) -> object:
    if isinstance(value, list):
        return [_scrub_browser_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict = {}
    for key, item in value.items():
        if key in BROWSER_PRIVATE_KEYS:
            continue
        if key == "released_actions":
            out["released_action_count"] = len(item) if isinstance(item, list) else 0
            continue
        out[key] = _scrub_browser_value(item)
    return out


def _scrub_block(block: object) -> object:
    if not isinstance(block, dict) or not str(block.get("type") or "").startswith("browser_"):
        return block
    if block["type"] in BROWSER_PRIVATE_CARDS:
        return {"type": block["type"], "status": block.get("status"), "private": True}
    return _scrub_browser_value(block)


def _scrub_browser(result: dict) -> dict:
    """Apply the 10.7 exclusions to the blocks of the messages and the
    payloads of the browser tools' results in one inspection result."""
    for message in result.get("messages") or []:
        if isinstance(message, dict) and isinstance(message.get("blocks"), list):
            message["blocks"] = [_scrub_block(block) for block in message["blocks"]]
    for item in result.get("tool_results") or []:
        if isinstance(item, dict) and item.get("tool_name") in BROWSER_TOOLS and item.get("payload") is not None:
            item["payload"] = _scrub_browser_value(item["payload"])
    return result


def _inspect(conversation_id: UUID, body: InspectionIn, action: str, user: CurrentUser) -> dict:
    _refuse_assistant_principal(user)
    if user.role != Role.IT_ADMIN or user.is_active is not True:
        raise _error(403, "Not permitted")
    try:
        result = repo.admin_conversation_read(user.id, str(body.owner_id), str(conversation_id),
                    reason_kind=body.reason_kind, reason=body.reason, action=action)
    except Exception:
        raise _error(503, "Conversation inspection is unavailable.") from None
    if result.get("ok") is not True:
        code = result.get("code")
        status = {"forbidden": 403, "not_found": 404, "reason_required": 422}.get(code, 409)
        raise _error(status, "Conversation inspection is unavailable.")
    return _scrub_browser(result)


@router.post("/inspection/conversations/{conversation_id}/read")
def read_conversation(conversation_id: UUID, body: InspectionIn, response: Response,
                      user: CurrentUser = Depends(require_internal)):
    result = _inspect(conversation_id, body, "read", user)
    response.headers.update(HEADERS)
    return result


@router.post("/inspection/conversations/{conversation_id}/export")
def export_conversation(conversation_id: UUID, body: InspectionIn,
                        user: CurrentUser = Depends(require_internal)):
    result = _inspect(conversation_id, body, "export", user)
    headers = {**HEADERS, "Content-Disposition": f'attachment; filename="assistant-conversation-{conversation_id}.json"'}
    return Response(json.dumps(jsonable_encoder(result), ensure_ascii=False), media_type="application/json", headers=headers)


@router.get("/conversations/{conversation_id}/views")
def conversation_views(conversation_id: UUID, response: Response,
                       user: CurrentUser = Depends(require_internal)):
    _refuse_assistant_principal(user)
    try:
        conversation = repo.get_conversation(user.id, str(conversation_id))
        if not conversation:
            raise _error(404, "Not Found")
        rows = repo.conversation_views(user.id, str(conversation_id))
    except HTTPException:
        raise
    except Exception:
        raise _error(503, "Conversation view history is unavailable.") from None
    response.headers.update(HEADERS)
    # The repo projection excludes private reasons and other users' content.
    return {"items": rows}


@router.get("/files/{file_id}/preview")
def preview_attachment(file_id: UUID, response: Response, user: CurrentUser = Depends(require_internal)):
    _refuse_assistant_principal(user)
    _require_assistant(user)
    try:
        rows = assistant_mail_send.files(user.id, [str(file_id)])
        url = storage.signed_url(rows[0]["storage_path"], ttl_seconds=60, use_cache=False)
    except Exception:
        raise _error(404, "Attachment unavailable.") from None
    response.headers.update(HEADERS)
    return {"url": url, "mime_type": "application/pdf"}

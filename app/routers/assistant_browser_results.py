"""Human-only "Continue in a new chat" for browser results
(docs/ASSISTANT_BROWSER_V2_PLAN.md 4.6 item 3).

The user ticks facts on a browser_result block; this route starts a new
conversation whose first user message is the selected text (origin
user_carried). The click is the same act as pasting, so the new conversation
starts untainted; no turn starts until the user asks.

app/main.py mounts this router behind require_browser_v2 (writer roles, 404
while ASSISTANT_BROWSER_V2_ENABLED is off), and the firewall denies the
prefix /assistant/browser-results to the assistant principal; the route also
refuses that principal itself.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, require_writer
from app.routers.assistant import _refuse_assistant_principal, _require_assistant
from app.services import assistant_browser_chat as chat

router = APIRouter(prefix="/assistant/browser-results", tags=["assistant-browser-results"])
NO_STORE = {"Cache-Control": "no-store"}
_CARRY_STATUS = {"not_found": 404, "no_facts": 422}
_CARRY_TEXT = {
    "not_found": "This browser result is not available.",
    "no_facts": "Tick at least one fact to carry over.",
}


class CarryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_ids: list[str] = Field(min_length=1, max_length=50)


class CarryOut(BaseModel):
    conversation_id: str
    message_id: str


def _human(response: Response, user: CurrentUser = Depends(require_writer)) -> CurrentUser:
    response.headers["Cache-Control"] = "no-store"
    _refuse_assistant_principal(user)
    _require_assistant(user)
    return user


@router.post("/{message_id}/carry", response_model=CarryOut, status_code=201)
def carry(message_id: str, body: CarryIn, user: CurrentUser = Depends(_human)) -> CarryOut:
    """Start a new untainted conversation whose first user message is the
    selected facts, origin user_carried."""
    fact_ids = [f for f in body.fact_ids if isinstance(f, str) and 0 < len(f) <= 80]
    try:
        result = chat.carry_result(user.id, message_id, fact_ids)
    except chat.CarryRefused as exc:
        message = _CARRY_TEXT.get(exc.code, _CARRY_TEXT["not_found"])
        raise HTTPException(_CARRY_STATUS.get(exc.code, 404), {"code": exc.code, "message": message, "msg": message},
                            headers={**NO_STORE, "X-Error-Code": exc.code}) from None
    return CarryOut(**result)

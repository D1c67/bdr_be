"""Application-access Outlook status. There is no per-user OAuth flow."""

from fastapi import APIRouter, Depends, HTTPException, Response

from app.core.deps import CurrentUser, require_internal
from app.routers.assistant import _refuse_assistant_principal
from app.services import assistant_ms

router = APIRouter(prefix="/assistant/outlook", tags=["assistant"])


@router.get("")
def outlook_status(response: Response, user: CurrentUser = Depends(require_internal)):
    _refuse_assistant_principal(user)
    response.headers["Cache-Control"] = "no-store"
    try:
        return assistant_ms.status(user.id)
    except assistant_ms.OutlookError:
        raise HTTPException(409, "Outlook application access is unavailable.",
                            headers={"Cache-Control": "no-store",
                                     "X-Error-Code": "outlook_unavailable"}) from None

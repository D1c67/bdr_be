"""Notification routing by role (0177, services/notification_routing).

  GET  /notification-routing        the grid: every event, every role that
                                     receives it, its Email and Teams switches
  PUT  /notification-routing/cell   change one switch
  POST /notification-routing/reset  every switch back to its default

Executives and IT Admins only: the grid decides what the whole team hears.
The bell is not routed, so nothing here can hide a notification from the
app itself. Handlers are plain `def` (sync Supabase SDK in the threadpool).
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import CurrentUser, require_role
from app.core.roles import Role
from app.services import notification_routing
from app.services.notifications import audit

router = APIRouter(prefix="/notification-routing", tags=["notification-routing"])

require_routing_admin = require_role(Role.EXECUTIVE, Role.IT_ADMIN)


class RoutingCellIn(BaseModel):
    role: str
    event: str
    channel: Literal["email", "teams"]
    enabled: bool


@router.get("")
def get_routing(user: CurrentUser = Depends(require_routing_admin)):
    return notification_routing.grid()


@router.put("/cell")
def update_cell(body: RoutingCellIn, user: CurrentUser = Depends(require_routing_admin)):
    try:
        notification_routing.set_cell(body.role, body.event, body.channel, body.enabled, user.id)
    except notification_routing.RoutingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    audit(user.id, "notification_routing.update", "notification_routing", None, body.model_dump())
    return notification_routing.grid()


@router.post("/reset")
def reset_routing(user: CurrentUser = Depends(require_routing_admin)):
    notification_routing.reset_all()
    audit(user.id, "notification_routing.reset", "notification_routing", None, {})
    return notification_routing.grid()

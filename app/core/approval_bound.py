"""Approval-bound routes (docs/ASSISTANT_PHASE4_PLAN.md 3.2.3).

A route that only an approved assistant card may run (the evidence POSTs,
the page-read and AE run routes of phase 4) checks two things: the caller is
the assistant principal executing an approval (provenance carries the
approval id, so a human and a chat turn principal are refused), and the
idempotency header repeats that approval id. The refusals mirror
routers/projects.py `_approval_bound_key` (phase 2 split-drop), which keeps
its own copy: 404 for a human, a turn principal or a header that differs
(byte-identical to an unknown route), 400 `idempotency_key_required` for a
missing or malformed header.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request, status

from app.core import provenance
from app.core.deps import CurrentUser, get_current_user

ASSISTANT_IDEMPOTENCY_HEADER = "X-Assistant-Idempotency-Key"
IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"


def approval_bound_key(request: Request, user: CurrentUser) -> str:
    """The approval id of an approval-bound assistant request, which the
    idempotency header must repeat. 404 for a human or a turn principal (no
    approval) and for a header that differs; 400 idempotency_key_required
    when the header is missing or not a uuid."""
    actor = provenance.current()
    if (not user.via_assistant_run_id or actor is None or not actor.approval_id
            or actor.user_id != str(user.id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    raw = (request.headers.get(ASSISTANT_IDEMPOTENCY_HEADER) or "").strip()
    try:
        key = str(uuid.UUID(raw))
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": IDEMPOTENCY_KEY_REQUIRED,
                    "message": f"{ASSISTANT_IDEMPOTENCY_HEADER} must carry the approval id."},
            headers={"X-Error-Code": IDEMPOTENCY_KEY_REQUIRED},
        ) from None
    if key != actor.approval_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return key


def require_approval_bound(request: Request, user: CurrentUser = Depends(get_current_user)) -> str:
    """FastAPI dependency form of approval_bound_key: returns the approval id."""
    return approval_bound_key(request, user)

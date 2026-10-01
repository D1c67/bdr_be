"""BuildingConnected: the /rfp-portal/buildingconnected API
(docs/RFP_BUILDINGCONNECTED.md sections 4.3 and 6; build contract D4, D5,
D8, D30, D33 and section 4).

Two routers in one module:

- `router`: connect, disconnect, status, run and the GC alias table. Gated
  on RFP_INGESTION_ENABLED and RFP_BC_ENABLED (a ROUTER-level dependency:
  while either is off every route 404s with the bare "Not Found" body
  before a token is read), plus a role per route (D8) and the catch-all
  rate limit:
    connect / disconnect   IT Admin, Executive
    status                 the review-queue roles (RFP_REVIEW_ROLES)
    run                    IT Admin, Executive, Estimating Admin
    gc-aliases             IT Admin
- `callback_router`: GET /callback, the browser's return from Autodesk. It
  has NO auth dependency and NOT the per-account rate limit (both need a
  bearer token the redirect cannot carry): only the same feature gate, one
  per-process limiter shared by every caller (RFP_BC_CALLBACK_RATE_LIMIT_
  PER_MIN, checked inside the handler), a shape check on `state` before
  any DB call, and the single-use,
  ten-minute OAuth `state` that binds it to the IT Admin or Executive who
  pressed Connect. A missing `state` is a 400 JSON answer; every other
  outcome is a 302 to the settings page with `?bc=connected` or
  `?bc=error&reason=<error code>`, so the person never lands on a JSON
  error page. The connection is refused, and nothing stored, when the
  Autodesk user cannot see the whole Bid Board (`bidBoardPermissions.
  viewAll` false), or when RFP_BC_EXPECTED_COMPANY_ID is set and the user's
  `companyId` differs.

What never leaves here: the tokens (the status block is
bc_client.connection_status, which drops them), the client secret and the
Settings object. Log lines name the failure's class and HTTP status only.

Event-loop rule: every handler is a plain `def` (the Supabase SDK is sync).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import deque
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_role
from app.core.error_codes import ErrorCode
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.routers.rfp_portal import _refused, require_review_queue, rfp_portal_rate_limit

logger = logging.getLogger(__name__)

PREFIX = "/rfp-portal/buildingconnected"
SETTINGS_PATH = "/settings/rfp-ingestion"
AUDIT_ENTITY = "rfp_oauth_connection"


# ── Feature gate ─────────────────────────────────────────────────────────


def rfp_bc_enabled() -> bool:
    """RFP_INGESTION_ENABLED and RFP_BC_ENABLED (the two switches, like GET
    /features' rfp_buildingconnected). Read through `getattr` so an absent
    flag fails CLOSED. A configured APS app is NOT part of it: the status
    block says so and connect answers 503."""
    settings = get_settings()
    return bool(getattr(settings, "rfp_ingest_enabled", False)) and bool(
        getattr(settings, "rfp_bc_enabled", False)
    )


def require_rfp_bc() -> None:
    """Router-level dependency (both routers): 404 with the bare body while
    the switch is off, before auth runs."""
    if not rfp_bc_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


# D8. Module-level so the route table test can find each one by identity.
CONNECT_ROLES = (Role.IT_ADMIN, Role.EXECUTIVE)
RUN_ROLES = (Role.IT_ADMIN, Role.EXECUTIVE, Role.ESTIMATING_ADMIN)
ALIAS_ROLES = (Role.IT_ADMIN,)
require_bc_connect = require_role(*CONNECT_ROLES)
require_bc_run = require_role(*RUN_ROLES)
require_bc_aliases = require_role(*ALIAS_ROLES)

router = APIRouter(prefix=PREFIX, tags=["rfp-portal"], dependencies=[Depends(require_rfp_bc)])
# The OAuth return: the feature gate only (see the module docstring).
callback_router = APIRouter(
    prefix=PREFIX, tags=["rfp-portal"], dependencies=[Depends(require_rfp_bc)]
)


# ── Service seams (imported on first use; tests replace them) ────────────


def _bc_client():
    from app.services import bc_client

    return bc_client


def _bc_portal():
    from app.services import rfp_bc_portal

    return rfp_bc_portal


def _portal_ingest():
    from app.services import rfp_portal_ingest

    return rfp_portal_ingest


def _gc_aliases():
    from app.services import gc_aliases

    return gc_aliases


def _audit(actor_id: str | None, action: str, payload: dict | None = None) -> None:
    """Best effort: an audit failure never changes what happened."""
    try:
        from app.services.notifications import audit

        audit(actor_id, action, AUDIT_ENTITY, None, payload)
    except Exception:  # noqa: BLE001
        logger.warning("rfp bc: audit %s failed", action, exc_info=True)


# ── Connect / disconnect / status / run ──────────────────────────────────


def _not_configured() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "BuildingConnected is not configured (client id, client secret and redirect URL).",
        headers={"X-Error-Code": ErrorCode.RFP_PORTAL_NOT_AVAILABLE.value},
    )


@router.get("/connect", dependencies=[Depends(rfp_portal_rate_limit)])
def connect(user: CurrentUser = Depends(require_bc_connect)) -> dict:
    """The Autodesk authorize URL with a fresh single-use state bound to the
    caller: `{url}` (the FE navigates to it). 503 rfp_portal_not_available
    while the client id, secret or redirect URL is blank."""
    bc = _bc_client()
    config = bc.config_from_settings(get_settings())
    if not (config.client_id and config.client_secret and config.redirect_url):
        raise _not_configured()
    state = bc.new_state(get_supabase(), user.id)
    return {"url": bc.authorize_url(config, state)}


@router.post("/disconnect", dependencies=[Depends(rfp_portal_rate_limit)])
def disconnect(user: CurrentUser = Depends(require_bc_connect)) -> dict:
    """Revoke the tokens at Autodesk (best effort), clear them and mark the
    connection disconnected (scans stop until someone connects again).
    Audited `rfp_bc.disconnect`."""
    bc = _bc_client()
    bc.disconnect(get_supabase(), config=bc.config_from_settings(get_settings()))
    _audit(user.id, "rfp_bc.disconnect")
    return {"status": "disconnected"}


@router.get("/status", dependencies=[Depends(rfp_portal_rate_limit)])
def bc_status(_: CurrentUser = Depends(require_review_queue)) -> dict:
    """The settings block (contract section 4): the connection (never the
    tokens), the switches, the schedule, the state row, the last and the
    active run and the lane counts."""
    return _bc_portal().status(get_supabase())


class RunIn(BaseModel):
    kind: Literal["incremental", "full"] = "incremental"


@router.post(
    "/run", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(rfp_portal_rate_limit)]
)
def run_now(body: RunIn | None = None, user: CurrentUser = Depends(require_bc_run)) -> dict:
    """Run now / Full sync now: 202 with the run (`_run_view`). 409
    rfp_portal_run_active while a run is queued or running; 409
    rfp_bc_not_connected while disconnected; 503 rfp_portal_not_available
    while the slice is inactive (the queue off or the app unconfigured)."""
    kind = body.kind if body else "incremental"
    pi = _portal_ingest()
    sb = get_supabase()
    try:
        run = pi.run_now(sb, user.id, pi.PORTAL_BUILDINGCONNECTED, kind=kind)
    except pi.RfpPortalError as exc:
        raise _refused(exc) from exc
    names = pi._profile_names(sb, {run.get("requested_by")}) if run else {}
    return pi._run_view(run, names)


# ── GC aliases (IT Admin) ────────────────────────────────────────────────

_ALIAS_NOT_FOUND = "Alias not found"


def _uuid_or(value, exc: HTTPException) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise exc from None


def _alias_404() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, _ALIAS_NOT_FOUND)


def _gc_400(message: str = "That GC does not exist.") -> HTTPException:
    return HTTPException(
        status.HTTP_400_BAD_REQUEST, message,
        headers={"X-Error-Code": ErrorCode.RFP_PORTAL_GC_REQUIRED.value},
    )


@router.get("/gc-aliases", dependencies=[Depends(rfp_portal_rate_limit)])
def list_gc_aliases(_: CurrentUser = Depends(require_bc_aliases)) -> dict:
    """Every confirmed BuildingConnected company -> BDR GC pairing:
    `{items: [{id, external_id, external_name, gc: {id, name}, confirmed_by:
    {id, name} | null, confirmed_at}]}`."""
    ga = _gc_aliases()
    return {"items": ga.list_aliases(get_supabase(), ga.SOURCE_BC)}


class AliasRepointIn(BaseModel):
    gc_id: str = Field(min_length=1, max_length=64)


@router.patch("/gc-aliases/{alias_id}", dependencies=[Depends(rfp_portal_rate_limit)])
def repoint_gc_alias(
    alias_id: str, body: AliasRepointIn, user: CurrentUser = Depends(require_bc_aliases)
) -> dict:
    """Point an alias at another GC: the alias in the list shape. 404 for an
    unknown alias; 400 rfp_portal_gc_required for an unknown GC. The
    service carries the change onto the invitations resolved through the
    alias (rows without a project take the new GC; a project's GC card
    reopens); its summary goes to the audit row."""
    alias_id = _uuid_or(alias_id, _alias_404())
    gc_id = _uuid_or(body.gc_id, _gc_400())
    ga = _gc_aliases()
    sb = get_supabase()
    try:
        row = ga.repoint(sb, alias_id, gc_id, user.id)
    except ga.AliasNotFound as exc:
        raise _alias_404() from exc
    except ga.GcNotFound as exc:
        raise _gc_400() from exc
    _audit(user.id, "rfp_bc.alias_repoint", {
        "alias_id": alias_id, "gc_id": gc_id,
        "propagated": row.get("propagated") if isinstance(row, dict) else None,
    })
    for item in ga.list_aliases(sb, ga.SOURCE_BC):
        if str(item.get("id")) == alias_id:
            return item
    return row


@router.delete("/gc-aliases/{alias_id}", dependencies=[Depends(rfp_portal_rate_limit)])
def delete_gc_alias(alias_id: str, user: CurrentUser = Depends(require_bc_aliases)) -> dict:
    """Forget an alias (the company is asked about again next time):
    `{id, deleted: true}`; 404 for an unknown alias. The service clears the
    GC on the invitations resolved through the alias that have no project
    (sending a row parked for its GC back to match) and reopens the GC card
    of the projects the others sit on; its summary goes to the audit row."""
    alias_id = _uuid_or(alias_id, _alias_404())
    ga = _gc_aliases()
    try:
        propagated = ga.delete(get_supabase(), alias_id)
    except ga.AliasNotFound as exc:
        raise _alias_404() from exc
    _audit(user.id, "rfp_bc.alias_delete", {"alias_id": alias_id, "propagated": propagated})
    return {"id": alias_id, "deleted": True}


# ── The OAuth callback (unauthenticated; the state is the proof) ─────────


def _frontend_redirect(**params: str) -> RedirectResponse:
    base = str(getattr(get_settings(), "frontend_url", "") or "").rstrip("/")
    return RedirectResponse(
        f"{base}{SETTINGS_PATH}?{urlencode(params)}", status_code=status.HTTP_302_FOUND
    )


def _fail(reason: ErrorCode) -> RedirectResponse:
    return _frontend_redirect(bc="error", reason=reason.value)


# The callback's own limiter: it has no user to key on, so one sliding
# 60 second window per process counts every caller together. A flood trips
# it long before it costs real DB traffic; a legitimate Connect is one hit.
_CALLBACK_WINDOW_SECONDS = 60.0
# bc_client.new_state mints secrets.token_urlsafe(32): 43 URL-safe characters.
_STATE_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
_callback_hits: deque[float] = deque()
_callback_lock = threading.Lock()


def _callback_rate_limit() -> None:
    settings = get_settings()
    if not bool(getattr(settings, "rate_limit_enabled", True)):
        return
    limit = int(getattr(settings, "rfp_bc_callback_rate_limit_per_min", 30) or 30)
    now = time.monotonic()
    with _callback_lock:
        while _callback_hits and _callback_hits[0] <= now - _CALLBACK_WINDOW_SECONDS:
            _callback_hits.popleft()
        if len(_callback_hits) >= limit:
            retry_after = max(1, int(_callback_hits[0] + _CALLBACK_WINDOW_SECONDS - now) + 1)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=ErrorCode.RATE_LIMITED,
                headers={"Retry-After": str(retry_after), "X-RateLimit-Scope": "rfp_bc_callback"},
            )
        _callback_hits.append(now)


@callback_router.get("/callback")
def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Autodesk's redirect back: consume the state (single use; a 400 JSON
    only when it is absent), exchange the code, read /users/me, refuse a
    user without `bidBoardPermissions.viewAll` (nothing stored), store the
    connection, then 302 to /settings/rfp-ingestion?bc=connected. Every
    failure after the state check is a 302 with `bc=error&reason=<code>`:
    rfp_bc_state_invalid (unknown, used or expired state),
    rfp_bc_view_all_required, or rfp_bc_not_connected (Autodesk refused,
    the person declined, the exchange failed, or the Autodesk company is not
    RFP_BC_EXPECTED_COMPANY_ID when that is set). The whole route shares one
    per-process limiter (429), and a state that is not token_urlsafe(32)
    shaped is state_invalid before any DB call."""
    _callback_rate_limit()
    if not isinstance(state, str) or not state.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The OAuth state is missing.",
            headers={"X-Error-Code": ErrorCode.RFP_BC_STATE_INVALID.value},
        )
    if not _STATE_SHAPE.fullmatch(state.strip()):
        logger.info("rfp bc callback: malformed state")
        return _fail(ErrorCode.RFP_BC_STATE_INVALID)
    bc = _bc_client()
    sb = get_supabase()
    try:
        row = bc.consume_state(sb, state.strip())
    except Exception as exc:  # noqa: BLE001 - a redirect, never a 500 page
        logger.warning("rfp bc callback: state check failed (%s)", type(exc).__name__)
        return _fail(ErrorCode.RFP_BC_NOT_CONNECTED)
    if not row:
        logger.info("rfp bc callback: unknown or expired state")
        return _fail(ErrorCode.RFP_BC_STATE_INVALID)
    actor_id = row.get("actor_id")
    if error or not isinstance(code, str) or not code.strip():
        # The person declined, or Autodesk answered an error: nothing to do.
        logger.info("rfp bc callback: no code (error=%s)", (error or "none")[:64])
        return _fail(ErrorCode.RFP_BC_NOT_CONNECTED)
    try:
        config = bc.config_from_settings(get_settings())
        if not (config.client_id and config.client_secret and config.redirect_url):
            logger.warning("rfp bc callback: the APS app is not configured")
            return _fail(ErrorCode.RFP_BC_NOT_CONNECTED)
        tokens = bc.exchange_code(config, code.strip())
        me = bc.fetch_me(tokens.get("access_token") or "", config)
        if not bc.view_all(me):
            logger.warning("rfp bc callback: refused, the Autodesk user lacks Bid Board viewAll")
            _audit(actor_id, "rfp_bc.connect_refused", {"reason": "view_all_required"})
            return _fail(ErrorCode.RFP_BC_VIEW_ALL_REQUIRED)
        expected = str(getattr(get_settings(), "rfp_bc_expected_company_id", "") or "").strip()
        company = str((me or {}).get("companyId") or "").strip()
        if expected and company != expected:
            logger.warning("rfp bc callback: refused, the Autodesk user is not in the expected company")
            _audit(actor_id, "rfp_bc.connect_refused", {
                "reason": "company_mismatch", "external_user_id": (me or {}).get("id"),
                "external_company_id": company or None,
            })
            return _fail(ErrorCode.RFP_BC_NOT_CONNECTED)
        bc.store_connection(sb, tokens, me, actor_id)
    except Exception as exc:  # noqa: BLE001 - never a JSON error page for the person
        logger.warning(
            "rfp bc callback: connect failed (%s, status %s)",
            type(exc).__name__, getattr(exc, "status", None),
        )
        return _fail(ErrorCode.RFP_BC_NOT_CONNECTED)
    _audit(actor_id, "rfp_bc.connect", {
        "external_user_id": (me or {}).get("id"), "external_company_id": (me or {}).get("companyId"),
    })
    return _frontend_redirect(bc="connected")


# Error codes on this module (docs/ERROR_CODES.md); rfp_bc_not_connected
# equals rfp_bc_portal.CODE_NOT_CONNECTED (Run now while disconnected).
_CODES = (
    ErrorCode.RFP_BC_DISCONNECTED,
    ErrorCode.RFP_BC_NOT_CONNECTED,
    ErrorCode.RFP_BC_STATE_INVALID,
    ErrorCode.RFP_BC_VIEW_ALL_REQUIRED,
)

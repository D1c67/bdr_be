"""Human API for the browser agents: /browser-agents (docs/browser_agents/HUMAN_API.md, C2).

The BDR web app's routes for the Agents pages, the help chat and
Settings > Browser agents. The shapes are exactly the ones HUMAN_API.md
freezes and bdr_fe/lib/browserAgents.ts types; the work lives in
services/browser_agents/admin.py (jobs, admin) and help.py (help).

Rules every handler follows:
  - plain `def` (the Supabase SDK is synchronous);
  - a bare 404 while BROWSER_AGENTS_ENABLED is off (router dependency, before auth);
  - the normal user JWT (get_current_user, which never accepts a bdrw_ worker token);
  - Cache-Control: no-store on every response, errors included;
  - refusals carry X-Error-Code (looks_like_secret, help_closed, probe_failed, ...);
  - every write is audited (audit_log, plus the settings change log for settings).

Roles (core/roles.py): viewers (Estimating Admin, IT Admin, Executive, and
dev accounts) read; helpers (Estimating Admin, IT Admin) answer help,
cancel and retry; admins (IT Admin, or any dev account) own /admin.
Model transcripts (llm_request / llm_response artifacts) are admin only.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user
from app.core.features import require_browser_agents
from app.core.roles import BROWSER_AGENT_ADMIN_ROLES, BROWSER_AGENT_HELPER_ROLES, BROWSER_AGENT_VIEW_ROLES
from app.services.browser_agents import admin
from app.services.browser_agents import help as help_flow
from app.services.browser_agents import protocol
from app.services.browser_agents.help import human_error

logger = logging.getLogger(__name__)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/browser-agents",
    tags=["browser-agents"],
    dependencies=[Depends(require_browser_agents), Depends(_no_store)],
)

# The help sweeper steps also register when only the human router is loaded.
help_flow.register_sweep_steps()


# ── Role gates (plain def: no I/O beyond what get_current_user did) ─────────────


def require_agent_viewer(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role in BROWSER_AGENT_VIEW_ROLES or user.is_dev:
        return user
    raise human_error(status.HTTP_403_FORBIDDEN, "forbidden", "Not permitted")


def require_agent_helper(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role in BROWSER_AGENT_HELPER_ROLES:
        return user
    raise human_error(status.HTTP_403_FORBIDDEN, "forbidden", "Only Estimating Admins and IT Admins can do this.")


def require_agent_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role in BROWSER_AGENT_ADMIN_ROLES or user.is_dev:
        return user
    raise human_error(status.HTTP_403_FORBIDDEN, "forbidden", "IT Admin only.")


Viewer = Annotated[CurrentUser, Depends(require_agent_viewer)]
Helper = Annotated[CurrentUser, Depends(require_agent_helper)]
Admin = Annotated[CurrentUser, Depends(require_agent_admin)]


def _body(model: type[BaseModel], payload: Any) -> Any:
    """Validate a JSON body; a malformed one is 422 invalid_request naming
    the first bad field only (never echoing values, which may be secrets)."""
    try:
        return model.model_validate(payload if payload is not None else {})
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "body"
        raise human_error(422, "invalid_request", f"Invalid request: {where}") from None


def _help(help_id: uuid.UUID) -> dict[str, Any]:
    row = help_flow.get_help(str(help_id))
    if not row or row.get("principal_kind", "org") != "org":
        raise human_error(404, "not_found", "No such help request.")
    return row


def _job(job_id: uuid.UUID) -> dict[str, Any]:
    row = admin.get_job(str(job_id))
    if not row or row.get("principal_kind", "org") != "org":
        raise human_error(404, "not_found", "No such job.")
    return row


# ── Request bodies ───────────────────────────────────────────────────────────────


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HelpMessageIn(_In):
    client_msg_id: uuid.UUID
    action: Literal["message", "retry", "give_up", "skip_daily"] = "message"
    body: Annotated[str, Field(max_length=help_flow.HUMAN_BODY_MAX)] | None = None
    is_secret: bool = False
    secret_id: uuid.UUID | None = None
    secret_value: Annotated[str, Field(max_length=help_flow.SECRET_VALUE_MAX)] | None = None
    artifact_ids: Annotated[list[uuid.UUID], Field(max_length=help_flow.MESSAGE_ARTIFACTS_MAX)] = Field(
        default_factory=list)


class GlobalSettingsPatch(_In):
    enabled: bool | None = None
    daily_cost_cap_usd: Annotated[float, Field(ge=0, le=1000)] | None = None
    per_job_cost_cap_usd: Annotated[float, Field(ge=0, le=50)] | None = None
    help_hold_seconds: Annotated[int, Field(ge=60, le=3600)] | None = None
    help_reminder_minutes: Annotated[int, Field(ge=10, le=1440)] | None = None
    help_claim_lapse_minutes: Annotated[int, Field(ge=5, le=240)] | None = None
    help_expire_hours: Annotated[int, Field(ge=1, le=336)] | None = None
    worker_offline_alert_minutes: Annotated[int, Field(ge=5, le=1440)] | None = None
    worker_offline_email_hours: Annotated[int, Field(ge=1, le=72)] | None = None
    job_ttl_hours: Annotated[int, Field(ge=1, le=720)] | None = None
    desired_worker_version: Annotated[str, Field(pattern=protocol.SEMVER_RE.pattern)] | None = None


class SpendResetIn(_In):
    note: Annotated[str, Field(max_length=500)] | None = None


class TypePatch(_In):
    label: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    enabled: bool | None = None
    provider: protocol.Provider | None = None
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9._:/-]{1,120}$")] | None = None
    price_in_per_mtok: Annotated[float, Field(ge=0, le=100000)] | None = None
    price_out_per_mtok: Annotated[float, Field(ge=0, le=100000)] | None = None
    max_concurrency: Annotated[int, Field(ge=0, le=8)] | None = None
    max_attempts: Annotated[int, Field(ge=1, le=10)] | None = None
    max_interruptions: Annotated[int, Field(ge=1, le=100)] | None = None
    max_steps: Annotated[int, Field(ge=0, le=200)] | None = None
    max_total_tokens: Annotated[int, Field(ge=0, le=5_000_000)] | None = None
    max_active_seconds: Annotated[int, Field(ge=60, le=7200)] | None = None
    host_narrowing: list[Annotated[str, Field(max_length=253)]] | None = None
    priority_default: Annotated[int, Field(ge=0, le=1000)] | None = None
    save_anyway: bool = False


class ProbeIn(_In):
    provider: protocol.Provider
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9._:/-]{1,120}$")]


class SecretIn(_In):
    value: Annotated[str, Field(min_length=1, max_length=200)]


class EnrollmentIn(_In):
    principal_kind: Literal["org", "user"] = "org"
    worker_name: Annotated[str, Field(min_length=2, max_length=60)]


class WorkerPatch(_In):
    name: Annotated[str, Field(min_length=2, max_length=60)] | None = None
    draining: bool | None = None
    max_slots: Annotated[int, Field(ge=1, le=8)] | None = None
    allowed_types: Annotated[list[str], Field(max_length=20)] | None = None
    allowed_ips: Annotated[list[str], Field(max_length=50)] | None = None


class CanaryIn(_In):
    worker_id: uuid.UUID | None = None


def _set_fields(model: BaseModel) -> dict[str, Any]:
    """Only the fields the caller sent (a partial PATCH); an explicit null
    is kept for desired_worker_version (clearing it), dropped elsewhere."""
    data = model.model_dump(exclude_unset=True)
    return {k: v for k, v in data.items() if v is not None or k == "desired_worker_version"}


# ── Summary ─────────────────────────────────────────────────────────────────────


@router.get("/summary")
def get_summary(_: Viewer) -> dict[str, Any]:
    return admin.summary()


# ── Jobs ────────────────────────────────────────────────────────────────────────


@router.get("/jobs")
def list_jobs(
    _: Viewer,
    status_: str | None = Query(default=None, alias="status"),
    type_: str | None = Query(default=None, alias="type"),
    project_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=admin.LIST_DEFAULT, ge=1, le=admin.LIST_MAX),
) -> dict[str, Any]:
    return admin.list_jobs(status_=status_, agent_type=type_, project_id=str(project_id) if project_id else None,
                           cursor=cursor, limit=limit)


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, user: Viewer) -> dict[str, Any]:
    return admin.job_detail_view(_job(job_id), user)


@router.get("/jobs/{job_id}/steps")
def get_job_steps(
    job_id: uuid.UUID,
    _: Viewer,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=admin.STEPS_DEFAULT, ge=1, le=admin.STEPS_MAX),
) -> dict[str, Any]:
    _job(job_id)
    return admin.job_steps(str(job_id), after_seq, limit)


@router.get("/jobs/{job_id}/turns")
def get_job_turns(job_id: uuid.UUID, _: Viewer) -> dict[str, Any]:
    _job(job_id)
    return admin.job_turns(str(job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: uuid.UUID, user: Helper) -> dict[str, Any]:
    return admin.cancel_job(_job(job_id), user)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: uuid.UUID, user: Helper) -> dict[str, Any]:
    return admin.retry_job(_job(job_id), user)


# ── Artifacts ───────────────────────────────────────────────────────────────────


@router.get("/artifacts/{artifact_id}/url")
def artifact_url(artifact_id: uuid.UUID, user: Viewer) -> dict[str, Any]:
    return admin.artifact_url(str(artifact_id), user)


# ── Help ────────────────────────────────────────────────────────────────────────


@router.get("/help")
def list_help(
    _: Viewer,
    status_: str | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=admin.LIST_DEFAULT, ge=1, le=admin.LIST_MAX),
) -> dict[str, Any]:
    from app.core import supabase_client

    q = supabase_client.get_supabase().table(help_flow.HELP).select("*").eq("principal_kind", "org")
    if status_:
        if status_ == "active":
            q = q.in_("status", help_flow.ACTIVE)
        elif status_ in protocol.HelpStatus.__args__:  # type: ignore[attr-defined]
            q = q.eq("status", status_)
        else:
            raise human_error(422, "invalid_request", "Unknown status filter.")
    rows, nxt = admin.page_newest_first(q, cursor, admin.clamp_limit(limit))
    return {"items": help_flow.help_rows_view(rows), "next_cursor": nxt}


@router.get("/help/{help_id}")
def get_help(help_id: uuid.UUID, user: Viewer) -> dict[str, Any]:
    return help_flow.help_detail(_help(help_id), user)


@router.get("/help/{help_id}/messages")
def get_help_messages(help_id: uuid.UUID, _: Viewer, after_id: int = Query(default=0, ge=0)) -> dict[str, Any]:
    row = _help(help_id)
    labels = help_flow.profile_labels({row["claimed_by"]}) if row.get("claimed_by") else {}
    return {"items": help_flow.human_messages(str(row["id"]), after_id), "status": row["status"],
            "claimed_by_label": labels.get(str(row.get("claimed_by"))) if row.get("claimed_by") else None}


@router.post("/help/{help_id}/messages")
def post_help_message(help_id: uuid.UUID, user: Helper, payload: Any = Body(default=None)) -> dict[str, Any]:
    """A message or a button (retry, give_up, skip_daily), idempotent on
    client_msg_id. A secret (login scopes only) is encrypted before its
    single insert; its value is never returned."""
    body: HelpMessageIn = _body(HelpMessageIn, payload)
    msg = help_flow.HumanMessage(
        client_msg_id=str(body.client_msg_id), action=body.action, body=body.body, is_secret=body.is_secret,
        secret_id=str(body.secret_id) if body.secret_id else None, secret_value=body.secret_value,
        artifact_ids=tuple(str(a) for a in body.artifact_ids),
    )
    return help_flow.post_message(_help(help_id), user, msg)


@router.post("/help/{help_id}/files")
def upload_help_file(
    help_id: uuid.UUID,
    user: Helper,
    file: UploadFile = File(...),
    attachable: str = Form(default="false"),
) -> dict[str, Any]:
    """A helper file for the thread (150 MB, sniffed, allowlisted). It joins
    the thread when a message cites it in artifact_ids."""
    flag = (attachable or "").strip().lower() in ("true", "1", "yes", "on")
    return help_flow.upload_file(_help(help_id), user, file.filename or "file", file.file, flag)


@router.post("/help/{help_id}/claim")
def claim_help(help_id: uuid.UUID, user: Helper) -> dict[str, Any]:
    help_flow.claim(_help(help_id), user)
    return help_flow.help_detail(_help(help_id), user)


@router.post("/help/{help_id}/unclaim")
def unclaim_help(help_id: uuid.UUID, user: Helper) -> dict[str, Any]:
    help_flow.unclaim(_help(help_id), user)
    return help_flow.help_detail(_help(help_id), user)


# ── Admin ───────────────────────────────────────────────────────────────────────


@router.get("/admin/state")
def admin_state(_: Admin) -> dict[str, Any]:
    return admin.admin_state()


@router.patch("/admin/settings")
def patch_settings(user: Admin, payload: Any = Body(default=None)) -> dict[str, Any]:
    body: GlobalSettingsPatch = _body(GlobalSettingsPatch, payload)
    return admin.update_global(_set_fields(body), user)


@router.post("/admin/spend/reset")
def reset_spend(user: Admin, payload: Any = Body(default=None)) -> dict[str, Any]:
    body: SpendResetIn = _body(SpendResetIn, payload)
    return admin.reset_spend(body.note, user)


@router.patch("/admin/types/{key}")
def patch_type(key: str, user: Admin, payload: Any = Body(default=None)) -> dict[str, Any]:
    body: TypePatch = _body(TypePatch, payload)
    fields = _set_fields(body)
    save_anyway = bool(fields.pop("save_anyway", False))
    return admin.update_type(key, fields, save_anyway, user)


@router.get("/admin/models")
def get_models(_: Admin, provider: str = Query(...)) -> dict[str, Any]:
    return admin.models(provider)


@router.post("/admin/types/{key}/probe")
def probe_type(key: str, user: Admin, payload: Any = Body(default=None)) -> dict[str, Any]:
    body: ProbeIn = _body(ProbeIn, payload)
    return admin.probe(key, body.provider, body.model, user)


@router.put("/admin/secrets/{name}")
def put_secret(name: str, user: Admin, payload: Any = Body(default=None)) -> dict[str, Any]:
    body: SecretIn = _body(SecretIn, payload)
    return admin.set_secret(name, body.value, user)


@router.delete("/admin/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_secret(name: str, user: Admin) -> Response:
    admin.clear_secret(name, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


def _api_base(request: Request) -> str:
    configured = (get_settings().browser_agent_api_base_url or "").strip()
    if configured:
        return configured.rstrip("/")
    parts = urlsplit(str(request.base_url))
    return f"https://{parts.netloc}"


@router.post("/admin/enrollments")
def create_enrollment(request: Request, user: Admin, payload: Any = Body(default=None)) -> dict[str, Any]:
    body: EnrollmentIn = _body(EnrollmentIn, payload)
    return admin.create_enrollment(body.worker_name, user, _api_base(request), principal_kind=body.principal_kind)


@router.patch("/admin/workers/{worker_id}")
def patch_worker(worker_id: uuid.UUID, user: Admin, payload: Any = Body(default=None)) -> dict[str, Any]:
    body: WorkerPatch = _body(WorkerPatch, payload)
    return admin.patch_worker(str(worker_id), _set_fields(body), user)


@router.post("/admin/workers/{worker_id}/approve")
def approve_worker(worker_id: uuid.UUID, user: Admin) -> dict[str, Any]:
    return admin.approve_worker(str(worker_id), user)


@router.post("/admin/workers/{worker_id}/revoke")
def revoke_worker(worker_id: uuid.UUID, user: Admin) -> dict[str, Any]:
    return admin.revoke_worker(str(worker_id), user)


@router.get("/admin/history")
def get_history(
    _: Admin,
    cursor: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=admin.LIST_DEFAULT, ge=1, le=admin.LIST_MAX),
) -> dict[str, Any]:
    return admin.history(cursor, limit)


@router.post("/admin/canary")
def run_canary(user: Admin, payload: Any = Body(default=None)) -> dict[str, Any]:
    body: CanaryIn = _body(CanaryIn, payload)
    return admin.run_canary(str(body.worker_id) if body.worker_id else None, user)


@router.post("/admin/stop-running")
def stop_running(user: Admin) -> dict[str, Any]:
    return admin.stop_running(user)

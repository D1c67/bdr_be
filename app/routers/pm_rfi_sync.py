"""PM RFI sync routes (docs/PM_RFI_SYNC.md, migration 0156): the project's
link to the GC's project management app, "Sync now", and linking an
imported Procore RFI to one of G3's own.

Mounted under the PM prefix with the PM sub-app guard (app/main.py) plus
`require_pm_rfi_sync` on the router itself, so with PM_RFI_SYNC_ENABLED off
every route 404s before auth. Reads are PM-read roles, writes PM-write
roles; every route runs require_pm_project first. The RFI list itself
(GET /rfis, with the new source / external_* / read_only fields) stays in
routers/pm_field.py.

Sync endpoints only queue work: a run is claimed by the scheduler loop in
services/pm_rfi_sync (two uvicorn workers, leased), never run inside the
request. PUT /pm-link may make one or two Procore GETs to resolve a
company id the URL did not carry; that happens in the threadpool (sync def).
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.features import require_pm_rfi_sync
from app.core.supabase_client import get_supabase
from app.models.schemas import PmExternalLinkIn, PmExternalLinkOut, RFILinkIn
from app.routers.pm_field import _enrich_rfis
from app.services import pm_rfi_sync, procore_creds
from app.services import procore_client as pc
from app.services.notifications import audit
from app.services.pm import require_pm_project

router = APIRouter(
    prefix="/pm/projects/{project_id}",
    tags=["pm-rfi-sync"],
    dependencies=[Depends(require_pm_rfi_sync)],
)


def _view(project_id: str) -> dict:
    sb = get_supabase()
    link = pm_rfi_sync.get_link(sb, project_id)
    return pm_rfi_sync.link_view(link, pm_rfi_sync.has_open_run(sb, project_id) if link else False)


@router.get("/pm-link", response_model=PmExternalLinkOut)
def get_pm_link(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    return _view(project_id)


def _resolve_company(ref: pc.ProcoreProjectRef) -> str | None:
    """Ask the shared Procore session for the project's company id (GET only).
    Skipped when Procore credentials are not configured."""
    settings = get_settings()
    if not procore_creds.configured(settings):
        return None
    with pm_rfi_sync.open_session(ref.host, settings) as session:
        return session.resolve_company_id(ref.project_id)


@router.put("/pm-link", response_model=PmExternalLinkOut)
def put_pm_link(
    project_id: str,
    body: PmExternalLinkIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    sb = get_supabase()
    try:
        row, changed = pm_rfi_sync.save_link(
            sb,
            project_id,
            platform=body.platform,
            url=body.url,
            sync_enabled=body.sync_enabled,
            actor_id=user.id,
            resolve_company=_resolve_company,
        )
    except pm_rfi_sync.LinkError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    queued = False
    # A new (or re-pointed) Procore link starts its backfill right away.
    if row.get("platform") == "procore" and row.get("sync_enabled") and (changed or not row.get("backfilled_at")):
        # fresh: a run already working loaded the old link, so it cannot stand in.
        _, queued = pm_rfi_sync.enqueue_run(
            sb, project_id, "manual", requested_by=user.id, fresh=changed
        )
    audit(user.id, "pm_link.update", "project", project_id, {
        "platform": row.get("platform"),
        "host": row.get("host"),
        "external_project_id": row.get("external_project_id"),
        "sync_enabled": row.get("sync_enabled"),
        "run_queued": queued,
    })
    return _view(project_id)


@router.post("/rfis/sync")
def sync_rfis(project_id: str, user: CurrentUser = Depends(require_pm_write)):
    require_pm_project(project_id)
    sb = get_supabase()
    link = pm_rfi_sync.get_link(sb, project_id)
    if not link or link.get("platform") != "procore" or not link.get("external_project_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Link this project to the GC's Procore project first.")
    run, created = pm_rfi_sync.enqueue_run(sb, project_id, "manual", requested_by=user.id)
    if run is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A sync could not be queued; try again in a moment.")
    if created:
        audit(user.id, "rfi.sync", "project", project_id, {"run_id": run.get("id")})
    return {"run_id": run.get("id"), "status": run.get("status")}


@router.post("/rfis/{rfi_id}/link")
def link_rfi(
    project_id: str,
    rfi_id: str,
    body: RFILinkIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Merge the imported Procore RFI `rfi_id` into G3's RFI
    `internal_rfi_id`; the imported row is deleted."""
    require_pm_project(project_id)
    try:
        row = pm_rfi_sync.link_rows(get_supabase(), project_id, rfi_id, body.internal_rfi_id)
    except pm_rfi_sync.LinkActionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    audit(user.id, "rfi.link", "project", project_id, {
        "rfi_id": row.get("id"),
        "rfi_number": row.get("rfi_number"),
        "imported_rfi_id": rfi_id,
        "external_id": row.get("external_id"),
        "external_number": row.get("external_number"),
    })
    return _enrich_rfis(project_id, [row])[0]


@router.post("/rfis/{rfi_id}/unlink")
def unlink_rfi(project_id: str, rfi_id: str, user: CurrentUser = Depends(require_pm_write)):
    """Split G3's RFI from its Procore RFI; the next sync re-imports the
    Procore RFI as its own read-only row."""
    require_pm_project(project_id)
    sb = get_supabase()
    before = (
        sb.table("rfis").select("external_id, external_number").eq("id", rfi_id)
        .eq("project_id", project_id).execute()
    ).data or [{}]
    try:
        row = pm_rfi_sync.unlink_row(sb, project_id, rfi_id)
    except pm_rfi_sync.LinkActionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    audit(user.id, "rfi.unlink", "project", project_id, {
        "rfi_id": rfi_id,
        "rfi_number": row.get("rfi_number"),
        "external_id": before[0].get("external_id"),
        "external_number": before[0].get("external_number"),
    })
    return _enrich_rfis(project_id, [row])[0]

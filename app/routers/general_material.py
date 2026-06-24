"""General-material price (the estimate's "wiring" material cost).

Unlike other material categories, General Material is not priced from vendor
quotes — its number comes from the estimate workbook (extracted by Sonnet 4.6,
see `services.general_material`) or is entered by hand. The PE manages it on
the receive-quotes step (re-run the extraction or override the figure); the
materials breakdown at markup/verify shows it read-only.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import GeneralMaterialIn
from app.services import general_material
from app.services.notifications import audit

router = APIRouter(prefix="/projects/{project_id}/general-material", tags=["general-material"])
_EDITOR = require_role(Role.PE, Role.PM, Role.IT_ADMIN)


def _get(project_id: str):
    rows = (
        get_supabase()
        .table("general_material_estimates")
        .select("*")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    return rows[0] if rows else None


@router.get("")
async def get_general_material(project_id: str, user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return _get(project_id)


@router.post("/extract")
async def rerun_extraction(
    project_id: str, background: BackgroundTasks, user: CurrentUser = Depends(_EDITOR)
):
    """Re-run the estimate extraction in the background."""
    get_supabase().table("general_material_estimates").upsert(
        {"project_id": project_id, "status": "running", "error": None, "updated_at": "now()"},
        on_conflict="project_id",
    ).execute()
    background.add_task(general_material.run_extraction, project_id)
    audit(user.id, "general_material.extract", "project", project_id, None)
    return {"status": "running"}


@router.put("")
async def set_general_material(
    project_id: str, body: GeneralMaterialIn, user: CurrentUser = Depends(_EDITOR)
):
    """Manually set / override the general-material price."""
    row = (
        get_supabase()
        .table("general_material_estimates")
        .upsert(
            {
                "project_id": project_id,
                "amount": str(body.amount) if body.amount is not None else None,
                "source": "manual",
                "status": "done",
                "set_by": user.id,
                "error": None,
                "updated_at": "now()",
            },
            on_conflict="project_id",
        )
        .execute()
    ).data[0]
    audit(
        user.id,
        "general_material.set",
        "project",
        project_id,
        {"amount": str(body.amount) if body.amount is not None else None},
    )
    return row

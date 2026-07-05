"""General-material price (the estimate's "wiring" material cost).

Unlike other material categories, General Material is not priced from vendor
quotes — its number comes from the estimate workbook (extracted by Sonnet 4.6,
see `services.general_material`) or is entered by hand. Any writer role manages
it on the receive-quotes step (re-run the extraction or override the figure); the
materials breakdown at markup/verify shows it read-only.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_writer
from app.core.roles import INTERNAL_ROLES
from app.core.supabase_client import get_supabase
from app.models.schemas import GeneralMaterialIn, TaxIn
from app.services import general_material, workflow
from app.services.notifications import audit

router = APIRouter(prefix="/projects/{project_id}/general-material", tags=["general-material"])
_EDITOR = require_writer


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
def get_general_material(project_id: str, user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return _get(project_id)


@router.post("/extract")
def rerun_extraction(
    project_id: str, background: BackgroundTasks, user: CurrentUser = Depends(_EDITOR)
):
    """Re-run the estimate extraction in the background.

    Reprocessing invalidates the sales-tax attestation up front (not on
    completion): the recorded answer described the old figure, and clearing it
    here re-arms the receive-quotes gate immediately — no window where the user
    advances on a stale attestation while the extraction is still running.
    tax_rate is kept as a prefill for the re-ask."""
    get_supabase().table("general_material_estimates").upsert(
        {
            "project_id": project_id,
            "status": "running",
            "error": None,
            "tax_included": None,
            "updated_at": "now()",
        },
        on_conflict="project_id",
    ).execute()
    background.add_task(general_material.run_extraction, project_id)
    audit(user.id, "general_material.extract", "project", project_id, None)
    return {"status": "running"}


@router.put("")
def set_general_material(
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
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "General material price changed")
    return row


@router.put("/tax")
def set_general_material_tax(
    project_id: str, body: TaxIn, user: CurrentUser = Depends(_EDITOR)
):
    """Record whether the general-material figure already includes sales tax,
    and the rate to apply when it doesn't — same attestation vendor quotes get,
    because this number feeds the materials total all the same. Upserts so the
    answer can be recorded even before an extraction has created the row."""
    row = (
        get_supabase()
        .table("general_material_estimates")
        .upsert(
            {
                "project_id": project_id,
                "tax_included": body.tax_included,
                "tax_rate": str(body.tax_rate),
                "updated_at": "now()",
            },
            on_conflict="project_id",
        )
        .execute()
    ).data[0]
    audit(
        user.id,
        "general_material.tax",
        "project",
        project_id,
        {"tax_included": body.tax_included, "tax_rate": str(body.tax_rate)},
    )
    # The tax-inclusive figure changes the materials price basis, so re-verify
    # if the project already passed Verify (mirrors the quote tax endpoint).
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "General material tax changed")
    return row

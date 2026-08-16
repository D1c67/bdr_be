"""General-material price (the estimate's "wiring" material cost).

Unlike other material categories, General Material is not priced from vendor
quotes — its number comes from the estimate workbook (extracted by Sonnet 4.6,
see `services.general_material`) or is entered by hand. Any writer role manages
it on the receive-quotes step (re-run the extraction or override the figure); the
materials breakdown at markup/verify shows it read-only.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_writer
from app.core.ratelimit import ai_rate_limit
from app.core.roles import INTERNAL_ROLES
from app.core.supabase_client import get_supabase
from app.models.schemas import GeneralMaterialIn, TaxIn
from app.services import general_material, llm_queue, workflow
from app.services.notifications import audit

logger = logging.getLogger(__name__)

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
    row = _get(project_id)
    if row and row.get("status") in ("pending", "running"):
        # Release rows stranded by a restart (queue-aware: live jobs are kept).
        row = general_material.fail_if_stale(row)
    if row and row.get("status") in ("pending", "running"):
        # Queue detail (position, attempt count, retry state) for the panel.
        try:
            row["queue"] = llm_queue.poll_info(llm_queue.JOB_GENERAL_MATERIAL, project_id)
        except Exception:  # noqa: BLE001 - detail is optional, polling must not break
            logger.exception("General-material queue poll_info failed")
    return row


@router.post("/extract", dependencies=[Depends(ai_rate_limit)])
def rerun_extraction(
    project_id: str, background: BackgroundTasks, user: CurrentUser = Depends(_EDITOR)
):
    """Queue a re-run of the estimate extraction.

    Reprocessing invalidates the sales-tax attestation up front (not on
    completion): the recorded answer described the old figure, and clearing it
    here re-arms the receive-quotes gate immediately — no window where the user
    advances on a stale attestation while the extraction is still running.
    tax_rate is kept as a prefill for the re-ask."""
    if get_settings().llm_queue_enabled:
        # Enqueue BEFORE touching the domain row, so 'pending' is only ever
        # written while a job demonstrably exists. A collapse onto a job that
        # is already RUNNING is surfaced as 409 (that run predates whatever
        # the user just changed); collapsing onto a QUEUED job is fine, it
        # reads the latest estimate file when it starts.
        try:
            llm_queue.enqueue(
                llm_queue.JOB_GENERAL_MATERIAL,
                target_id=project_id,
                project_id=project_id,
                payload={"project_id": project_id},
                created_by=user.id,
                raise_on_active=True,
            )
        except llm_queue.JobAlreadyActive as exc:
            if exc.job.get("status") == "running":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "An extraction is already running for this project. "
                    "Re-run it when it finishes.",
                ) from exc
        except Exception:  # noqa: BLE001 - queue outage degrades to inline dispatch
            logger.exception("General-material enqueue failed; falling back to BackgroundTasks")
            background.add_task(general_material.run_extraction, project_id)
    else:
        background.add_task(general_material.run_extraction, project_id)
    get_supabase().table("general_material_estimates").upsert(
        {
            "project_id": project_id,
            "status": "pending",
            "error": None,
            "tax_included": None,
            "updated_at": "now()",
        },
        on_conflict="project_id",
    ).execute()
    audit(user.id, "general_material.extract", "project", project_id, None)
    return {"status": "pending"}


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
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "General material price changed", stale="materials")
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
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "General material tax changed", stale="materials")
    return row

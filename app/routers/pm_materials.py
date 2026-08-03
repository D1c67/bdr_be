"""PM materials — the project's category-grouped material list (migration 0062).

Bid-origin projects are seeded at won→Precon activation with exactly what the
BOQ extraction returned (services.pm.seed_pm_materials_from_boq); direct
projects start empty and are built by hand. Rows carry no pricing — this is
the BOQ-extraction shape (description/quantity/unit/notes per category), the
same format the BOQ estimate panel shows on the bidding side. Reads are any
PM-read role (accountant included, the external estimator never); writes are
PM-write roles. Every row lookup is scoped to the project, so an id from
another project is indistinguishable from a missing one.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.supabase_client import get_supabase
from app.models.schemas import PmMaterialBulkIn, PmMaterialIn, PmMaterialUpdate
from app.services.notifications import audit
from app.services.pm import require_pm_project

router = APIRouter(prefix="/pm/projects/{project_id}/materials", tags=["pm-materials"])


def _require_category(category_id: str) -> None:
    """Unknown category must be a clean 400, not a raw FK violation."""
    rows = (
        get_supabase()
        .table("material_categories")
        .select("id")
        .eq("id", category_id)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown material category")


@router.get("")
def list_materials(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    require_pm_project(project_id)
    rows = (
        get_supabase()
        .table("pm_materials")
        .select("*, material_categories(name)")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    for r in rows:
        cat = r.pop("material_categories", None) or {}
        # Server-resolved display group: the category name, else the
        # extraction's group label for rows that never matched a category.
        r["category_name"] = cat.get("name") or r.get("category_label")
    # Named groups A→Z with uncategorized last; within a group the seeded
    # extraction order first, then additions by age.
    rows.sort(
        key=lambda r: (
            r.get("category_name") is None,
            (r.get("category_name") or "").lower(),
            r.get("sort_order") or 0,
            r.get("created_at") or "",
        )
    )
    return rows


@router.post("", status_code=status.HTTP_201_CREATED)
def create_material(
    project_id: str,
    body: PmMaterialIn,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    if body.material_category_id:
        _require_category(body.material_category_id)
    payload = body.model_dump(mode="json")
    payload.update({"project_id": project_id, "created_by": user.id})
    created = get_supabase().table("pm_materials").insert(payload).execute().data[0]
    audit(
        user.id,
        "pm_material.create",
        "project",
        project_id,
        {"material_id": created.get("id"), "description": body.description[:200]},
    )
    return created


@router.post("/bulk", status_code=status.HTTP_201_CREATED)
def create_materials_bulk(
    project_id: str,
    body: PmMaterialBulkIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Insert a whole batch of lines at once — the add-materials modal lets a
    writer type many rows before saving. All-or-nothing: every category is
    validated before the single insert, so a typo in row 12 doesn't leave rows
    1-11 behind."""
    require_pm_project(project_id)
    for category_id in {m.material_category_id for m in body.materials if m.material_category_id}:
        _require_category(category_id)
    payloads = []
    for m in body.materials:
        payload = m.model_dump(mode="json")
        payload.update({"project_id": project_id, "created_by": user.id})
        payloads.append(payload)
    created = get_supabase().table("pm_materials").insert(payloads).execute().data or []
    audit(
        user.id,
        "pm_material.create_bulk",
        "project",
        project_id,
        {
            "count": len(created),
            "descriptions": [m.description[:200] for m in body.materials[:20]],
        },
    )
    return created


@router.patch("/{material_id}")
def update_material(
    project_id: str,
    material_id: str,
    body: PmMaterialUpdate,
    user: CurrentUser = Depends(require_pm_write),
):
    require_pm_project(project_id)
    # exclude_unset (not exclude_none) so an explicit null clears a field.
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if "description" in patch and patch["description"] is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "description cannot be cleared")
    if patch.get("material_category_id"):
        _require_category(patch["material_category_id"])
    if "material_category_id" in patch:
        # The row now reflects an explicit category choice (or none) — the
        # extraction's group label no longer applies.
        patch["category_label"] = None
    updated = (
        get_supabase()
        .table("pm_materials")
        .update(patch)
        .eq("id", material_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Material not found")
    audit(
        user.id,
        "pm_material.update",
        "project",
        project_id,
        {"material_id": material_id, "fields": sorted(patch)},
    )
    return updated[0]


@router.delete("/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_material(
    project_id: str,
    material_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    """Always allowed for writers — the list is a living document; even seeded
    BOQ rows may be pruned as the buyout evolves."""
    require_pm_project(project_id)
    deleted = (
        get_supabase()
        .table("pm_materials")
        .delete()
        .eq("id", material_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Material not found")
    audit(
        user.id,
        "pm_material.delete",
        "project",
        project_id,
        {
            "material_id": material_id,
            "description": (deleted[0].get("description") or "")[:200],
        },
    )

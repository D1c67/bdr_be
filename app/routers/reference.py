"""Reference data: general contractors (+ their contacts) and material categories.

GCs are needed by the intake form's multi-select; material categories drive RFQ
splitting. Any internal user can read GCs and GC contacts; writer roles can add
them (the Contacts page). Any writer may ADD a material category — the BOQ
extraction and PM materials panels create one inline when a group doesn't fit
an existing bucket — but editing/deactivating an existing category rewrites the
taxonomy under everyone's live projects, so that stays with CATEGORY_ADMIN_ROLES
(Executive + the IT Admin override) on the Contacts → Categories tab.
GC contacts mirror
vendor_contacts: many named people per company; proposal sends pick recipients
from them per send.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_internal, require_role, require_writer
from app.core.roles import CATEGORY_ADMIN_ROLES
from app.core.supabase_client import get_supabase
from app.models.schemas import GCContactIn, GCContactOut, GCIn, GCOut, MaterialCategoryUpdate

router = APIRouter(tags=["reference"])


# ── General contractors ───────────────────────────────────────────────────


@router.get("/gcs", response_model=list[GCOut])
def list_gcs(_: CurrentUser = Depends(require_internal)):
    return get_supabase().table("general_contractors").select("*").order("name").execute().data or []


@router.post("/gcs", response_model=GCOut, status_code=status.HTTP_201_CREATED)
def create_gc(body: GCIn, _: CurrentUser = Depends(require_writer)):
    return (
        get_supabase()
        .table("general_contractors")
        .insert(body.model_dump(mode="json"))
        .execute()
    ).data[0]


@router.get("/gc-contacts", response_model=list[GCContactOut])
def list_gc_contacts(
    gc_id: str | None = None,
    _: CurrentUser = Depends(require_internal),
):
    q = get_supabase().table("gc_contacts").select("*")
    if gc_id:
        q = q.eq("gc_id", gc_id)
    return q.order("name").execute().data or []


@router.post("/gc-contacts", response_model=GCContactOut, status_code=status.HTTP_201_CREATED)
def create_gc_contact(body: GCContactIn, _: CurrentUser = Depends(require_writer)):
    return (
        get_supabase().table("gc_contacts").insert(body.model_dump(mode="json")).execute()
    ).data[0]


# ── Material categories ───────────────────────────────────────────────────


@router.get("/material-categories")
def list_material_categories(_: CurrentUser = Depends(require_internal)):
    return (
        get_supabase()
        .table("material_categories")
        .select("*")
        .eq("is_active", True)
        .order("sort_order")
        .execute()
    ).data or []


@router.post("/material-categories", status_code=status.HTTP_201_CREATED)
def create_material_category(
    name: str,
    kind: str = "material",
    sort_order: int = 0,
    _: CurrentUser = Depends(require_writer),
):
    if kind not in ("material", "markup"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be material|markup")
    name = name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    sb = get_supabase()
    # Any writer can add one inline from the BOQ/PM panels, so two people naming
    # the same bucket must not fork the taxonomy — that would split one material
    # group across two RFQs. Same active name+kind: hand back what already
    # exists instead of inserting a twin. (Inactive same-name rows are left
    # alone; reactivating what IT Admin retired is their call, not a writer's.)
    existing = (
        sb.table("material_categories")
        .select("*")
        .eq("kind", kind)
        .eq("is_active", True)
        .execute()
    ).data or []
    for row in existing:
        if (row.get("name") or "").strip().casefold() == name.casefold():
            return row
    return (
        sb.table("material_categories")
        .insert({"name": name, "kind": kind, "sort_order": sort_order})
        .execute()
    ).data[0]


@router.patch("/material-categories/{category_id}")
def update_material_category(
    category_id: str,
    body: MaterialCategoryUpdate,
    _: CurrentUser = Depends(require_role(*CATEGORY_ADMIN_ROLES)),
):
    """Rename, reorder, or deactivate a category.

    Deactivating drops it from the active list, so it stops appearing in the
    BOQ-extraction prompt automatically (the prompt reads active categories at
    call time).
    """
    fields = body.model_dump(mode="json", exclude_none=True)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    updated = (
        get_supabase()
        .table("material_categories")
        .update(fields)
        .eq("id", category_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    return updated[0]

"""Reference data: general contractors (+ their contacts) and material categories.

GCs are needed by the intake form's multi-select; material categories drive RFQ
splitting. Any internal user can read and add GCs and GC contacts (the Contacts
page); material category writes stay restricted to IT Admin. GC contacts mirror
vendor_contacts: many named people per company; proposal sends pick recipients
from them per send.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_internal, require_role
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.models.schemas import GCContactIn, GCContactOut, GCIn, GCOut, MaterialCategoryUpdate

router = APIRouter(tags=["reference"])


# ── General contractors ───────────────────────────────────────────────────


@router.get("/gcs", response_model=list[GCOut])
async def list_gcs(_: CurrentUser = Depends(require_internal)):
    return get_supabase().table("general_contractors").select("*").order("name").execute().data or []


@router.post("/gcs", response_model=GCOut, status_code=status.HTTP_201_CREATED)
async def create_gc(body: GCIn, _: CurrentUser = Depends(require_internal)):
    return (
        get_supabase()
        .table("general_contractors")
        .insert(body.model_dump(mode="json"))
        .execute()
    ).data[0]


@router.get("/gc-contacts", response_model=list[GCContactOut])
async def list_gc_contacts(
    gc_id: str | None = None,
    _: CurrentUser = Depends(require_internal),
):
    q = get_supabase().table("gc_contacts").select("*")
    if gc_id:
        q = q.eq("gc_id", gc_id)
    return q.order("name").execute().data or []


@router.post("/gc-contacts", response_model=GCContactOut, status_code=status.HTTP_201_CREATED)
async def create_gc_contact(body: GCContactIn, _: CurrentUser = Depends(require_internal)):
    return (
        get_supabase().table("gc_contacts").insert(body.model_dump(mode="json")).execute()
    ).data[0]


# ── Material categories ───────────────────────────────────────────────────


@router.get("/material-categories")
async def list_material_categories(_: CurrentUser = Depends(require_internal)):
    return (
        get_supabase()
        .table("material_categories")
        .select("*")
        .eq("is_active", True)
        .order("sort_order")
        .execute()
    ).data or []


@router.post("/material-categories", status_code=status.HTTP_201_CREATED)
async def create_material_category(
    name: str,
    kind: str = "material",
    sort_order: int = 0,
    _: CurrentUser = Depends(require_role(Role.IT_ADMIN)),
):
    if kind not in ("material", "markup"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be material|markup")
    return (
        get_supabase()
        .table("material_categories")
        .insert({"name": name, "kind": kind, "sort_order": sort_order})
        .execute()
    ).data[0]


@router.patch("/material-categories/{category_id}")
async def update_material_category(
    category_id: str,
    body: MaterialCategoryUpdate,
    _: CurrentUser = Depends(require_role(Role.IT_ADMIN)),
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

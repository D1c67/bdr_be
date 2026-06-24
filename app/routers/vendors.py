"""Vendor and vendor-contact directory (used by RFQ dispatch).

Vendors/contacts are tagged with the material category they quote, so RFQ
dispatch can suggest the right contacts. Any internal user may read and add;
the external estimator has no access to the vendor directory.
"""

from fastapi import APIRouter, Depends

from app.core.deps import CurrentUser, require_internal
from app.core.supabase_client import get_supabase
from app.models.schemas import VendorContactIn, VendorIn

router = APIRouter(tags=["vendors"])


@router.get("/vendors")
async def list_vendors(_: CurrentUser = Depends(require_internal)):
    return get_supabase().table("vendors").select("*").order("name").execute().data or []


@router.post("/vendors", status_code=201)
async def create_vendor(body: VendorIn, _: CurrentUser = Depends(require_internal)):
    return get_supabase().table("vendors").insert(body.model_dump()).execute().data[0]


@router.get("/vendor-contacts")
async def list_contacts(
    material_category_id: str | None = None,
    _: CurrentUser = Depends(require_internal),
):
    q = get_supabase().table("vendor_contacts").select("*, vendors(name)")
    if material_category_id:
        q = q.eq("material_category_id", material_category_id)
    return q.order("name").execute().data or []


@router.post("/vendor-contacts", status_code=201)
async def create_contact(body: VendorContactIn, _: CurrentUser = Depends(require_internal)):
    return (
        get_supabase().table("vendor_contacts").insert(body.model_dump(mode="json")).execute()
    ).data[0]

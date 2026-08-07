"""Vendor and vendor-contact directory (used by RFQ dispatch).

Contacts are tagged with the material categories they quote, so RFQ dispatch can
suggest the right people. A contact may serve SEVERAL categories at once
(vendor_contact_categories, 0095) and a vendor company's categories are the
union across its contacts, so a rep who covers both switchgear and lighting is
one row that appears in both recipient lists. Any internal user may read and
add; the external estimator has no access to the vendor directory.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_internal, require_writer
from app.core.supabase_client import get_supabase
from app.models.schemas import VendorContactIn, VendorContactUpdate, VendorIn
from app.services.directory import (
    clean_company_name,
    duplicate_company_message,
    find_duplicate_company,
)

router = APIRouter(tags=["vendors"])

# A contact quotes a handful of trades at most. The cap only exists so a
# malformed client cannot write thousands of link rows in a single insert.
_MAX_CATEGORIES = 50


def _as_uuid(value: str, label: str) -> str:
    """Reject non-UUID ids before they reach Postgres.

    A garbage id would otherwise surface as a raw 22P02 and a 500; callers get a
    400 naming the field instead.
    """
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid {label}") from None
    return value


def _categories_by_contact(sb, contact_ids: list[str]) -> dict[str, list[str]]:
    """{vendor_contact_id: [material_category_id, ...]} for the given contacts."""
    if not contact_ids:
        return {}
    rows = (
        sb.table("vendor_contact_categories")
        .select("vendor_contact_id, material_category_id")
        .in_("vendor_contact_id", contact_ids)
        .execute()
    ).data or []
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["vendor_contact_id"], []).append(r["material_category_id"])
    return out


def _clean_category_ids(sb, ids: list[str]) -> list[str]:
    """Dedupe, cap, and verify that every id is a real material category.

    An unknown id would otherwise be a foreign-key 23505/23503 and a 500. The
    check does not require is_active: a contact already filed under a retired
    category keeps that link when their other categories are edited.
    """
    seen: list[str] = []
    for cid in ids:
        if cid and cid not in seen:
            seen.append(_as_uuid(cid, "material category id"))
    if not seen:
        return []
    if len(seen) > _MAX_CATEGORIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"At most {_MAX_CATEGORIES} categories per contact"
        )
    found = (sb.table("material_categories").select("id").in_("id", seen).execute()).data or []
    if len(found) != len(seen):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown material category")
    return seen


@router.get("/vendors")
def list_vendors(_: CurrentUser = Depends(require_internal)):
    """Vendor companies, each with the union of its contacts' categories."""
    sb = get_supabase()
    vendors = (sb.table("vendors").select("*").order("name").execute()).data or []
    if not vendors:
        return []
    contacts = (
        sb.table("vendor_contacts")
        .select("id, vendor_id")
        .in_("vendor_id", [v["id"] for v in vendors])
        .execute()
    ).data or []
    links = _categories_by_contact(sb, [c["id"] for c in contacts])
    by_vendor: dict[str, list[str]] = {}
    for c in contacts:
        bucket = by_vendor.setdefault(c["vendor_id"], [])
        for cid in links.get(c["id"], []):
            if cid not in bucket:
                bucket.append(cid)
    for v in vendors:
        v["material_category_ids"] = by_vendor.get(v["id"], [])
    return vendors


@router.post("/vendors", status_code=201)
def create_vendor(body: VendorIn, _: CurrentUser = Depends(require_writer)):
    """Add a vendor company. Refuses a name the directory already holds.

    Reached from the Vendors page, the Contacts page, and the "new company"
    option inside a project's RFQ step; the duplicate check sits here so all
    three are covered. A twin vendor would scatter one supplier's reps across
    two rows, so an RFQ would reach only the half filed under whichever twin
    the sender happened to pick. See app/services/directory.py.
    """
    sb = get_supabase()
    name = clean_company_name(body.name)
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Company name is required")
    dupe = find_duplicate_company(sb, "vendors", name)
    if dupe:
        raise HTTPException(
            status.HTTP_409_CONFLICT, duplicate_company_message("vendor", dupe["name"])
        )
    row = (sb.table("vendors").insert({**body.model_dump(), "name": name}).execute()).data[0]
    # A brand-new company has no contacts yet, so its category union is empty.
    # Stated explicitly so the shape matches GET /vendors.
    row["material_category_ids"] = []
    return row


@router.get("/vendor-contacts")
def list_contacts(
    material_category_id: str | None = None,
    _: CurrentUser = Depends(require_internal),
):
    """Contacts, optionally narrowed to one category.

    The filter resolves through the link table, so a contact who serves several
    categories is returned once under each of them. Reading via the link table
    (rather than fetching every contact and filtering in Python) keeps the
    recipient list to one round trip no matter how big the directory gets.
    """
    sb = get_supabase()
    if material_category_id:
        _as_uuid(material_category_id, "material category id")
        rows = (
            sb.table("vendor_contact_categories")
            .select("vendor_contacts(*, vendors(name))")
            .eq("material_category_id", material_category_id)
            .execute()
        ).data or []
        contacts = [r["vendor_contacts"] for r in rows if r.get("vendor_contacts")]
        contacts.sort(key=lambda c: (c.get("name") or "").casefold())
    else:
        contacts = (
            sb.table("vendor_contacts").select("*, vendors(name)").order("name").execute()
        ).data or []
    links = _categories_by_contact(sb, [c["id"] for c in contacts])
    for c in contacts:
        c["material_category_ids"] = links.get(c["id"], [])
    return contacts


@router.post("/vendor-contacts", status_code=201)
def create_contact(body: VendorContactIn, _: CurrentUser = Depends(require_writer)):
    sb = get_supabase()
    cat_ids = _clean_category_ids(sb, body.material_category_ids)
    payload = body.model_dump(mode="json", exclude={"material_category_ids"})
    contact = (sb.table("vendor_contacts").insert(payload).execute()).data[0]
    if cat_ids:
        sb.table("vendor_contact_categories").insert(
            [
                {"vendor_contact_id": contact["id"], "material_category_id": cid}
                for cid in cat_ids
            ]
        ).execute()
    contact["material_category_ids"] = cat_ids
    return contact


@router.patch("/vendor-contacts/{contact_id}")
def update_contact_categories(
    contact_id: str,
    body: VendorContactUpdate,
    _: CurrentUser = Depends(require_writer),
):
    """Replace which categories a contact quotes.

    The picker submits the whole set, so this deletes the existing links and
    writes the new ones rather than diffing. Existing contacts predate 0095 with
    a single category each, so this is how they gain the rest.
    """
    sb = get_supabase()
    _as_uuid(contact_id, "contact id")
    exists = (
        sb.table("vendor_contacts").select("id").eq("id", contact_id).limit(1).execute()
    ).data
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor contact not found")
    cat_ids = _clean_category_ids(sb, body.material_category_ids)
    sb.table("vendor_contact_categories").delete().eq("vendor_contact_id", contact_id).execute()
    if cat_ids:
        sb.table("vendor_contact_categories").insert(
            [{"vendor_contact_id": contact_id, "material_category_id": cid} for cid in cat_ids]
        ).execute()
    return {"id": contact_id, "material_category_ids": cat_ids}

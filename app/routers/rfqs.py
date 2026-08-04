"""RFQs and vendor quotes (steps 5-6).

The Estimating Engineer creates one RFQ per material category and bulk-sends it —
one individual email per selected vendor contact (optionally CC'ing coworkers at
the same vendor company on that contact's email), each tracked by its Graph
conversationId so inbound replies and quote PDFs can be matched automatically. Quotes arrive via the inbox poller (AI-extracted from PDFs) or
manual entry on the receive-quotes step; every manual change to an amount is
recorded.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_writer
from app.core.ratelimit import ai_rate_limit, bulk_send_rate_limit
from app.core.roles import INTERNAL_ROLES
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    QuoteIn,
    QuoteOverrideIn,
    RFQBulkSendIn,
    RFQCreate,
    RfqCustomPriceIn,
    RfqQuotesConfirmIn,
    TaxIn,
)
from app.routers.pricing import taxed_amount
from app.services import rfq_inbox, rfq_sending, workflow
from app.services.notifications import audit, dismiss_notifications

# Quote/reply notifications for an RFQ are stale once the engineer makes that
# category's pricing decision — selecting a quote, setting a custom price, or
# correcting an amount. Dismissed per-RFQ (not by stage) so late vendor quotes
# arriving after the project advances still produce fresh notifications.
_QUOTE_NOTIF_TYPES = ["quote.received", "rfq.reply_received"]

router = APIRouter(prefix="/projects/{project_id}/rfqs", tags=["rfqs"])
# RFQ/quote writes are open to any writer role (was PE-only).
_PE = require_writer


def _internal(user: CurrentUser) -> None:
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")


@router.get("")
def list_rfqs(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    # quotes(id, tax_included) rides along so the frontend's advance gate can
    # see unanswered per-quote tax attestations without an N+1 fetch.
    return (
        get_supabase()
        .table("rfqs")
        .select("*, material_categories(name, kind, is_general), quotes(id, tax_included)")
        .eq("project_id", project_id)
        .order("created_at")
        .execute()
    ).data or []


@router.post("", status_code=status.HTTP_201_CREATED)
def create_rfq(project_id: str, body: RFQCreate, user: CurrentUser = Depends(_PE)):
    payload = body.model_dump(mode="json")
    payload.update({"project_id": project_id, "created_by": user.id})
    try:
        row = get_supabase().table("rfqs").insert(payload).execute().data[0]
    except Exception as exc:  # unique(project_id, material_category_id)
        # Only a unique violation is a client-fixable 409; never echo the raw DB
        # error back (it can leak schema/internals). Anything else is a logged 500.
        if "23505" in str(getattr(exc, "code", "")) or "23505" in str(exc):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "An RFQ already exists for this category."
            ) from exc
        logging.getLogger("bdr.rfqs").exception("RFQ insert failed")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not create the RFQ."
        ) from exc
    audit(user.id, "rfq.create", "rfq", row["id"], {"category": body.material_category_id})
    return row


# ── Sending ───────────────────────────────────────────────────────────────


@router.get("/email-preview")
def email_preview(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """Representative subject/body so the PE can see what vendors will receive.
    The actual body is lightly varied per email by AI; this is the base template."""
    _internal(user)
    proj = (
        get_supabase().table("projects").select("*").eq("id", project_id).single().execute()
    ).data
    due = proj.get("due_from_vendors_at")
    return {
        "subject": rfq_sending.build_subject(proj),
        "body": rfq_sending.build_base_body(
            "<Contact Name>",
            rfq_sending.format_bid_datetime(due) if due else "<due from vendors date>",
            None,
        ),
    }


@router.post("/bulk-send", dependencies=[Depends(bulk_send_rate_limit)])
def bulk_send(project_id: str, body: RFQBulkSendIn, user: CurrentUser = Depends(_PE)):
    """Send each group's RFQ to its selected contacts — one email per contact.
    Per-contact failures are reported in `results`, not raised."""
    if not body.groups or not any(g.vendor_contact_ids for g in body.groups):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No contacts selected")
    try:
        result = rfq_sending.bulk_send(
            project_id,
            [g.model_dump() for g in body.groups],
            user.id,
            body.email_body,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    audit(
        user.id,
        "rfq.bulk_send",
        "project",
        project_id,
        {
            "sent": sum(1 for r in result["results"] if r["status"] == "sent"),
            "failed": sum(1 for r in result["results"] if r["status"] == "failed"),
            "cc": sum(len(ids) for g in body.groups for ids in (g.cc or {}).values()),
            "custom_body": body.email_body is not None,
            "custom_attachments": any(g.attachment_file_ids is not None for g in body.groups),
        },
    )
    return result


@router.get("/sends")
def list_sends(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """Every individual RFQ email for the project, incl. its Graph conversation id."""
    _internal(user)
    return (
        get_supabase()
        .table("rfq_sends")
        .select(
            "id, rfq_id, vendor_contact_id, cc_recipients, conversation_id, subject, "
            "status, error, polling_active, quote_received_at, sent_at, created_at, "
            "rfqs!inner(project_id), vendor_contacts(name, email, vendors(name))"
        )
        .eq("rfqs.project_id", project_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []


@router.get("/messages")
def list_messages(project_id: str, user: CurrentUser = Depends(get_current_user)):
    """Inbound vendor replies matched by conversation id, newest first."""
    _internal(user)
    return (
        get_supabase()
        .table("rfq_messages")
        .select(
            "id, rfq_send_id, from_addr, subject, body_preview, received_at, "
            "has_attachments, extraction_status, extraction_error, cloud_link_count, "
            "created_at, rfq_sends!inner(rfq_id, conversation_id, rfqs!inner(project_id))"
        )
        .eq("rfq_sends.rfqs.project_id", project_id)
        .order("received_at", desc=True)
        .execute()
    ).data or []


@router.post("/messages/{message_id}/refetch-links", dependencies=[Depends(ai_rate_limit)])
def refetch_message_links(
    project_id: str, message_id: str, user: CurrentUser = Depends(_PE)
):
    """Retry pulling the cloud-share links (OneDrive/Drive/Dropbox/Box) out of a
    vendor reply whose files never made it in — downloads run in the request,
    so the PE sees the outcome immediately."""
    try:
        result = rfq_inbox.refetch_link_files(project_id, message_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    audit(
        user.id,
        "rfq.link_refetch",
        "rfq_message",
        message_id,
        {
            "links_found": result["links_found"],
            "files_ingested": result["files_ingested"],
            "extraction_status": result["extraction_status"],
        },
    )
    return result


# ── Quotes ────────────────────────────────────────────────────────────────


def _rfq_in_project(sb, project_id: str, rfq_id: str) -> dict:
    """The RFQ, 404ing when it doesn't exist under the path's project — so an
    ID mix-up can never read or mutate another project's pricing."""
    rows = (
        sb.table("rfqs")
        .select("id, custom_amount, material_categories(is_general)")
        .eq("id", rfq_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFQ not found")
    return rows[0]


def _not_general(rfq: dict) -> None:
    """Category-price actions are meaningless on General Material — pricing
    takes the estimate's wiring figure and would silently ignore them."""
    if (rfq.get("material_categories") or {}).get("is_general"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "General Material is priced from the estimate, not vendor quotes",
        )


@router.get("/{rfq_id}/quotes")
def list_quotes(project_id: str, rfq_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    sb = get_supabase()
    rfq = _rfq_in_project(sb, project_id, rfq_id)
    quotes = (
        sb.table("quotes")
        .select("*, vendors(name)")
        .eq("rfq_id", rfq_id)
        .order("amount")
        .execute()
    ).data or []
    # Lowest by tax-INCLUSIVE amount — comparing raw quotes would flatter a
    # vendor whose price doesn't yet carry sales tax.
    lowest = min((taxed_amount(q) for q in quotes), default=None)
    return {
        "quotes": quotes,
        "lowest_amount": str(lowest) if lowest is not None else None,
        "custom_amount": rfq["custom_amount"],
    }


@router.post("/{rfq_id}/quotes", status_code=status.HTTP_201_CREATED)
def add_quote(project_id: str, rfq_id: str, body: QuoteIn, user: CurrentUser = Depends(_PE)):
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
    payload = body.model_dump(mode="json")
    payload["rfq_id"] = rfq_id
    payload["source"] = "manual"
    row = sb.table("quotes").insert(payload).execute().data[0]
    sb.table("rfqs").update({"status": "quotes_in"}).eq("id", rfq_id).execute()
    audit(user.id, "quote.add", "quote", row["id"], {"amount": str(body.amount)})
    return row


@router.patch("/{rfq_id}/quotes/{quote_id}")
def override_quote(
    project_id: str,
    rfq_id: str,
    quote_id: str,
    body: QuoteOverrideIn,
    user: CurrentUser = Depends(_PE),
):
    """Manually change a quote amount (e.g. correct an AI-extracted number).
    Every change is recorded in quote_revisions."""
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
    # Plain list select: .single() raises on zero rows, which would turn a
    # missing quote into a 500 instead of this 404.
    quotes = (
        sb.table("quotes").select("*").eq("id", quote_id).eq("rfq_id", rfq_id).execute()
    ).data
    if not quotes:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quote not found")
    quote = quotes[0]
    sb.table("quote_revisions").insert(
        {
            "quote_id": quote_id,
            "previous_amount": quote["amount"],
            "new_amount": str(body.amount),
            "previous_source": quote.get("source"),
            "changed_by": user.id,
            "note": body.note,
        }
    ).execute()
    updated = (
        sb.table("quotes")
        .update({"amount": str(body.amount), "source": "manual"})
        .eq("id", quote_id)
        .execute()
    ).data[0]
    audit(
        user.id,
        "quote.override",
        "quote",
        quote_id,
        {"previous_amount": str(quote["amount"]), "new_amount": str(body.amount)},
    )
    dismiss_notifications(rfq_id=rfq_id, types=_QUOTE_NOTIF_TYPES)
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Vendor quote amount changed")
    return updated


@router.post("/{rfq_id}/quotes/{quote_id}/select")
def select_quote(
    project_id: str, rfq_id: str, quote_id: str, user: CurrentUser = Depends(_PE)
):
    """Mark the chosen quote as the category price (clears any prior selection
    and any custom price — the two are mutually exclusive). Validates before
    mutating so a 404 never alters pricing state."""
    sb = get_supabase()
    rfq = _rfq_in_project(sb, project_id, rfq_id)
    _not_general(rfq)
    target = (
        sb.table("quotes").select("id").eq("id", quote_id).eq("rfq_id", rfq_id).execute()
    ).data
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quote not found")
    sb.table("quotes").update({"is_selected": False}).eq("rfq_id", rfq_id).execute()
    updated = (
        sb.table("quotes").update({"is_selected": True}).eq("id", quote_id).execute()
    ).data
    sb.table("rfqs").update(
        {"custom_amount": None, "custom_set_by": None, "custom_set_at": None}
    ).eq("id", rfq_id).execute()
    audit(user.id, "quote.select", "quote", quote_id, None)
    dismiss_notifications(rfq_id=rfq_id, types=_QUOTE_NOTIF_TYPES)
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Quote selection changed")
    return updated[0]


@router.put("/{rfq_id}/custom-price")
def set_custom_price(
    project_id: str, rfq_id: str, body: RfqCustomPriceIn, user: CurrentUser = Depends(_PE)
):
    """Price the category with a custom number instead of any vendor quote
    (amount=null clears it). Setting it deselects any selected quote; pricing
    precedence is custom > selected > lowest."""
    sb = get_supabase()
    rfq = _rfq_in_project(sb, project_id, rfq_id)
    _not_general(rfq)
    if body.amount is not None:
        sb.table("quotes").update({"is_selected": False}).eq("rfq_id", rfq_id).execute()
    updated = (
        sb.table("rfqs")
        .update(
            {
                "custom_amount": str(body.amount) if body.amount is not None else None,
                "custom_set_by": user.id if body.amount is not None else None,
                "custom_set_at": "now()" if body.amount is not None else None,
            }
        )
        .eq("id", rfq_id)
        .execute()
    ).data[0]
    audit(
        user.id,
        "quote.custom_price",
        "rfq",
        rfq_id,
        {
            "previous_amount": str(rfq["custom_amount"]) if rfq["custom_amount"] is not None else None,
            "new_amount": str(body.amount) if body.amount is not None else None,
            "note": body.note,
        },
    )
    # Only a real price (not clearing it back to null) counts as handling the RFQ.
    if body.amount is not None:
        dismiss_notifications(rfq_id=rfq_id, types=_QUOTE_NOTIF_TYPES)
    # Both setting and clearing a custom price change the category's effective
    # amount, so either way re-verify if the project already passed Verify.
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Custom material price changed")
    return updated


@router.put("/{rfq_id}/quotes-confirmed")
def set_quotes_confirmed(
    project_id: str, rfq_id: str, body: RfqQuotesConfirmIn, user: CurrentUser = Depends(_PE)
):
    """Record the PE's "it's complete" check on the receive-quotes step — an
    attestation that the vendor quoted the entire RFQ and didn't miss a
    material. The frontend won't leave the step until every (non-General)
    category is confirmed; the backend only stores who confirmed and when."""
    sb = get_supabase()
    rfq = _rfq_in_project(sb, project_id, rfq_id)
    _not_general(rfq)  # General Material has no vendor quotes to confirm
    updated = (
        sb.table("rfqs")
        .update(
            {
                "quotes_confirmed": body.confirmed,
                "quotes_confirmed_by": user.id if body.confirmed else None,
                "quotes_confirmed_at": "now()" if body.confirmed else None,
            }
        )
        .eq("id", rfq_id)
        .execute()
    ).data[0]
    audit(user.id, "rfq.quotes_confirmed", "rfq", rfq_id, {"confirmed": body.confirmed})
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Quotes-confirmed flag changed")
    return updated


@router.put("/{rfq_id}/quotes/{quote_id}/tax")
def set_quote_tax(
    project_id: str,
    rfq_id: str,
    quote_id: str,
    body: TaxIn,
    user: CurrentUser = Depends(_PE),
):
    """Record whether THIS vendor's quote already included sales tax, and the
    rate to apply when it did not. Quotes are compared and carried by their
    tax-inclusive amount, so the materials figure reflects the true cost
    incurred; the receive-quotes step can't be left until every quote in a
    non-General category is answered (frontend gate). General Material's tax
    question lives on its estimate figure (see routers/general_material), not
    on its informational quotes."""
    sb = get_supabase()
    rfq = _rfq_in_project(sb, project_id, rfq_id)
    _not_general(rfq)  # General Material is priced from the estimate, not quotes
    updated = (
        sb.table("quotes")
        .update({"tax_included": body.tax_included, "tax_rate": str(body.tax_rate)})
        .eq("id", quote_id)
        .eq("rfq_id", rfq_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quote not found")
    audit(
        user.id,
        "quote.tax",
        "quote",
        quote_id,
        {"tax_included": body.tax_included, "tax_rate": str(body.tax_rate)},
    )
    # The tax-inclusive amount changes the materials price basis, so re-verify
    # if the project already passed Verify (mirrors custom-price / selection).
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Quote tax setting changed")
    return updated[0]

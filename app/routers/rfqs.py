"""RFQs and vendor quotes (steps 5-6), and the read behind Select Vendors.

The Estimating Engineer creates one RFQ per material category and bulk-sends it,
one individual email per selected vendor contact (optionally CC'ing coworkers at
the same vendor company on that contact's email), each tracked by its Graph
conversationId so inbound replies and quote PDFs can be matched automatically.
Quotes arrive via the inbox poller (AI-extracted from PDFs), by hand against a
vendor on the receive-quotes step, or as a hand-entered figure with no vendor
behind it at all; every manual change to an amount is recorded.

Two rules govern everything below.

  SELECTION IS THE PRICE. A category is priced by the quote a human picked on
  Select Vendors, or it has no price. There is no precedence chain: the lowest
  quote received is a display detail, a hand-entered number is just another
  candidate (quotes.origin 'manual'), and General Material's wiring figure off
  the estimate is a candidate too ('estimate'). rfqs.custom_amount is a dead
  column that nothing reads, and nothing here may write it as a price again.

  ONLY AN APPROVED QUOTE CAN WIN. Approval (quotes.is_approved) is a human
  saying the amount on the row is the amount that was quoted and that its
  sales-tax question has been answered. It happens on Receive Quotes and is per
  QUOTE, not per category, so a winner can be picked out of the approved quotes
  already in while other vendors are still out.
"""

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_writer
from app.core.error_codes import RateLimitScope
from app.core.ratelimit import (
    ai_rate_limit,
    bulk_send_rate_limit,
    rate_limit,
    rfq_nudge_rate_limit,
)
from app.core.roles import INTERNAL_ROLES
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    ManualQuoteIn,
    QuoteApprovalIn,
    QuoteIn,
    QuoteOverrideIn,
    ReplyManualQuoteIn,
    RFQBulkSendIn,
    RFQCreate,
    RFQNudgeIn,
    RFQNudgeOut,
    RFQRecipientsOut,
    RfqQuotesConfirmIn,
    TaxIn,
)
from app.routers.pricing import tax_info, taxed_amount
from app.services import rfq_inbox, rfq_nudges, rfq_sending, vendor_selection, workflow
from app.services.notifications import audit, dismiss_notifications
from app.services.sanitize import sanitize_rich_text

# Quote/reply notifications for an RFQ are stale once the engineer makes that
# category's pricing decision — picking the winning quote or correcting an
# amount. Dismissed per-RFQ (not by stage) so late vendor quotes arriving after
# the project advances still produce fresh notifications.
_QUOTE_NOTIF_TYPES = ["quote.received", "rfq.reply_received"]

# An RFQ that actually left the building; a draft never did.
_SENT_RFQ_STATUSES = ("sent", "quotes_in", "closed")

# The exported router is project-scoped because Select Vendors reads the whole
# project in one call; everything RFQ-shaped hangs off /rfqs under it, on the
# nested router included at the bottom of this module.
router = APIRouter(prefix="/projects/{project_id}", tags=["rfqs"])
rfq_router = APIRouter(prefix="/rfqs")
# RFQ/quote writes are open to any writer role (was PE-only).
_PE = require_writer

# Approving a quote and typing one in are cheap, high-frequency clicks: an
# estimator works through a stack of them in one sitting. They ride the generous
# catch-all budget, not the AI/bulk-send limits that guard expensive surfaces.
quote_write_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)


def _internal(user: CurrentUser) -> None:
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")


@rfq_router.get("")
def list_rfqs(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    # The quotes ride along so the frontend's advance gates can see, without an
    # N+1 fetch, which quotes still have an unanswered tax question, which are
    # signed off, and whether the category has a winner at all.
    return (
        get_supabase()
        .table("rfqs")
        .select(
            "*, material_categories(name, kind, is_general),"
            " quotes(id, tax_included, is_approved, is_selected, origin)"
        )
        .eq("project_id", project_id)
        .order("created_at")
        .execute()
    ).data or []


@rfq_router.post("", status_code=status.HTTP_201_CREATED)
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


@rfq_router.get("/email-preview")
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
        # Per-vendor CCs are picked in the panel; this is the standing internal
        # CC so the confirm modal can show who else is copied on every send.
        "cc": rfq_sending.internal_cc([]),
    }


@rfq_router.post("/bulk-send", dependencies=[Depends(bulk_send_rate_limit)])
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


@rfq_router.get("/sends")
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


@rfq_router.get("/messages")
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


@rfq_router.post("/messages/{message_id}/refetch-links", dependencies=[Depends(ai_rate_limit)])
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


# Everything the reply review modal needs in one read: the email itself, who it
# came from, and the quote files it brought in.
_MESSAGE_DETAIL_SELECT = (
    "id, rfq_send_id, from_addr, subject, body, received_at, has_attachments, "
    "extraction_status, extraction_error, cloud_link_count, created_at, "
    "rfq_sends(id, rfq_id, quote_received_at, "
    "vendor_contacts(id, name, email, vendor_id, vendors(name)), "
    "rfqs(id, project_id, material_category_id, material_categories(name)))"
)


def _message_in_project(sb, project_id: str, message_id: str) -> dict:
    """One inbound reply with its send/contact/RFQ context, 404ing when it does
    not belong to the path's project."""
    rows = (
        sb.table("rfq_messages")
        .select(_MESSAGE_DETAIL_SELECT)
        .eq("id", message_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    row = rows[0]
    send = row.get("rfq_sends") or {}
    if ((send.get("rfqs") or {}).get("project_id")) != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    return row


def _quote_file_in_category(
    sb, project_id: str, material_category_id: str, quote_file_id: str
) -> None:
    """404 unless the file a quote wants to carry is one of this project's
    quote files for this category, so a stray id can't attach another
    project's (or another category's) document."""
    files = (
        sb.table("project_files")
        .select("id")
        .eq("id", quote_file_id)
        .eq("project_id", project_id)
        .eq("category", "quote")
        .eq("material_category_id", material_category_id)
        .execute()
    ).data
    if not files:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quote file not found")


@rfq_router.get("/messages/{message_id}")
def get_message(project_id: str, message_id: str, user: CurrentUser = Depends(get_current_user)):
    """One vendor reply in full, for the reply review modal: the email body
    (sanitized), the sender/vendor context, and the quote files this reply
    brought in.

    The stored body is raw vendor HTML and must stay raw (refetch-links
    re-parses it for share URLs), so this endpoint is the sanitize boundary:
    the browser only ever receives the nh3-cleaned rendering, never the
    original. Vendor markup (tables, styles, images) is reduced to its text -
    fine for a review modal; the attachments carry the real content.
    """
    _internal(user)
    sb = get_supabase()
    row = _message_in_project(sb, project_id, message_id)
    send = row["rfq_sends"]
    contact = send.get("vendor_contacts") or {}
    rfq = send["rfqs"]
    files = (
        sb.table("project_files")
        .select("id, filename, mime_type, size_bytes, created_at")
        .eq("rfq_message_id", row["id"])
        .order("created_at")
        .execute()
    ).data or []
    return {
        "id": row["id"],
        "rfq_id": send["rfq_id"],
        "from_addr": row.get("from_addr"),
        "subject": row.get("subject"),
        "received_at": row.get("received_at"),
        "has_attachments": row.get("has_attachments"),
        "extraction_status": row.get("extraction_status"),
        "extraction_error": row.get("extraction_error"),
        "cloud_link_count": row.get("cloud_link_count"),
        "body_html": sanitize_rich_text(row.get("body") or ""),
        "vendor_name": (contact.get("vendors") or {}).get("name"),
        "contact_name": contact.get("name"),
        "contact_email": contact.get("email"),
        "category_name": (rfq.get("material_categories") or {}).get("name"),
        "files": files,
    }


@rfq_router.post(
    "/messages/{message_id}/manual-quote",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(quote_write_rate_limit)],
)
def add_reply_manual_quote(
    project_id: str,
    message_id: str,
    body: ReplyManualQuoteIn,
    user: CurrentUser = Depends(_PE),
):
    """Enter the amount for a reply the extractor could not read, FROM that
    reply. This is what the generic add-quote form cannot do: the quote lands
    against the vendor the send went to, bound to the reply (and optionally one
    of its files), the reply's notice is resolved ('manual'), and the send is
    marked answered so the poller stops watching it — exactly the bookkeeping a
    successful extraction performs, minus the extraction.

    The quote itself is a candidate like any other vendor quote: unapproved
    until someone signs it off on the table (an optional tax answer given here
    is recorded on the row, saving that later trip).
    """
    sb = get_supabase()
    row = _message_in_project(sb, project_id, message_id)
    if row.get("extraction_status") in ("done", "manual"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This reply already has a quote recorded."
        )
    send = row["rfq_sends"]
    contact = send.get("vendor_contacts") or {}
    rfq = send["rfqs"]

    if body.quote_file_id:
        _quote_file_in_category(
            sb, project_id, rfq["material_category_id"], body.quote_file_id
        )

    payload: dict = {
        "rfq_id": send["rfq_id"],
        "vendor_id": contact.get("vendor_id"),
        "vendor_contact_id": contact.get("id"),
        "amount": str(body.amount),
        "origin": "vendor",
        "source": "manual",
        "rfq_send_id": send["id"],
        "rfq_message_id": row["id"],
        "quote_file_id": body.quote_file_id,
        "notes": body.notes,
    }
    if body.tax_included is not None:
        payload["tax_included"] = body.tax_included
        payload["tax_rate"] = str(body.tax_rate)
    quote = sb.table("quotes").insert(payload).execute().data[0]

    sb.table("rfq_messages").update(
        {"extraction_status": "manual", "extraction_error": None}
    ).eq("id", message_id).execute()
    sb.table("rfqs").update({"status": "quotes_in"}).eq("id", send["rfq_id"]).execute()
    send_update: dict = {"polling_active": False}
    if not send.get("quote_received_at"):
        send_update["quote_received_at"] = "now()"
    sb.table("rfq_sends").update(send_update).eq("id", send["id"]).execute()

    audit(
        user.id,
        "quote.reply_manual_add",
        "quote",
        quote["id"],
        {
            "rfq_id": send["rfq_id"],
            "rfq_message_id": row["id"],
            "amount": str(body.amount),
            "tax_included": body.tax_included,
            "quote_file_id": body.quote_file_id,
        },
    )
    return quote


@rfq_router.post("/check-quotes", dependencies=[Depends(ai_rate_limit)])
def check_quotes(project_id: str, user: CurrentUser = Depends(_PE)):
    """Check the mailbox for this project's outstanding RFQ replies right now,
    instead of waiting for the background poller's next pass. New replies are
    matched, their PDFs stored and their amounts extracted in the request, so the
    estimator sees what came in without leaving Receive Quotes.

    The inbox runs under a single-runner lease: when the poller (or another
    person's check) already holds it, this is a 409 to retry, not a failure.
    """
    try:
        result = rfq_inbox.check_project_quotes(project_id)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            str(exc) or "A quote check is already running. Give it a moment and try again.",
        ) from exc
    audit(
        user.id,
        "rfq.check_quotes",
        "project",
        project_id,
        {
            "sends_checked": result.get("sends_checked"),
            "messages_seen": result.get("messages_seen"),
            "quotes_created": result.get("quotes_created"),
            "errors": len(result.get("errors") or []),
        },
    )
    return result


# ── Nudges (vendor reminders on the RFQ thread) ────────────────────────────


@rfq_router.get("/{rfq_id}/recipients", response_model=RFQRecipientsOut)
def list_rfq_recipients(
    project_id: str, rfq_id: str, user: CurrentUser = Depends(get_current_user)
):
    """Per-contact reply status for one RFQ: who quoted, who replied without a
    quote, who never answered - and which send a nudge would reply on. A
    contact's re-sends collapse onto their latest send (see rfq_nudges)."""
    _internal(user)
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
    return {"recipients": rfq_nudges.recipients_status(sb, rfq_id)}


@rfq_router.post(
    "/{rfq_id}/nudges",
    response_model=RFQNudgeOut,
    dependencies=[Depends(rfq_nudge_rate_limit)],
)
def send_rfq_nudges(
    project_id: str, rfq_id: str, body: RFQNudgeIn, user: CurrentUser = Depends(_PE)
):
    """Email reminder "nudges" as reply-alls on the selected sends' existing
    Graph threads. Each target carries its FINAL message text (the frontend
    already substituted the template tokens). Per-target failures are reported
    in `results`, not raised, and a contact whose quote arrived in the
    meantime is skipped, never emailed."""
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
    results = rfq_nudges.send_nudges(
        sb, project_id, rfq_id, [t.model_dump() for t in body.targets], user.id
    )
    audit(
        user.id,
        "rfq.nudge_batch",
        "rfq",
        rfq_id,
        {
            "targets": len(results),
            "sent": sum(1 for r in results if r["status"] == "sent"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "skipped": sum(
                1 for r in results if r["status"] == "skipped_quote_received"
            ),
        },
    )
    return {"results": results}


# ── Quotes ────────────────────────────────────────────────────────────────


def _rfq_in_project(sb, project_id: str, rfq_id: str) -> dict:
    """The RFQ, 404ing when it doesn't exist under the path's project — so an
    ID mix-up can never read or mutate another project's pricing.

    General Material is deliberately NOT special-cased anywhere below it: it is a
    category like any other now, with candidates that have to be approved and one
    of them picked.
    """
    rows = (
        sb.table("rfqs")
        .select("id, status, material_category_id, material_categories(is_general)")
        .eq("id", rfq_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFQ not found")
    return rows[0]


def _quote_in_rfq(sb, rfq_id: str, quote_id: str) -> dict:
    """One quote, 404ing when it isn't on this RFQ. Plain list select: .single()
    raises on zero rows, which would turn a missing quote into a 500."""
    rows = (
        sb.table("quotes").select("*").eq("id", quote_id).eq("rfq_id", rfq_id).execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quote not found")
    return rows[0]


@rfq_router.get("/{rfq_id}/quotes")
def list_quotes(project_id: str, rfq_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
    quotes = (
        sb.table("quotes")
        .select("*, vendors(name)")
        .eq("rfq_id", rfq_id)
        .order("amount")
        .execute()
    ).data or []
    # Lowest by tax-INCLUSIVE amount — comparing raw quotes would flatter a
    # vendor whose price doesn't yet carry sales tax. Display only: the lowest
    # quote prices nothing, the SELECTED one does.
    lowest = min((taxed_amount(q) for q in quotes), default=None)
    return {
        "quotes": quotes,
        "lowest_amount": str(lowest) if lowest is not None else None,
    }


@rfq_router.post("/{rfq_id}/quotes", status_code=status.HTTP_201_CREATED)
def add_quote(project_id: str, rfq_id: str, body: QuoteIn, user: CurrentUser = Depends(_PE)):
    """Type in a quote that a VENDOR gave (origin stays 'vendor' — the number
    came from them, it just didn't arrive through the mailbox). It lands
    unapproved with its tax question unanswered, exactly like an extracted one,
    so Receive Quotes still has to sign it off before it can win. An optional
    quote_file_id binds an already-uploaded quote document to the row; the file
    is reference only, never extracted."""
    sb = get_supabase()
    rfq = _rfq_in_project(sb, project_id, rfq_id)
    if body.quote_file_id:
        _quote_file_in_category(
            sb, project_id, rfq["material_category_id"], body.quote_file_id
        )
    payload = body.model_dump(mode="json")
    payload["rfq_id"] = rfq_id
    payload["source"] = "manual"
    row = sb.table("quotes").insert(payload).execute().data[0]
    sb.table("rfqs").update({"status": "quotes_in"}).eq("id", rfq_id).execute()
    audit(
        user.id,
        "quote.add",
        "quote",
        row["id"],
        {"amount": str(body.amount), "quote_file_id": body.quote_file_id},
    )
    return row


@rfq_router.post(
    "/{rfq_id}/quotes/manual",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(quote_write_rate_limit)],
)
def add_manual_quote(
    project_id: str, rfq_id: str, body: ManualQuoteIn, user: CurrentUser = Depends(_PE)
):
    """Add a hand-entered candidate to a category: a figure the estimator has in
    hand with no vendor behind it (a price given over the phone, a budget number,
    a price carried across from another job). This replaces the old per-category
    custom price, which used to outrank every quote received; it now competes on
    exactly the same footing as one, and only prices the category if somebody
    picks it on Select Vendors. As many per category as the estimator wants.

    The row is created APPROVED: the person typing it is the human who attests
    both to the amount and to the sales-tax answer the payload carries, which is
    the whole content of an approval. An optional quote_file_id binds an
    already-uploaded quote document to the row; the file is reference only,
    never extracted — the typed amount is the quote.
    """
    sb = get_supabase()
    rfq = _rfq_in_project(sb, project_id, rfq_id)
    if body.quote_file_id:
        _quote_file_in_category(
            sb, project_id, rfq["material_category_id"], body.quote_file_id
        )
    row = (
        sb.table("quotes")
        .insert(
            {
                "rfq_id": rfq_id,
                # No vendor, no contact: nobody quoted this, a human wrote it down.
                "vendor_id": None,
                "amount": str(body.amount),
                "origin": "manual",
                "source": "manual",
                "notes": body.notes,
                "quote_file_id": body.quote_file_id,
                "tax_included": body.tax_included,
                "tax_rate": str(body.tax_rate),
                "is_approved": True,
                "approved_by": user.id,
                "approved_at": "now()",
            }
        )
        .execute()
    ).data[0]
    audit(
        user.id,
        "quote.manual_add",
        "quote",
        row["id"],
        {
            "rfq_id": rfq_id,
            "amount": str(body.amount),
            "tax_included": body.tax_included,
            "note": body.notes,
            "quote_file_id": body.quote_file_id,
        },
    )
    # No re-verify bounce here: adding a candidate changes no price. The category
    # keeps whatever it had until someone selects this row (see select_quote).
    return row


@rfq_router.delete(
    "/{rfq_id}/quotes/{quote_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(quote_write_rate_limit)],
)
def delete_quote(
    project_id: str, rfq_id: str, quote_id: str, user: CurrentUser = Depends(_PE)
):
    """Take a candidate back off the table on Receive Quotes: a typo, a figure
    that turned out to be wrong, a duplicate of a quote that also arrived by
    email, a number logged against the wrong vendor.

    THE FILE STAYS. quotes.quote_file_id is a plain reference with no cascade, so
    the vendor's PDF, and the reply it arrived on, remain in the project's files
    exactly as they were. This removes the number, not the paperwork, which is
    also what keeps the removal recoverable: the document the figure came from is
    still there to re-read.

    An emailed quote's reply is reopened for manual entry (see below), so the
    estimator who removes a mis-extracted figure can type the right one straight
    back in against the same vendor and the same file.

    General Material's estimate row is the one candidate that cannot go: it is
    not a quote, it is the wiring figure the estimate extraction wrote, and it
    belongs to general_material_estimates. Correct that figure on the General
    Material line instead, where the edit keeps the two records in step.
    """
    sb = get_supabase()
    rfq = _rfq_in_project(sb, project_id, rfq_id)
    quote = _quote_in_rfq(sb, rfq_id, quote_id)
    origin = quote.get("origin") or "vendor"
    if origin == "estimate":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The estimate's figure can't be removed. Correct the amount instead.",
        )
    was_selected = bool(quote.get("is_selected"))
    sb.table("quotes").delete().eq("id", quote_id).eq("rfq_id", rfq_id).execute()
    audit(
        user.id,
        "quote.delete",
        "quote",
        quote_id,
        {
            "rfq_id": rfq_id,
            "amount": str(quote["amount"]),
            "origin": origin,
            "vendor_id": quote.get("vendor_id"),
            "quote_file_id": quote.get("quote_file_id"),
            "was_selected": was_selected,
        },
    )
    # A quote that came in on a reply marked that reply answered, and an answered
    # reply refuses manual entry (add_reply_manual_quote 409s on 'done'/'manual').
    # Removing the number without reopening the reply would therefore be a dead
    # end: the estimator could no longer enter the correct figure from the email
    # it actually arrived on. Back to needs_review, which is exactly what the
    # reply is now, and it reappears in the panel's notices ready to be reviewed.
    message_id = quote.get("rfq_message_id")
    if message_id:
        sb.table("rfq_messages").update(
            {
                "extraction_status": "needs_review",
                "extraction_error": "The quote recorded for this reply was removed",
            }
        ).eq("id", message_id).execute()
    # An RFQ with nothing left on it is back to waiting on vendors rather than
    # sitting on quotes: left at 'quotes_in' it would keep suppressing its due
    # reminders (see due_reminders) over numbers that no longer exist. 'closed'
    # is a deliberate end state and is left alone. The send's polling_active
    # stays off; "Check for new quotes" re-reads answered sends anyway, which is
    # how a vendor's revised quote gets in.
    if rfq.get("status") == "quotes_in":
        remaining = (
            sb.table("quotes").select("id").eq("rfq_id", rfq_id).execute()
        ).data or []
        if not remaining:
            sb.table("rfqs").update({"status": "sent"}).eq("id", rfq_id).execute()
    # Removing the winner leaves the category with no price at all.
    if was_selected:
        workflow.maybe_reopen_verify_after_edit(
            project_id, user.id, "Winning quote removed"
        )


@rfq_router.patch("/{rfq_id}/quotes/{quote_id}")
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
    quote = _quote_in_rfq(sb, rfq_id, quote_id)
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


@rfq_router.post(
    "/{rfq_id}/quotes/{quote_id}/approval",
    dependencies=[Depends(quote_write_rate_limit)],
)
def set_quote_approval(
    project_id: str,
    rfq_id: str,
    quote_id: str,
    body: QuoteApprovalIn,
    user: CurrentUser = Depends(_PE),
):
    """Sign one quote off on Receive Quotes, or withdraw that sign-off.

    Approving says two things at once: the amount on the row is the amount that
    was quoted (the extractor's number has been eyeballed against the PDF), and
    the sales-tax question has been answered — which is why an unanswered
    tax_included is a 409 rather than a silent approval of a figure whose true
    cost isn't known yet.

    Withdrawing approval from the quote that currently WINS its category also
    withdraws the selection: a number nobody stands behind must never be left
    standing as the price.
    """
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
    quote = _quote_in_rfq(sb, rfq_id, quote_id)
    if body.approved and quote.get("tax_included") is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Answer the sales tax question on this quote before approving it.",
        )
    payload: dict = {
        "is_approved": body.approved,
        "approved_by": user.id if body.approved else None,
        "approved_at": "now()" if body.approved else None,
    }
    cleared_selection = bool(quote.get("is_selected")) and not body.approved
    if cleared_selection:
        payload["is_selected"] = False
    updated = (
        sb.table("quotes")
        .update(payload)
        .eq("id", quote_id)
        .eq("rfq_id", rfq_id)
        .execute()
    ).data[0]
    audit(
        user.id,
        "quote.approval",
        "quote",
        quote_id,
        {
            "rfq_id": rfq_id,
            "approved": body.approved,
            "amount": str(quote["amount"]),
            "cleared_selection": cleared_selection,
        },
    )
    # Only the un-approval that dropped a winner moved a price.
    if cleared_selection:
        workflow.maybe_reopen_verify_after_edit(
            project_id, user.id, "Winning quote's approval withdrawn"
        )
    return updated


@rfq_router.post("/{rfq_id}/quotes/{quote_id}/select")
def select_quote(
    project_id: str, rfq_id: str, quote_id: str, user: CurrentUser = Depends(_PE)
):
    """Pick this quote as the category's price, replacing any previous winner.

    This is the ONLY thing that prices a category, so it is deliberately open to
    every candidate: a vendor's quote, a hand-entered figure, or General
    Material's estimate row (General is picked from like any other category —
    its estimate figure is a candidate, not an automatic answer).

    The quote must be approved first: selection carries the number into markup,
    verify and the bid, and only approval says a human has checked it. Validates
    before mutating so a 404 or a 409 never alters pricing state.
    """
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
    target = (
        sb.table("quotes")
        .select("id, is_approved")
        .eq("id", quote_id)
        .eq("rfq_id", rfq_id)
        .execute()
    ).data
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quote not found")
    if not target[0].get("is_approved"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Approve this quote on Receive Quotes before making it the winner.",
        )
    # Clear first: quotes_one_selected_per_rfq is a partial unique index, so the
    # new winner would collide with the old one.
    sb.table("quotes").update({"is_selected": False}).eq("rfq_id", rfq_id).execute()
    updated = (
        sb.table("quotes").update({"is_selected": True}).eq("id", quote_id).execute()
    ).data
    audit(user.id, "quote.select", "quote", quote_id, {"rfq_id": rfq_id})
    dismiss_notifications(rfq_id=rfq_id, types=_QUOTE_NOTIF_TYPES)
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Quote selection changed")
    return updated[0]


@rfq_router.delete("/{rfq_id}/select", status_code=status.HTTP_204_NO_CONTENT)
def clear_selected_quote(
    project_id: str, rfq_id: str, user: CurrentUser = Depends(_PE)
):
    """Put a category back to undecided, removing its price entirely.

    The counterpart to select_quote. Selection is the only thing that prices a
    category, so clearing it is a pricing edit like any other: it un-prices the
    category, which re-opens the Select Vendors gate and bounces a past-Verify
    bid back for a re-commit. Idempotent, so clearing an already-undecided
    category is a no-op rather than a 404.
    """
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
    cleared = (
        sb.table("quotes")
        .update({"is_selected": False})
        .eq("rfq_id", rfq_id)
        .eq("is_selected", True)
        .execute()
    ).data
    if cleared:
        audit(user.id, "quote.deselect", "rfq", rfq_id, {"cleared": len(cleared)})
        workflow.maybe_reopen_verify_after_edit(
            project_id, user.id, "Winning quote cleared"
        )


@rfq_router.put("/{rfq_id}/quotes-confirmed")
def set_quotes_confirmed(
    project_id: str, rfq_id: str, body: RfqQuotesConfirmIn, user: CurrentUser = Depends(_PE)
):
    """Record the PE's "it's complete" check on the receive-quotes step, an
    attestation that the vendor quoted the entire RFQ and didn't miss a
    material. Accepted on every category, General Material included: General now
    holds candidates like any other category, so it can be attested like any
    other. The backend only stores who confirmed and when; which categories the
    step waits on is the frontend's gate."""
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
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


@rfq_router.put("/{rfq_id}/quotes/{quote_id}/tax")
def set_quote_tax(
    project_id: str,
    rfq_id: str,
    quote_id: str,
    body: TaxIn,
    user: CurrentUser = Depends(_PE),
):
    """Record whether THIS quote already included sales tax, and the rate to
    apply when it did not. Quotes are compared and carried by their tax-inclusive
    amount, so the materials figure reflects the true cost incurred, and a quote
    with the question unanswered cannot be approved (and so cannot win).

    Answerable on every category, General Material included: its candidates go
    through approval and selection like anybody else's, so their tax question has
    to be answerable here. The estimate figure's own attestation still lives on
    routers/general_material."""
    sb = get_supabase()
    _rfq_in_project(sb, project_id, rfq_id)
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
    # The tax-inclusive amount changes the materials price basis whenever this
    # quote is the winner, so re-verify if the project already passed Verify.
    workflow.maybe_reopen_verify_after_edit(project_id, user.id, "Quote tax setting changed")
    return updated[0]


# ── Select Vendors (the step that prices the project) ─────────────────────


def _candidate(quote: dict) -> dict:
    """One row on the Select Vendors table. `total` is the tax-inclusive figure
    computed by pricing.tax_info, so the number the estimator compares here is
    byte-for-byte the number pricing will carry into markup, verify and the bid.
    An unanswered tax question counts as "tax not included" (apply_tax), which
    is what stops an approved-looking figure from being understated."""
    info = tax_info(quote)
    return {
        "id": quote["id"],
        # Null for a hand-entered figure and for General Material's estimate row:
        # nobody quoted those, so the frontend labels them by origin instead.
        "vendor_name": (quote.get("vendors") or {}).get("name"),
        "contact_name": (quote.get("vendor_contacts") or {}).get("name"),
        "amount": str(info["pre_tax"]),
        "tax_included": quote.get("tax_included"),
        "tax_rate": str(info["rate"]),
        "total": str(info["total"]),
        "is_approved": bool(quote.get("is_approved")),
        "is_selected": bool(quote.get("is_selected")),
        "origin": quote.get("origin") or "vendor",
        # How the AMOUNT got onto the row, which is a different question from
        # where the number came from: a vendor's quote typed in by hand, or an
        # extracted one a person has since corrected, both read 'manual' here.
        # Provenance only, like origin: it confers no priority either.
        "source": quote.get("source") or "manual",
        "notes": quote.get("notes"),
        "quote_file_id": quote.get("quote_file_id"),
        # Resolved so the row can open a preview without a second lookup.
        "quote_file_name": (quote.get("project_files") or {}).get("filename"),
        "received_at": quote.get("received_at"),
    }


@router.get("/vendor-selection")
def get_vendor_selection(
    project_id: str, user: CurrentUser = Depends(get_current_user)
):
    """Everything the Select Vendors step shows, in one read: every category on
    the project, every candidate behind it, and which one currently wins.

    Two queries whatever the project's size (the RFQs, then all their quotes at
    once) — this page is opened on every bid and a per-category fetch would fan
    out into dozens of round trips.

    Categories come back in the material-category display order; candidates come
    back cheapest first on the tax-INCLUSIVE total, which is the only comparison
    that means anything (a quote without tax in it would otherwise look cheaper
    than it is). Cheapest is a suggestion, never a decision: nothing is priced
    until a human picks a winner.
    """
    _internal(user)
    sb = get_supabase()
    rfqs = (
        sb.table("rfqs")
        .select(
            "id, material_category_id, status,"
            " material_categories(name, is_general, pricing_section, sort_order)"
        )
        .eq("project_id", project_id)
        .execute()
    ).data or []
    rfq_ids = [r["id"] for r in rfqs]
    quotes: list[dict] = []
    if rfq_ids:
        quotes = (
            sb.table("quotes")
            .select(
                "id, rfq_id, amount, tax_included, tax_rate, is_approved, is_selected,"
                " origin, source, notes, quote_file_id, received_at,"
                " vendors(name), vendor_contacts(name), project_files(filename)"
            )
            .in_("rfq_id", rfq_ids)
            .execute()
        ).data or []

    by_rfq: dict[str, list[dict]] = {r["id"]: [] for r in rfqs}
    for q in quotes:
        by_rfq[q["rfq_id"]].append(_candidate(q))

    def _cat(r: dict) -> dict:
        return r.get("material_categories") or {}

    rfqs.sort(
        key=lambda r: (
            _cat(r).get("sort_order") is None,
            _cat(r).get("sort_order") or 0,
            _cat(r).get("name") or "",
        )
    )

    out: list[dict] = []
    for r in rfqs:
        cat = _cat(r)
        candidates = by_rfq[r["id"]]
        candidates.sort(key=lambda c: (Decimal(c["total"]), c["received_at"] or ""))
        out.append(
            {
                "rfq_id": r["id"],
                "material_category_id": r["material_category_id"],
                "category_name": cat.get("name"),
                "is_general": bool(cat.get("is_general")),
                "pricing_section": cat.get("pricing_section")
                or vendor_selection.DEFAULT_SECTION,
                # Whether the RFQ actually went out; a draft never did, so a
                # category with no candidates reads as "not sent yet", not
                # "nobody answered".
                "was_sent": r.get("status") in _SENT_RFQ_STATUSES,
                "selected_quote_id": next(
                    (c["id"] for c in candidates if c["is_selected"]), None
                ),
                "quotes": candidates,
            }
        )
    return out


# Mounted last: include_router copies the routes that exist at this moment, so
# every RFQ route above has to be registered before this line.
router.include_router(rfq_router)

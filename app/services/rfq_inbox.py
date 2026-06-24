"""Inbound RFQ reply polling.

A background loop watches the bids@ inbox via Graph delta queries while any RFQ
send is still "active" (sent, no quote yet, younger than the polling window).
Messages whose conversationId matches a send are stored, their file attachments
saved as quote files, and PDF attachments run through OpenAI to extract the
quoted price — which auto-creates a `quotes` row the PE can override.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import graph_inbox, office_preview, storage
from app.services.notifications import audit, notify_role
from app.services.openai_text import extract_quote_from_pdf

logger = logging.getLogger(__name__)

_LEASE_KEY = "inbox"  # row id prefix in graph_sync_state


async def polling_loop() -> None:
    interval = get_settings().rfq_poll_interval_seconds
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:  # noqa: BLE001 — the loop must survive any tick failure
            logger.exception("RFQ inbox poll failed")
        await asyncio.sleep(interval)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def poll_once() -> None:
    settings = get_settings()
    sb = get_supabase()

    # 0. Expire sends past the polling window (1 week per spec).
    cutoff = (_now() - timedelta(days=settings.rfq_poll_active_days)).isoformat()
    sb.table("rfq_sends").update({"polling_active": False}).eq(
        "polling_active", True
    ).lt("sent_at", cutoff).execute()

    # 1. Active sends — skip Graph entirely when there is nothing to watch.
    active = _active_sends(sb)
    if not active:
        return

    # 2. Single-runner lease (belt-and-braces for multi-worker deployments).
    key = f"{_LEASE_KEY}:{settings.ms_sender}"
    state = (
        sb.table("graph_sync_state").select("*").eq("id", key).execute()
    ).data
    state = state[0] if state else None
    now = _now()
    if state and state.get("lease_until"):
        lease = datetime.fromisoformat(state["lease_until"].replace("Z", "+00:00"))
        if lease > now:
            return
    lease_until = (now + timedelta(seconds=2 * settings.rfq_poll_interval_seconds)).isoformat()
    if state is None:
        sb.table("graph_sync_state").insert({"id": key, "lease_until": lease_until}).execute()
        delta_link = None
    else:
        sb.table("graph_sync_state").update(
            {"lease_until": lease_until, "updated_at": now.isoformat()}
        ).eq("id", key).execute()
        delta_link = state.get("delta_link")

    # 3. Delta sync (reset on expired tokens; re-ingest is idempotent).
    try:
        messages, new_delta = graph_inbox.delta_inbox(delta_link)
    except graph_inbox.DeltaExpired:
        messages, new_delta = graph_inbox.delta_inbox(None)

    # 4. Match + ingest, each message isolated so one failure can't stall the rest.
    by_conversation = {s["conversation_id"]: s for s in active if s.get("conversation_id")}
    for msg in messages:
        try:
            _ingest_message(sb, msg, by_conversation)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to ingest inbox message %s", msg.get("id"))

    # 5. Persist the delta token only after the batch is processed.
    sb.table("graph_sync_state").update(
        {"delta_link": new_delta, "updated_at": _now().isoformat()}
    ).eq("id", key).execute()


def _active_sends(sb) -> list[dict]:
    return (
        sb.table("rfq_sends")
        .select(
            "id, conversation_id, vendor_contact_id, rfq_id, "
            "vendor_contacts(id, name, email, vendor_id, vendors(name)), "
            "rfqs(id, project_id, material_category_id, material_categories(name), "
            "projects(id, name, number))"
        )
        .eq("polling_active", True)
        .eq("status", "sent")
        .is_("quote_received_at", "null")
        .execute()
    ).data or []


def _ingest_message(sb, msg: dict, by_conversation: dict[str, dict]) -> None:
    settings = get_settings()
    from_addr = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "")
    if not from_addr or from_addr.lower() == settings.ms_sender.lower():
        return
    send = by_conversation.get(msg.get("conversationId"))
    if not send:
        return

    contact = send["vendor_contacts"]
    if from_addr.lower() != (contact.get("email") or "").lower():
        # A reply in the right conversation from an unexpected address — don't
        # ingest, but leave a trace so the PE can spot forwarded replies.
        audit(
            None,
            "rfq.reply_sender_mismatch",
            "rfq_send",
            send["id"],
            {"from": from_addr, "expected": contact.get("email")},
        )
        return

    # Idempotency: the poller may see the same message again after a delta reset.
    existing = (
        sb.table("rfq_messages")
        .select("id")
        .eq("graph_message_id", msg["id"])
        .execute()
    ).data
    if existing:
        return

    full = graph_inbox.get_message(msg["id"])
    row = (
        sb.table("rfq_messages")
        .insert(
            {
                "rfq_send_id": send["id"],
                "graph_message_id": msg["id"],
                "from_addr": from_addr,
                "subject": msg.get("subject"),
                "body_preview": msg.get("bodyPreview"),
                "body": (full.get("body") or {}).get("content"),
                "received_at": msg.get("receivedDateTime"),
                "has_attachments": bool(msg.get("hasAttachments")),
            }
        )
        .execute()
    ).data[0]

    if msg.get("hasAttachments"):
        _ingest_attachments(sb, send, row)

    rfq = send["rfqs"]
    notify_role(
        Role.PE,
        rfq["project_id"],
        "rfq.reply_received",
        f"{contact['name']} replied on the {rfq['material_categories']['name']} RFQ "
        f"for {rfq['projects']['name']}",
        rfq_id=rfq["id"],
    )


def _ingest_attachments(sb, send: dict, message_row: dict) -> None:
    import base64

    rfq = send["rfqs"]
    project = rfq["projects"]
    contact = send["vendor_contacts"]

    attachments = graph_inbox.list_attachments(message_row["graph_message_id"])
    pdf_files: list[tuple[dict, bytes]] = []  # (project_files row, content)
    for att in attachments:
        content = base64.b64decode(att["contentBytes"])
        path = storage.build_object_path(project["id"], "quote", att["name"])
        storage.upload_file(path, content, att.get("contentType") or "application/octet-stream")
        convertible = office_preview.is_convertible(att["name"], "quote")
        file_row = (
            sb.table("project_files")
            .insert(
                {
                    "project_id": project["id"],
                    "category": "quote",
                    "storage_path": path,
                    "filename": att["name"],
                    "material_category_id": rfq["material_category_id"],
                    "mime_type": att.get("contentType"),
                    "size_bytes": len(content),
                    "preview_status": "pending" if convertible else "none",
                }
            )
            .execute()
        ).data[0]
        if convertible:
            # Inline is fine — we're already on the poller's worker thread, and
            # generate_preview records its own failures; never derail ingestion.
            try:
                office_preview.generate_preview(file_row["id"])
            except Exception:  # noqa: BLE001
                logger.exception("Preview conversion failed for %s", file_row["filename"])
        is_pdf = (att.get("contentType") or "").lower() == "application/pdf" or att[
            "name"
        ].lower().endswith(".pdf")
        if is_pdf:
            pdf_files.append((file_row, content))

    if not pdf_files:
        return

    sb.table("rfq_messages").update({"extraction_status": "pending"}).eq(
        "id", message_row["id"]
    ).execute()
    context = {
        "project_name": project["name"],
        "project_number": project.get("number"),
        "category_name": rfq["material_categories"]["name"],
        "vendor_name": (contact.get("vendors") or {}).get("name"),
    }
    extraction_status = "no_amount"
    for file_row, content in pdf_files:
        try:
            result = extract_quote_from_pdf(content, file_row["filename"], context)
        except Exception:  # noqa: BLE001
            logger.exception("Quote extraction crashed for %s", file_row["filename"])
            result = None
        if result is None:
            extraction_status = "failed"
            continue
        if result.get("total_amount") is None:
            continue
        sb.table("quotes").insert(
            {
                "rfq_id": rfq["id"],
                "vendor_id": contact["vendor_id"],
                "vendor_contact_id": contact["id"],
                "amount": str(result["total_amount"]),
                "quote_file_id": file_row["id"],
                "source": "ai_extracted",
                "rfq_send_id": send["id"],
                "rfq_message_id": message_row["id"],
                "ai_extraction": result,
            }
        ).execute()
        sb.table("rfqs").update({"status": "quotes_in"}).eq("id", rfq["id"]).execute()
        sb.table("rfq_sends").update(
            {"quote_received_at": _now().isoformat(), "polling_active": False}
        ).eq("id", send["id"]).execute()
        extraction_status = "done"
        notify_role(
            Role.PE,
            rfq["project_id"],
            "quote.received",
            f"Quote received from {contact['name']} for "
            f"{rfq['material_categories']['name']} on {project['name']}: "
            f"${result['total_amount']}",
            rfq_id=rfq["id"],
        )
        break  # one quote per reply; remaining PDFs are still saved as files

    sb.table("rfq_messages").update(
        {
            "extraction_status": extraction_status,
            "extraction_error": "Price extraction failed" if extraction_status == "failed" else None,
        }
    ).eq("id", message_row["id"]).execute()

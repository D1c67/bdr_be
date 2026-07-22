"""Match inbound vendor emails to project submittal requests.

A submittal request is sent FROM the ingestion mailbox, so a vendor's reply
threads back with the same Graph conversationId (and the outbound Sent-Items copy
carries it too). The email-ingestion pipeline calls match_send() at the top of R1
(before the generic conversation-map lookup): a hit both records the response
(marks the send received and links the inbound email — whose attachments the
pipeline already stored) and lets the pipeline assign the email to the request's
project via its normal _assign path, teaching the conversation map for free.

Guards (applied by the caller): only an INBOUND email whose from-address matches
the send's contact flips the response — the outbound Sent-Items copy shares the
conversationId and must assign the project without being counted as a reply.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text


def match_send(sb, conversation_id: str | None) -> dict | None:
    """The submittal send this conversation belongs to (each send is its own
    Graph draft → its own conversationId), or None. Carries the request's
    project_id and the contact email for the sender guard."""
    if not conversation_id:
        return None
    rows = (
        sb.table("submittal_request_sends")
        .select(
            "id, request_id, response_received_at, sent_by, "
            "submittal_requests(project_id), vendor_contacts(email)"
        )
        .eq("conversation_id", conversation_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return None
    r = rows[0]
    return {
        "id": r["id"],
        "request_id": r["request_id"],
        "project_id": (r.get("submittal_requests") or {}).get("project_id"),
        "contact_email": (r.get("vendor_contacts") or {}).get("email"),
        "response_received_at": r.get("response_received_at"),
        "sent_by": r.get("sent_by"),
    }


def is_from_contact(email: dict, send: dict) -> bool:
    """True when the inbound email's sender is the contact we emailed — a reply
    in the right thread from an unexpected address still assigns the project but
    is not counted as a submittal response."""
    contact = (send.get("contact_email") or "").strip().lower()
    return bool(contact and (email.get("from_address") or "").strip().lower() == contact)


def record_response(sb, send: dict, email: dict) -> None:
    """Mark the send as having a vendor response: link the inbound email (whose
    attachments are already stored by the pipeline under emails/{email_id}/…),
    set the first-response timestamp, and refresh the response count. Idempotent
    — a re-run of the same email is a no-op (unique(email_id))."""
    existing = (
        sb.table("submittal_response_emails")
        .select("id")
        .eq("email_id", email["id"])
        .limit(1)
        .execute()
    ).data
    if existing:
        return
    try:
        sb.table("submittal_response_emails").insert(
            {"send_id": send["id"], "email_id": email["id"]}
        ).execute()
    except Exception as exc:  # noqa: BLE001 — unique(email_id) race with another runner
        if _is_unique_violation(exc):
            return
        raise

    count = (
        sb.table("submittal_response_emails")
        .select("id", count="exact")
        .eq("send_id", send["id"])
        .execute()
    ).count or 0
    update: dict = {"response_count": count}
    if not send.get("response_received_at"):
        update["response_received_at"] = _now_iso()
    sb.table("submittal_request_sends").update(update).eq("id", send["id"]).execute()

    _notify(sb, send, email)


def _notify(sb, send: dict, email: dict) -> None:
    """Best-effort: tell the person who sent the request that a submittal came
    back. A notification failure must never disturb ingestion."""
    try:
        from app.services.notifications import notify_user

        recipient = send.get("sent_by")
        if not recipient:
            return
        vendor = email.get("from_address") or "a vendor"
        notify_user(
            recipient,
            send.get("project_id"),
            "submittal.response_received",
            f"Submittal response received from {vendor}.",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to notify submittal response")

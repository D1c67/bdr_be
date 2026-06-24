"""Microsoft Graph inbox reading for the bids@ mailbox.

Used by the RFQ reply poller: delta queries surface new inbox messages cheaply,
and matched messages get their full body and file attachments fetched. Requires
the application permission Mail.ReadWrite (admin-consented; the Exchange
ApplicationAccessPolicy scoping the app to bids@ covers it).
"""

from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import get_settings
from app.services.graph_email import graph_request

_DELTA_SELECT = "id,conversationId,from,subject,bodyPreview,receivedDateTime,hasAttachments"


class DeltaExpired(Exception):
    """The stored deltaLink was rejected (HTTP 410); a fresh initial sync is needed."""


def initial_delta_url() -> str:
    s = get_settings()
    since = (
        datetime.now(timezone.utc) - timedelta(days=s.rfq_poll_active_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"https://graph.microsoft.com/v1.0/users/{s.ms_sender}"
        f"/mailFolders/inbox/messages/delta"
        f"?$select={_DELTA_SELECT}&$filter=receivedDateTime ge {since}"
    )


def delta_inbox(delta_link: str | None) -> tuple[list[dict], str]:
    """Fetch all new inbox messages since `delta_link` (or do an initial sync
    bounded to the polling window). Returns (messages, new_delta_link).

    Raises DeltaExpired when the stored token is no longer valid (410 Gone).
    """
    url = delta_link or initial_delta_url()
    messages: list[dict] = []
    while True:
        # The delta/next links are absolute and already carry the query string.
        path = url.removeprefix("https://graph.microsoft.com/v1.0")
        try:
            page = graph_request("GET", path).json()
        except httpx.HTTPStatusError as exc:
            if delta_link and exc.response.status_code == 410:
                raise DeltaExpired from exc
            raise
        messages.extend(page.get("value", []))
        if "@odata.nextLink" in page:
            url = page["@odata.nextLink"]
            continue
        return messages, page["@odata.deltaLink"]


def get_message(message_id: str) -> dict:
    sender = get_settings().ms_sender
    return graph_request(
        "GET",
        f"/users/{sender}/messages/{message_id}",
        params={"$select": "id,conversationId,from,subject,body,bodyPreview,receivedDateTime,hasAttachments"},
    ).json()


def list_attachments(message_id: str) -> list[dict]:
    """Return file attachments with their content bytes (base64 in `contentBytes`).
    Item/reference attachments (attached emails, links) are skipped."""
    sender = get_settings().ms_sender
    listing = graph_request(
        "GET",
        f"/users/{sender}/messages/{message_id}/attachments",
        params={"$select": "id,name,contentType,size"},
    ).json()
    out = []
    for att in listing.get("value", []):
        full = graph_request(
            "GET",
            f"/users/{sender}/messages/{message_id}/attachments/{att['id']}",
        ).json()
        if full.get("@odata.type") == "#microsoft.graph.fileAttachment":
            out.append(full)
    return out

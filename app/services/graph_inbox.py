"""Microsoft Graph mailbox reading (delta queries + message/attachment fetch).

Used by the RFQ reply poller (bids@ inbox) and the email-ingestion poller (the
PM mailbox's Inbox + Sent Items): delta queries surface new messages cheaply,
and selected messages get their full body and file attachments fetched.
Requires the application permission Mail.ReadWrite (admin-consented;
tenant-wide — no ApplicationAccessPolicy restricts this app, so any mailbox
is reachable without Exchange-side changes).

All helpers default to the legacy behavior (ms_sender's inbox, the RFQ polling
window) so existing callers are unchanged; the email-ingestion poller passes
mailbox/folder/select explicitly.
"""

from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import get_settings
from app.services.graph_email import graph_request

_DELTA_SELECT = "id,conversationId,from,subject,bodyPreview,receivedDateTime,hasAttachments"
_MESSAGE_SELECT = (
    "id,conversationId,from,subject,body,bodyPreview,receivedDateTime,hasAttachments"
)


class DeltaExpired(Exception):
    """The stored deltaLink was rejected (HTTP 410); a fresh initial sync is needed."""


def initial_delta_url(
    *,
    mailbox: str | None = None,
    folder: str = "inbox",
    since_days: int | None = None,
    select: str | None = None,
) -> str:
    s = get_settings()
    since = (
        datetime.now(timezone.utc) - timedelta(days=since_days or s.rfq_poll_active_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"https://graph.microsoft.com/v1.0/users/{mailbox or s.ms_sender}"
        f"/mailFolders/{folder}/messages/delta"
        f"?$select={select or _DELTA_SELECT}&$filter=receivedDateTime ge {since}"
    )


def delta_inbox(
    delta_link: str | None,
    *,
    mailbox: str | None = None,
    folder: str = "inbox",
    since_days: int | None = None,
    select: str | None = None,
) -> tuple[list[dict], str]:
    """Fetch all new messages in `folder` since `delta_link` (or do an initial
    sync bounded to the polling window). Returns (messages, new_delta_link).

    Raises DeltaExpired when the stored token is no longer valid (410 Gone).
    """
    url = delta_link or initial_delta_url(
        mailbox=mailbox, folder=folder, since_days=since_days, select=select
    )
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


def get_message(
    message_id: str,
    *,
    mailbox: str | None = None,
    select: str | None = None,
    body_type: str | None = None,
) -> dict:
    """Fetch one full message. `body_type="text"` asks Graph to render the body
    as plain text (no HTML stripping needed on our side)."""
    user = mailbox or get_settings().ms_sender
    prefer = f'outlook.body-content-type="{body_type}"' if body_type else None
    return graph_request(
        "GET",
        f"/users/{user}/messages/{message_id}",
        params={"$select": select or _MESSAGE_SELECT},
        prefer=prefer,
    ).json()


def list_reference_links(message_id: str, *, mailbox: str | None = None) -> list[dict]:
    """`{name, sourceUrl}` for each reference attachment (a OneDrive/cloud file
    "attached" via Outlook is not a fileAttachment — it is a link). The cheap
    listing's $select can't include sourceUrl, so each one costs a full GET."""
    user = mailbox or get_settings().ms_sender
    listing = graph_request(
        "GET",
        f"/users/{user}/messages/{message_id}/attachments",
        params={"$select": "id,name"},
    ).json()
    links: list[dict] = []
    for att in listing.get("value", []):
        if att.get("@odata.type") != "#microsoft.graph.referenceAttachment":
            continue
        full = graph_request(
            "GET",
            f"/users/{user}/messages/{message_id}/attachments/{att['id']}",
        ).json()
        if full.get("sourceUrl"):
            links.append({"name": full.get("name"), "sourceUrl": full["sourceUrl"]})
    return links


def list_attachments(
    message_id: str,
    *,
    mailbox: str | None = None,
    max_count: int | None = None,
    max_bytes: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return (fetched, skipped) attachments for a message.

    `fetched` are file attachments with their content bytes (base64 in
    `contentBytes`). `skipped` carries cheap-listing metadata dicts
    `{name, contentType, size, reason}` for anything not fetched, so callers
    can record that an attachment existed without storing it.

    `max_bytes` skips oversized attachments using the `size` field from the
    cheap listing BEFORE the per-attachment content GET, and `max_count` caps
    how many file attachments are pulled — so a hostile inbound message can't
    force an unbounded number of large downloads. Item/reference attachments
    (attached emails, links) are never fetched.
    """
    user = mailbox or get_settings().ms_sender
    listing = graph_request(
        "GET",
        f"/users/{user}/messages/{message_id}/attachments",
        params={"$select": "id,name,contentType,size"},
    ).json()
    fetched: list[dict] = []
    skipped: list[dict] = []

    def _skip(att: dict, reason: str) -> None:
        skipped.append(
            {
                "id": att.get("id"),
                "name": att.get("name"),
                "contentType": att.get("contentType"),
                "size": att.get("size"),
                "reason": reason,
            }
        )

    for att in listing.get("value", []):
        listed_type = att.get("@odata.type")
        if listed_type and listed_type != "#microsoft.graph.fileAttachment":
            _skip(att, "item_attachment")
            continue
        if max_count is not None and len(fetched) >= max_count:
            _skip(att, "too_many")
            continue
        if max_bytes is not None and (att.get("size") or 0) > max_bytes:
            _skip(att, "too_large")  # skip oversized before fetching its content
            continue
        full = graph_request(
            "GET",
            f"/users/{user}/messages/{message_id}/attachments/{att['id']}",
        ).json()
        if full.get("@odata.type") == "#microsoft.graph.fileAttachment":
            fetched.append(full)
        else:
            _skip(att, "item_attachment")
    return fetched, skipped

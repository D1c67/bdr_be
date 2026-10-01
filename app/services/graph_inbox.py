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

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.services.graph_email import graph_request, graph_stream

# Streamed attachment downloads are written in 1 MB chunks.
_DOWNLOAD_CHUNK = 1024 * 1024

_DELTA_SELECT = (
    "id,conversationId,internetMessageId,from,subject,bodyPreview,"
    "receivedDateTime,hasAttachments"
)
_MESSAGE_SELECT = (
    "id,conversationId,from,subject,body,bodyPreview,receivedDateTime,hasAttachments"
)


class DeltaExpired(Exception):
    """The stored deltaLink was rejected (HTTP 410); a fresh initial sync is needed."""


class AttachmentTooLarge(RuntimeError):
    """A streamed attachment exceeded the caller's byte cap mid-download; the
    response was closed and the partial file removed."""


class AttachmentNotStored(RuntimeError):
    """Graph no longer serves the attachment content (HTTP 404 or 410): the
    message or attachment was deleted, or the id belongs to a different
    mailbox."""


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


_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "emz", "wmz"}


def is_inline_image(att: dict) -> bool:
    """An image embedded in the message body (signature logo, social icon,
    pasted picture) rather than a file the sender attached. Outlook carries
    every earlier message's inline images forward on each reply, so a long
    thread piles up dozens of them. Needs `isInline` in the listing $select."""
    if not att.get("isInline"):
        return False
    content_type = (att.get("contentType") or "").lower()
    name = (att.get("name") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return content_type.startswith("image/") or ext in _IMAGE_EXTS


# Body images at or above this size are kept as files. Signature art on real
# RFQ threads topped out near 300 KB (animated banner GIFs); a screenshot or
# photo of a quote pasted into the body is usually larger, and is the one
# inline image worth keeping.
INLINE_IMAGE_KEEP_BYTES = 512 * 1024


def is_signature_art(att: dict) -> bool:
    """A body image small enough to be signature art (see is_inline_image)."""
    return is_inline_image(att) and (att.get("size") or 0) < INLINE_IMAGE_KEEP_BYTES


def list_attachments(
    message_id: str,
    *,
    mailbox: str | None = None,
    max_count: int | None = None,
    max_bytes: int | None = None,
    skip_inline_images: bool = False,
    rank: Callable[[dict], int] | None = None,
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

    `skip_inline_images` drops small body-embedded images (signature art, see
    is_signature_art) before they count toward `max_count`: Graph lists them
    ahead of the real attachments, so on a deep reply thread they used to fill
    the cap and push the actual file off the end. `rank` orders the listing before the cap is
    applied (lowest first, stable), so the files the caller cares about most
    are the ones fetched when a message carries more than the cap.
    """
    user = mailbox or get_settings().ms_sender
    listing = graph_request(
        "GET",
        f"/users/{user}/messages/{message_id}/attachments",
        params={"$select": "id,name,contentType,size,isInline"},
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
                # Tells a reference attachment (a cloud link, which callers
                # may resolve another way) from an attached email or item.
                "odata_type": att.get("@odata.type"),
            }
        )

    entries = listing.get("value", [])
    if rank is not None:
        entries = sorted(entries, key=rank)
    for att in entries:
        listed_type = att.get("@odata.type")
        if listed_type and listed_type != "#microsoft.graph.fileAttachment":
            _skip(att, "item_attachment")
            continue
        if skip_inline_images and is_signature_art(att):
            _skip(att, "inline_image")
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


def _open_excl(dest: Path):
    """Create `dest` for writing, refusing an existing path (FileExistsError)
    and never following a symlink at it. Returns a buffered writer."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dest, flags, 0o644)
    try:
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def download_attachment_to_file(
    message_id: str,
    attachment_id: str,
    *,
    mailbox: str,
    max_bytes: int,
    dest: Path,
) -> int:
    """Stream one file attachment's raw content (`.../attachments/{id}/$value`)
    into `dest`, which is created O_EXCL, in 1 MB chunks under a running byte
    cap. Returns the bytes written.

    Unlike `list_attachments` this never decodes base64 in memory and never
    trusts Graph's `size` (the MIME-encoded size): the cap is enforced on the
    bytes actually received. Over the cap the response is closed at once and
    AttachmentTooLarge is raised; HTTP 404/410 raise AttachmentNotStored; any
    other non-2xx propagates as httpx.HTTPStatusError (a 3xx included, since
    `graph_stream` refuses redirects). On every failure the partial `dest`
    this call created is removed, so a retry's O_EXCL open succeeds. An
    existing `dest` raises FileExistsError before any request is made and is
    left untouched.

    `mailbox` is required and must be the `ingested_emails.mailbox` of the
    stored row: the default ms_sender mailbox is a different mailbox and the
    stored (immutable) ids do not resolve there.
    """
    if not mailbox or not isinstance(mailbox, str):
        raise ValueError("mailbox is required")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")
    dest = Path(dest)
    path = f"/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}/$value"
    out = _open_excl(dest)
    written = 0
    try:
        with out:
            try:
                with graph_stream("GET", path) as resp:
                    for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            # Stop reading before the next chunk arrives.
                            resp.close()
                            raise AttachmentTooLarge(
                                "The attachment is larger than the per-file limit."
                            )
                        out.write(chunk)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (404, 410):
                    raise AttachmentNotStored(
                        "The attachment content is no longer available."
                    ) from exc
                raise
    except BaseException:
        _unlink_quietly(dest)
        raise
    return written

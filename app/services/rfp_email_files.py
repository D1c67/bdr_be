"""RFP Ingestion: the email harvester's pure parts (docs/RFP_HARVEST.md 2.5).

A GC that invites directly sends the bid documents as attachments, as
cloud-share links, or both, so the "platform" is the email itself. This
module holds what can be decided without Graph or the network: the
attachment policy (what is downloaded, what is an image, what looks like a
signature, what is an attached email), the trigger (`email_reference`: does
this row carry anything worth a harvest) and the `files[]` entry builders
for the three origins (attachment, zip member, link file). The job body,
the session adapter and the reuse query live in `rfp_email_harvest.py`;
`rfp_harvest.py` imports this module for the registry and the trigger, so
nothing here may import either of them.

Policy, from the shapes seen on the dev database (2026-09-16): PDFs arrive
declared `application/octet-stream`, so the declared type can never decide
to KEEP a file (the sandbox sniff does); it can only say "image". Every
image is skipped from download, inline or not, and recorded under
`data.attachments.images` with `signature_like` so the card can collapse
the Outlook signature clutter and still list a real site photo by name.
Entries never carry a locator (Graph attachment id, scratch path, URL): the
harvester keeps those in a parallel list exactly like `pipelinesuite_files`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.services import cloud_folders
from app.services.rfp_email_auth import METHOD_GENERAL, METHOD_NONORGANIC, METHOD_ORGANIC

# The registry key and the methods it serves (rfp_harvest.harvester_for).
HARVESTER_EMAIL = "email"
EMAIL_METHODS = (METHOD_ORGANIC, METHOD_GENERAL, METHOD_NONORGANIC)
EXTERNAL_KEY_PREFIX = "email:"

IMAGE_EXTENSIONS = frozenset(
    {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "heic", "svg", "ico"}
)
_EMAIL_EXTENSIONS = frozenset({"msg", "eml"})
_EMAIL_CONTENT_TYPES = frozenset({"message/rfc822", "application/vnd.ms-outlook"})
# Outlook names pasted signature images image001.jpg, image002.png, ...;
# the new Outlook names them Outlook-<random>.png.
_SIGNATURE_NAME_RE = re.compile(r"^(?:image\d{3}\.|outlook-)", re.IGNORECASE)

# Graph attachment kinds as `rfp_email_ingest._attachment_kind` stores them.
KIND_FILE = "file"
KIND_REFERENCE = "reference"
KIND_ITEM = "item"
KIND_UNKNOWN = "unknown"

# files[] entry vocabulary added by this harvester (doc section 4).
ORIGIN_ATTACHMENT = "attachment"
ORIGIN_ZIP = "zip"
ORIGIN_LINK = "link"
PROVIDER_GRAPH = "graph"
FILE_REUSED = "reused"
FILE_EXPANDED = "expanded"

# data.attachments.skipped[].reason for attachments; zip members carry
# rfp_zip's own words (nested_zip, encrypted, too_large, empty, unsafe_name)
# and its `image` is the one reason routed to images instead.
SKIP_ATTACHED_EMAIL = "attached_email"
SKIP_UNKNOWN = "unknown"
SKIP_IMAGE = "image"

_NAME_MAX_CHARS = 200
_PATH_MAX_CHARS = 400


# ── Names and types ──────────────────────────────────────────────────────────


def _extension(name: Any) -> str:
    text = str(name or "").strip().lower()
    if "." not in text:
        return ""
    return text.rsplit(".", 1)[-1]


def _name(value: Any, fallback: str = "attachment") -> str:
    text = " ".join(str(value or "").split())
    return (text or fallback)[:_NAME_MAX_CHARS]


def _size(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def is_image_name(name: Any) -> bool:
    return _extension(name) in IMAGE_EXTENSIONS


def is_image(name: Any, content_type: Any) -> bool:
    """An image by extension OR by declared `image/*` type. The type alone
    never keeps a file (octet-stream PDFs), it only adds images."""
    if is_image_name(name):
        return True
    return str(content_type or "").strip().lower().startswith("image/")


def is_attached_email(name: Any, content_type: Any, odata_kind: Any) -> bool:
    """An email attached to the email (`itemAttachment`, `.msg`, `.eml`,
    or the two message content types): never downloaded."""
    if odata_kind == KIND_ITEM:
        return True
    if _extension(name) in _EMAIL_EXTENSIONS:
        return True
    return str(content_type or "").strip().lower() in _EMAIL_CONTENT_TYPES


def is_zip_name(name: Any) -> bool:
    return _extension(name) == "zip"


def signature_like(name: Any, size: Any, inline: bool, settings: Settings) -> bool:
    """Inline (cid) images, Outlook's signature names, or anything at or
    under `rfp_harvest_image_signature_max_bytes`: the card collapses these
    to one count and lists every other image by name and size."""
    if inline:
        return True
    if _SIGNATURE_NAME_RE.match(str(name or "").strip()):
        return True
    return _size(size) <= settings.rfp_harvest_image_signature_max_bytes


def image_record(name: Any, size: Any, inline: bool, settings: Settings) -> dict:
    """One `data.attachments.images[]` row."""
    return {
        "name": _name(name, "image"),
        "size": _size(size),
        "inline": bool(inline),
        "signature_like": signature_like(name, size, inline, settings),
    }


# ── Attachment policy ────────────────────────────────────────────────────────


@dataclass
class AttachmentPlan:
    """What the job does with one message's attachment listing: `files`
    are downloaded (`{name, size, content_type, id, inline}`; `id` is None
    on the stored meta, present on the live listing), `images` and
    `skipped` are recorded on `data.attachments`, `references` counts the
    cloud "attachments" the job turns into links."""

    files: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    references: int = 0


def downloadable(meta: dict) -> bool:
    """A file attachment that is neither an image nor an attached email:
    the trigger's half about attachments, and what `classify_attachments`
    puts under `files`. Inline only matters for images (an inline PDF is a
    document a GC dragged into the body)."""
    if not isinstance(meta, dict):
        return False
    kind = meta.get("kind") or KIND_FILE
    if kind != KIND_FILE:
        return False
    name, content_type = meta.get("name"), meta.get("contentType")
    if is_attached_email(name, content_type, kind) or is_image(name, content_type):
        return False
    return True


def classify_attachments(meta: list[dict] | None, settings: Settings) -> AttachmentPlan:
    """The attachment policy over a listing in the stored `attachments_meta`
    shape (`name, contentType, size, kind`, plus `inline` and `id` when the
    listing is live). Reference attachments are counted, not listed: the
    job resolves them as links."""
    plan = AttachmentPlan()
    for att in meta or []:
        if not isinstance(att, dict):
            continue
        kind = att.get("kind") or KIND_FILE
        name, content_type, size = att.get("name"), att.get("contentType"), att.get("size")
        inline = bool(att.get("inline"))
        if kind == KIND_REFERENCE:
            plan.references += 1
            continue
        if is_attached_email(name, content_type, kind):
            plan.skipped.append({"name": _name(name), "size": _size(size), "reason": SKIP_ATTACHED_EMAIL})
            continue
        if kind != KIND_FILE:
            plan.skipped.append({"name": _name(name), "size": _size(size), "reason": SKIP_UNKNOWN})
            continue
        if is_image(name, content_type):
            plan.images.append(image_record(name, size, inline, settings))
            continue
        plan.files.append(
            {
                "name": _name(name),
                "size": _size(size),
                "content_type": str(content_type) if content_type else None,
                "id": att.get("id"),
                "inline": inline,
            }
        )
    return plan


# ── The trigger ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmailRef:
    """What the registry's row helpers read off a reference: the harvest
    row's key (the email itself) and no URL, no session."""

    external_key: str
    external_url: None = None
    session_provider: None = None


def external_key_for(email_id: Any) -> str:
    return f"{EXTERNAL_KEY_PREFIX}{email_id}"


def email_reference(row: dict) -> EmailRef | None:
    """The doc 2.5 trigger: at least one downloadable attachment in the
    stored `attachments_meta`, OR at least one recognised share link in
    `body_text` (supported or not: an unsupported link still earns the
    row a harvest so the card can show it for download by hand). None means
    the row drains to done exactly as before this slice."""
    meta = row.get("attachments_meta")
    if any(downloadable(att) for att in (meta if isinstance(meta, list) else [])):
        return EmailRef(external_key_for(row.get("id")))
    if cloud_folders.find_share_links(row.get("body_text"), None):
        return EmailRef(external_key_for(row.get("id")))
    return None


# ── Entry builders ───────────────────────────────────────────────────────────


def _entry(
    file_path: str, size: Any, *, origin: str, provider: str | None, link_key: str | None,
    zip_of: str | None,
) -> dict:
    return {
        "file_path": file_path[:_PATH_MAX_CHARS],
        "size": _size(size),
        "kind": None,
        "discipline": None,
        "drawing_title": None,
        "revision": None,
        "sandbox_file_id": None,
        "status": None,
        "error": None,
        "origin": origin,
        "provider": provider,
        "link_key": link_key,
        "zip_of": zip_of,
        "reused_harvest_id": None,
    }


def attachment_entry(name: Any, size: Any) -> dict:
    """A file attachment: `origin = attachment`, provider `graph`."""
    return _entry(_name(name), size, origin=ORIGIN_ATTACHMENT, provider=PROVIDER_GRAPH,
                  link_key=None, zip_of=None)


def zip_member_entry(
    zip_path: str, member_path: Any, size: Any, *, provider: str | None, link_key: str | None
) -> dict:
    """A member of an opened zip: `file_path` is `<zip path>/<member path>`
    and `zip_of` names the zip entry it followed."""
    member = "/".join(p for p in str(member_path or "").split("/") if p) or "member"
    return _entry(f"{zip_path}/{member}", size, origin=ORIGIN_ZIP, provider=provider,
                  link_key=link_key, zip_of=zip_path)


def link_file_entry(
    remote_path: Any, size: Any, *, provider: str, link_key: str, origin: str = ORIGIN_LINK
) -> dict:
    """A file a share link listed: `file_path` is the path the provider
    reported (relative to the shared folder, or the file's own name).
    `origin = zip` for the members of the zip Dropbox serves for a folder."""
    path = "/".join(p for p in str(remote_path or "").split("/") if p) or "document"
    return _entry(path, size, origin=origin, provider=provider, link_key=link_key, zip_of=None)

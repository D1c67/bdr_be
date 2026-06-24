"""RFQ bulk sending — one individual email per vendor contact.

Each contact gets their own Graph draft (so we capture its conversationId for
reply matching), the category's BOM split Excel, the project drawings (or a
OneDrive link when they exceed the configured size), and — for the Trenching
category — the estimator's markup files. From the confirm modal the PE can
override the attachment list per category and replace the generated body. The
generated body is lightly varied per email by OpenAI, falling back to the base
template on any failure; an edited body is sent exactly as written. Every body
goes out wrapped in the branded HTML shell (see email_branding) with the G3
logo + office-phone signature; the plain text remains what is stored and edited.
"""

import hashlib
import logging
import mimetypes
import re
import time

from app.core.config import get_settings
from app.services import email_branding, graph_email, office_preview, storage
from app.services.datetime_format import _parse_ts, format_bid_datetime  # noqa: F401 — re-exported; formatter lives in datetime_format
from app.services.email_branding import SIGNOFF
from app.services.notifications import audit
from app.services.openai_text import vary_email_body

logger = logging.getLogger(__name__)

# Token in the editable body template, replaced per recipient. Must match what
# the email-preview endpoint emits (build_base_body("<Contact Name>", ...)).
CONTACT_NAME_PLACEHOLDER = "<Contact Name>"


def build_subject(project: dict) -> str:
    return (
        f"{project.get('number') or 'TBD'} - {project['name']} - BOM - "
        f"{format_bid_datetime(project.get('actual_bid_at'))}"
    )


def build_base_body(contact_name: str, due_str: str, drawings_link: str | None) -> str:
    drawings_line = (
        f"The drawings are available here: {drawings_link}\n\n" if drawings_link else ""
    )
    return (
        f"Hello {contact_name},\n\n"
        f"Can you please get me quotes for the attached BOM, we need them by {due_str}?\n\n"
        f"{drawings_line}"
        "If there are any other attachments/drawings, please review them as well.\n\n"
        "Please also let me know what you are not able to quote.\n\n"
        "Thank you,\n"
        f"{SIGNOFF}"
    )


def build_custom_body(template: str, contact_name: str, drawings_link: str | None) -> str:
    """Personalize a PE-edited body: substitute the contact-name placeholder and
    make sure an over-size drawings link is never silently dropped."""
    body = template.replace(CONTACT_NAME_PLACEHOLDER, contact_name)
    if drawings_link and drawings_link not in body:
        body += f"\n\nThe drawings are available here: {drawings_link}"
    return body


def _is_trenching(category_name: str) -> bool:
    return "trench" in (category_name or "").lower()


def _content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _as_immutable_pdf(f: dict) -> dict:
    """Convert an editable Office attachment (the BOM split .xlsx) to an
    immutable PDF so a vendor cannot alter our quantities. Non-Office files
    (drawings are already PDFs) pass through unchanged. Raises
    office_preview.ConversionError on failure — the caller fails that group's
    sends rather than emailing a malleable file."""
    if not office_preview.is_office_file(f.get("filename")):
        return f
    pdf = office_preview.convert_for_send(f["content"], f["filename"])
    return {**f, "filename": office_preview.pdf_filename(f["filename"]), "content": pdf}


def _record_failed_send(sb, rfq: dict, contact: dict, subject: str, error: str, user_id: str) -> dict:
    """Record a failed rfq_send for a contact we never emailed (e.g. the BOM
    could not be converted to PDF) and return the batch result entry."""
    try:
        sb.table("rfq_sends").insert(
            {
                "rfq_id": rfq["id"],
                "vendor_contact_id": contact["id"],
                "subject": subject,
                "status": "failed",
                "error": error,
                "polling_active": False,
                "sent_by": user_id,
            }
        ).execute()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record failed rfq_send")
    return {
        "rfq_id": rfq["id"],
        "vendor_contact_id": contact["id"],
        "status": "failed",
        "error": error,
    }


def _safe_component(name: str) -> str:
    """Make a value safe to use as a single OneDrive path component."""
    return re.sub(r'[\\/:*?"<>|#%]+', "_", name).strip() or "file"


def _load_files(sb, project_id: str, category: str) -> list[dict]:
    """[{filename, content}] for all project files in a storage category."""
    rows = (
        sb.table("project_files")
        .select("filename, storage_path")
        .eq("project_id", project_id)
        .eq("category", category)
        .execute()
    ).data or []
    return [
        {"filename": r["filename"], "content": storage.download_file(r["storage_path"])}
        for r in rows
    ]


def _prepare_drawings(sb, project: dict) -> tuple[list[dict], str | None]:
    """Download drawings; if they exceed the inline limit, push them to OneDrive
    and return a single anonymous folder link instead (shared by every email)."""
    settings = get_settings()
    drawings = _load_files(sb, project["id"], "drawing")
    total = sum(len(d["content"]) for d in drawings)
    if total <= settings.rfq_drawings_inline_limit_mb * 1024 * 1024:
        return drawings, None
    folder = f"BDR/{_safe_component(str(project.get('number') or project['id']))}/drawings"
    for d in drawings:
        graph_email.drive_upload(f"{folder}/{_safe_component(d['filename'])}", d["content"])
    link = graph_email.drive_create_link(graph_email.drive_get_item_id(folder))
    return [], link


class _ExplicitAttachments:
    """Pre-downloaded files for groups that customized their attachment list.

    The oversize-drawings decision is per group — the inline limit is a
    per-email constraint. When one group's chosen drawings exceed it, only that
    group's drawings are uploaded, into a OneDrive folder unique to that exact
    selection, so a vendor's link never exposes files the PE removed from their
    category. Identical selections share one upload/link.
    """

    def __init__(self, files: dict[str, dict], project: dict):
        self._files = files  # file_id -> {filename, content, category}
        self._project = project
        self._links: dict[frozenset[str], str] = {}  # drawing selection -> folder link
        self.used_link = False

    def for_group(self, file_ids: list[str]) -> tuple[list[dict], str | None]:
        """(attachments, drawings_link) for one group's chosen file ids."""
        ids = list(dict.fromkeys(file_ids))  # dedupe, keep the PE's order
        drawing_ids = [i for i in ids if self._files[i]["category"] == "drawing"]
        total = sum(len(self._files[i]["content"]) for i in drawing_ids)
        limit = get_settings().rfq_drawings_inline_limit_mb * 1024 * 1024
        if not drawing_ids or total <= limit:
            return [self._files[i] for i in ids], None
        link = self._link_for(drawing_ids)
        self.used_link = True
        inline = [i for i in ids if i not in set(drawing_ids)]
        return [self._files[i] for i in inline], link

    def _link_for(self, drawing_ids: list[str]) -> str:
        key = frozenset(drawing_ids)
        if key not in self._links:
            digest = hashlib.sha1("|".join(sorted(key)).encode()).hexdigest()[:12]
            number = _safe_component(str(self._project.get("number") or self._project["id"]))
            folder = f"BDR/{number}/rfq-drawings/{digest}"
            for i in drawing_ids:
                graph_email.drive_upload(
                    f"{folder}/{_safe_component(self._files[i]['filename'])}",
                    self._files[i]["content"],
                )
            self._links[key] = graph_email.drive_create_link(
                graph_email.drive_get_item_id(folder)
            )
        return self._links[key]


def _prepare_explicit_attachments(sb, project: dict, groups: list[dict]) -> _ExplicitAttachments:
    """Validate and download every file referenced by an explicit attachment
    list, once each. Raises ValueError (→ 400) before anything is sent if a
    file id doesn't belong to this project."""
    ids = list(
        dict.fromkeys(
            fid
            for g in groups
            if g.get("attachment_file_ids") is not None
            for fid in g["attachment_file_ids"]
        )
    )
    if not ids:
        return _ExplicitAttachments({}, project)
    rows = (
        sb.table("project_files")
        .select("id, filename, storage_path, category")
        .eq("project_id", project["id"])
        .in_("id", ids)
        .execute()
    ).data or []
    found = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ValueError(f"Attachment not found in this project: {missing[0]}")
    files = {
        r["id"]: {
            "filename": r["filename"],
            "content": storage.download_file(r["storage_path"]),
            "category": r["category"],
        }
        for r in rows
    }
    return _ExplicitAttachments(files, project)


def bulk_send(
    project_id: str, groups: list[dict], user_id: str, email_body: str | None = None
) -> dict:
    """Send each group's RFQ to its selected contacts, one email per contact.

    `groups` = [{"rfq_id": ..., "vendor_contact_ids": [...],
    "attachment_file_ids": [...] | None}]. A None attachment list means the
    default set (BOM split + drawings + Trenching markup); an explicit list is
    exactly what the PE confirmed in the modal. `email_body` is an optional
    PE-edited template sent verbatim (see build_custom_body). Failures are
    per-contact: one bad address never aborts the batch.
    """
    from app.core.supabase_client import get_supabase

    sb = get_supabase()
    project = sb.table("projects").select("*").eq("id", project_id).single().execute().data
    if not project.get("due_from_vendors_at"):
        raise ValueError("Set the vendor due date (due_from_vendors_at) before sending")

    # A group with no recipients sends nothing — drop it before doing any
    # download/upload work on its behalf.
    groups = [g for g in groups if g.get("vendor_contact_ids")]

    subject = build_subject(project)
    due_str = format_bid_datetime(project["due_from_vendors_at"])

    # Validate every RFQ id up front so a bad group 400s before anything sends.
    rfq_by_id: dict[str, dict] = {}
    for group in groups:
        rows = (
            sb.table("rfqs")
            .select("*, material_categories(name)")
            .eq("id", group["rfq_id"])
            .eq("project_id", project_id)
            .execute()
        ).data or []
        if not rows:
            raise ValueError(f"RFQ not found in this project: {group['rfq_id']}")
        rfq_by_id[group["rfq_id"]] = rows[0]

    # Default attachment set — only assembled when some group still uses it.
    use_defaults = any(g.get("attachment_file_ids") is None for g in groups)
    drawings, drawings_link = _prepare_drawings(sb, project) if use_defaults else ([], None)
    markup_files = _load_files(sb, project_id, "markup") if use_defaults else []
    explicit = _prepare_explicit_attachments(sb, project, groups)
    # Resolve every explicit group now: any OneDrive upload failure aborts the
    # whole batch cleanly, before the first email goes out.
    explicit_plans = {
        g["rfq_id"]: explicit.for_group(g["attachment_file_ids"])
        for g in groups
        if g.get("attachment_file_ids") is not None
    }

    results: list[dict] = []
    touched_rfqs: set[str] = set()
    first = True
    for group in groups:
        rfq = rfq_by_id[group["rfq_id"]]
        category_name = rfq["material_categories"]["name"]
        if group.get("attachment_file_ids") is not None:
            attachments, group_link = explicit_plans[group["rfq_id"]]
        else:
            split = None
            if rfq.get("split_file_id"):
                sf = (
                    sb.table("project_files")
                    .select("filename, storage_path")
                    .eq("id", rfq["split_file_id"])
                    .single()
                    .execute()
                ).data
                split = {"filename": sf["filename"], "content": storage.download_file(sf["storage_path"])}
            attachments = (
                ([split] if split else [])
                + drawings
                + (markup_files if _is_trenching(category_name) else [])
            )
            group_link = drawings_link
        contacts = (
            sb.table("vendor_contacts")
            .select("id, name, email")
            .in_("id", group["vendor_contact_ids"])
            .execute()
        ).data or []

        # Convert the BOM (any editable Office attachment) to an immutable PDF
        # before sending. A conversion failure fails only this group's contacts
        # — unrelated categories still go out — and never emails a malleable BOM.
        try:
            attachments = [_as_immutable_pdf(f) for f in attachments]
        except office_preview.ConversionError as exc:
            logger.warning("BOM PDF conversion failed for rfq %s: %s", rfq["id"], exc)
            for contact in contacts:
                results.append(
                    _record_failed_send(
                        sb, rfq, contact, subject,
                        f"Could not convert the BOM to PDF — retry. ({exc})", user_id,
                    )
                )
            continue

        for contact in contacts:
            if not first:
                time.sleep(1)  # Exchange throttles ~30 messages/min per mailbox
            first = False
            result = _send_one(
                sb,
                project=project,
                rfq=rfq,
                category_name=category_name,
                contact=contact,
                subject=subject,
                due_str=due_str,
                attachments=attachments,
                drawings_link=group_link,
                custom_body=email_body,
                user_id=user_id,
            )
            results.append(result)
            if result["status"] == "sent":
                touched_rfqs.add(rfq["id"])

    for rfq_id in touched_rfqs:
        # Flip draft → sent without regressing quotes_in/closed.
        sb.table("rfqs").update({"status": "sent"}).eq("id", rfq_id).eq("status", "draft").execute()

    return {
        "results": results,
        "drawings_delivery": "onedrive_link" if (drawings_link or explicit.used_link) else "attached",
    }


def _send_one(
    sb,
    *,
    project: dict,
    rfq: dict,
    category_name: str,
    contact: dict,
    subject: str,
    due_str: str,
    attachments: list[dict],
    drawings_link: str | None,
    custom_body: str | None,
    user_id: str,
) -> dict:
    if custom_body is not None:
        # The PE's words go out exactly as written — no AI variation.
        body = build_custom_body(custom_body, contact["name"], drawings_link)
    else:
        base_body = build_base_body(contact["name"], due_str, drawings_link)
        # The link is the vendor's only route to the drawings when they were
        # too big to attach — a rewrite must never drop it.
        tokens = [contact["name"], due_str, SIGNOFF] + ([drawings_link] if drawings_link else [])
        body = vary_email_body(base_body, must_contain=tokens)
    try:
        # The text body is what gets stored/varied/edited; the wire format is
        # the branded HTML shell with the inline-logo signature.
        draft = graph_email.create_draft(
            contact["email"], subject, email_branding.render_vendor_email(body), html=True
        )
        graph_email.add_attachment(
            draft["id"],
            email_branding.LOGO_FILENAME,
            email_branding.logo_bytes(),
            "image/jpeg",
            content_id=email_branding.LOGO_CONTENT_ID,
        )
        for f in attachments:
            graph_email.add_attachment(
                draft["id"], f["filename"], f["content"], _content_type(f["filename"])
            )
        graph_email.send_draft(draft["id"])

        log = (
            sb.table("email_log")
            .insert(
                {
                    "to_addrs": contact["email"],
                    "subject": subject,
                    "body": body,
                    "status": "sent",
                    "graph_message_id": draft.get("id"),
                    "project_id": project["id"],
                    "rfq_id": rfq["id"],
                    "sent_by": user_id,
                }
            )
            .execute()
        ).data[0]
        send_row = (
            sb.table("rfq_sends")
            .insert(
                {
                    "rfq_id": rfq["id"],
                    "vendor_contact_id": contact["id"],
                    "graph_message_id": draft.get("id"),
                    "conversation_id": draft.get("conversationId"),
                    "internet_message_id": draft.get("internetMessageId"),
                    "subject": subject,
                    "body": body,
                    "status": "sent",
                    "sent_at": "now()",
                    "sent_by": user_id,
                    "email_log_id": log["id"],
                }
            )
            .execute()
        ).data[0]
        # Back-compat: rfq_recipients is superseded by rfq_sends but still read
        # in places; keep it updated for one release.
        sb.table("rfq_recipients").upsert(
            {
                "rfq_id": rfq["id"],
                "vendor_contact_id": contact["id"],
                "email_log_id": log["id"],
                "sent_at": "now()",
            },
            on_conflict="rfq_id,vendor_contact_id",
        ).execute()
        audit(
            user_id,
            "rfq.send_one",
            "rfq_send",
            send_row["id"],
            {
                "to": contact["email"],
                "category": category_name,
                "conversation_id": draft.get("conversationId"),
                "attachments": [f["filename"] for f in attachments],
                "drawings_link": drawings_link,
                "custom_body": custom_body is not None,
            },
        )
        return {
            "rfq_id": rfq["id"],
            "vendor_contact_id": contact["id"],
            "status": "sent",
            "rfq_send_id": send_row["id"],
        }
    except Exception as exc:  # noqa: BLE001 — record and continue with the batch
        logger.exception("RFQ send failed for %s", contact["email"])
        try:
            sb.table("rfq_sends").insert(
                {
                    "rfq_id": rfq["id"],
                    "vendor_contact_id": contact["id"],
                    "subject": subject,
                    "body": body,
                    "status": "failed",
                    "error": str(exc),
                    "polling_active": False,
                    "sent_by": user_id,
                }
            ).execute()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record failed rfq_send")
        return {
            "rfq_id": rfq["id"],
            "vendor_contact_id": contact["id"],
            "status": "failed",
            "error": str(exc),
        }

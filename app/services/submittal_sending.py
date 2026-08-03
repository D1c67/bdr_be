"""Project submittal requests — create a request and email each vendor contact.

One request is a whole-modal BATCH across categories. Per category we render a
materials-list PDF (the items we're asking that category's vendors to provide
submittals for), attach it alongside the project plans/drawings (always) and any
selected spec sheets, and send ONE email per selected contact — each its own
Graph draft so we capture its conversationId. Replies thread back through the
email-ingestion pipeline because we send FROM the ingestion mailbox (see
submittal_sender / email_ingest_mailbox and services/submittal_ingest).

Modeled directly on services/rfq_sending: per-contact failures are recorded, not
raised; a PDF-render failure fails only that category's contacts; drawings/specs
that exceed the inline limit go to OneDrive as a single anonymous link.
"""

import hashlib
import logging
import mimetypes
import re
import time

from app.core.config import get_settings
from app.services import email_branding, graph_email, pm_folders, storage, submittal_pdf
from app.services.email_branding import SIGNOFF
from app.services.notifications import audit
from app.services.office_preview import ConversionError

logger = logging.getLogger(__name__)

# Token in a PE-edited body template, replaced per recipient.
CONTACT_NAME_PLACEHOLDER = "<Contact Name>"


def build_subject(project: dict) -> str:
    return f"{project.get('number') or 'TBD'} - {project['name']} - Submittal Request"


def build_base_body(
    contact_name: str, project: dict, link: str | None, *, shared_present: bool, has_specs: bool
) -> str:
    proj = f"{project.get('number') or ''} {project.get('name') or ''}".strip()
    docs = "plans/drawings" + (" and specifications" if has_specs else "")
    if link:
        docs_line = f"The {docs} are available here: {link}\n\n"
    elif shared_present:
        docs_line = f"The {docs} are attached.\n\n"
    else:
        docs_line = ""
    return (
        f"Hello {contact_name},\n\n"
        f"Please provide product submittals (cut sheets) for the attached items on {proj}.\n\n"
        f"{docs_line}"
        "Please let me know if there is anything you are unable to provide.\n\n"
        "Thank you,\n"
        f"{SIGNOFF}"
    )


def build_custom_body(template: str, contact_name: str, link: str | None) -> str:
    """Personalize a PE-edited body: substitute the placeholder and make sure an
    over-size documents link is never silently dropped."""
    body = template.replace(CONTACT_NAME_PLACEHOLDER, contact_name)
    if link and link not in body:
        body += f"\n\nThe plans/drawings are available here: {link}"
    return body


def _content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _safe_component(name: str) -> str:
    """Make a value safe to use as a single OneDrive path component."""
    return re.sub(r'[\\/:*?"<>|#%]+', "_", name).strip() or "file"


def _prepare_shared_files(
    project: dict, sender: str, items: list[dict]
) -> tuple[list[dict], str | None]:
    """Download the always-attached shared files (plans + selected specs). If
    their combined size exceeds the inline limit, upload them once to the
    sender's OneDrive and return a single anonymous link instead (shared by every
    email), mirroring rfq_sending._prepare_drawings."""
    if not items:
        return [], None
    files = [
        {"filename": i.get("filename") or "file", "content": storage.download_file(i["storage_path"])}
        for i in items
    ]
    total = sum(len(f["content"]) for f in files)
    if total <= get_settings().rfq_drawings_inline_limit_mb * 1024 * 1024:
        return files, None
    number = _safe_component(str(project.get("number") or project["id"]))
    # The anonymous link is minted on the upload FOLDER, so it must be unique to
    # THIS exact file selection. A project-level folder accumulates every send's
    # files and its link exposes all of them — leaking one vendor batch's docs to
    # another. Digest the selection (mirrors rfq_sending._link_for): the same
    # plans+specs re-send lands in the same folder (idempotent), a different
    # selection gets its own, and no link ever reaches files outside its set.
    digest = hashlib.sha1(
        "|".join(sorted(i.get("storage_path") or "" for i in items)).encode()
    ).hexdigest()[:12]
    folder = f"BDR/{number}/submittal-docs/{digest}"
    for f in files:
        graph_email.drive_upload(
            f"{folder}/{_safe_component(f['filename'])}", f["content"], sender=sender
        )
    link = graph_email.drive_create_link(
        graph_email.drive_get_item_id(folder, sender=sender), sender=sender
    )
    return [], link


def _archive_request_pdf(
    sb, project_id: str, filename: str, content: bytes, category_name: str, user_id: str
) -> None:
    """File the category's request PDF into the Documents hub (category
    'submittal' → Submittals folder). Best-effort: an archive failure must never
    fail the send it records."""
    try:
        path = storage.build_object_path(project_id, "pm/submittal", filename)
        storage.upload_file(path, content, "application/pdf")
        sb.table("pm_documents").insert(
            {
                "project_id": project_id,
                "category": "submittal",
                "storage_path": path,
                "filename": filename,
                "mime_type": "application/pdf",
                "size_bytes": len(content),
                "note": f"Submittal request — {category_name}",
                "uploaded_by": user_id,
            }
        ).execute()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to archive submittal request PDF for %s", project_id)


def _record_failed_send(
    sb, request_id: str, category_id: str | None, contact: dict, subject: str, error: str, user_id: str
) -> dict:
    """Record a failed send for a contact we never emailed (e.g. the PDF could
    not be rendered) and return the batch result entry."""
    send_id = None
    try:
        row = (
            sb.table("submittal_request_sends")
            .insert(
                {
                    "request_id": request_id,
                    "material_category_id": category_id,
                    "vendor_contact_id": contact["id"],
                    "subject": subject,
                    "status": "failed",
                    "error": error,
                    "sent_by": user_id,
                }
            )
            .execute()
        ).data[0]
        send_id = row["id"]
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record failed submittal send")
    return {"send_id": send_id, "vendor_contact_id": contact["id"], "status": "failed", "error": error}


def create_and_send(project_id: str, body: dict, user_id: str) -> dict:
    """Create a submittal request and email each group's selected contacts — one
    email per contact. `body` is a SubmittalRequestIn dump. Raises ValueError
    (→ 400) for a bad config or an id that doesn't belong to the project."""
    from app.core.supabase_client import get_supabase

    settings = get_settings()
    sender = (settings.submittal_sender or settings.email_ingest_mailbox or "").strip()
    if not sender:
        raise ValueError(
            "Configure SUBMITTAL_SENDER (or EMAIL_INGEST_MAILBOX) before sending "
            "submittal requests — vendor replies must have a mailbox to return to."
        )
    # Non-blocking heads-up: replies are only tracked when we send from the
    # mailbox the ingestion poller reads.
    if not settings.email_ingest_enabled:
        logger.warning(
            "Submittal request sent but EMAIL_INGEST_ENABLED is off — vendor "
            "replies will not be tracked."
        )
    elif sender.lower() != (settings.email_ingest_mailbox or "").strip().lower():
        logger.warning(
            "SUBMITTAL_SENDER (%s) differs from EMAIL_INGEST_MAILBOX — vendor "
            "replies will not be tracked.", sender,
        )

    sb = get_supabase()
    project = (
        sb.table("projects").select("id, number, name").eq("id", project_id).single().execute()
    ).data

    # A group with no recipients sends nothing — drop it before any work.
    groups = [g for g in body["groups"] if g.get("vendor_contact_ids")]
    if not groups:
        raise ValueError("No recipients selected")

    # ── Validate + resolve ──────────────────────────────────────────────────
    cat_ids = list({g["material_category_id"] for g in groups if g.get("material_category_id")})
    cat_names: dict[str, str] = {}
    if cat_ids:
        for c in (
            sb.table("material_categories").select("id, name").in_("id", cat_ids).execute()
        ).data or []:
            cat_names[c["id"]] = c["name"]

    wanted_material_ids = list({mid for g in groups for mid in g.get("included_material_ids", [])})
    materials: dict[str, dict] = {}
    if wanted_material_ids:
        rows = (
            sb.table("pm_materials")
            .select("id, description, quantity, unit, notes, category_label")
            .eq("project_id", project_id)
            .in_("id", wanted_material_ids)
            .execute()
        ).data or []
        materials = {r["id"]: r for r in rows}
        missing = [m for m in wanted_material_ids if m not in materials]
        if missing:
            raise ValueError(f"Material not found in this project: {missing[0]}")

    wanted_contact_ids = list({cid for g in groups for cid in g["vendor_contact_ids"]})
    contacts_by_id = {
        c["id"]: c
        for c in (
            sb.table("vendor_contacts")
            .select("id, name, email")
            .in_("id", wanted_contact_ids)
            .execute()
        ).data or []
    }
    missing_c = [c for c in wanted_contact_ids if c not in contacts_by_id]
    if missing_c:
        raise ValueError(f"Vendor contact not found: {missing_c[0]}")

    # Documents hub: plans always, selected specs when include_specs.
    hub = pm_folders.list_project_documents(project_id)
    drawing_items = [i for i in hub if i.get("folder") == "plans"]
    spec_items: list[dict] = []
    if body.get("include_specs") and body.get("spec_document_keys"):
        by_key = {i["key"]: i for i in hub}
        for k in body["spec_document_keys"]:
            it = by_key.get(k)
            if not it:
                raise ValueError(f"Spec document not found in this project: {k}")
            spec_items.append(it)

    shared_files, shared_link = _prepare_shared_files(project, sender, drawing_items + spec_items)
    shared_present = bool(drawing_items or spec_items)
    has_specs = bool(spec_items)
    drawings_delivery = "onedrive_link" if shared_link else "attached"

    # ── Persist the request + its snapshot items ─────────────────────────────
    request = (
        sb.table("submittal_requests")
        .insert(
            {
                "project_id": project_id,
                "status": "sent",
                "include_specs": bool(body.get("include_specs")),
                "spec_document_keys": [i["key"] for i in spec_items],
                "drawings_delivery": drawings_delivery,
                "deselected_material_ids": body.get("deselected_material_ids") or [],
                "email_body": body.get("email_body"),
                "created_by": user_id,
            }
        )
        .execute()
    ).data[0]
    request_id = request["id"]

    item_rows: list[dict] = []
    group_display: list[tuple[dict, str | None, str, list[dict]]] = []  # (group, cat_id, cat_name, disp)
    for g in groups:
        cat_id = g.get("material_category_id")
        cat_name = cat_names.get(cat_id) if cat_id else None
        disp: list[dict] = []
        for mid in g.get("included_material_ids", []):
            m = materials[mid]
            item_rows.append(
                {
                    "request_id": request_id,
                    "material_category_id": cat_id,
                    "category_label": None if cat_id else m.get("category_label"),
                    "pm_material_id": mid,
                    "description": (m.get("description") or "(no description)")[:2000],
                    "source": "material",
                }
            )
            disp.append(
                {
                    "description": m.get("description"),
                    "quantity": m.get("quantity"),
                    "unit": m.get("unit"),
                    "notes": m.get("notes"),
                }
            )
        for desc in g.get("adhoc_descriptions", []):
            text = " ".join((desc or "").split())[:2000]
            if not text:
                continue
            item_rows.append(
                {
                    "request_id": request_id,
                    "material_category_id": cat_id,
                    "category_label": None,
                    "pm_material_id": None,
                    "description": text,
                    "source": "adhoc",
                }
            )
            disp.append({"description": text, "quantity": None, "unit": None, "notes": None})
        group_display.append((g, cat_id, cat_name or "Materials", disp))
    if item_rows:
        sb.table("submittal_request_items").insert(item_rows).execute()

    # ── Send: one email per contact per group ────────────────────────────────
    subject = build_subject(project)
    results: list[dict] = []
    statuses: list[str] = []
    first = True
    for g, cat_id, cat_name, disp in group_display:
        contacts = [
            contacts_by_id[c] for c in dict.fromkeys(g["vendor_contact_ids"]) if c in contacts_by_id
        ]
        try:
            pdf_bytes = submittal_pdf.render_pdf(project, cat_name, disp)
        except ConversionError as exc:
            logger.warning("Submittal PDF render failed for %s / %s: %s", project_id, cat_name, exc)
            for contact in contacts:
                results.append(
                    _record_failed_send(
                        sb, request_id, cat_id, contact, subject,
                        f"Could not render the submittal request PDF — retry. ({exc})", user_id,
                    )
                )
                statuses.append("failed")
            continue
        pdf_name = submittal_pdf.pdf_filename(project, cat_name)
        _archive_request_pdf(sb, project_id, pdf_name, pdf_bytes, cat_name, user_id)
        attachments = [{"filename": pdf_name, "content": pdf_bytes}] + shared_files

        for contact in contacts:
            if not first:
                time.sleep(1)  # Exchange throttles ~30 messages/min per mailbox
            first = False
            res = _send_one(
                sb,
                project=project,
                request_id=request_id,
                category_id=cat_id,
                category_name=cat_name,
                contact=contact,
                subject=subject,
                attachments=attachments,
                link=shared_link,
                shared_present=shared_present,
                has_specs=has_specs,
                custom_body=body.get("email_body"),
                sender=sender,
                user_id=user_id,
            )
            results.append(res)
            statuses.append(res["status"])

    sent = sum(1 for s in statuses if s == "sent")
    if not statuses or sent == 0:
        final = "failed"
    elif sent == len(statuses):
        final = "sent"
    else:
        final = "partial"
    sb.table("submittal_requests").update({"status": final}).eq("id", request_id).execute()

    audit(
        user_id,
        "submittal.request",
        "submittal_request",
        request_id,
        {
            "sent": sent,
            "failed": len(statuses) - sent,
            "categories": len(groups),
            "include_specs": bool(body.get("include_specs")),
            "drawings_delivery": drawings_delivery,
        },
    )
    return {"request_id": request_id, "results": results, "drawings_delivery": drawings_delivery}


def _send_one(
    sb,
    *,
    project: dict,
    request_id: str,
    category_id: str | None,
    category_name: str,
    contact: dict,
    subject: str,
    attachments: list[dict],
    link: str | None,
    shared_present: bool,
    has_specs: bool,
    custom_body: str | None,
    sender: str,
    user_id: str,
) -> dict:
    if custom_body is not None:
        body = build_custom_body(custom_body, contact["name"], link)
    else:
        body = build_base_body(
            contact["name"], project, link, shared_present=shared_present, has_specs=has_specs
        )
    try:
        draft = graph_email.create_draft(
            contact["email"],
            subject,
            email_branding.render_vendor_email(body, subtitle="SUBMITTAL REQUEST"),
            html=True,
            sender=sender,
        )
        graph_email.add_attachment(
            draft["id"],
            email_branding.LOGO_FILENAME,
            email_branding.logo_bytes(),
            "image/jpeg",
            content_id=email_branding.LOGO_CONTENT_ID,
            sender=sender,
        )
        for f in attachments:
            graph_email.add_attachment(
                draft["id"], f["filename"], f["content"], _content_type(f["filename"]), sender=sender
            )
        graph_email.send_draft(draft["id"], sender=sender)

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
                    "sent_by": user_id,
                }
            )
            .execute()
        ).data[0]
        send_row = (
            sb.table("submittal_request_sends")
            .insert(
                {
                    "request_id": request_id,
                    "material_category_id": category_id,
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
        audit(
            user_id,
            "submittal.send_one",
            "submittal_request_send",
            send_row["id"],
            {
                "to": contact["email"],
                "category": category_name,
                "conversation_id": draft.get("conversationId"),
            },
        )
        return {"send_id": send_row["id"], "vendor_contact_id": contact["id"], "status": "sent"}
    except Exception as exc:  # noqa: BLE001 — record and continue with the batch
        logger.exception("Submittal send failed for %s", contact["email"])
        try:
            sb.table("submittal_request_sends").insert(
                {
                    "request_id": request_id,
                    "material_category_id": category_id,
                    "vendor_contact_id": contact["id"],
                    "subject": subject,
                    "body": body,
                    "status": "failed",
                    "error": str(exc),
                    "sent_by": user_id,
                }
            ).execute()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record failed submittal send")
        return {
            "send_id": None,
            "vendor_contact_id": contact["id"],
            "status": "failed",
            "error": str(exc),
        }

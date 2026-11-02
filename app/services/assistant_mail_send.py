"""Canonical mail previews and one-claim application send dispatch.

Conservative disclosure policy: every tainted draft and every attachment
requires all recipients to hold ACTUAL_BID_VIEWER_ROLES. Email and PDF contents
cannot be reliably classified by keywords. Clean, user-typed drafts without
attachments may use external recipients after the additional amount guard.
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import re
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from app.assistant import mail_binding, repo
from app.core import provenance, supabase_client
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, INTERNAL_ROLES
from app.services import assistant_mail as mail
from app.services import assistant_ms, email_branding, graph_email, storage
from app.services.assistant_graph import GraphTransport

MAX_FILES = 10
MONEY = re.compile(r"\$|\bUSD\b|\b(?:actual|final)\s+bid\b|\bbid\s+(?:amount|total|price)\b|"
                   r"\b(?:price|pricing|cost|total|amount)\b|"
                   r"\b(?:bid|quote)\b[^\n]{0,60}\d[\d,.]*|\b\d{4,}(?:[,.]\d+)?\b", re.I)
LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)|(https?://[^\s<>]+)", re.I)


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        raise mail.MailError("invalid_reference") from None


def _profiles():
    return (supabase_client.get_supabase().table("profiles")
            .select("id,email,full_name,role,is_active").eq("is_active", True).limit(1000).execute()).data or []


def recipients(user_id, args, user_texts):
    profiles = [row for row in _profiles() if row.get("is_active") is True and row.get("role") in INTERNAL_ROLES]
    trusted = [row.get("email") for row in profiles]
    # Internal roles already have the ordinary domain contact directory access.
    contacts = []
    contact_ids = {}
    for field in ("gc_contact_ids", "vendor_contact_ids"):
        values = args.get(field) or []
        if not isinstance(values, list) or len(values) > 30:
            raise mail.MailError("recipients_invalid")
        contact_ids[field] = [_uuid(value) for value in values]
    if any(contact_ids.values()):
        contacts = repo.mail_contacts(user_id, **contact_ids)
        if {str(row["id"]) for row in contacts} != set(contact_ids["gc_contact_ids"] + contact_ids["vendor_contact_ids"]):
            raise mail.MailError("recipients_invalid")
        trusted.extend(row.get("email") for row in contacts)
    to, cc = args.get("to", []), args.get("cc", [])
    if not isinstance(to, list) or not isinstance(cc, list) or len(to) + len(cc) > 50:
        raise mail.MailError("recipients_invalid")
    to = [*to, *(row.get("email") for row in contacts)]
    if len(to) + len(cc) > 50:
        raise mail.MailError("recipients_invalid")
    bound_to, stripped_to = mail_binding.bind_recipients(to, allowed_emails=trusted, user_texts=user_texts)
    bound_cc, stripped_cc = mail_binding.bind_recipients(cc, allowed_emails=trusted, user_texts=user_texts)
    if stripped_to or stripped_cc or not bound_to:
        raise mail.MailError("recipients_not_bound")
    bound_cc = [value for value in bound_cc if value not in bound_to]
    by_email = {str(row.get("email") or "").casefold(): row for row in profiles}
    def entry(address):
        row = by_email.get(address, {})
        return {"address": address, "name": str(row.get("full_name") or "").replace(chr(0x2014), ","), "role": row.get("role")}
    return [entry(value) for value in bound_to], [entry(value) for value in bound_cc]


def files(user_id, file_ids):
    if not isinstance(file_ids, list) or len(file_ids) > MAX_FILES:
        raise mail.MailError("attachments_invalid")
    ids = [_uuid(value) for value in file_ids]
    rows = repo.staged_files_by_ids(user_id, ids)
    if {str(row["id"]) for row in rows} != set(ids):
        raise mail.MailError("attachment_not_owned")
    now = datetime.now(timezone.utc)
    result = []
    for row in rows:
        try:
            expiry = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if (row.get("purged_at") or row.get("consumed_at") or expiry <= now
                    or row.get("mime") != "application/pdf" or not 0 < int(row["size_bytes"]) <= 20 * 1024 * 1024):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise mail.MailError("attachment_not_owned") from None
        result.append(row)
    return sorted(result, key=lambda row: str(row["id"]))


def attachment_bytes(row):
    value = storage.download_file(row["storage_path"])
    if (len(value) != row["size_bytes"] or hashlib.sha256(value).hexdigest() != row["sha256"]
            or not value.startswith(b"%PDF-")):
        raise mail.MailError("attachment_changed")
    return value


def has_bid_amounts(text, attachments):
    if MONEY.search(text):
        return True
    for row in attachments:
        try:
            pdf = PdfReader(io.BytesIO(attachment_bytes(row)))
            if len(pdf.pages) > 400:
                return True
            text = "".join((page.extract_text() or "")[:10000] for page in pdf.pages)
            if not text.strip() or MONEY.search(text):
                return True
        except mail.MailError:
            raise
        except Exception:
            return True
    return False


def render_body(text, user_texts, signed_urls=()):
    """Plain text/Markdown only; safe destinations become server-built hrefs."""
    parts, hrefs, shown = [], [], []
    offset = 0
    for match in LINK.finditer(text):
        prefix = text[offset:match.start()]
        parts.append(html.escape(prefix))
        shown.append(prefix)
        anchor = match.group(1) or match.group(3)
        url = match.group(2) or match.group(3)
        if mail_binding.url_is_bound(url, user_texts=user_texts, signed_urls=signed_urls):
            parts.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(anchor)}</a>')
            shown.append(anchor + " (" + url + ")")
            hrefs.append({"anchor": anchor, "href": url})
        else:
            plain = re.sub(r"https?://", "", anchor, flags=re.I)
            parts.append(html.escape(plain))
            shown.append(plain)
        offset = match.end()
    parts.append(html.escape(text[offset:]))
    shown.append(text[offset:])
    return "".join(parts).replace("\n", "<br>"), "".join(shown), hrefs


def prepare(user_id: str, run_id: str, args: dict, *, frozen_attachment_links=None) -> dict:
    sender = assistant_ms.sender_mailbox(user_id)
    profile = repo.human_profile(user_id)
    conversation_id = repo.conversation_for_run(user_id, run_id)
    conversation = repo.get_conversation(user_id, conversation_id) if conversation_id else None
    if not conversation or conversation.get("mode") != "normal" or conversation.get("mailbox_tainted_at"):
        raise mail.MailError("normal_mode_required")
    user_texts = repo.mail_user_texts(user_id, run_id)
    if not user_texts:
        raise mail.MailError("turn_context_unavailable")
    allowed_keys = {"to", "cc", "gc_contact_ids", "vendor_contact_ids", "subject", "body_text", "attachment_ids",
                    "response_format", "limit"}
    if not isinstance(args, dict) or set(args) - allowed_keys:
        raise mail.MailError("draft_invalid")
    subject, body = args.get("subject"), args.get("body_text")
    if (not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 500
            or any(ord(c) < 32 for c in subject) or not isinstance(body, str) or not 1 <= len(body) <= 20000):
        raise mail.MailError("draft_invalid")
    subject, body = subject.replace(chr(0x2014), ","), body.replace(chr(0x2014), ",")
    to, cc = recipients(user_id, args, user_texts)
    attachments = files(user_id, args.get("attachment_ids") or [])
    source_tainted = (repo.mail_sensitive_context(user_id, run_id)
                      or bool(repo.tainted_results(user_id, conversation_id, limit=1)))
    if source_tainted or attachments or has_bid_amounts(subject + "\n" + body, []):
        if any(row.get("role") not in ACTUAL_BID_VIEWER_ROLES for row in [*to, *cc]):
            raise mail.MailError("bid_recipient_not_authorized")
    rendered, display, hrefs = render_body(body, user_texts)
    attachment_links = []
    if sum(row["size_bytes"] for row in attachments) >= 3 * 1024 * 1024:
        if frozen_attachment_links is None:
            attachment_links = [{"id": str(row["id"]), "filename": row["filename"],
                "href": storage.signed_url(row["storage_path"], ttl_seconds=21600, use_cache=False,
                                            download=row["filename"])} for row in attachments]
        else:
            # Only dispatch passes links from the immutable server-rendered
            # approval; the model's draft schema cannot supply these URLs.
            if ([str(row["id"]) for row in attachments]
                    != [str(row.get("id")) for row in frozen_attachment_links]):
                raise mail.MailError("attachment_changed")
            attachment_links = frozen_attachment_links
        for link in attachment_links:
            rendered += f'<p><a href="{html.escape(link["href"], quote=True)}">{html.escape(link["filename"])}</a></p>'
            display += "\nAttachment: " + link["filename"]
            hrefs.append({"anchor": link["filename"], "href": link["href"]})
    name = str(profile.get("full_name") or sender).replace(chr(0x2014), ",")[:160]
    authority = {"sender": sender, "role": str(profile.get("role")),
                 "source_tainted": source_tainted, "contains_attachments": bool(attachments),
                 "email": profile.get("email"), "rfp_mailboxes": sorted(profile.get("rfp_mailboxes") or []),
                 "attachments": [{"id": str(row["id"]), "sha256": row["sha256"], "size_bytes": row["size_bytes"]}
                                 for row in attachments]}
    return {"sender": sender, "to": to, "cc": cc, "reply_to": sender, "subject": subject,
            "body_text": display, "body_html": rendered, "hrefs": hrefs, "attachment_links": attachment_links,
            "attachments": [{"id": str(row["id"]), "filename": row["filename"], "size_bytes": row["size_bytes"],
                             "sha256": row["sha256"]} for row in attachments],
            "authority": authority, "name": name}


def dispatch(user_id: str, run_id: str, approval_id: str, body: dict) -> dict:
    from app.assistant.approvals import sha256_hex
    approval = repo.get_approval(user_id, approval_id)
    if (not approval or approval.get("status") != "executing" or approval.get("action_kind") != "email_draft"
            or approval.get("payload", {}).get("request", {}).get("json") != body):
        raise mail.MailError("approval_mismatch")
    # Rebuild every bound destination from the original, server-stored request args.
    fresh = prepare(user_id, run_id, approval["payload"]["args"],
                    frozen_attachment_links=body.get("attachment_links"))
    for key in fresh:
        if fresh[key] != body.get(key):
            raise mail.MailError("changed_since_preview")
    send_id = _uuid(body.get("send_id"))
    to = [row["address"] for row in fresh["to"]]
    cc = [row["address"] for row in fresh["cc"]]
    footer = body.get("footer")
    if not isinstance(footer, str) or footer != (
        f"Sent by {fresh['name']}'s assistant in BDR, approved by {fresh['name']} on {body.get('approved_date')}"):
        raise mail.MailError("footer_invalid")
    if body.get("approved_date") != "approval date":
        raise mail.MailError("footer_invalid")
    try:
        decided = datetime.fromisoformat(str(approval["decided_at"]).replace("Z", "+00:00"))
        if decided.tzinfo is None:
            raise ValueError()
    except (ValueError, KeyError):
        raise mail.MailError("approval_mismatch") from None
    footer = (f"Sent by {fresh['name']}'s assistant in BDR, approved by {fresh['name']} on "
              f"{decided.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()}")
    attachment_rows = files(user_id, [row["id"] for row in fresh["attachments"]])
    branded = email_branding.render_branded_html(fresh["body_html"], "Assistant email")
    branded = branded.replace("</body>", "<p>" + html.escape(footer) + "</p></body>")
    message = {"subject": fresh["subject"],
        "body": {"contentType": "HTML", "content": branded},
        "toRecipients": [{"emailAddress": {"address": value}} for value in to],
        "ccRecipients": [{"emailAddress": {"address": value}} for value in cc],
        "replyTo": [{"emailAddress": {"address": fresh["sender"]}}],
        "internetMessageHeaders": [{"name": "X-BDR-Assistant", "value": send_id}],
        "categories": ["Sent by assistant"]}
    total = sum(row["size_bytes"] for row in attachment_rows)
    inline = []
    for row in attachment_rows:
        content = attachment_bytes(row)
        if total < 3 * 1024 * 1024:
            inline.append({"@odata.type": "#microsoft.graph.fileAttachment", "name": row["filename"],
                           "contentType": "application/pdf", "contentBytes": base64.b64encode(content).decode()})
    if inline:
        message["attachments"] = inline
    sb = supabase_client.get_supabase()
    session = graph_email._active_test_session(sb)
    redirected = None
    if session is not None:
        from app.services import rfp_test
        redirected = rfp_test.redirect(message, sb=sb, session=session, sender_mailbox=fresh["sender"], context={})
    effective = [item["emailAddress"]["address"] for key in ("toRecipients", "ccRecipients") for item in message.get(key, [])]
    # Fetch the app token before the transactional dispatch claim; no HTTP is sent yet.
    token = assistant_ms.application_token(user_id)
    claimed = repo.mail_send_claim(user_id, approval_id, run_id, fresh["sender"],
                                  sha256_hex(approval["payload"]), effective_recipients=effective)
    if not claimed.get("ok"):
        raise mail.MailError("send_refused")
    if not claimed.get("claimed"):
        status = claimed.get("status")
        return {"outcome": status if status in {"accepted", "refused"} else "uncertain", "send_id": send_id}
    log = None
    try:
        log = sb.table("email_log").insert({"sent_by": user_id, "actor_kind": "assistant",
            "assistant_run_id": run_id, "approval_id": approval_id, "assistant_send_id": send_id,
            "to_addrs": ", ".join(to), "subject": fresh["subject"], "body": message["body"]["content"],
            "status": "queued", **provenance.fields(user_id)}).execute().data[0]
        GraphTransport(token, sender_mailbox=fresh["sender"]).send_mail({"message": message, "saveToSentItems": True})
        outcome = "accepted"
    except Exception:
        outcome = "uncertain"
    # Durable claimed attempts never automatically replay, even if outcome persistence fails.
    try:
        closed = repo.mail_send_finish(user_id, send_id, outcome,
                                      error_code="dispatch_uncertain" if outcome == "uncertain" else None)
        if not closed.get("ok"):
            outcome = "uncertain"
    except Exception:
        outcome = "uncertain"
    if log:
        try:
            sb.table("email_log").update({"status": "sent" if outcome == "accepted" else "failed",
                "error": None if outcome == "accepted" else "assistant_send_uncertain"}).eq("id", log["id"]).execute()
        except Exception:
            outcome = "uncertain"
    if redirected is not None:
        from app.services import rfp_test
        try:
            rfp_test.record_redirected(sb, session, redirected, email_log_id=log["id"] if log else None,
                                      status="sent" if outcome == "accepted" else "failed")
        except Exception:
            outcome = "uncertain"
    return {"outcome": outcome, "send_id": send_id,
            "message": "Microsoft accepted the email." if outcome == "accepted" else "The send outcome is uncertain. Do not retry."}

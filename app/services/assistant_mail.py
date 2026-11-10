"""Live, owner-scoped application mail reads and bounded PDF staging."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from app.assistant import repo
from app.services import assistant_ms, assistant_staging
from app.services.assistant_graph import GraphTransport, graph_url

MAX_RESULTS = 50
MAX_PAGES = 5
MAX_BODY = 24000
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class MailError(ValueError):
    def __init__(self, code="mail_unavailable"):
        self.code = code
        super().__init__("Mail is unavailable.")


def window(value: int = 30) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 90:
        raise MailError("window_invalid")
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise MailError()
    return quote(value, safe="")


def mailbox_path(mailbox: str) -> str:
    return f"/v1.0/users/{quote(mailbox, safe='@')}/messages"


def continuation(value: str, path: str) -> str:
    """A provider cursor may change the query, never mailbox or operation."""
    url = graph_url(value)
    if urlsplit(url).path != path:
        raise MailError()
    return url


def metadata(row: dict) -> dict:
    sender = row.get("from") or {}
    address = sender.get("emailAddress") or {}
    return {"id": str(row.get("id") or "")[:2048], "subject": str(row.get("subject") or "")[:500],
            "from": {"address": str(address.get("address") or "")[:254],
                     "name": str(address.get("name") or "")[:200]},
            "received_at": row.get("receivedDateTime"), "has_attachments": bool(row.get("hasAttachments"))}


def scrubber(user_id: str, *, review_target: str | None = None):
    """Browser v2 8.3 (WI-14): codes become [code] and sign-in links
    [sign-in link] in code emails (and in every message while the owner
    has an open mfa help) before a result is returned or stored. In a
    Mailbox review the reviewed mailbox's owner counts too."""
    from app.services import assistant_browser_mfa_mail

    if review_target is not None:
        return assistant_browser_mfa_mail.review_scrubber_for(user_id, review_target)
    return assistant_browser_mfa_mail.scrubber_for(user_id)


def save_raw(user_id: str, tool: str, payload: Any, *, conversation_id=None, review=False) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, default=str)
    row = repo.insert_tool_result(user_id, {
        "conversation_id": conversation_id, "tool_name": tool,
        "retention_class": "cross_mailbox" if review else "standard", "tainted": True,
        "payload": payload, "payload_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "size_bytes": len(encoded.encode()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7 if review else 30)).isoformat(),
    })
    if not row.get("id"):
        raise MailError("retention_failed")
    return str(row["id"])


def search(user_id: str, mailbox: str, query="", window_days=30, limit=20) -> dict:
    window_days = window(window_days)
    if not isinstance(query, str) or len(query) > 300 or any(ord(c) < 32 for c in query):
        raise MailError("query_invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise MailError("limit_invalid")
    mailbox, token = assistant_ms.for_mailbox(user_id, mailbox)
    path = mailbox_path(mailbox)
    since = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    filters = f"receivedDateTime ge {since}"
    if query:
        # Literal-only contains; the model cannot provide OData expressions.
        filters += " and contains(subject,'" + query.replace("'", "''") + "')"
    params = {"$select": "id,subject,from,receivedDateTime,hasAttachments",
              "$filter": filters, "$top": str(min(limit, 50))}
    scrub = scrubber(user_id)
    rows = []
    next_url = path
    seen = set()
    for _ in range(MAX_PAGES):
        # Reload live authority on every page, even when the app token is cached.
        current, token = assistant_ms.for_mailbox(user_id, mailbox)
        if current != mailbox or next_url in seen:
            raise MailError()
        seen.add(next_url)
        page = GraphTransport(token).read_json(next_url, params=params)
        params = None
        values = page.get("value")
        if not isinstance(values, list):
            raise MailError()
        rows.extend(metadata(scrub.view(row)[0]) for row in values[:limit - len(rows)] if isinstance(row, dict))
        following = page.get("@odata.nextLink")
        if not following or len(rows) >= limit:
            return {"mailbox": mailbox, "messages": rows, "truncated": bool(following), "window_days": window_days}
        next_url = continuation(following, path)
    return {"mailbox": mailbox, "messages": rows, "truncated": True, "window_days": window_days}


def read(user_id: str, mailbox: str, message_id: str, *, conversation_id=None, run_id=None) -> dict:
    mailbox, token = assistant_ms.for_mailbox(user_id, mailbox)
    path = mailbox_path(mailbox) + "/" + identifier(message_id)
    raw = GraphTransport(token).read_json(path, params={"$select":
        "id,subject,from,receivedDateTime,hasAttachments,bodyPreview,body"},
        prefer='outlook.body-content-type="text"')
    body = raw.get("body") or {}
    if body.get("contentType", "text").casefold() != "text":
        raise MailError()
    scrub = scrubber(user_id)
    raw, code_email = scrub.view(raw)
    body = raw.get("body") or {}
    text = str(body.get("content") or "")[:MAX_BODY]
    result_id = save_raw(user_id, "email_read", {"mailbox": mailbox, "message": raw},
                         conversation_id=conversation_id)
    from app.services.assistant_mail_review import extract
    if not run_id:
        raise MailError("turn_context_unavailable")
    # A code email reaches the extractor with no body (8.3).
    summary = scrub.text(extract(user_id, run_id, [metadata(raw) if code_email else {**metadata(raw), "body": text}]),
                         code_email=code_email)
    attachments = []
    if raw.get("hasAttachments"):
        mailbox, token = assistant_ms.for_mailbox(user_id, mailbox)
        listing = GraphTransport(token).read_json(path + "/attachments", params={
            "$select": "id,name,contentType,size,isInline", "$top": "20"})
        attachments = [{"id": str(item.get("id") or "")[:2048], "filename": str(item.get("name") or "")[:200],
                        "size_bytes": item.get("size"), "content_type": item.get("contentType"),
                        "file_attachment": item.get("@odata.type") == "#microsoft.graph.fileAttachment"}
                       for item in listing.get("value", [])[:20] if isinstance(item, dict)]
    return {**metadata(raw), "mailbox": mailbox, "summary": summary, "raw_result_id": result_id,
            "attachments": attachments,
            "truncated": len(str(body.get("content") or "")) > MAX_BODY}


def attachment_to_staging(user: Any, mailbox: str, message_id: str, attachment_id: str) -> dict:
    # Parent lookup proves message authority before attachment content can be read.
    mailbox, token = assistant_ms.for_mailbox(user.id, mailbox)
    parent = mailbox_path(mailbox) + "/" + identifier(message_id)
    GraphTransport(token).read_json(parent, params={"$select": "id"})
    mailbox, token = assistant_ms.for_mailbox(user.id, mailbox)
    path = parent + "/attachments/" + identifier(attachment_id)
    graph = GraphTransport(token)
    attachment = graph.read_json(path, params={"$select": "id,name,contentType,size,isInline"})
    if (attachment.get("@odata.type") != "#microsoft.graph.fileAttachment"
            or not str(attachment.get("name") or "").casefold().endswith(".pdf")
            or attachment.get("contentType") not in {"application/pdf", "application/octet-stream"}
            or not isinstance(attachment.get("size"), int)
            or not 0 < attachment["size"] <= MAX_ATTACHMENT_BYTES):
        raise MailError("attachment_refused")
    mailbox, token = assistant_ms.for_mailbox(user.id, mailbox)
    content = GraphTransport(token).read_bytes(path + "/$value", max_bytes=MAX_ATTACHMENT_BYTES)
    row, reused = assistant_staging.stage(user, attachment["name"], content)
    from app.services.notifications import audit
    audit(user.id, "assistant.email_attachment_staged", "assistant_file", str(row["id"]),
          {"mailbox": mailbox, "message_id": message_id, "attachment_id": attachment_id})
    # Only owner-bound staged metadata returns; no path selected by the model.
    return {"id": row["id"], "filename": row["filename"], "size_bytes": row["size_bytes"],
            "page_count": row.get("page_count"), "reused": reused,
            "source": {"mailbox": mailbox, "message_id": message_id, "attachment_id": attachment_id}}

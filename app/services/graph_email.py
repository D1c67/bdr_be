"""Microsoft Graph email — send-as the shared mailbox bids@g3electrical.com.

Uses the MSAL client-credentials (application) flow. The Azure app must have the
application permission `Mail.Send` (admin-consented) and an Exchange
ApplicationAccessPolicy restricting that permission to ONLY the bids@ mailbox.

Every send is recorded in `email_log`. For large attachments, prefer including
short-TTL signed download links in the body over inlining bytes.
"""

import base64

import httpx
import msal

from app.core.config import get_settings
from app.core.supabase_client import get_supabase

_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_msal_app: msal.ConfidentialClientApplication | None = None


def _get_msal_app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        s = get_settings()
        _msal_app = msal.ConfidentialClientApplication(
            client_id=s.ms_client_id,
            authority=f"https://login.microsoftonline.com/{s.ms_tenant_id}",
            client_credential=s.ms_client_secret,
        )
    return _msal_app


def _acquire_token() -> str:
    result = _get_msal_app().acquire_token_for_client(scopes=_GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"Graph token error: {result.get('error_description', result)}"
        )
    return result["access_token"]


def graph_request(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    timeout: float = 30,
    follow_redirects: bool = False,
) -> httpx.Response:
    """Authenticated Graph call. Always asks for immutable message ids so the
    ids we store at draft time survive the move to Sent Items after send.

    `follow_redirects` is needed for endpoints that 302 to a pre-authenticated
    download URL (e.g. `/content?format=pdf`); httpx strips the Authorization
    header on cross-host redirects, so following is safe.
    """
    resp = httpx.request(
        method,
        f"{_GRAPH_BASE}{path}",
        headers={
            "Authorization": f"Bearer {_acquire_token()}",
            "Prefer": 'IdType="ImmutableId"',
        },
        json=json,
        params=params,
        timeout=timeout,
        follow_redirects=follow_redirects,
    )
    resp.raise_for_status()
    return resp


# ── Draft flow (used for RFQs: lets us capture the conversationId) ──────────

_INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024  # Graph inline fileAttachment cap
_UPLOAD_CHUNK = 10 * 320 * 1024             # upload-session chunks: 320 KiB multiples


def create_draft(to_addr: str, subject: str, body: str, *, html: bool = False) -> dict:
    """Create a draft in the sender mailbox; returns the Graph message resource
    including `id`, `conversationId` and `internetMessageId`."""
    sender = get_settings().ms_sender
    resp = graph_request(
        "POST",
        f"/users/{sender}/messages",
        json={
            "subject": subject,
            "body": {"contentType": "HTML" if html else "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
        },
    )
    return resp.json()


def add_attachment(
    message_id: str,
    name: str,
    content: bytes,
    content_type: str,
    *,
    content_id: str | None = None,
) -> None:
    """Attach a file to a draft. Small files inline; large ones via upload session.

    `content_id` marks the file as an inline image referenced from an HTML body
    via `<img src="cid:...">` (small attachments only — body images never come
    near the upload-session threshold).
    """
    sender = get_settings().ms_sender
    if len(content) < _INLINE_ATTACHMENT_LIMIT:
        payload = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name,
            "contentType": content_type,
            "contentBytes": base64.b64encode(content).decode(),
        }
        if content_id:
            payload["isInline"] = True
            payload["contentId"] = content_id
        graph_request(
            "POST",
            f"/users/{sender}/messages/{message_id}/attachments",
            json=payload,
        )
        return
    session = graph_request(
        "POST",
        f"/users/{sender}/messages/{message_id}/attachments/createUploadSession",
        json={
            "AttachmentItem": {
                "attachmentType": "file",
                "name": name,
                "size": len(content),
            }
        },
    ).json()
    _upload_in_chunks(session["uploadUrl"], content)


def _upload_in_chunks(upload_url: str, content: bytes) -> None:
    """PUT the content in 320 KiB-multiple chunks. The pre-authenticated upload
    URL must NOT receive an Authorization header."""
    total = len(content)
    for start in range(0, total, _UPLOAD_CHUNK):
        chunk = content[start : start + _UPLOAD_CHUNK]
        end = start + len(chunk) - 1
        resp = httpx.put(
            upload_url,
            content=chunk,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
            timeout=120,
        )
        resp.raise_for_status()


def send_draft(message_id: str) -> None:
    sender = get_settings().ms_sender
    graph_request("POST", f"/users/{sender}/messages/{message_id}/send")


# ── OneDrive (fallback when drawings are too large to attach) ────────────────


def drive_upload(path: str, content: bytes) -> str:
    """Upload a file to the sender's OneDrive via an upload session; returns the
    drive item id. `path` is relative to the drive root, e.g. 'BDR/123/drawings/a.pdf'."""
    sender = get_settings().ms_sender
    session = graph_request(
        "POST",
        f"/users/{sender}/drive/root:/{path}:/createUploadSession",
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
    ).json()
    upload_url = session["uploadUrl"]
    total = len(content)
    item: dict = {}
    for start in range(0, total, _UPLOAD_CHUNK):
        chunk = content[start : start + _UPLOAD_CHUNK]
        end = start + len(chunk) - 1
        resp = httpx.put(
            upload_url,
            content=chunk,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
            timeout=120,
        )
        resp.raise_for_status()
        if resp.status_code in (200, 201):
            item = resp.json()
    return item["id"]


def drive_get_item_id(path: str) -> str:
    sender = get_settings().ms_sender
    return graph_request("GET", f"/users/{sender}/drive/root:/{path}").json()["id"]


def drive_create_link(item_id: str) -> str:
    """Anonymous view link (vendors are external). Requires 'Anyone' sharing links
    to be enabled in the SharePoint admin center."""
    sender = get_settings().ms_sender
    resp = graph_request(
        "POST",
        f"/users/{sender}/drive/items/{item_id}/createLink",
        json={"type": "view", "scope": "anonymous"},
    )
    return resp.json()["link"]["webUrl"]


def send_mail(
    *,
    to: list[str],
    subject: str,
    body_html: str,
    attachments: list[tuple[str, bytes]] | None = None,
    inline_images: list[tuple[str, str, bytes, str]] | None = None,
    project_id: str | None = None,
    rfq_id: str | None = None,
    sent_by: str | None = None,
) -> dict:
    """Send an email from the shared mailbox and record it in email_log.

    `attachments` is a list of (filename, content_bytes). `inline_images` is a
    list of (content_id, filename, content_bytes, content_type) for images the
    HTML body references via `<img src="cid:content_id">` (e.g. the G3 logo).
    Returns the email_log row.
    """
    settings = get_settings()
    sb = get_supabase()

    log = (
        sb.table("email_log")
        .insert(
            {
                "to_addrs": ", ".join(to),
                "subject": subject,
                "body": body_html,
                "status": "queued",
                "project_id": project_id,
                "rfq_id": rfq_id,
                "sent_by": sent_by,
            }
        )
        .execute()
    ).data[0]

    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    }
    msg_attachments: list[dict] = []
    if attachments:
        msg_attachments += [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentBytes": base64.b64encode(content).decode(),
            }
            for name, content in attachments
        ]
    if inline_images:
        msg_attachments += [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": content_type,
                "contentBytes": base64.b64encode(content).decode(),
                "isInline": True,
                "contentId": content_id,
            }
            for content_id, name, content, content_type in inline_images
        ]
    if msg_attachments:
        message["attachments"] = msg_attachments

    try:
        token = _acquire_token()
        resp = httpx.post(
            f"{_GRAPH_BASE}/users/{settings.ms_sender}/sendMail",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": message, "saveToSentItems": True},
            timeout=30,
        )
        resp.raise_for_status()
        sb.table("email_log").update(
            {"status": "sent", "graph_message_id": resp.headers.get("request-id")}
        ).eq("id", log["id"]).execute()
        log["status"] = "sent"
    except Exception as exc:  # noqa: BLE001 — record failure, surface to caller
        sb.table("email_log").update({"status": "failed", "error": str(exc)}).eq(
            "id", log["id"]
        ).execute()
        raise

    return log

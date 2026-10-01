"""Microsoft Graph email — send-as the shared mailbox bids@g3electrical.com.

Uses the MSAL client-credentials (application) flow with the admin-consented
application permission `Mail.Send`. Application permissions are TENANT-WIDE:
no Exchange ApplicationAccessPolicy is configured (verified 2026-07-12), so
this app can act on any mailbox — adding one scoped to the mailboxes BDR
actually uses (bids@ + the ingestion mailbox) would be a worthwhile
least-privilege hardening if the client secret ever leaks.

Every send is recorded in `email_log`. For large attachments, prefer including
short-TTL signed download links in the body over inlining bytes.

Test mode (docs/RFP_TESTING.md 6): while an RFP test session is active
EVERY send from this server, `send_mail` and `send_draft` alike, is
rewritten by `rfp_test.redirect` to go to the session's redirect address as
plain text under a header naming the intended recipients; with the address
empty the send is refused (fail closed). Inert while RFP_TESTING_ENABLED is
false: `rfp_test.active_session` answers None without a query.
"""

import base64
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import msal

from app.core.config import get_settings
from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Streamed calls (attachment $value downloads): a fresh connection per call;
# read/write cover a single socket operation, not the whole body.
_STREAM_TIMEOUT = httpx.Timeout(connect=10, read=60, write=60, pool=30)

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
    prefer: str | None = None,
) -> httpx.Response:
    """Authenticated Graph call. Always asks for immutable message ids so the
    ids we store at draft time survive the move to Sent Items after send.

    `follow_redirects` is needed for endpoints that 302 to a pre-authenticated
    download URL (e.g. `/content?format=pdf`); httpx strips the Authorization
    header on cross-host redirects, so following is safe.

    `prefer` appends an extra Prefer value (comma-joined is valid HTTP), e.g.
    'outlook.body-content-type="text"' to get plain-text message bodies.
    """
    prefer_header = 'IdType="ImmutableId"'
    if prefer:
        prefer_header += f", {prefer}"
    resp = httpx.request(
        method,
        f"{_GRAPH_BASE}{path}",
        headers={
            "Authorization": f"Bearer {_acquire_token()}",
            "Prefer": prefer_header,
        },
        json=json,
        params=params,
        timeout=timeout,
        follow_redirects=follow_redirects,
    )
    resp.raise_for_status()
    return resp


def _graph_client(timeout: httpx.Timeout) -> httpx.Client:
    """Short-lived client for one streamed Graph call. Redirects are never
    followed: a streamed `$value` download must land on Graph itself, and a
    redirect would carry the tenant token somewhere we did not choose. Tests
    replace this factory with a MockTransport-backed client."""
    return httpx.Client(timeout=timeout, follow_redirects=False)


@contextmanager
def graph_stream(
    method: str,
    path: str,
    *,
    timeout: httpx.Timeout = _STREAM_TIMEOUT,
    prefer: str | None = None,
) -> Iterator[httpx.Response]:
    """Streaming sibling of `graph_request`: same Authorization and
    `Prefer: IdType="ImmutableId"` headers, yields the response from
    `client.stream` so the caller can consume the body in chunks
    (`resp.iter_bytes`) under its own byte cap instead of buffering it.

    Unlike `graph_request` redirects are NEVER followed; a 3xx raises
    `httpx.HTTPStatusError` like any other non-2xx (fail closed: the caller
    inspects `exc.response.status_code` to map 404/410). The response and the
    connection are closed on exit, whether the body was fully read or not.
    """
    prefer_header = 'IdType="ImmutableId"'
    if prefer:
        prefer_header += f", {prefer}"
    headers = {
        "Authorization": f"Bearer {_acquire_token()}",
        "Prefer": prefer_header,
    }
    with _graph_client(timeout) as client:
        with client.stream(method, f"{_GRAPH_BASE}{path}", headers=headers) as resp:
            # Raises for 3xx too (redirects are refused, see above).
            resp.raise_for_status()
            yield resp


# ── Draft flow (used for RFQs: lets us capture the conversationId) ──────────

_INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024  # Graph inline fileAttachment cap
_UPLOAD_CHUNK = 10 * 320 * 1024             # upload-session chunks: 320 KiB multiples


def create_draft(
    to_addr: str | list[str],
    subject: str,
    body: str,
    *,
    html: bool = False,
    sender: str | None = None,
    cc: list[str] | None = None,
) -> dict:
    """Create a draft in the sender mailbox; returns the Graph message resource
    including `id`, `conversationId` and `internetMessageId`.

    `sender` overrides the default mailbox (settings.ms_sender = bids@). Submittal
    requests pass the ingestion mailbox so their replies thread back through the
    email-ingestion pipeline; RFQ callers omit it and keep bids@.

    `to_addr` accepts a single address (the vendor-facing senders, one email per
    contact) or a list (GC-facing submittal approval packages, which go out as
    one email to several people). `cc` adds copied recipients — they receive the
    same message rather than a separate one, so the GC's thread stays single."""
    sender = sender or get_settings().ms_sender
    to_list = [to_addr] if isinstance(to_addr, str) else list(to_addr)
    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML" if html else "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to_list],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    resp = graph_request("POST", f"/users/{sender}/messages", json=message)
    return resp.json()


def add_attachment(
    message_id: str,
    name: str,
    content: bytes,
    content_type: str,
    *,
    content_id: str | None = None,
    sender: str | None = None,
) -> None:
    """Attach a file to a draft. Small files inline; large ones via upload session.

    `content_id` marks the file as an inline image referenced from an HTML body
    via `<img src="cid:...">` (small attachments only — body images never come
    near the upload-session threshold). `sender` must match the mailbox the draft
    was created in.
    """
    sender = sender or get_settings().ms_sender
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


# What the redirect reads off a draft before it goes: the fields it rewrites
# and the attachment listing (names and sizes) for the header.
_DRAFT_SELECT = "id,subject,body,toRecipients,ccRecipients,bccRecipients"
_DRAFT_EXPAND = "attachments($select=id,name,size,isInline,contentType)"


def _active_test_session(sb=None):
    """The active RFP test session, or None. Free while the bench is off (no
    client, no query). Imported here: rfp_test imports this module for
    `graph_request`."""
    if not get_settings().rfp_testing_enabled:
        return None
    from app.services import rfp_test

    return rfp_test.active_session(sb or get_supabase())


def _redirect_draft(
    message_id: str, *, sender: str, sb, session: dict, project_id: str | None,
    rfq_id: str | None,
):
    """Section 6 for a draft: GET the draft, rewrite the fields through
    `rfp_test.redirect`, PATCH them back, drop the inline attachments (the
    body is text now). The draft's id and conversationId stay valid, so the
    caller's threading (rfq_sends) is unchanged. Returns the Redirected
    record for the event."""
    from app.services import rfp_test

    draft = graph_request(
        "GET",
        f"/users/{sender}/messages/{message_id}",
        params={"$select": _DRAFT_SELECT, "$expand": _DRAFT_EXPAND},
    ).json()
    message = {
        "subject": draft.get("subject"),
        "body": draft.get("body") or {},
        "toRecipients": draft.get("toRecipients") or [],
        "ccRecipients": draft.get("ccRecipients") or [],
        "bccRecipients": draft.get("bccRecipients") or [],
        "attachments": draft.get("attachments") or [],
    }
    inline_ids = [a.get("id") for a in message["attachments"] if a.get("isInline") and a.get("id")]
    redirected = rfp_test.redirect(
        message, sb=sb, session=session, sender_mailbox=sender,
        context={"project_id": project_id, "rfq_id": rfq_id},
    )
    graph_request(
        "PATCH",
        f"/users/{sender}/messages/{message_id}",
        json={
            "subject": message["subject"],
            "body": message["body"],
            "toRecipients": message["toRecipients"],
            "ccRecipients": message["ccRecipients"],
            "bccRecipients": message["bccRecipients"],
        },
    )
    for att_id in inline_ids:
        try:
            graph_request("DELETE", f"/users/{sender}/messages/{message_id}/attachments/{att_id}")
        except Exception:  # noqa: BLE001 - a leftover logo is harmless in a text body
            logger.warning("test redirect: inline attachment %s not removed", att_id, exc_info=True)
    return redirected


def send_draft(
    message_id: str, *, sender: str | None = None, project_id: str | None = None,
    rfq_id: str | None = None,
) -> None:
    """Send a draft. `project_id` / `rfq_id` only label the test bench's
    redirect event (docs/RFP_TESTING.md 6); callers that have them pass them."""
    sender = sender or get_settings().ms_sender
    session = _active_test_session()
    sb = get_supabase() if session is not None else None
    redirected = None
    if session is not None:
        # Refuses (RedirectRefused) before anything is sent when the
        # session's redirect address is empty: fail closed.
        redirected = _redirect_draft(
            message_id, sender=sender, sb=sb, session=session, project_id=project_id,
            rfq_id=rfq_id,
        )
    try:
        graph_request("POST", f"/users/{sender}/messages/{message_id}/send")
    except Exception as exc:
        if redirected is not None:
            from app.services import rfp_test

            rfp_test.record_redirected(
                sb, session, redirected, email_log_id=None, status="failed", error=str(exc)
            )
        raise
    if redirected is not None:
        from app.services import rfp_test

        rfp_test.record_redirected(sb, session, redirected, email_log_id=None, status="sent")


def create_reply_all_draft(message_id: str, *, sender: str | None = None) -> dict:
    """Create a reply-all draft on an existing message; returns the draft's
    Graph message resource including `id`, `conversationId`,
    `internetMessageId`, the recipients, the "RE: ..." subject, and a `body`
    carrying the quoted thread history.

    Works on the id stored at the original send: graph_request always asks for
    immutable ids, so `rfq_sends.graph_message_id` stays valid after the sent
    message moved to Sent Items."""
    sender = sender or get_settings().ms_sender
    resp = graph_request(
        "POST", f"/users/{sender}/messages/{message_id}/createReplyAll"
    )
    return resp.json()


def update_message_body(
    message_id: str, html: str, *, cc: list[str] | None = None, sender: str | None = None
) -> None:
    """Replace a draft's body with HTML content - used to swap a reply draft's
    plain body for the branded shell (with the quoted trail spliced back in by
    the caller) before sending it.

    `cc` (when given) replaces the draft's whole CC line in the same PATCH:
    createReplyAll strips the replying mailbox's own address from the
    recipients it builds, so a caller that needs the mailbox copied back onto
    its own reply (RFQ nudges) rewrites the full CC list here. None leaves the
    draft's CC untouched."""
    sender = sender or get_settings().ms_sender
    payload: dict = {"body": {"contentType": "HTML", "content": html}}
    if cc is not None:
        payload["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    graph_request(
        "PATCH",
        f"/users/{sender}/messages/{message_id}",
        json=payload,
    )


# ── OneDrive (fallback when drawings are too large to attach) ────────────────


def drive_upload(path: str, content: bytes, *, sender: str | None = None) -> str:
    """Upload a file to a OneDrive via an upload session; returns the drive
    item id. `path` is relative to the drive root, e.g. 'BDR/123/drawings/a.pdf'.

    The drive owner is MS_DRIVE_OWNER when configured - it overrides even an
    explicit `sender`, because the send mailbox (a shared mailbox) has no
    OneDrive at all and drive calls against it 404. All three drive_* helpers
    resolve the owner the same way so an upload, its item lookup, and its share
    link always land on the same drive."""
    sender = get_settings().ms_drive_owner or sender or get_settings().ms_sender
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


def drive_get_item_id(path: str, *, sender: str | None = None) -> str:
    sender = get_settings().ms_drive_owner or sender or get_settings().ms_sender
    return graph_request("GET", f"/users/{sender}/drive/root:/{path}").json()["id"]


def drive_create_link(item_id: str, *, sender: str | None = None) -> str:
    """Anonymous view link (vendors are external). Requires 'Anyone' sharing links
    to be enabled in the SharePoint admin center.

    The link is given an expiry (settings.rfq_drawings_link_ttl_days) so a
    confidential drawing isn't reachable forever by anyone who ever held the URL.
    Graph clamps this to the tenant's anonymous-link max-expiry policy; if the
    tenant rejects the parameter the call raises rather than silently minting a
    permanent link."""
    from datetime import datetime, timedelta, timezone

    settings = get_settings()
    sender = settings.ms_drive_owner or sender or settings.ms_sender
    expiry = (
        datetime.now(timezone.utc) + timedelta(days=settings.rfq_drawings_link_ttl_days)
    ).isoformat()
    resp = graph_request(
        "POST",
        f"/users/{sender}/drive/items/{item_id}/createLink",
        json={"type": "view", "scope": "anonymous", "expirationDateTime": expiry},
    )
    return resp.json()["link"]["webUrl"]


def send_mail(
    *,
    to: list[str],
    subject: str,
    body_html: str,
    cc: list[str] | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
    inline_images: list[tuple[str, str, bytes, str]] | None = None,
    project_id: str | None = None,
    rfq_id: str | None = None,
    sent_by: str | None = None,
    importance: str | None = None,
) -> dict:
    """Send an email from the shared mailbox and record it in email_log.

    `attachments` is a list of (filename, content_bytes). `inline_images` is a
    list of (content_id, filename, content_bytes, content_type) for images the
    HTML body references via `<img src="cid:content_id">` (e.g. the G3 logo).
    `cc` is a list of plain addresses copied on the same message (same shape as
    `create_draft`). `importance` maps to Graph's message importance
    ("low"/"normal"/"high") — omit for normal mail; "high" shows the red "!"
    marker in Outlook. Returns the email_log row.
    """
    settings = get_settings()
    sb = get_supabase()
    session = _active_test_session(sb)

    # to_addrs records the To line ONLY, never the CC. proposal_send proves a
    # crashed send actually delivered by comparing proposal_sends.gc_email to
    # this string for exact equality (see its join_recipients docstring), so
    # folding CC addresses in here would break crash recovery. In test mode
    # the row keeps the INTENDED To line and says where it really went.
    log_row = {
        "to_addrs": ", ".join(to),
        "subject": subject,
        "body": body_html,
        "status": "queued",
        "project_id": project_id,
        "rfq_id": rfq_id,
        "sent_by": sent_by,
    }
    if session is not None:
        log_row["test_session_id"] = session.get("id")
        log_row["redirected_to"] = (session.get("redirect_to") or "").strip() or None
    log = sb.table("email_log").insert(log_row).execute().data[0]

    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    if importance:
        message["importance"] = importance
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

    redirected = None
    try:
        if session is not None:
            # Test mode: the assembled message goes to the redirect address
            # as text (docs/RFP_TESTING.md 6); an empty address refuses the
            # send (RedirectRefused) and the log row records the refusal.
            from app.services import rfp_test

            redirected = rfp_test.redirect(
                message, sb=sb, session=session, sender_mailbox=settings.ms_sender,
                context={"project_id": project_id, "rfq_id": rfq_id},
            )
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
        if redirected is not None:
            from app.services import rfp_test

            rfp_test.record_redirected(
                sb, session, redirected, email_log_id=log["id"], status="failed", error=str(exc)
            )
        raise
    if redirected is not None:
        from app.services import rfp_test

        rfp_test.record_redirected(sb, session, redirected, email_log_id=log["id"], status="sent")

    return log

"""RFQ vendor nudges - reminder emails on an existing RFQ send's Graph thread.

A nudge is a reply-all on the rfq_send's original message: createReplyAll
keeps the "RE: ..." subject, the recipients and the quoted thread history, and
the reminder text goes out wrapped in the branded HTML shell like every other
outbound vendor email, with the history preserved below it. createReplyAll
strips the replying mailbox's own address from the recipients it builds, so
the bids-desk CC the original send carried (rfq_sending.internal_cc) is put
back onto the draft before it goes out. Every nudge is recorded in rfq_nudges
BEFORE its draft is sent: that CC lands our own nudge back in the watched
inbox, and the row's internet_message_id is what lets the poller recognize it
and refuse to ingest it as a vendor quote reply (see
rfq_inbox._ingest_message).

Also home to the recipients/reply-status read behind the nudge picker: one row
per vendor CONTACT (re-sends collapse onto the latest send) stating whether
they quoted, replied without a quote, or never answered. A contact who replied
without a quote still counts as "hasn't quoted" for nudging - a user-confirmed
product decision, so both individual nudges and Nudge All include them.
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone

from app.services import email_branding, graph_email
from app.services.notifications import audit
from app.services.rfq_sending import internal_cc

logger = logging.getLogger(__name__)

# States a nudge may be sent in. Everything else is either already answered
# (quote_received) or has no thread to nudge on yet (failed / queued).
NUDGEABLE_STATES = ("replied_no_quote", "no_reply")

# A pending claim row older than this is presumed abandoned by a crashed run
# and may be reclaimed (_reclaim_pending); a younger one is a nudge genuinely
# in flight and blocks the send instead of double-emailing the vendor.
_PENDING_STALE_MINUTES = 10

# Header-band label on the branded shell (render_vendor_email subtitle).
_NUDGE_SUBTITLE = "QUOTE REMINDER"

# The quoted-thread half of a createReplyAll draft body: Graph hands the draft
# back as a full HTML document, and only its <body> content gets carried into
# the branded shell.
_HTML_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)


def _send_sort_key(send: dict) -> str:
    """"Latest" for a contact's re-sends: sent_at, falling back to created_at
    for rows that never went out (pending/failed)."""
    return send.get("sent_at") or send.get("created_at") or ""


def _recipient_addresses(recipients: list[dict] | None) -> list[str]:
    """Plain addresses out of a Graph recipients list, empties dropped."""
    addrs = [(r.get("emailAddress") or {}).get("address") for r in recipients or []]
    return [a for a in addrs if a]


def recipients_status(sb, rfq_id: str) -> list[dict]:
    """One status row per vendor CONTACT this RFQ was sent to.

    A contact can hold several rfq_sends rows (re-sends create new rows; there
    is no uniqueness on rfq_id + vendor_contact_id), so they collapse onto the
    latest one. The state ladder: a quote anywhere wins - either a send marked
    answered or a quotes row against the contact's vendor company, so a
    coworker's quote through a different contact counts and General Material's
    estimate row never does; then any inbound reply; then the latest send's
    own failed/pending; else no reply yet.
    """
    sends = (
        sb.table("rfq_sends")
        .select(
            "id, vendor_contact_id, cc_recipients, graph_message_id, status, "
            "error, quote_received_at, sent_at, created_at, "
            "vendor_contacts(id, name, email, vendor_id, vendors(name))"
        )
        .eq("rfq_id", rfq_id)
        .execute()
    ).data or []
    if not sends:
        return []

    send_ids = [s["id"] for s in sends]
    messages = (
        sb.table("rfq_messages")
        .select("rfq_send_id, received_at")
        .in_("rfq_send_id", send_ids)
        .execute()
    ).data or []
    quote_rows = (
        sb.table("quotes")
        .select("vendor_id")
        .eq("rfq_id", rfq_id)
        .neq("origin", "estimate")
        .execute()
    ).data or []
    quoted_vendor_ids = {q["vendor_id"] for q in quote_rows if q.get("vendor_id")}
    nudge_rows = (
        sb.table("rfq_nudges")
        .select("rfq_send_id, created_at, sent_at, status, error")
        .in_("rfq_send_id", send_ids)
        .execute()
    ).data or []

    by_contact: dict[str, list[dict]] = {}
    for s in sends:
        by_contact.setdefault(s["vendor_contact_id"], []).append(s)

    out: list[dict] = []
    for contact_id, contact_sends in by_contact.items():
        latest = max(contact_sends, key=_send_sort_key)
        contact = latest.get("vendor_contacts") or {}
        ids = {s["id"] for s in contact_sends}
        received = [
            m["received_at"]
            for m in messages
            if m["rfq_send_id"] in ids and m.get("received_at")
        ]
        has_reply = any(m["rfq_send_id"] in ids for m in messages)
        has_quote = any(s.get("quote_received_at") for s in contact_sends) or (
            contact.get("vendor_id") in quoted_vendor_ids
        )
        if has_quote:
            state = "quote_received"
        elif has_reply:
            state = "replied_no_quote"
        elif latest.get("status") == "failed":
            state = "failed"
        elif latest.get("status") == "pending":
            state = "queued"
        else:
            state = "no_reply"

        # The send a nudge replies on: the latest one that actually went out
        # and kept its Graph id. A contact with none falls back to the latest
        # row (and is never nudgeable).
        sent_with_graph = [
            s
            for s in contact_sends
            if s.get("status") == "sent" and s.get("graph_message_id")
        ]
        nudge_target = (
            max(sent_with_graph, key=_send_sort_key) if sent_with_graph else latest
        )

        contact_nudges = [n for n in nudge_rows if n["rfq_send_id"] in ids]
        last = (
            max(contact_nudges, key=lambda n: n.get("created_at") or "")
            if contact_nudges
            else None
        )
        out.append(
            {
                "rfq_send_id": nudge_target["id"],
                "vendor_contact_id": contact_id,
                "contact_name": contact.get("name"),
                "contact_email": contact.get("email"),
                "vendor_name": (contact.get("vendors") or {}).get("name"),
                "cc_recipients": latest.get("cc_recipients"),
                "sent_at": latest.get("sent_at"),
                "send_status": latest.get("status"),
                "send_error": latest.get("error"),
                "state": state,
                "replied_at": max(received) if received else None,
                "nudgeable": state in NUDGEABLE_STATES and bool(sent_with_graph),
                "last_nudge": {
                    "created_at": last["created_at"],
                    "sent_at": last.get("sent_at"),
                    "status": last.get("status"),
                    "error": last.get("error"),
                }
                if last
                else None,
            }
        )
    out.sort(
        key=lambda r: ((r["vendor_name"] or "").lower(), (r["contact_name"] or "").lower())
    )
    return out


def _splice_quoted_history(branded_html: str, draft_body_html: str) -> str:
    """The branded nudge card first, then the quoted thread history the
    createReplyAll draft carried - exactly where a reply's trail normally
    sits, so the vendor sees which RFQ this reminds them about."""
    quoted = draft_body_html or ""
    match = _HTML_BODY_RE.search(quoted)
    if match:
        quoted = match.group(1)
    quoted = quoted.strip()
    if not quoted:
        return branded_html
    idx = branded_html.rfind("</body>")
    if idx == -1:
        return branded_html + quoted
    return branded_html[:idx] + quoted + branded_html[idx:]


def _contact_has_quote(sb, rfq_id: str, send: dict) -> bool:
    """Re-check right before emailing: a quote that arrived after the
    recipients panel was loaded must turn the nudge into a skip, not a
    reminder about a quote we already hold. Same definition as the
    quote_received state above: any of the contact's sends marked answered,
    or a non-estimate quotes row against their vendor company."""
    answered = (
        sb.table("rfq_sends")
        .select("id")
        .eq("rfq_id", rfq_id)
        .eq("vendor_contact_id", send["vendor_contact_id"])
        .not_.is_("quote_received_at", "null")
        .limit(1)
        .execute()
    ).data
    if answered:
        return True
    vendor_id = (send.get("vendor_contacts") or {}).get("vendor_id")
    if not vendor_id:
        return False
    quotes = (
        sb.table("quotes")
        .select("id")
        .eq("rfq_id", rfq_id)
        .eq("vendor_id", vendor_id)
        .neq("origin", "estimate")
        .limit(1)
        .execute()
    ).data
    return bool(quotes)


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text


def _pending_is_stale(row: dict) -> bool:
    stamp_text = row.get("created_at") or ""
    try:
        stamp = datetime.fromisoformat(stamp_text.replace("Z", "+00:00"))
    except ValueError:
        return False  # unreadable stamp: assume genuinely in flight
    return datetime.now(timezone.utc) - stamp > timedelta(minutes=_PENDING_STALE_MINUTES)


def _reclaim_pending(sb, rfq_send_id: str, claim: dict) -> dict | None:
    """The one-pending-per-send index just rejected our claim. A pending row
    younger than _PENDING_STALE_MINUTES is a nudge genuinely in flight, so
    back off. An older one was left behind by a crashed run (rare, since the
    sent path stamps its row best-effort): mark it failed as 'abandoned' and
    retry the claim once. Returns the claimed row, or None when the send is
    genuinely locked."""
    pending = (
        sb.table("rfq_nudges")
        .select("id, created_at")
        .eq("rfq_send_id", rfq_send_id)
        .eq("status", "pending")
        .execute()
    ).data or []
    if any(not _pending_is_stale(row) for row in pending):
        return None
    for row in pending:
        sb.table("rfq_nudges").update({"status": "failed", "error": "abandoned"}).eq(
            "id", row["id"]
        ).eq("status", "pending").execute()
    try:
        return (sb.table("rfq_nudges").insert(claim).execute()).data[0]
    except Exception as exc:  # noqa: BLE001 - lost the retry to another racer
        if _is_unique_violation(exc):
            return None
        raise


def send_nudges(
    sb, project_id: str, rfq_id: str, targets: list[dict], user_id: str
) -> list[dict]:
    """Send one nudge per target, sequentially with the same 1s pacing as
    bulk_send (Exchange throttles ~30 messages/min per mailbox). Failures are
    per-target: one bad send never aborts the batch.

    `targets` = [{"rfq_send_id": ..., "message": ...}]; `message` is the FINAL
    plain text (the frontend already substituted the template tokens).
    """
    results: list[dict] = []
    first = True
    for target in targets:
        if not first:
            time.sleep(1)
        first = False
        try:
            results.append(
                _nudge_one(
                    sb, project_id, rfq_id, target["rfq_send_id"], target["message"], user_id
                )
            )
        except Exception as exc:  # noqa: BLE001 - per-target isolation
            logger.exception("RFQ nudge failed for send %s", target.get("rfq_send_id"))
            results.append(
                {
                    "rfq_send_id": target.get("rfq_send_id"),
                    "nudge_id": None,
                    "status": "failed",
                    "error": str(exc),
                }
            )
    return results


def _nudge_one(
    sb, project_id: str, rfq_id: str, rfq_send_id: str, message: str, user_id: str
) -> dict:
    def _failed(error: str, nudge_id: str | None = None) -> dict:
        return {
            "rfq_send_id": rfq_send_id,
            "nudge_id": nudge_id,
            "status": "failed",
            "error": error,
        }

    rows = (
        sb.table("rfq_sends")
        .select(
            "id, rfq_id, vendor_contact_id, graph_message_id, status, "
            "vendor_contacts(id, name, email, vendor_id), rfqs(project_id)"
        )
        .eq("id", rfq_send_id)
        .execute()
    ).data or []
    if (
        not rows
        or rows[0].get("rfq_id") != rfq_id
        or ((rows[0].get("rfqs") or {}).get("project_id")) != project_id
    ):
        return _failed("Send not found on this RFQ")
    send = rows[0]
    if send.get("status") != "sent" or not send.get("graph_message_id"):
        return _failed("This RFQ email never went out, so there is no thread to nudge on")

    if _contact_has_quote(sb, rfq_id, send):
        return {
            "rfq_send_id": rfq_send_id,
            "nudge_id": None,
            "status": "skipped_quote_received",
            "error": None,
        }

    contact = send.get("vendor_contacts") or {}
    try:
        # Reply-all on the original send: recipients and the "RE: ..." subject
        # stay whatever the thread already has - never rewritten - and the
        # draft body carries the quoted history the branded card sits on top of.
        draft = graph_email.create_reply_all_draft(send["graph_message_id"])
    except Exception as exc:  # noqa: BLE001 - per-target isolation
        logger.exception("createReplyAll failed for rfq_send %s", rfq_send_id)
        return _failed(str(exc))

    # createReplyAll builds the recipient lines from the thread but strips the
    # replying mailbox's own address, so the bids-desk CC the original RFQ
    # carried never survives onto the nudge. Put it back (rewritten with the
    # body PATCH below): the desk sees the reminder on the vendor's thread,
    # and the copy landing in the watched inbox is what the ingestion guard
    # expects to recognize.
    to_addrs = _recipient_addresses(draft.get("toRecipients"))
    cc_addrs = _recipient_addresses(draft.get("ccRecipients"))
    desk_cc = internal_cc([*to_addrs, *cc_addrs])
    cc_addrs = [*cc_addrs, *desk_cc]

    # The claim row goes in BEFORE the send: bids@ is CC'd on the thread, so
    # our own nudge lands back in the watched inbox, and the poller's guard
    # (rfq_inbox._ingest_message) matches on this internet_message_id. Insert
    # first or the nudge races the poller and gets ingested as a vendor reply.
    # The insert is also the concurrency lock (0104's insert-as-lock pattern):
    # rfq_nudges_one_pending_per_send admits one pending claim per send, so
    # two racing batches cannot double-email the same vendor.
    claim = {
        "rfq_send_id": rfq_send_id,
        "sent_by": user_id,
        "message": message,
        "status": "pending",
        "graph_message_id": draft.get("id"),
        "internet_message_id": draft.get("internetMessageId"),
    }
    try:
        nudge = (sb.table("rfq_nudges").insert(claim).execute()).data[0]
    except Exception as exc:  # noqa: BLE001 - the unique index rejecting a racer
        if not _is_unique_violation(exc):
            raise
        nudge = _reclaim_pending(sb, rfq_send_id, claim)
        if nudge is None:
            return _failed("A nudge for this vendor is already in progress")

    try:
        body_html = _splice_quoted_history(
            email_branding.render_vendor_email(message, subtitle=_NUDGE_SUBTITLE),
            (draft.get("body") or {}).get("content") or "",
        )
        graph_email.update_message_body(
            draft["id"], body_html, cc=cc_addrs if desk_cc else None
        )
        graph_email.add_attachment(
            draft["id"],
            email_branding.LOGO_FILENAME,
            email_branding.logo_bytes(),
            "image/jpeg",
            content_id=email_branding.LOGO_CONTENT_ID,
        )
        graph_email.send_draft(draft["id"])
    except Exception as exc:  # noqa: BLE001 - record and continue with the batch
        logger.exception("RFQ nudge send failed for rfq_send %s", rfq_send_id)
        try:
            sb.table("rfq_nudges").update({"status": "failed", "error": str(exc)}).eq(
                "id", nudge["id"]
            ).execute()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record failed rfq_nudge")
        audit(
            user_id,
            "rfq.nudge",
            "rfq_nudge",
            nudge["id"],
            {
                "rfq_send_id": rfq_send_id,
                "to": contact.get("email"),
                "status": "failed",
                "error": str(exc),
            },
        )
        return _failed(str(exc), nudge_id=nudge["id"])

    # Ledger + bookkeeping, mirroring _send_one: record where the reminder
    # went (the draft's recipient lines plus the re-added desk CC) and stamp
    # the row sent. From here on the vendor already HAS the email, so every
    # step is best-effort: a DB blip must never surface as 'failed', or the
    # user retries a nudge that went out and double-emails the vendor.
    addrs = [*to_addrs, *cc_addrs] or (
        [contact["email"]] if contact.get("email") else []
    )
    stamp = {"status": "sent", "sent_at": "now()"}
    try:
        log = (
            sb.table("email_log")
            .insert(
                {
                    "to_addrs": ", ".join(addrs),
                    "subject": draft.get("subject"),
                    "body": message,
                    "status": "sent",
                    "graph_message_id": draft.get("id"),
                    "project_id": project_id,
                    "rfq_id": rfq_id,
                    "sent_by": user_id,
                }
            )
            .execute()
        ).data[0]
        stamp["email_log_id"] = log["id"]
    except Exception:  # noqa: BLE001 - ledger only; the nudge is already out
        logger.exception("Failed to record email_log for rfq_nudge %s", nudge["id"])
    try:
        sb.table("rfq_nudges").update(stamp).eq("id", nudge["id"]).execute()
    except Exception:  # noqa: BLE001 - row stays pending until reclaimed
        logger.exception("Failed to mark rfq_nudge %s sent", nudge["id"])
    try:
        audit(
            user_id,
            "rfq.nudge",
            "rfq_nudge",
            nudge["id"],
            {
                "rfq_send_id": rfq_send_id,
                "to": contact.get("email"),
                "internet_message_id": draft.get("internetMessageId"),
                "status": "sent",
            },
        )
    except Exception:  # noqa: BLE001 - audit is best-effort post-send
        logger.exception("Failed to audit rfq_nudge %s", nudge["id"])
    return {
        "rfq_send_id": rfq_send_id,
        "nudge_id": nudge["id"],
        "status": "sent",
        "error": None,
    }

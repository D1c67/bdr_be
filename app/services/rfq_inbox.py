"""Inbound RFQ reply ingestion.

Two entry points, one downstream path:

* `poll_once()` (background loop) watches the bids@ inbox via Graph DELTA
  queries while any RFQ send is still "active" (sent, no quote yet, younger
  than the polling window). It reads and advances one global cursor.
* `check_project_quotes()` ("Check for quotes now") targets ONE project's
  Graph CONVERSATIONS by id. It never touches the delta cursor.

Either way, a message whose conversation matches an RFQ send is stored, its
file attachments and cloud-share links saved as quote files, and its PDFs run
through the extractor to pull the quoted price, which creates a `quotes` row.
That row is a candidate, not a price: it has to be approved on Receive Quotes
and then picked as the winner on Select Vendors before it prices anything.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import (
    cloud_links,
    graph_email,
    graph_inbox,
    office_preview,
    storage,
)
from app.services.notifications import audit, notify_role
from app.services.openai_text import extract_quote_from_pdf

logger = logging.getLogger(__name__)

_LEASE_KEY = "inbox"  # row id prefix in graph_sync_state
_RENEW_EVERY = 20     # messages between lease renewals during a batch

# Per-process lease holder token. Without it a runner cannot tell its OWN live
# lease from a rival's and stands down on the next tick — which silently halved
# the poll rate to one real poll per 2 × RFQ_POLL_INTERVAL_SECONDS.
_RUNNER_TOKEN = uuid.uuid4().hex

# Only these extensions are ingested as quote files from an inbound reply — an
# allowlist so a hostile reply can't drop arbitrary content into project storage.
_QUOTE_ATTACHMENT_EXTS = {
    "pdf", "xlsx", "xls", "xlsm", "csv", "docx", "doc", "png", "jpg", "jpeg",
}
# Extracted-quote sanity band. A vendor-controlled PDF is fed to an LLM, so a
# prompt-injected or garbled amount must not silently land in the candidate list
# a human then picks the winner from (a nonsense figure sitting next to real
# quotes is exactly the kind of thing that gets clicked by mistake).
_QUOTE_MAX_AMOUNT = Decimal("100000000")   # $100M ceiling
_QUOTE_MIN_CONFIDENCE = 0.5


def _valid_extracted_amount(result: dict) -> bool:
    """A quote may auto-create only if the extracted amount is a finite, positive,
    in-band number and the model's confidence (when reported) clears the bar."""
    try:
        amount = Decimal(str(result.get("total_amount")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not amount.is_finite() or amount <= 0 or amount > _QUOTE_MAX_AMOUNT:
        return False
    conf = result.get("confidence")
    if conf is not None:
        try:
            if float(conf) < _QUOTE_MIN_CONFIDENCE:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _pipeline_tick() -> None:
    """One poll tick with LLM calls tagged as pipeline tier: quote extractions
    ride the background lane of the concurrency gate (and the call log) rather
    than the reserved interactive slots."""
    from app.services import llm_gate

    with llm_gate.tier(llm_gate.TIER_PIPELINE):
        poll_once()


async def polling_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            await asyncio.to_thread(_pipeline_tick)
        except Exception:  # noqa: BLE001 — the loop must survive any tick failure
            logger.exception("RFQ inbox poll failed")
        # Sleep the REMAINDER of the interval, not a full one: a slow tick
        # (attachment downloads, preview conversion, PDF extraction all run
        # inline) would otherwise push every later poll out by its duration.
        interval = get_settings().rfq_poll_interval_seconds
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_until() -> str:
    """Crash bound only — a healthy tick releases the lease on its way out, so
    this TTL is what frees a lease orphaned by a killed worker, nothing more."""
    return (
        _now() + timedelta(seconds=2 * get_settings().rfq_poll_interval_seconds)
    ).isoformat()


def _acquire_lease(sb, key: str) -> bool:
    """Fenced single-runner lease. Acquire when the lease is free, expired, or
    already ours. The conditional UPDATE closes the theft window the previous
    read-then-write check left open; the unique pk makes the insert race safe."""
    now_iso = _now().isoformat()
    rows = (sb.table("graph_sync_state").select("id").eq("id", key).execute()).data
    if not rows:
        try:
            sb.table("graph_sync_state").insert(
                {"id": key, "lease_until": _lease_until(), "holder": _RUNNER_TOKEN}
            ).execute()
            return True
        except Exception as exc:  # noqa: BLE001
            if "23505" in str(exc).lower() or "duplicate key" in str(exc).lower():
                return False  # another runner created it first
            raise
    resp = (
        sb.table("graph_sync_state")
        .update(
            {"lease_until": _lease_until(), "holder": _RUNNER_TOKEN,
             "updated_at": now_iso}
        )
        .eq("id", key)
        .or_(f"lease_until.is.null,lease_until.lt.{now_iso},holder.eq.{_RUNNER_TOKEN}")
        .execute()
    )
    return bool(resp.data)


def _renew_lease(sb, key: str) -> bool:
    """Extend our own lease mid-batch; fails closed if another runner holds it."""
    resp = (
        sb.table("graph_sync_state")
        .update({"lease_until": _lease_until(), "updated_at": _now().isoformat()})
        .eq("id", key)
        .eq("holder", _RUNNER_TOKEN)
        .execute()
    )
    if not resp.data:
        logger.warning("RFQ inbox lease %s lost mid-tick; aborting", key)
        return False
    return True


def _release_lease(sb, key: str, delta_link: str | None) -> None:
    """Hand the lease back so the NEXT tick can poll, and advance the delta
    cursor when the batch completed. Holding the lease to its TTL is what made
    the poller skip every other tick. Fenced on holder: a runner that lost the
    lease mid-tick writes nothing, so it can neither free a rival's lease nor
    commit a cursor for a batch it did not finish."""
    payload: dict = {"lease_until": None, "updated_at": _now().isoformat()}
    if delta_link is not None:
        payload["delta_link"] = delta_link
    sb.table("graph_sync_state").update(payload).eq("id", key).eq(
        "holder", _RUNNER_TOKEN
    ).execute()


def poll_once() -> None:
    settings = get_settings()
    sb = get_supabase()

    # 0. Expire sends past the polling window (1 week per spec).
    cutoff = (_now() - timedelta(days=settings.rfq_poll_active_days)).isoformat()
    sb.table("rfq_sends").update({"polling_active": False}).eq(
        "polling_active", True
    ).lt("sent_at", cutoff).execute()

    # 1. Active sends — skip Graph entirely when there is nothing to watch.
    active = _active_sends(sb)
    if not active:
        return

    # 2. Single-runner lease (belt-and-braces for multi-worker deployments).
    key = f"{_LEASE_KEY}:{settings.ms_sender}"
    if not _acquire_lease(sb, key):
        return
    rows = (sb.table("graph_sync_state").select("*").eq("id", key).execute()).data
    delta_link = rows[0].get("delta_link") if rows else None

    new_delta: str | None = None
    try:
        # 3. Delta sync (reset on expired tokens; re-ingest is idempotent).
        try:
            messages, new_delta = graph_inbox.delta_inbox(delta_link)
        except graph_inbox.DeltaExpired:
            messages, new_delta = graph_inbox.delta_inbox(None)

        # 4. Match + ingest, each message isolated so one failure can't stall
        #    the rest. Ingest is network-heavy (Graph fetch, storage upload,
        #    preview conversion, PDF extraction), so renew as we go rather than
        #    trusting one lease to cover the whole batch.
        by_conversation = {
            s["conversation_id"]: s for s in active if s.get("conversation_id")
        }
        for i, msg in enumerate(messages):
            if i and i % _RENEW_EVERY == 0 and not _renew_lease(sb, key):
                # Another runner owns the lease now. Drop the cursor so the old
                # one stands and this batch re-pulls next tick; rfq_messages
                # .graph_message_id is UNIQUE, so the re-pull cannot duplicate.
                new_delta = None
                return
            try:
                _ingest_message(sb, msg, by_conversation)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to ingest inbox message %s", msg.get("id"))
    finally:
        # 5. Release the lease and persist the delta token — the token only when
        #    the batch actually finished (new_delta is None if the delta call
        #    itself failed, in which case the old cursor must stand).
        _release_lease(sb, key, new_delta)


def _active_sends(sb) -> list[dict]:
    return (
        sb.table("rfq_sends")
        .select(
            "id, conversation_id, vendor_contact_id, rfq_id, "
            "vendor_contacts(id, name, email, vendor_id, vendors(name)), "
            "rfqs(id, project_id, material_category_id, material_categories(name), "
            "projects(id, name, number))"
        )
        .eq("polling_active", True)
        .eq("status", "sent")
        .is_("quote_received_at", "null")
        .execute()
    ).data or []


def _is_duplicate_key(exc: Exception) -> bool:
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text


def _ingest_message(
    sb,
    msg: dict,
    by_conversation: dict[str, dict],
    *,
    allow_sender_mismatch: bool = False,
    max_attachments: int | None = None,
) -> str | None:
    """Turn one Graph message into a stored reply: files, cloud links, and the
    extracted quote. Shared by the background poller and the on-demand check.

    Returns the reply's final extraction status, or None when nothing was
    ingested (our own outbound copy, a nudge we sent, no matching send, a
    refused sender, or a message already stored).

    `allow_sender_mismatch` accepts a reply that came from a different address
    than the contact we mailed. The conversation id is the match, so a forward
    or a colleague answering for the vendor still belongs to this RFQ. The
    poller leaves it off (it sweeps the whole mailbox, so the address is its
    only other signal); the project-scoped check turns it on, and the actual
    sender is recorded on the message row and audited either way.

    `max_attachments` tightens the per-message attachment cap below the
    configured inbound limit.
    """
    settings = get_settings()
    from_addr = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "")
    if not from_addr or from_addr.lower() == settings.ms_sender.lower():
        return None
    send = by_conversation.get(msg.get("conversationId"))
    if not send:
        return None

    # Belt-and-suspenders on top of the From check above: a nudge is our own
    # reply-all on this very conversation, recorded (at draft time, BEFORE the
    # send) in rfq_nudges.internet_message_id. Refuse to ingest one as a
    # vendor reply whatever address it appears to come from - this covers both
    # the poller and check_project_quotes, which funnel through here. Only
    # runs after the conversation match, so the (indexed) lookup is spent on
    # RFQ-thread messages alone; a message without internetMessageId is a
    # no-match.
    internet_message_id = msg.get("internetMessageId")
    if internet_message_id:
        nudges = (
            sb.table("rfq_nudges")
            .select("id")
            .eq("internet_message_id", internet_message_id)
            .limit(1)
            .execute()
        ).data
        if nudges:
            return None

    contact = send["vendor_contacts"]
    if from_addr.lower() != (contact.get("email") or "").lower():
        # A reply in the right conversation from an unexpected address. Always
        # leave a trace so the PE can spot forwarded replies; whether it is also
        # ingested is the caller's call (see allow_sender_mismatch above).
        audit(
            None,
            "rfq.reply_sender_mismatch",
            "rfq_send",
            send["id"],
            {
                "from": from_addr,
                "expected": contact.get("email"),
                "ingested": allow_sender_mismatch,
            },
        )
        if not allow_sender_mismatch:
            return None

    # Idempotency: the poller may see the same message again after a delta reset,
    # and the on-demand check re-reads whole conversations by design.
    existing = (
        sb.table("rfq_messages")
        .select("id")
        .eq("graph_message_id", msg["id"])
        .execute()
    ).data
    if existing:
        return None

    full = graph_inbox.get_message(msg["id"])
    try:
        row = (
            sb.table("rfq_messages")
            .insert(
                {
                    "rfq_send_id": send["id"],
                    "graph_message_id": msg["id"],
                    "from_addr": from_addr,
                    "subject": msg.get("subject"),
                    "body_preview": msg.get("bodyPreview"),
                    "body": (full.get("body") or {}).get("content"),
                    "received_at": msg.get("receivedDateTime"),
                    "has_attachments": bool(msg.get("hasAttachments")),
                }
            )
            .execute()
        ).data[0]
    except Exception as exc:  # noqa: BLE001
        if _is_duplicate_key(exc):
            # rfq_messages.graph_message_id is UNIQUE, so this insert is the real
            # claim on the message: the lookup above only saves the wasted work.
            # A rival runner (the poller, or a check on another worker) got there
            # between the two statements and now owns every downstream side
            # effect, including the paid extraction. Stand down.
            logger.info("Message %s was claimed by another runner", msg["id"])
            return None
        raise

    pdf_files: list[tuple[dict, bytes]] = []
    ref_links: list[cloud_links.CloudLink] = []
    if msg.get("hasAttachments"):
        pdf_files = _ingest_attachments(sb, send, row, max_attachments=max_attachments)
        ref_links = _reference_links(row["graph_message_id"])
    links = cloud_links.merge_links(
        ref_links,
        cloud_links.find_cloud_links((full.get("body") or {}).get("content") or ""),
    )
    if links:
        sb.table("rfq_messages").update({"cloud_link_count": len(links)}).eq(
            "id", row["id"]
        ).execute()
    link_pdfs, link_failures = _ingest_cloud_links(
        sb, send, links, rfq_message_id=row["id"]
    )
    pdf_files.extend(link_pdfs)
    status = _run_extraction(sb, send, row, pdf_files, link_failures)

    rfq = send["rfqs"]
    notify_role(
        Role.ESTIMATING_ENGINEER_MATERIALS,
        rfq["project_id"],
        "rfq.reply_received",
        f"{contact['name']} replied on the {rfq['material_categories']['name']} RFQ "
        f"for {rfq['projects']['name']}",
        rfq_id=rfq["id"],
    )
    return status


def _store_quote_file(
    sb, send: dict, filename: str, content: bytes, content_type: str | None,
    *, rfq_message_id: str | None = None, reuse_existing: bool = False,
) -> dict:
    """Store bytes as a quote file for the send's category (storage object +
    project_files row + preview), shared by the attachment and link paths.
    `rfq_message_id` records WHICH reply the file arrived on, so the reply
    review modal can show that reply's own files. `reuse_existing` (retry
    flows) returns the already-ingested row for the same filename instead of
    duplicating it."""
    rfq = send["rfqs"]
    project = rfq["projects"]
    if reuse_existing:
        # Dedupe a retry against the file the SAME link already produced. Two
        # different vendors quote one material category through the same rfq, so
        # (project, category, material_category_id, filename) is NOT vendor-unique
        # — matching on filename alone could hand back another vendor's same-named
        # "Quote.pdf" and bind THIS reply's extracted amount to an unrelated file.
        # Require an exact byte-size match too: a re-fetched link yields identical
        # bytes (so the legitimate retry still dedupes), while a different vendor's
        # file almost never matches, and an outsider cannot size theirs to a stored
        # file they can't see. On no match we fall through and store fresh below.
        existing = (
            sb.table("project_files")
            .select("id, filename, rfq_message_id")
            .eq("project_id", project["id"])
            .eq("category", "quote")
            .eq("material_category_id", rfq["material_category_id"])
            .eq("filename", filename)
            .eq("size_bytes", len(content))
            .execute()
        ).data
        if existing:
            # A pre-0105 row (or one stored by an older worker) has no reply
            # link yet; the retry that re-fetched it knows the reply, so claim
            # it. Never overwrite an existing link: byte-size dedupe can hand
            # back another reply's identical file.
            row = existing[0]
            if rfq_message_id and not row.get("rfq_message_id"):
                sb.table("project_files").update(
                    {"rfq_message_id": rfq_message_id}
                ).eq("id", row["id"]).is_("rfq_message_id", "null").execute()
            return row
    path = storage.build_object_path(project["id"], "quote", filename)
    storage.upload_file(path, content, content_type or "application/octet-stream")
    convertible = office_preview.is_convertible(filename, "quote")
    file_row = (
        sb.table("project_files")
        .insert(
            {
                "project_id": project["id"],
                "category": "quote",
                "storage_path": path,
                "filename": filename,
                "material_category_id": rfq["material_category_id"],
                "rfq_message_id": rfq_message_id,
                "mime_type": content_type,
                "size_bytes": len(content),
                "preview_status": "pending" if convertible else "none",
            }
        )
        .execute()
    ).data[0]
    if convertible:
        # Inline is fine — we're already on the poller's worker thread, and
        # generate_preview records its own failures; never derail ingestion.
        try:
            office_preview.generate_preview(file_row["id"])
        except Exception:  # noqa: BLE001
            logger.exception("Preview conversion failed for %s", file_row["filename"])
    return file_row


def _is_pdf(filename: str, content_type: str | None) -> bool:
    return (content_type or "").lower().startswith("application/pdf") or filename.lower().endswith(".pdf")


def _ingest_attachments(
    sb, send: dict, message_row: dict, *, max_attachments: int | None = None
) -> list[tuple[dict, bytes]]:
    """Store the reply's file attachments; returns (project_files row, bytes)
    for the PDFs among them so the caller can run extraction.

    `max_attachments` only ever tightens the configured cap (the on-demand check
    passes a smaller one so a single click cannot pull dozens of files)."""
    import base64

    settings = get_settings()

    max_count = settings.inbound_attachment_max_count
    if max_attachments is not None:
        max_count = min(max_count, max_attachments)
    # Cap count + skip oversized attachments before their bytes are fetched.
    attachments, _ = graph_inbox.list_attachments(
        message_row["graph_message_id"],
        max_count=max_count,
        max_bytes=settings.inbound_attachment_max_bytes,
    )
    pdf_files: list[tuple[dict, bytes]] = []  # (project_files row, content)
    for att in attachments:
        name = att.get("name") or "attachment"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in _QUOTE_ATTACHMENT_EXTS:
            # A reply may carry signatures/logos/etc.; only ingest quote-shaped
            # files, and leave an audit trace for anything skipped.
            audit(None, "rfq.attachment_skipped", "rfq_send", send["id"],
                  {"name": name, "reason": "disallowed_type"})
            continue
        content = base64.b64decode(att["contentBytes"])
        file_row = _store_quote_file(
            sb, send, att["name"], content, att.get("contentType"),
            rfq_message_id=message_row["id"],
        )
        if _is_pdf(att["name"], att.get("contentType")):
            pdf_files.append((file_row, content))
    return pdf_files


def _reference_links(graph_message_id: str) -> list[cloud_links.CloudLink]:
    """Cloud links sent as Outlook reference attachments (never fetchable as
    file bytes through Graph's attachment API)."""
    try:
        refs = graph_inbox.list_reference_links(graph_message_id)
    except Exception:  # noqa: BLE001 — best-effort; body links still get scanned
        logger.exception("Reference-attachment listing failed for %s", graph_message_id)
        return []
    links = []
    for ref in refs:
        link = cloud_links.link_from_url(ref["sourceUrl"], ref.get("name") or "")
        if link:
            links.append(link)
    return links


# What the PE reads in the reply notice when a linked file couldn't be pulled.
_LINK_FAIL_TEXT = {
    "auth_required": "the link requires sign-in",
    "html_page": "the link opens a web page, not a file",
    "too_large": "the linked file is too large",
    "unsupported": "this link type is not supported",
    "unreachable": "the file could not be downloaded",
    "disallowed_type": "the linked file type is not accepted",
}


def _link_failure_note(failures: list[tuple[cloud_links.CloudLink, str]]) -> str | None:
    if not failures:
        return None
    parts = []
    for link, reason in failures[:3]:
        label = link.label or urlparse(link.url).hostname or "link"
        parts.append(f'"{label}" — {_LINK_FAIL_TEXT.get(reason, reason)}')
    more = len(failures) - 3
    note = "Linked file(s) not fetched: " + "; ".join(parts)
    return note + (f" (+{more} more)" if more > 0 else "")


def _ingest_cloud_links(
    sb, send: dict, links: list[cloud_links.CloudLink],
    *, rfq_message_id: str | None = None, reuse_existing: bool = False,
) -> tuple[list[tuple[dict, bytes]], list[tuple[cloud_links.CloudLink, str]]]:
    """Download + store each detected share link as a quote file. Returns the
    PDFs (for extraction) and per-link failures (for the PE's reply notice)."""
    settings = get_settings()
    pdfs: list[tuple[dict, bytes]] = []
    failures: list[tuple[cloud_links.CloudLink, str]] = []
    skipped = links[settings.inbound_link_max_count:]
    if skipped:
        # One summary row, not one per link: a hostile reply can carry hundreds of
        # links (capped at cloud_links._MAX_LINKS), and a row apiece would flood
        # audit_log and stall the single-runner poller.
        audit(None, "rfq.links_skipped", "rfq_send", send["id"],
              {"skipped_count": len(skipped),
               "sample_urls": [link.url[:200] for link in skipped[:5]],
               "reason": "too_many"})
    for link in links[: settings.inbound_link_max_count]:
        try:
            fetched = cloud_links.fetch(
                link, max_bytes=settings.inbound_attachment_max_bytes
            )
        except cloud_links.CloudLinkError as exc:
            failures.append((link, exc.reason))
            audit(None, "rfq.link_fetch_failed", "rfq_send", send["id"],
                  {"url": link.url[:500], "label": link.label, "reason": exc.reason})
            continue
        except Exception:  # noqa: BLE001 — one bad link must not stall the reply
            logger.exception("Cloud link fetch crashed for %s", link.url[:200])
            failures.append((link, "unreachable"))
            continue
        ext = fetched.filename.rsplit(".", 1)[-1].lower() if "." in fetched.filename else ""
        if ext not in _QUOTE_ATTACHMENT_EXTS:
            failures.append((link, "disallowed_type"))
            audit(None, "rfq.attachment_skipped", "rfq_send", send["id"],
                  {"name": fetched.filename, "reason": "disallowed_type"})
            continue
        file_row = _store_quote_file(
            sb, send, fetched.filename, fetched.content, fetched.content_type,
            rfq_message_id=rfq_message_id, reuse_existing=reuse_existing,
        )
        audit(None, "rfq.link_file_ingested", "rfq_send", send["id"],
              {"file_id": file_row["id"], "name": fetched.filename, "url": link.url[:500]})
        if _is_pdf(fetched.filename, fetched.content_type):
            pdfs.append((file_row, fetched.content))
    return pdfs, failures


def _run_extraction(
    sb,
    send: dict,
    message_row: dict,
    pdf_files: list[tuple[dict, bytes]],
    link_failures: list[tuple[cloud_links.CloudLink, str]] | None = None,
) -> str:
    """Extract a quote amount from the reply's PDFs (attached or link-fetched)
    and record the outcome on the message. Returns the final status."""
    settings = get_settings()
    rfq = send["rfqs"]
    project = rfq["projects"]
    contact = send["vendor_contacts"]
    fail_note = _link_failure_note(link_failures or [])

    if not pdf_files:
        # No extractable file. Links that failed to download are the one case
        # the PE must act on (fetch manually / ask the vendor to attach).
        if fail_note:
            sb.table("rfq_messages").update(
                {"extraction_status": "failed", "extraction_error": fail_note}
            ).eq("id", message_row["id"]).execute()
            return "failed"
        if message_row.get("extraction_status") == "failed":
            # A retry cleared every failure but yielded no PDF (e.g. an xlsx
            # quote) — drop the stale link error so the warning goes away.
            sb.table("rfq_messages").update(
                {"extraction_status": "skipped", "extraction_error": None}
            ).eq("id", message_row["id"]).execute()
            return "skipped"
        return message_row.get("extraction_status") or "skipped"

    sb.table("rfq_messages").update({"extraction_status": "pending"}).eq(
        "id", message_row["id"]
    ).execute()
    context = {
        "project_name": project["name"],
        "project_number": project.get("number"),
        "category_name": rfq["material_categories"]["name"],
        "vendor_name": (contact.get("vendors") or {}).get("name"),
    }
    extraction_status = "no_amount"
    attempts = 0
    for file_row, content in pdf_files:
        if attempts >= settings.inbound_pdf_extract_max:
            break  # cap paid extraction calls per inbound message
        attempts += 1
        try:
            result = extract_quote_from_pdf(content, file_row["filename"], context)
        except Exception as exc:  # noqa: BLE001
            from app.services.llm_gate import LlmBusy

            if isinstance(exc, LlmBusy):
                # AI capacity gate saturated (no tokens were spent). Record an
                # actionable note; "Fetch linked files" or manual entry cover
                # the PE, and link-carrying replies can be re-fetched later.
                sb.table("rfq_messages").update(
                    {
                        "extraction_status": "failed",
                        "extraction_error": (
                            "AI capacity was busy when this reply arrived, so the "
                            "price was not extracted. Enter the amount manually, "
                            "or use Fetch linked files to retry."
                        ),
                    }
                ).eq("id", message_row["id"]).execute()
                return "failed"
            logger.exception("Quote extraction crashed for %s", file_row["filename"])
            result = None
        if result is None:
            extraction_status = "failed"
            continue
        if result.get("total_amount") is None:
            continue
        if not _valid_extracted_amount(result):
            # Out-of-band or low-confidence amount from a vendor-controlled PDF —
            # do NOT auto-create a quote or stop polling on it. Keep the file and
            # message so the PE enters the amount manually via the quote UI.
            extraction_status = "needs_review"
            logger.warning(
                "Rejected implausible extracted quote for %s (amount=%r conf=%r)",
                file_row["filename"],
                result.get("total_amount"),
                result.get("confidence"),
            )
            continue
        sb.table("quotes").insert(
            {
                "rfq_id": rfq["id"],
                "vendor_id": contact["vendor_id"],
                "vendor_contact_id": contact["id"],
                "amount": str(result["total_amount"]),
                "quote_file_id": file_row["id"],
                "source": "ai_extracted",
                "rfq_send_id": send["id"],
                "rfq_message_id": message_row["id"],
                "ai_extraction": result,
            }
        ).execute()
        sb.table("rfqs").update({"status": "quotes_in"}).eq("id", rfq["id"]).execute()
        sb.table("rfq_sends").update(
            {"quote_received_at": _now().isoformat(), "polling_active": False}
        ).eq("id", send["id"]).execute()
        extraction_status = "done"
        notify_role(
            Role.ESTIMATING_ENGINEER_MATERIALS,
            rfq["project_id"],
            "quote.received",
            f"Quote received from {contact['name']} for "
            f"{rfq['material_categories']['name']} on {project['name']}: "
            f"${result['total_amount']}",
            rfq_id=rfq["id"],
        )
        break  # one quote per reply; remaining PDFs are still saved as files

    extraction_error = {
        "failed": "Price extraction failed",
        "needs_review": "Extracted amount needs manual review",
    }.get(extraction_status)
    if fail_note and extraction_status != "done":
        extraction_error = f"{extraction_error}. {fail_note}" if extraction_error else fail_note
    sb.table("rfq_messages").update(
        {
            "extraction_status": extraction_status,
            "extraction_error": extraction_error,
        }
    ).eq("id", message_row["id"]).execute()
    return extraction_status


def refetch_link_files(project_id: str, message_id: str) -> dict:
    """PE-triggered retry for a reply whose share links never became files —
    re-parse the stored body (and any Outlook reference attachments), fetch +
    store the files, and re-run extraction. Raises LookupError when the message
    is not in the project, ValueError when there is nothing to do."""
    sb = get_supabase()
    rows = (
        sb.table("rfq_messages")
        .select(
            "id, body, graph_message_id, has_attachments, extraction_status, "
            "rfq_sends(id, conversation_id, vendor_contact_id, rfq_id, "
            "vendor_contacts(id, name, email, vendor_id, vendors(name)), "
            "rfqs(id, project_id, material_category_id, material_categories(name), "
            "projects(id, name, number)))"
        )
        .eq("id", message_id)
        .execute()
    ).data
    if not rows:
        raise LookupError("message not found")
    row = rows[0]
    send = row.get("rfq_sends") or {}
    if ((send.get("rfqs") or {}).get("project_id")) != project_id:
        raise LookupError("message not found")
    if row.get("extraction_status") in ("done", "manual"):
        raise ValueError("A quote was already recorded for this reply.")

    links = cloud_links.find_cloud_links(row.get("body") or "")
    if row.get("has_attachments"):
        links = cloud_links.merge_links(_reference_links(row["graph_message_id"]), links)
    if not links:
        raise ValueError("No cloud-share links found in this reply.")
    sb.table("rfq_messages").update({"cloud_link_count": len(links)}).eq(
        "id", row["id"]
    ).execute()

    pdfs, failures = _ingest_cloud_links(
        sb, send, links, rfq_message_id=row["id"], reuse_existing=True
    )
    status = _run_extraction(sb, send, row, pdfs, failures)
    attempted = min(len(links), get_settings().inbound_link_max_count)
    return {
        "links_found": len(links),
        "files_ingested": attempted - len(failures),
        "failures": [
            {"label": link.label or link.url, "reason": _LINK_FAIL_TEXT.get(reason, reason)}
            for link, reason in failures
        ],
        "extraction_status": status,
    }


# ── "Check for quotes now" (one project, one click) ───────────────────────────
#
# Conversation-targeted, NOT a mailbox poll. For each of this project's RFQ
# sends we ask Graph for the messages in THAT conversationId and push the new
# ones through _ingest_message, the same path the poller uses.
#
# WHY THE DELTA CURSOR IS NEVER TOUCHED
# -------------------------------------
# graph_sync_state['inbox:{ms_sender}'].delta_link is a single, mailbox-wide,
# CONSUMING cursor: Graph hands back each message once per token and the next
# token starts after it. It is shared by every project.
#
# If this function read that cursor it would receive every project's unprocessed
# vendor replies, not just this one's. _ingest_message drops anything whose
# conversation is not in the map it was handed, so those replies would be
# silently discarded, and because the poller advances the cursor past them they
# would never be offered again. One person clicking a button on project A would
# quietly destroy project B's inbound quotes.
#
# So this section calls graph_request directly (never delta_inbox), matches on
# conversation id instead of a cursor, and keeps its lease under its own
# per-project key writing only lease_until/holder. The word delta_link does not
# appear below, and re-reading a conversation is harmless: rfq_messages
# .graph_message_id is UNIQUE, so an already-ingested message is simply skipped.


class CheckAlreadyRunning(RuntimeError):
    """A quote check is already in flight for this project. The router turns
    this into a 409 so a double-click cannot double-charge the extractor."""


_CHECK_LEASE_PREFIX = "quote-check"      # its own id namespace in graph_sync_state
_CHECK_LEASE_TTL_SECONDS = 120           # crash bound; renewed as the run proceeds

# Bounds. One click must never run for minutes or spend unbounded LLM money.
_CHECK_MAX_SENDS = 60                    # RFQ conversations inspected per click
_CHECK_MAX_MESSAGES_PER_CONVERSATION = 15
_CHECK_MAX_INGESTS = 12                  # NEW replies processed per click; each one
                                         # costs up to inbound_pdf_extract_max calls
_CHECK_MAX_ATTACHMENTS = 5               # files pulled per new reply
_CHECK_TIME_BUDGET_SECONDS = 90          # wall clock, checked between conversations
_CHECK_MAX_GRAPH_FAILURES = 3            # consecutive thread lookups before giving up
_CHECK_MAX_NOTES = 8

# Mirrors graph_inbox's delta $select so _ingest_message sees the same shape
# (internetMessageId included: the nudge guard matches on it).
_CONVERSATION_SELECT = (
    "id,conversationId,internetMessageId,from,subject,bodyPreview,"
    "receivedDateTime,hasAttachments"
)


def _check_lease_key(project_id: str) -> str:
    return f"{_CHECK_LEASE_PREFIX}:{project_id}"


def _check_lease_until() -> str:
    return (_now() + timedelta(seconds=_CHECK_LEASE_TTL_SECONDS)).isoformat()


def _acquire_check_lease(sb, key: str, token: str) -> bool:
    """Project-scoped mutual exclusion, taken only when the lease is free or
    expired. Deliberately WITHOUT the poller's `holder.eq.<runner>` escape: the
    token is generated per CALL, not per process, because _RUNNER_TOKEN is
    shared by every request on a worker (and prod runs two of them), so a
    process-wide token would let two simultaneous clicks on the same worker both
    "re-acquire" the same lease and ingest in parallel."""
    now_iso = _now().isoformat()
    rows = (sb.table("graph_sync_state").select("id").eq("id", key).execute()).data
    if not rows:
        try:
            sb.table("graph_sync_state").insert(
                {"id": key, "lease_until": _check_lease_until(), "holder": token}
            ).execute()
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_duplicate_key(exc):
                return False  # another check created the row first
            raise
    resp = (
        sb.table("graph_sync_state")
        .update(
            {"lease_until": _check_lease_until(), "holder": token,
             "updated_at": now_iso}
        )
        .eq("id", key)
        .or_(f"lease_until.is.null,lease_until.lt.{now_iso}")
        .execute()
    )
    return bool(resp.data)


def _renew_check_lease(sb, key: str, token: str) -> bool:
    """Push the TTL out mid-run so a slow check never outlives its own lease.
    Fenced on our token: False means someone else owns it now and we must stop."""
    resp = (
        sb.table("graph_sync_state")
        .update({"lease_until": _check_lease_until(), "updated_at": _now().isoformat()})
        .eq("id", key)
        .eq("holder", token)
        .execute()
    )
    return bool(resp.data)


def _release_check_lease(sb, key: str, token: str) -> None:
    """Free the lease immediately so the button works again on the next click.
    Fenced on our token, and it writes lease_until only: no cursor, ever."""
    try:
        sb.table("graph_sync_state").update(
            {"lease_until": None, "updated_at": _now().isoformat()}
        ).eq("id", key).eq("holder", token).execute()
    except Exception:  # noqa: BLE001 — the TTL frees it anyway
        logger.exception("Could not release quote-check lease %s", key)


def _conversation_messages(conversation_id: str) -> list[dict]:
    """Messages in one Graph conversation, newest first, hard-capped.

    Searches the whole mailbox rather than the Inbox folder the poller watches:
    a reply filed away by an Outlook rule, or landed in Junk, is precisely the
    one this button exists to go and find. Our own outbound copies come back too
    and are dropped by the caller.
    """
    settings = get_settings()
    # OData string literals escape a quote by doubling it. Graph conversation ids
    # are base64-ish so this is belt and braces, but the filter is built by
    # concatenation and must not be breakable by its input.
    literal = conversation_id.replace("'", "''")
    params = {
        "$select": _CONVERSATION_SELECT,
        "$filter": f"conversationId eq '{literal}'",
        "$top": str(_CHECK_MAX_MESSAGES_PER_CONVERSATION),
        "$orderby": "receivedDateTime desc",
    }
    path = f"/users/{settings.ms_sender}/messages"
    try:
        page = graph_email.graph_request("GET", path, params=params).json()
    except httpx.HTTPStatusError as exc:
        # Exchange refuses some $filter + $orderby pairings as "too complex".
        # Drop the sort and order the page ourselves rather than lose the check.
        if exc.response.status_code not in (400, 501):
            raise
        params.pop("$orderby")
        page = graph_email.graph_request("GET", path, params=params).json()
    messages = page.get("value", [])[:_CHECK_MAX_MESSAGES_PER_CONVERSATION]
    messages.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)
    return messages


def _project_check_sends(sb, project_id: str) -> list[dict]:
    """Every RFQ email this project has actually sent that has a conversation to
    look in, newest first.

    polling_active is deliberately NOT a filter. A send that already produced a
    quote has polling_active false and the background poller has stopped
    watching it, which is exactly the case this button is for: the vendor's
    revised or second quote arrives on the same thread days later.
    """
    return (
        sb.table("rfq_sends")
        .select(
            "id, conversation_id, sent_at, polling_active, quote_received_at, "
            "vendor_contact_id, rfq_id, "
            "vendor_contacts(id, name, email, vendor_id, vendors(name)), "
            "rfqs!inner(id, project_id, material_category_id, "
            "material_categories(name), projects(id, name, number))"
        )
        .eq("rfqs.project_id", project_id)
        .eq("status", "sent")
        .not_.is_("conversation_id", "null")
        .order("sent_at", desc=True)
        .limit(_CHECK_MAX_SENDS)
        .execute()
    ).data or []


def _send_label(send: dict) -> str:
    contact = send.get("vendor_contacts") or {}
    vendor = (
        (contact.get("vendors") or {}).get("name") or contact.get("name") or "a vendor"
    )
    category = (
        ((send.get("rfqs") or {}).get("material_categories") or {}).get("name")
        or "an RFQ"
    )
    return f"{vendor} ({category})"


def _add_note(notes: list[str], text: str) -> None:
    """One line per distinct problem, capped: the caller renders these."""
    if text not in notes and len(notes) < _CHECK_MAX_NOTES:
        notes.append(text)


def check_project_quotes(project_id: str) -> dict:
    """Check this project's RFQ conversations for vendor replies, right now.

    Returns {"sends_checked", "messages_seen", "quotes_created", "errors"};
    `errors` is a list of short human-readable notices (a thread that could not
    be read, a cap that cut the run short). Raises CheckAlreadyRunning when a
    check for this project is already in flight, so the router can answer 409.

    Plain def on purpose: the Supabase SDK, Graph and the extractor all block,
    so this belongs on the threadpool, never inside an `async def`.
    """
    from app.services import llm_gate

    sb = get_supabase()
    key = _check_lease_key(project_id)
    token = uuid.uuid4().hex  # per call, see _acquire_check_lease
    if not _acquire_check_lease(sb, key, token):
        raise CheckAlreadyRunning(
            "A check is already running for this project. Give it a moment."
        )
    try:
        # Pipeline tier: one click can queue a dozen extractions, and they must
        # not eat the slots the gate reserves for calls a user is waiting on.
        # It also means a saturated gate is recorded on the reply as an
        # actionable note instead of blowing up the whole check.
        with llm_gate.tier(llm_gate.TIER_PIPELINE):
            return _run_project_check(sb, project_id, key, token)
    finally:
        _release_check_lease(sb, key, token)


def _run_project_check(sb, project_id: str, key: str, token: str) -> dict:
    sender = get_settings().ms_sender.lower()
    notes: list[str] = []
    result: dict = {
        "sends_checked": 0,
        "messages_seen": 0,
        "quotes_created": 0,
        "errors": notes,
    }
    sends = _project_check_sends(sb, project_id)
    if len(sends) >= _CHECK_MAX_SENDS:
        _add_note(
            notes,
            f"Only the {_CHECK_MAX_SENDS} most recent RFQ emails were checked.",
        )

    deadline = time.monotonic() + _CHECK_TIME_BUDGET_SECONDS
    seen_conversations: set[str] = set()
    ingested = 0
    graph_failures = 0
    stop = False

    for send in sends:
        conversation_id = send.get("conversation_id")
        # Two sends can share a thread (a resend into the same conversation);
        # one lookup covers both, and the second send's messages are already in.
        if not conversation_id or conversation_id in seen_conversations:
            continue
        seen_conversations.add(conversation_id)
        if time.monotonic() > deadline:
            _add_note(
                notes,
                "Time ran out before every RFQ email was checked. "
                "Run it again to pick up the rest.",
            )
            break
        if not _renew_check_lease(sb, key, token):
            _add_note(notes, "Another check took over partway through this one.")
            break

        try:
            messages = _conversation_messages(conversation_id)
        except Exception:  # noqa: BLE001 — one bad thread must not sink the run
            logger.exception("Conversation lookup failed for rfq_send %s", send["id"])
            graph_failures += 1
            _add_note(notes, f"Could not read the email thread for {_send_label(send)}.")
            if graph_failures >= _CHECK_MAX_GRAPH_FAILURES:
                _add_note(notes, "The mailbox stopped responding, so the check stopped early.")
                break
            continue
        graph_failures = 0
        result["sends_checked"] += 1

        for msg in messages:
            from_addr = ((msg.get("from") or {}).get("emailAddress") or {}).get(
                "address", ""
            )
            if not from_addr or from_addr.lower() == sender:
                continue  # our own copy of the RFQ we sent, not a reply
            result["messages_seen"] += 1
            if ingested >= _CHECK_MAX_INGESTS:
                _add_note(
                    notes,
                    f"Stopped after {_CHECK_MAX_INGESTS} new replies. "
                    "Run it again to pick up the rest.",
                )
                stop = True
                break
            try:
                # allow_sender_mismatch: the conversation id already proves this
                # message belongs to the send, so a reply from the vendor's
                # colleague or a forwarded address counts. from_addr is stored on
                # the message row and the mismatch is audited, so it stays visible.
                status = _ingest_message(
                    sb,
                    msg,
                    {conversation_id: send},
                    allow_sender_mismatch=True,
                    max_attachments=_CHECK_MAX_ATTACHMENTS,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to ingest checked message %s", msg.get("id"))
                _add_note(notes, f"A reply from {_send_label(send)} could not be read in.")
                continue
            if status is None:
                continue  # already stored, or claimed by another runner
            ingested += 1
            if status == "done":
                result["quotes_created"] += 1
        if stop:
            break

    return result

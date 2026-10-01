"""Unit tests for inbound RFQ reply matching — pure guard paths (no DB / Graph)
plus the single-runner lease, against the in-memory fake Supabase."""

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import rfqs as rfqs_router
from app.services import cloud_links, graph_inbox, rfq_inbox
from tests.test_email_ingest import FakeDB


def _msg(from_addr: str, conversation_id: str = "conv-1", **extra) -> dict:
    return {
        "id": "msg-1",
        "conversationId": conversation_id,
        "from": {"emailAddress": {"address": from_addr}},
        "subject": "RE: 26-104 - Riverside Plaza - BOM",
        "bodyPreview": "Quote attached",
        "receivedDateTime": "2026-06-10T12:00:00Z",
        "hasAttachments": True,
        **extra,
    }


SEND = {
    "id": "send-1",
    "conversation_id": "conv-1",
    "vendor_contacts": {"id": "c1", "name": "Jane", "email": "jane@vendor.com", "vendor_id": "v1"},
    "rfqs": {
        "id": "rfq-1",
        "project_id": "p1",
        "material_category_id": "mc1",
        "material_categories": {"name": "Switchgear"},
        "projects": {"id": "p1", "name": "Riverside Plaza", "number": "26-104"},
    },
}


# sb=None proves these paths never touch the database.


def test_own_sent_mail_is_skipped():
    rfq_inbox._ingest_message(None, _msg("bids@g3electrical.com"), {"conv-1": SEND})


def test_unmatched_conversation_is_skipped():
    rfq_inbox._ingest_message(None, _msg("jane@vendor.com", "other-conv"), {"conv-1": SEND})


def test_missing_from_address_is_skipped():
    rfq_inbox._ingest_message(None, {"id": "m", "conversationId": "conv-1"}, {"conv-1": SEND})


def test_own_copy_under_an_exchange_directory_path_is_skipped():
    # bids@'s Sent Items copy of the RFQ reports its sender as the tenant's
    # directory path, not bids@. It is our own mail, never a vendor reply.
    dn = (
        "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP (FYDIBOHF23SPDLT)"
        "/CN=RECIPIENTS/CN=1CAD18FC663B4707B99C1274EF795330-1454091C-21"
    )
    assert rfq_inbox._ingest_message(None, _msg(dn), {"conv-1": SEND}) is None


# ── Who counts as the vendor ───────────────────────────────────────────────────
# The contact we mailed and anyone at the vendor's company are accepted; our
# own people never are; any other outside address only through the
# project-scoped check (allow_sender_mismatch).

SENDER = "bids@g3electrical.com"
VENDOR_CONTACTS = [
    {"id": "c1", "name": "Jane", "email": "jane@vendor.com", "vendor_id": "v1"},
    {"id": "c2", "name": "Tim", "email": "Tim@Vendor.com", "vendor_id": "v1"},
    # A dev-style test contact on our own domain, under the same vendor.
    {"id": "c3", "name": "Tester", "email": "tester@g3electrical.com", "vendor_id": "v1"},
    {"id": "c4", "name": "Bob", "email": "bob@othervendor.com", "vendor_id": "v2"},
]


@pytest.fixture
def sender_env(monkeypatch):
    """_ingest_message against a fake DB holding the vendor directory, with a
    plain no-attachment reply body and every side effect past the DB recorded."""
    db = FakeDB({"vendor_contacts": VENDOR_CONTACTS})
    monkeypatch.setattr(
        rfq_inbox, "get_settings", lambda: Settings(_env_file=None, ms_sender=SENDER)
    )
    monkeypatch.setattr(
        graph_inbox, "get_message", lambda mid, **k: {"body": {"content": "<p>see below</p>"}}
    )
    audits, notes = [], []
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(rfq_inbox, "notify_role", lambda *a, **k: notes.append(a))
    return db, audits, notes


def _ingest(db, from_addr, send=SEND, **kw):
    return rfq_inbox._ingest_message(
        db, _msg(from_addr, hasAttachments=False), {"conv-1": send}, **kw
    )


def test_the_contact_is_ingested_without_an_audit(sender_env):
    db, audits, _ = sender_env
    _ingest(db, "Jane@Vendor.COM")  # case-insensitive
    assert len(db.tables["rfq_messages"]) == 1
    assert audits == []


def test_a_coworker_in_the_directory_is_ingested_by_the_poller(sender_env):
    db, audits, notes = sender_env
    _ingest(db, "tim@vendor.com")  # the poller: allow_sender_mismatch off
    [message] = db.tables["rfq_messages"]
    assert message["from_addr"] == "tim@vendor.com"
    [audit_row] = audits
    assert audit_row[1] == "rfq.reply_sender_mismatch"
    assert audit_row[4]["relation"] == "company"
    assert audit_row[4]["ingested"] is True
    # The notice names who actually answered, not the contact.
    assert "tim@vendor.com" in notes[0][3]


def test_anyone_on_the_vendors_domain_is_ingested_by_the_poller(sender_env):
    db, audits, _ = sender_env
    _ingest(db, "Quotes@VENDOR.com")
    assert len(db.tables["rfq_messages"]) == 1
    assert audits[0][4]["relation"] == "company"


def test_a_stranger_is_dropped_by_the_poller_but_audited(sender_env):
    db, audits, _ = sender_env
    assert _ingest(db, "someone@elsewhere.com") is None
    assert db.tables.get("rfq_messages", []) == []
    [audit_row] = audits
    assert audit_row[3] == "send-1"
    assert audit_row[4]["relation"] == "other"
    assert audit_row[4]["ingested"] is False


def test_a_stranger_is_ingested_by_the_project_check(sender_env):
    db, audits, _ = sender_env
    _ingest(db, "someone@elsewhere.com", allow_sender_mismatch=True)
    assert len(db.tables["rfq_messages"]) == 1
    assert audits[0][4]["ingested"] is True


def test_another_vendors_contact_does_not_count_as_this_vendor(sender_env):
    db, audits, _ = sender_env
    assert _ingest(db, "bob@othervendor.com") is None
    assert audits[0][4]["relation"] == "other"


def test_our_own_people_are_never_the_vendor(sender_env):
    # An estimator answering the vendor from their own mailbox is on the
    # thread too. Not stored, not audited, even through the project check.
    db, audits, _ = sender_env
    assert _ingest(db, "estimator@g3electrical.com") is None
    assert _ingest(db, "estimator@g3electrical.com", allow_sender_mismatch=True) is None
    assert db.tables.get("rfq_messages", []) == []
    assert audits == []


def test_a_directory_match_wins_even_on_our_own_domain(sender_env):
    db, _, _ = sender_env
    _ingest(db, "tester@g3electrical.com")
    assert len(db.tables["rfq_messages"]) == 1


def test_a_public_mailbox_domain_never_vouches_for_a_company(sender_env):
    # The contact quotes from gmail.com: that makes Tim a coworker only by his
    # directory address, never every other Gmail user.
    db, audits, _ = sender_env
    gmail_contact = {**SEND["vendor_contacts"], "email": "jane.vendor@gmail.com"}
    send = {**SEND, "vendor_contacts": gmail_contact}
    assert _ingest(db, "random.person@gmail.com", send=send) is None
    assert audits[0][4]["relation"] == "other"


def test_a_consumer_isp_domain_never_vouches_for_a_company(sender_env):
    db, audits, _ = sender_env
    cox_contact = {**SEND["vendor_contacts"], "email": "smallshop@cox.net"}
    send = {**SEND, "vendor_contacts": cox_contact}
    assert _ingest(db, "neighbor@cox.net", send=send) is None
    assert audits[0][4]["relation"] == "other"


def test_a_stored_reply_is_not_re_audited_on_a_re_read(sender_env):
    db, audits, _ = sender_env
    _ingest(db, "tim@vendor.com")
    _ingest(db, "tim@vendor.com")  # the check re-reads whole threads
    assert len(db.tables["rfq_messages"]) == 1
    assert len(audits) == 1


def test_initial_delta_url_targets_inbox_with_window():
    url = graph_inbox.initial_delta_url()
    assert "/mailFolders/inbox/messages/delta" in url
    assert "$filter=receivedDateTime ge " in url
    assert "conversationId" in url  # in the $select list


# ── Single-runner lease ────────────────────────────────────────────────────────
# The poller must actually poll on EVERY tick. It previously took a lease of
# 2 × the interval and could not recognise its own lease on the next tick, so a
# lone worker stood itself down every other tick and the real cadence was double
# the configured one.

LEASE_ROW = f"inbox:{SENDER}"


@pytest.fixture
def db():
    return FakeDB(
        {
            "rfq_sends": [
                {
                    "id": "send-1",
                    "conversation_id": "conv-1",
                    "status": "sent",
                    "polling_active": True,
                    "quote_received_at": None,
                    "sent_at": "2099-01-01T00:00:00+00:00",  # inside the window
                    "vendor_contacts": SEND["vendor_contacts"],
                    "rfqs": SEND["rfqs"],
                }
            ]
        }
    )


@pytest.fixture
def poller(monkeypatch, db):
    """poll_once wired to the fake DB, with Graph returning an empty delta batch
    and handing back a fresh cursor each call."""
    monkeypatch.setattr(
        rfq_inbox, "get_settings",
        lambda: Settings(_env_file=None, ms_sender=SENDER, rfq_poll_interval_seconds=180),
    )
    monkeypatch.setattr(rfq_inbox, "get_supabase", lambda: db)
    calls = []

    def _delta(link):
        calls.append(link)
        return [], f"delta-{len(calls)}"

    monkeypatch.setattr(graph_inbox, "delta_inbox", _delta)
    return calls


def _state(db):
    return next(r for r in db.tables["graph_sync_state"] if r["id"] == LEASE_ROW)


def test_consecutive_ticks_both_poll(db, poller):
    """The regression: back-to-back ticks must BOTH reach Graph. Before the
    holder token the second saw its own 360s lease and returned early."""
    rfq_inbox.poll_once()
    rfq_inbox.poll_once()
    rfq_inbox.poll_once()
    assert len(poller) == 3


def test_tick_releases_lease_and_advances_cursor(db, poller):
    rfq_inbox.poll_once()
    state = _state(db)
    assert state["lease_until"] is None       # released, not held to its TTL
    assert state["holder"] == rfq_inbox._RUNNER_TOKEN
    assert state["delta_link"] == "delta-1"
    rfq_inbox.poll_once()
    assert poller[1] == "delta-1"             # second tick resumes from the cursor


def test_live_lease_from_another_runner_blocks(db, poller, monkeypatch):
    rfq_inbox.poll_once()
    _state(db).update(
        {"holder": "someone-else", "lease_until": "2099-01-01T00:00:00+00:00"}
    )
    rfq_inbox.poll_once()
    assert len(poller) == 1                   # stood down for the rival


def test_expired_lease_from_a_dead_runner_is_stolen(db, poller):
    rfq_inbox.poll_once()
    _state(db).update(
        {"holder": "dead-worker", "lease_until": "2000-01-01T00:00:00+00:00"}
    )
    rfq_inbox.poll_once()
    assert len(poller) == 2


def test_failed_delta_releases_lease_without_advancing_cursor(db, poller, monkeypatch):
    rfq_inbox.poll_once()
    assert _state(db)["delta_link"] == "delta-1"

    def _boom(link):
        raise RuntimeError("graph down")

    monkeypatch.setattr(graph_inbox, "delta_inbox", _boom)
    with pytest.raises(RuntimeError):
        rfq_inbox.poll_once()
    state = _state(db)
    assert state["delta_link"] == "delta-1"   # old cursor stands → batch re-pulls
    assert state["lease_until"] is None       # but the lease is NOT left dangling


def test_no_active_sends_skips_graph_entirely(db, poller):
    db.tables["rfq_sends"][0]["polling_active"] = False
    rfq_inbox.poll_once()
    assert poller == []


# ── Cloud-share link ingestion ─────────────────────────────────────────────────
# A vendor reply carrying a OneDrive/Drive/Dropbox link instead of an attachment
# must still produce a stored quote file and run extraction.

SHARE_URL = "https://vendor-my.sharepoint.com/:b:/p/jane/IQTOKEN"
LINK_BODY = (
    f'<html><body><a href="{SHARE_URL}" '
    'class="ms-outlook-mobile-sharing-link-anchor">CODALE QUOTE.pdf</a>'
    "<div>Link test.</div></body></html>"
)
PDF_BYTES = b"%PDF-1.7 quote"


@pytest.fixture
def link_env(monkeypatch, db):
    """_ingest_message wired to the fake DB with Graph, storage, preview, and
    notification side effects stubbed out; cloud_links.fetch left to each test."""
    monkeypatch.setattr(rfq_inbox, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(rfq_inbox, "get_supabase", lambda: db)
    monkeypatch.setattr(
        graph_inbox, "get_message",
        lambda mid, **k: {"body": {"content": LINK_BODY}},
    )
    monkeypatch.setattr(rfq_inbox.storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(rfq_inbox.office_preview, "is_convertible", lambda *a: False)
    audits = []
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(rfq_inbox, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(
        rfq_inbox, "extract_quote_from_pdf",
        lambda content, filename, ctx: {"total_amount": 1234.5, "confidence": 0.9},
    )
    return audits


def _link_msg():
    return _msg("jane@vendor.com", hasAttachments=False)


def test_body_link_reply_ingests_file_and_extracts_quote(db, link_env, monkeypatch):
    monkeypatch.setattr(
        cloud_links, "fetch",
        lambda link, max_bytes: cloud_links.FetchedFile(
            "CODALE QUOTE.pdf", PDF_BYTES, "application/pdf"
        ),
    )
    rfq_inbox._ingest_message(db, _link_msg(), {"conv-1": SEND})

    [file_row] = db.tables["project_files"]
    assert file_row["filename"] == "CODALE QUOTE.pdf"
    assert file_row["category"] == "quote"
    assert file_row["material_category_id"] == "mc1"
    [quote] = db.tables["quotes"]
    assert quote["amount"] == "1234.5"
    assert quote["quote_file_id"] == file_row["id"]
    [message] = db.tables["rfq_messages"]
    assert message["extraction_status"] == "done"
    assert message["cloud_link_count"] == 1


def test_link_fetch_failure_marks_failed_with_actionable_reason(db, link_env, monkeypatch):
    def _boom(link, max_bytes):
        raise cloud_links.CloudLinkError("auth_required", "HTTP 403")

    monkeypatch.setattr(cloud_links, "fetch", _boom)
    rfq_inbox._ingest_message(db, _link_msg(), {"conv-1": SEND})

    assert db.tables.get("project_files", []) == []
    assert db.tables.get("quotes", []) == []
    [message] = db.tables["rfq_messages"]
    assert message["extraction_status"] == "failed"
    assert "requires sign-in" in message["extraction_error"]
    assert "CODALE QUOTE.pdf" in message["extraction_error"]


def test_linkless_reply_without_attachments_stays_skipped(db, link_env, monkeypatch):
    monkeypatch.setattr(
        graph_inbox, "get_message", lambda mid, **k: {"body": {"content": "<p>thanks</p>"}}
    )
    rfq_inbox._ingest_message(db, _link_msg(), {"conv-1": SEND})
    [message] = db.tables["rfq_messages"]
    assert "extraction_status" not in message  # untouched → DB default 'skipped'


# ── PE-triggered link refetch ──────────────────────────────────────────────────


def _stored_message(**over):
    row = {
        "id": "m-1",
        "body": LINK_BODY,
        "graph_message_id": "g-1",
        "has_attachments": False,
        "extraction_status": "failed",
        "rfq_sends": SEND,
    }
    row.update(over)
    return row


@pytest.fixture
def refetch_env(monkeypatch, db):
    monkeypatch.setattr(rfq_inbox, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(rfq_inbox, "get_supabase", lambda: db)
    monkeypatch.setattr(rfq_inbox.storage, "upload_file", lambda *a, **k: None)
    monkeypatch.setattr(rfq_inbox.office_preview, "is_convertible", lambda *a: False)
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfq_inbox, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(
        rfq_inbox, "extract_quote_from_pdf",
        lambda content, filename, ctx: {"total_amount": 999, "confidence": 0.9},
    )


def test_refetch_ingests_and_extracts(db, refetch_env, monkeypatch):
    db.tables["rfq_messages"] = [_stored_message()]
    monkeypatch.setattr(
        cloud_links, "fetch",
        lambda link, max_bytes: cloud_links.FetchedFile(
            "CODALE QUOTE.pdf", PDF_BYTES, "application/pdf"
        ),
    )
    result = rfq_inbox.refetch_reply_files("p1", "m-1")
    assert result["links_found"] == 1
    assert result["files_ingested"] == 1
    assert result["extraction_status"] == "done"
    [quote] = db.tables["quotes"]
    assert quote["rfq_message_id"] == "m-1"


def test_refetch_reuses_existing_file_row(db, refetch_env, monkeypatch):
    # Re-fetching the SAME link yields identical bytes, so the retry dedupes
    # against the file that link already produced (filename + exact byte size).
    db.tables["rfq_messages"] = [_stored_message()]
    db.tables["project_files"] = [
        {"id": "f-1", "project_id": "p1", "category": "quote",
         "material_category_id": "mc1", "filename": "CODALE QUOTE.pdf",
         "size_bytes": len(PDF_BYTES)}
    ]
    monkeypatch.setattr(
        cloud_links, "fetch",
        lambda link, max_bytes: cloud_links.FetchedFile(
            "CODALE QUOTE.pdf", PDF_BYTES, "application/pdf"
        ),
    )
    rfq_inbox.refetch_reply_files("p1", "m-1")
    assert len(db.tables["project_files"]) == 1  # no duplicate row
    [quote] = db.tables["quotes"]
    assert quote["quote_file_id"] == "f-1"


def test_refetch_does_not_reuse_a_different_vendors_same_named_file(
    db, refetch_env, monkeypatch
):
    # Two vendors quote one material category through the same rfq, so a same-named
    # "CODALE QUOTE.pdf" from ANOTHER vendor already sits on (project, category).
    # The retry must not bind this reply's extracted amount to that unrelated file:
    # a different byte size means no reuse, a fresh row is stored, and the quote
    # points at the freshly-fetched file — not the other vendor's.
    db.tables["rfq_messages"] = [_stored_message()]
    db.tables["project_files"] = [
        {"id": "other-vendor-file", "project_id": "p1", "category": "quote",
         "material_category_id": "mc1", "filename": "CODALE QUOTE.pdf",
         "size_bytes": len(PDF_BYTES) + 4096}  # same name, different file
    ]
    monkeypatch.setattr(
        cloud_links, "fetch",
        lambda link, max_bytes: cloud_links.FetchedFile(
            "CODALE QUOTE.pdf", PDF_BYTES, "application/pdf"
        ),
    )
    rfq_inbox.refetch_reply_files("p1", "m-1")
    assert len(db.tables["project_files"]) == 2  # fresh row, not reuse
    [quote] = db.tables["quotes"]
    assert quote["quote_file_id"] != "other-vendor-file"


def test_refetch_without_links_or_attachments_raises(db, refetch_env):
    db.tables["rfq_messages"] = [_stored_message(body="<p>no links here</p>")]
    with pytest.raises(ValueError):
        rfq_inbox.refetch_reply_files("p1", "m-1")


def test_refetch_rereads_the_attachments_of_a_reply_stored_before_the_fix(
    db, refetch_env, monkeypatch
):
    # A reply ingested under the old rules: its quote PDF sat behind 13
    # signature images, the cap dropped it, and the reply was left 'skipped'.
    db.tables["rfq_messages"] = [
        _stored_message(body="<p>see attached</p>", has_attachments=True,
                        extraction_status="skipped")
    ]
    _fake_attachments(monkeypatch, _deep_thread_listing())
    monkeypatch.setattr(rfq_inbox, "_reference_links", lambda gid: [])
    result = rfq_inbox.refetch_reply_files("p1", "m-1")

    assert result["pdfs_found"] == 1
    assert result["extraction_status"] == "done"
    [quote] = db.tables["quotes"]
    assert quote["rfq_message_id"] == "m-1"
    [file_row] = db.tables["project_files"]  # no signature images stored
    assert file_row["filename"] == "LVCC LIGHTING QUOTE.pdf"
    assert file_row["rfq_message_id"] == "m-1"


def test_refetch_reuses_an_attachment_already_stored(db, refetch_env, monkeypatch):
    db.tables["rfq_messages"] = [
        _stored_message(body="<p>see attached</p>", has_attachments=True,
                        extraction_status="no_amount")
    ]
    db.tables["project_files"] = [
        {"id": "f-1", "project_id": "p1", "category": "quote",
         "material_category_id": "mc1", "filename": "LVCC LIGHTING QUOTE.pdf",
         "size_bytes": len(PDF_BYTES), "rfq_message_id": "m-1"}
    ]
    _fake_attachments(monkeypatch, _deep_thread_listing())
    monkeypatch.setattr(rfq_inbox, "_reference_links", lambda gid: [])
    rfq_inbox.refetch_reply_files("p1", "m-1")

    assert len(db.tables["project_files"]) == 1
    assert db.tables["quotes"][0]["quote_file_id"] == "f-1"


def _status(db, message_id="m-1"):
    return next(r for r in db.tables["rfq_messages"] if r["id"] == message_id)[
        "extraction_status"
    ]


@pytest.mark.parametrize("from_addr", [
    "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=BIDS",
    "estimator@g3electrical.com",
])
def test_refetch_refuses_our_own_mail_stored_as_a_reply(db, refetch_env, from_addr):
    # The old check stored bids@'s Sent Items copy (and teammates) as replies.
    # Re-reading one would run the extractor over our own drawings.
    db.tables["rfq_messages"] = [
        _stored_message(from_addr=from_addr, has_attachments=True, extraction_status="no_amount")
    ]
    with pytest.raises(ValueError, match="our side of the thread"):
        rfq_inbox.refetch_reply_files("p1", "m-1")
    assert _status(db) == "no_amount"


def test_refetch_refuses_a_reply_another_reread_is_holding(db, refetch_env):
    db.tables["rfq_messages"] = [_stored_message(extraction_status="pending")]
    with pytest.raises(ValueError, match="already being read"):
        rfq_inbox.refetch_reply_files("p1", "m-1")
    assert db.tables.get("quotes", []) == []


def test_refetch_releases_its_claim_when_nothing_new_turns_up(db, refetch_env, monkeypatch):
    db.tables["rfq_messages"] = [
        _stored_message(body="<p>thanks</p>", has_attachments=True, extraction_status="no_amount")
    ]
    _fake_attachments(monkeypatch, [_inline_image(i) for i in range(4)])
    monkeypatch.setattr(rfq_inbox, "_reference_links", lambda gid: [])
    assert rfq_inbox.refetch_reply_files("p1", "m-1")["extraction_status"] == "no_amount"
    assert _status(db) == "no_amount"  # not left stuck at the 'pending' claim


def test_refetch_hands_the_reply_back_if_it_crashes(db, refetch_env, monkeypatch):
    db.tables["rfq_messages"] = [_stored_message(extraction_status="failed")]
    monkeypatch.setattr(
        cloud_links, "fetch",
        lambda link, max_bytes: cloud_links.FetchedFile("Q.pdf", PDF_BYTES, "application/pdf"),
    )

    def _boom(*a, **k):
        raise RuntimeError("extractor down")

    monkeypatch.setattr(rfq_inbox, "_run_extraction", _boom)
    with pytest.raises(RuntimeError):
        rfq_inbox.refetch_reply_files("p1", "m-1")
    assert _status(db) == "failed"


def test_refetch_reports_a_mailbox_that_cannot_return_the_attachments(
    db, refetch_env, monkeypatch
):
    # The email was deleted (Graph 404) or the mailbox is throttling: say so
    # on the reply instead of failing the request.
    db.tables["rfq_messages"] = [
        _stored_message(body="<p>see attached</p>", has_attachments=True,
                        extraction_status="skipped")
    ]

    def _gone(*a, **k):
        raise RuntimeError("404 ErrorItemNotFound")

    monkeypatch.setattr(graph_inbox, "graph_request", _gone)
    monkeypatch.setattr(rfq_inbox, "_reference_links", lambda gid: [])
    result = rfq_inbox.refetch_reply_files("p1", "m-1")
    assert result["extraction_status"] == "failed"
    [message] = db.tables["rfq_messages"]
    assert "the mailbox could not return them" in message["extraction_error"]


# ── Attachments on a deep reply thread ─────────────────────────────────────────
# Outlook re-attaches every earlier message's signature images on each reply,
# and Graph lists them ahead of the real files. Counted against the per-reply
# cap, they pushed the quote PDF off the end once a thread had some back and
# forth (seen live: 13 images ahead of a Codale lighting quote, 50 ahead of an
# Alarmax one).


def _inline_image(i: int) -> dict:
    return {"@odata.type": "#microsoft.graph.fileAttachment", "id": f"img-{i}",
            "name": f"image{i:03d}.png", "contentType": "image/png", "size": 9000,
            "isInline": True}


def _file(att_id: str, name: str, content_type: str) -> dict:
    return {"@odata.type": "#microsoft.graph.fileAttachment", "id": att_id,
            "name": name, "contentType": content_type, "size": len(PDF_BYTES),
            "isInline": False}


def _deep_thread_listing(images: int = 13) -> list[dict]:
    return [_inline_image(i) for i in range(images)] + [
        _file("pdf-1", "LVCC LIGHTING QUOTE.pdf", "application/pdf")
    ]


def _fake_attachments(monkeypatch, listing: list[dict]) -> list[str]:
    """Serve `listing` from Graph's attachment endpoints; returns the ids whose
    content was actually fetched."""
    import base64

    by_id = {a["id"]: a for a in listing}
    fetched: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _graph(method, path, params=None, **kw):
        tail = path.rsplit("/attachments", 1)[1]
        if not tail:
            return _Resp({"value": listing})
        att_id = tail.lstrip("/")
        fetched.append(att_id)
        return _Resp({**by_id[att_id],
                      "contentBytes": base64.b64encode(PDF_BYTES).decode()})

    monkeypatch.setattr(graph_inbox, "graph_request", _graph)
    return fetched


def test_inline_images_no_longer_fill_the_cap(monkeypatch):
    fetched = _fake_attachments(monkeypatch, _deep_thread_listing())
    got, skipped = graph_inbox.list_attachments(
        "m", mailbox="x@y.com", max_count=10, skip_inline_images=True
    )
    assert [a["name"] for a in got] == ["LVCC LIGHTING QUOTE.pdf"]
    assert fetched == ["pdf-1"]  # no image bytes were downloaded
    assert {s["reason"] for s in skipped} == {"inline_image"}


def test_other_callers_keep_the_old_listing_behaviour(monkeypatch):
    # email_ingest stores every attachment, inline ones included; the flag is
    # opt-in, so its behaviour is unchanged.
    _fake_attachments(monkeypatch, _deep_thread_listing(images=3))
    got, _ = graph_inbox.list_attachments("m", mailbox="x@y.com", max_count=10)
    assert len(got) == 4


def test_an_inline_pdf_is_still_a_file(monkeypatch):
    # Apple Mail marks attached PDFs inline; only inline IMAGES are body art.
    pdf = {**_file("pdf-1", "Quote.pdf", "application/pdf"), "isInline": True}
    _fake_attachments(monkeypatch, [pdf])
    got, _ = graph_inbox.list_attachments(
        "m", mailbox="x@y.com", skip_inline_images=True
    )
    assert [a["name"] for a in got] == ["Quote.pdf"]


def test_a_large_pasted_screenshot_is_kept(monkeypatch):
    # Signature art tops out near 300 KB; a quote pasted into the body as a
    # screenshot is bigger, and is the one body image worth keeping.
    shot = {**_inline_image(99), "name": "image099.png", "size": 900 * 1024}
    _fake_attachments(monkeypatch, [_inline_image(1), shot])
    got, skipped = graph_inbox.list_attachments(
        "m", mailbox="x@y.com", skip_inline_images=True
    )
    assert [a["name"] for a in got] == ["image099.png"]
    assert [s["reason"] for s in skipped] == ["inline_image"]


def test_rank_fetches_the_pdf_first_under_the_cap(monkeypatch):
    listing = [
        _file("x-1", "takeoff.xlsx", "application/vnd.ms-excel"),
        _file("p-1", "photo.jpg", "image/jpeg"),
        _file("pdf-1", "Quote.pdf", "application/pdf"),
    ]
    _fake_attachments(monkeypatch, listing)
    got, skipped = graph_inbox.list_attachments(
        "m", mailbox="x@y.com", max_count=1, rank=rfq_inbox._attachment_rank
    )
    assert [a["name"] for a in got] == ["Quote.pdf"]
    assert [s["reason"] for s in skipped] == ["too_many", "too_many"]


@pytest.fixture
def attach_env(monkeypatch, db, link_env):
    """link_env plus Graph reference links switched off, for attachment-only replies."""
    monkeypatch.setattr(
        graph_inbox, "get_message", lambda mid, **k: {"body": {"content": "<p>quote attached</p>"}}
    )
    monkeypatch.setattr(rfq_inbox, "_reference_links", lambda gid: [])
    return link_env


def test_a_quote_behind_a_deep_threads_signature_images_is_picked_up(
    db, attach_env, monkeypatch
):
    _fake_attachments(monkeypatch, _deep_thread_listing(images=50))
    rfq_inbox._ingest_message(db, _msg("jane@vendor.com"), {"conv-1": SEND})

    [file_row] = db.tables["project_files"]  # the PDF only, no signature art
    assert file_row["filename"] == "LVCC LIGHTING QUOTE.pdf"
    [quote] = db.tables["quotes"]
    assert quote["quote_file_id"] == file_row["id"]
    assert db.tables["rfq_messages"][0]["extraction_status"] == "done"


def test_a_quote_behind_signature_images_survives_the_checks_smaller_cap(
    db, attach_env, monkeypatch
):
    _fake_attachments(monkeypatch, _deep_thread_listing(images=7))
    rfq_inbox._ingest_message(
        db, _msg("jane@vendor.com"), {"conv-1": SEND},
        allow_sender_mismatch=True, max_attachments=rfq_inbox._CHECK_MAX_ATTACHMENTS,
    )
    assert len(db.tables["quotes"]) == 1


def test_an_attachment_that_was_not_saved_is_reported_on_the_reply(
    db, attach_env, monkeypatch
):
    monkeypatch.setattr(
        rfq_inbox, "get_settings",
        lambda: Settings(_env_file=None, inbound_attachment_max_count=1),
    )
    listing = [
        _file("x-1", "pricing.xlsx", "application/vnd.ms-excel"),
        _file("x-2", "pricing-alt.xlsx", "application/vnd.ms-excel"),
        {"@odata.type": "#microsoft.graph.itemAttachment", "id": "i-1",
         "name": "FW: our quote", "contentType": None, "size": 5000, "isInline": False},
        {"@odata.type": "#microsoft.graph.referenceAttachment", "id": "r-1",
         "name": "Quote on OneDrive", "contentType": None, "size": 100, "isInline": False},
        *[_inline_image(i) for i in range(5)],
    ]
    _fake_attachments(monkeypatch, listing)
    rfq_inbox._ingest_message(db, _msg("jane@vendor.com"), {"conv-1": SEND})

    [message] = db.tables["rfq_messages"]
    assert message["extraction_status"] == "failed"
    error = message["extraction_error"]
    assert '"pricing-alt.xlsx" (past the per-reply file limit)' in error
    assert '"FW: our quote" (an attached email, open it in Outlook)' in error
    assert "OneDrive" not in error   # a cloud link is resolved separately
    assert "image" not in error      # signature art is not news
    skipped = [a for a in attach_env if a[1] == "rfq.attachment_skipped"]
    assert {a[4]["reason"] for a in skipped} == {"too_many", "item_attachment"}


def test_refetch_wrong_project_404s(db, refetch_env):
    db.tables["rfq_messages"] = [_stored_message()]
    with pytest.raises(LookupError):
        rfq_inbox.refetch_reply_files("other-project", "m-1")


def test_refetch_already_extracted_refuses(db, refetch_env):
    db.tables["rfq_messages"] = [_stored_message(extraction_status="done")]
    with pytest.raises(ValueError):
        rfq_inbox.refetch_reply_files("p1", "m-1")


# ── On-demand quote check (the Receive Quotes button) ──────────────────────────
# check_project_quotes goes and reads THIS project's RFQ conversations right now,
# instead of waiting for the poller's next pass. Two things must hold or it does
# real damage:
#
#   • it must never touch the delta cursor. That cursor is a single mailbox-wide
#     CONSUMING position: reading from it would hand this project every other
#     project's unprocessed vendor replies, which _ingest_message would then drop
#     on the floor (wrong conversation) while the poller advanced past them for
#     good. One click on project A would destroy project B's inbound quotes.
#   • it must serialise. Each new reply costs paid extraction calls, so a
#     double-click has to be refused, not run twice.


@pytest.fixture
def check_db():
    """One sent RFQ with a conversation to look in, plus the poller's cursor
    sitting in graph_sync_state where the check must leave it."""
    return FakeDB(
        {
            "rfq_sends": [
                {
                    "id": "send-1",
                    "conversation_id": "conv-1",
                    "status": "sent",
                    # A send the poller has already stopped watching: the button
                    # exists for exactly this case (a revised quote days later).
                    "polling_active": False,
                    "quote_received_at": "2026-06-01T00:00:00+00:00",
                    "sent_at": "2026-05-01T00:00:00+00:00",
                    # The fake matches embedded filters literally, as PostgREST
                    # does with rfqs!inner(...).eq("rfqs.project_id", ...).
                    "rfqs.project_id": "p1",
                    "vendor_contacts": SEND["vendor_contacts"],
                    "rfqs": SEND["rfqs"],
                }
            ],
            "graph_sync_state": [
                {
                    "id": LEASE_ROW,
                    "delta_link": "cursor-1",
                    "holder": rfq_inbox._RUNNER_TOKEN,
                    "lease_until": None,
                }
            ],
        }
    )


@pytest.fixture
def check_env(monkeypatch, check_db):
    """check_project_quotes wired to the fake, with one vendor reply waiting in
    the conversation and every side effect past the DB stubbed out. Returns the
    list of delta_inbox calls, which must stay empty."""
    monkeypatch.setattr(
        rfq_inbox, "get_settings", lambda: Settings(_env_file=None, ms_sender=SENDER)
    )
    monkeypatch.setattr(rfq_inbox, "get_supabase", lambda: check_db)
    monkeypatch.setattr(
        rfq_inbox, "_conversation_messages",
        lambda conversation_id: [_msg("jane@vendor.com", hasAttachments=False)],
    )
    monkeypatch.setattr(
        graph_inbox, "get_message", lambda mid, **k: {"body": {"content": "<p>quote coming</p>"}}
    )
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfq_inbox, "notify_role", lambda *a, **k: None)
    delta_calls = []
    monkeypatch.setattr(
        graph_inbox, "delta_inbox",
        lambda link: delta_calls.append(link) or ([], "cursor-2"),
    )
    return delta_calls


def _sync_row(db, row_id):
    return next(r for r in db.tables["graph_sync_state"] if r["id"] == row_id)


def test_check_ingests_the_reply_the_poller_stopped_watching(check_db, check_env):
    result = rfq_inbox.check_project_quotes("p1")

    assert result["sends_checked"] == 1
    assert result["messages_seen"] == 1
    assert result["errors"] == []
    [message] = check_db.tables["rfq_messages"]
    assert message["graph_message_id"] == "msg-1"
    assert message["from_addr"] == "jane@vendor.com"


def test_check_never_touches_the_delta_cursor(check_db, check_env):
    rfq_inbox.check_project_quotes("p1")

    assert check_env == []  # delta_inbox was never called
    poller = _sync_row(check_db, LEASE_ROW)
    assert poller["delta_link"] == "cursor-1"          # cursor stands untouched
    assert poller["holder"] == rfq_inbox._RUNNER_TOKEN  # poller's lease untouched
    # And the check's own lease row is cursor-free: it only ever writes the lease.
    own = _sync_row(check_db, "quote-check:p1")
    assert "delta_link" not in own


def test_check_releases_its_lease_so_the_next_click_works(check_db, check_env):
    rfq_inbox.check_project_quotes("p1")
    assert _sync_row(check_db, "quote-check:p1")["lease_until"] is None
    # A second click runs; the reply is already stored, so nothing is re-ingested
    # and the extractor is not charged again.
    second = rfq_inbox.check_project_quotes("p1")
    assert second["messages_seen"] == 1
    assert second["quotes_created"] == 0
    assert len(check_db.tables["rfq_messages"]) == 1


def test_a_second_check_while_one_is_running_is_refused(check_db, check_env, monkeypatch):
    """Re-entrancy proven from INSIDE a live run rather than by planting a row:
    the second call has to see the first one's lease, which is what stops a
    double-click paying for the same extraction twice."""
    refusals: list[Exception] = []

    def _reenter(conversation_id):
        with pytest.raises(rfq_inbox.CheckAlreadyRunning) as exc:
            rfq_inbox.check_project_quotes("p1")
        refusals.append(exc.value)
        return []

    monkeypatch.setattr(rfq_inbox, "_conversation_messages", _reenter)
    outer = rfq_inbox.check_project_quotes("p1")

    assert len(refusals) == 1
    assert "already running" in str(refusals[0])
    # The refused re-entry stands down before doing anything, so it can neither
    # steal nor release the lease the outer run is still holding.
    assert outer["sends_checked"] == 1
    assert outer["errors"] == []


def test_check_refuses_while_another_holds_the_lease(check_db, check_env):
    check_db.tables["graph_sync_state"].append(
        {
            "id": "quote-check:p1",
            "holder": "another-worker",
            "lease_until": "2099-01-01T00:00:00+00:00",
        }
    )
    with pytest.raises(rfq_inbox.CheckAlreadyRunning):
        rfq_inbox.check_project_quotes("p1")

    assert check_db.tables.get("rfq_messages", []) == []  # nothing was ingested
    # The refused caller must not release or steal the rival's lease on its way out.
    rival = _sync_row(check_db, "quote-check:p1")
    assert rival["holder"] == "another-worker"
    assert rival["lease_until"] == "2099-01-01T00:00:00+00:00"


def test_a_lease_orphaned_by_a_dead_worker_is_reclaimed(check_db, check_env):
    check_db.tables["graph_sync_state"].append(
        {
            "id": "quote-check:p1",
            "holder": "dead-worker",
            "lease_until": "2000-01-01T00:00:00+00:00",
        }
    )
    assert rfq_inbox.check_project_quotes("p1")["sends_checked"] == 1


def test_a_thread_that_cannot_be_read_is_reported_not_raised(check_db, check_env, monkeypatch):
    def _boom(conversation_id):
        raise RuntimeError("graph down")

    monkeypatch.setattr(rfq_inbox, "_conversation_messages", _boom)
    result = rfq_inbox.check_project_quotes("p1")

    # A partial run is still a success: the notice is user-facing text, not a 500.
    assert result["sends_checked"] == 0
    assert result["errors"] == ["Could not read the email thread for Jane (Switchgear)."]
    assert _sync_row(check_db, "quote-check:p1")["lease_until"] is None  # still released
    assert _sync_row(check_db, LEASE_ROW)["delta_link"] == "cursor-1"


def test_our_own_outbound_copy_is_not_counted_as_a_reply(check_db, check_env, monkeypatch):
    monkeypatch.setattr(
        rfq_inbox, "_conversation_messages", lambda cid: [_msg(SENDER)]
    )
    result = rfq_inbox.check_project_quotes("p1")
    assert result["messages_seen"] == 0
    assert check_db.tables.get("rfq_messages", []) == []


def test_the_sent_items_copy_under_a_directory_path_is_not_a_reply(
    check_db, check_env, monkeypatch
):
    # Seen in dev: the check stored bids@'s own Sent Items copy of the RFQ as
    # a vendor reply and ran the extractor over our BOM, because Graph
    # reports that copy's sender as the tenant directory path.
    dn = "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=BIDS"
    monkeypatch.setattr(rfq_inbox, "_conversation_messages", lambda cid: [_msg(dn)])
    result = rfq_inbox.check_project_quotes("p1")
    assert result["messages_seen"] == 0
    assert check_db.tables.get("rfq_messages", []) == []


def test_a_teammate_on_the_thread_is_not_stored_by_the_check(
    check_db, check_env, monkeypatch
):
    monkeypatch.setattr(
        rfq_inbox, "_conversation_messages",
        lambda cid: [_msg("estimator@g3electrical.com", hasAttachments=False)],
    )
    rfq_inbox.check_project_quotes("p1")
    assert check_db.tables.get("rfq_messages", []) == []


def test_the_check_picks_up_a_coworkers_quote_the_poller_used_to_drop(
    check_db, check_env, monkeypatch
):
    check_db.tables["vendor_contacts"] = [dict(c) for c in VENDOR_CONTACTS]
    monkeypatch.setattr(
        rfq_inbox, "_conversation_messages",
        lambda cid: [_msg("quotedesk@vendor.com", hasAttachments=False)],
    )
    rfq_inbox.check_project_quotes("p1")
    [message] = check_db.tables["rfq_messages"]
    assert message["from_addr"] == "quotedesk@vendor.com"


# ── POST /projects/{id}/rfqs/check-quotes (the route in front of it) ───────────


def _writer():
    return CurrentUser(
        id="u1", email="mats@g3.com", role=Role.ESTIMATING_ENGINEER_MATERIALS,
        is_active=True,
    )


@pytest.fixture
def check_route(monkeypatch):
    monkeypatch.setattr(rfqs_router, "audit", lambda *a, **k: None)


def test_check_quotes_route_hands_back_the_service_result(monkeypatch, check_route):
    payload = {"sends_checked": 3, "messages_seen": 5, "quotes_created": 2, "errors": []}
    monkeypatch.setattr(
        rfqs_router.rfq_inbox, "check_project_quotes", lambda pid: payload
    )
    assert rfqs_router.check_quotes("p1", _writer()) == payload


def test_check_quotes_route_reports_a_busy_check_as_409(monkeypatch, check_route):
    """A second click is a "try again in a moment", not a failure, and the
    message the service raises is already what the user should read."""
    def _busy(pid):
        raise rfq_inbox.CheckAlreadyRunning(
            "A check is already running for this project. Give it a moment."
        )

    monkeypatch.setattr(rfqs_router.rfq_inbox, "check_project_quotes", _busy)
    with pytest.raises(HTTPException) as exc:
        rfqs_router.check_quotes("p1", _writer())

    assert exc.value.status_code == 409
    assert "already running" in exc.value.detail


def test_check_quotes_route_passes_partial_run_notices_through(monkeypatch, check_route):
    # A thread that could not be read is reported in the body, NOT as an error
    # status: the replies that did come in are already stored.
    payload = {
        "sends_checked": 1,
        "messages_seen": 1,
        "quotes_created": 0,
        "errors": ["Could not read the email thread for Jane (Switchgear)."],
    }
    monkeypatch.setattr(
        rfqs_router.rfq_inbox, "check_project_quotes", lambda pid: payload
    )
    assert rfqs_router.check_quotes("p1", _writer())["errors"] == payload["errors"]

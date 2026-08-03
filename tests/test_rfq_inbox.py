"""Unit tests for inbound RFQ reply matching — pure guard paths (no DB / Graph)
plus the single-runner lease, against the in-memory fake Supabase."""

import pytest

from app.core.config import Settings
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


def test_sender_mismatch_is_audited_and_not_ingested(monkeypatch):
    calls = []
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: calls.append((a, k)))
    rfq_inbox._ingest_message(None, _msg("stranger@elsewhere.com"), {"conv-1": SEND})
    assert len(calls) == 1
    assert calls[0][0][1] == "rfq.reply_sender_mismatch"
    assert calls[0][0][3] == "send-1"


def test_sender_match_is_case_insensitive(monkeypatch):
    # Reaching the idempotency check (first sb access) proves the guards passed.
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: pytest.fail("should not audit"))
    with pytest.raises(AttributeError):  # sb=None -> .table() blows up at the DB step
        rfq_inbox._ingest_message(None, _msg("Jane@Vendor.COM"), {"conv-1": SEND})


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

SENDER = "bids@g3electrical.com"
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
    result = rfq_inbox.refetch_link_files("p1", "m-1")
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
    rfq_inbox.refetch_link_files("p1", "m-1")
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
    rfq_inbox.refetch_link_files("p1", "m-1")
    assert len(db.tables["project_files"]) == 2  # fresh row, not reuse
    [quote] = db.tables["quotes"]
    assert quote["quote_file_id"] != "other-vendor-file"


def test_refetch_without_links_raises(db, refetch_env):
    db.tables["rfq_messages"] = [_stored_message(body="<p>no links here</p>")]
    with pytest.raises(ValueError):
        rfq_inbox.refetch_link_files("p1", "m-1")


def test_refetch_wrong_project_404s(db, refetch_env):
    db.tables["rfq_messages"] = [_stored_message()]
    with pytest.raises(LookupError):
        rfq_inbox.refetch_link_files("other-project", "m-1")


def test_refetch_already_extracted_refuses(db, refetch_env):
    db.tables["rfq_messages"] = [_stored_message(extraction_status="done")]
    with pytest.raises(ValueError):
        rfq_inbox.refetch_link_files("p1", "m-1")

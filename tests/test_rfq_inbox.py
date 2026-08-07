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

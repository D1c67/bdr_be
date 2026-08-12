"""RFQ vendor nudges - the per-contact recipients state matrix, the nudge send
loop (Graph mocked), request validation, and the ingestion guard that keeps our
own nudges from ever being processed as vendor quote replies."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import RFQNudgeIn
from app.routers import rfqs as rfqs_router
from app.services import graph_inbox, rfq_inbox, rfq_nudges
from tests.test_email_ingest import FakeDB

RFQ_ID = "rfq-1"
PROJECT_ID = "p1"


def _contact(cid="c1", vendor_id="v1", name="Jane", email="jane@vendor.com"):
    return {
        "id": cid,
        "name": name,
        "email": email,
        "vendor_id": vendor_id,
        "vendors": {"name": "Acme Supply"},
    }


def _send(sid, cid="c1", vendor_id="v1", **over):
    row = {
        "id": sid,
        "rfq_id": RFQ_ID,
        "vendor_contact_id": cid,
        "cc_recipients": None,
        "graph_message_id": f"graph-{sid}",
        "status": "sent",
        "error": None,
        "quote_received_at": None,
        "sent_at": "2026-08-01T00:00:00+00:00",
        "created_at": "2026-08-01T00:00:00+00:00",
        "vendor_contacts": _contact(cid, vendor_id),
        "rfqs": {"project_id": PROJECT_ID},
    }
    row.update(over)
    return row


def _recipients(db):
    return rfq_nudges.recipients_status(db, RFQ_ID)


# ── Recipients state matrix ────────────────────────────────────────────────


def test_state_quote_received_via_send_flag():
    db = FakeDB(
        {"rfq_sends": [_send("s1", quote_received_at="2026-08-02T00:00:00+00:00")]}
    )
    [r] = _recipients(db)
    assert r["state"] == "quote_received"
    assert not r["nudgeable"]


def test_state_quote_received_via_vendor_level_quote_from_another_contact():
    # Ann and Bob work at the same vendor company; Bob's quote answers for the
    # company, so Ann must not be nudged. Carol at a different vendor stays
    # unanswered and nudgeable.
    db = FakeDB(
        {
            "rfq_sends": [
                _send("s1", cid="ann", vendor_id="v1"),
                _send("s2", cid="bob", vendor_id="v1"),
                _send("s3", cid="carol", vendor_id="v2"),
            ],
            "quotes": [
                {"id": "q1", "rfq_id": RFQ_ID, "vendor_id": "v1", "origin": "vendor"}
            ],
        }
    )
    by_contact = {r["vendor_contact_id"]: r for r in _recipients(db)}
    assert by_contact["ann"]["state"] == "quote_received"
    assert by_contact["bob"]["state"] == "quote_received"
    assert by_contact["carol"]["state"] == "no_reply"
    assert by_contact["carol"]["nudgeable"]


def test_estimate_origin_quote_never_counts_as_an_answer():
    # General Material's estimate row is the wiring figure, not a vendor reply.
    db = FakeDB(
        {
            "rfq_sends": [_send("s1")],
            "quotes": [
                {"id": "q1", "rfq_id": RFQ_ID, "vendor_id": "v1", "origin": "estimate"}
            ],
        }
    )
    [r] = _recipients(db)
    assert r["state"] == "no_reply"
    assert r["nudgeable"]


def test_state_replied_no_quote_is_nudgeable_with_latest_replied_at():
    db = FakeDB(
        {
            "rfq_sends": [_send("s1")],
            "rfq_messages": [
                {"id": "m1", "rfq_send_id": "s1", "received_at": "2026-08-03T10:00:00+00:00"},
                {"id": "m2", "rfq_send_id": "s1", "received_at": "2026-08-04T10:00:00+00:00"},
            ],
        }
    )
    [r] = _recipients(db)
    assert r["state"] == "replied_no_quote"
    assert r["replied_at"] == "2026-08-04T10:00:00+00:00"
    # A reply without a quote still counts as "hasn't quoted" for nudging.
    assert r["nudgeable"]


def test_state_failed_and_queued_are_not_nudgeable():
    db = FakeDB(
        {
            "rfq_sends": [
                _send("s1", cid="c1", status="failed", graph_message_id=None,
                      sent_at=None, error="bad address"),
                _send("s2", cid="c2", status="pending", graph_message_id=None,
                      sent_at=None),
            ]
        }
    )
    by_contact = {r["vendor_contact_id"]: r for r in _recipients(db)}
    assert by_contact["c1"]["state"] == "failed"
    assert by_contact["c1"]["send_status"] == "failed"
    assert by_contact["c1"]["send_error"] == "bad address"
    assert not by_contact["c1"]["nudgeable"]
    assert by_contact["c2"]["state"] == "queued"
    assert not by_contact["c2"]["nudgeable"]


def test_resend_collapses_to_the_latest_row():
    # c1 got the RFQ twice: the newer send is the one a nudge replies on, and a
    # reply on the OLD send still counts for the contact.
    db = FakeDB(
        {
            "rfq_sends": [
                _send("s-old", sent_at="2026-08-01T00:00:00+00:00"),
                _send("s-new", sent_at="2026-08-05T00:00:00+00:00"),
            ],
            "rfq_messages": [
                {"id": "m1", "rfq_send_id": "s-old", "received_at": "2026-08-02T00:00:00+00:00"}
            ],
        }
    )
    [r] = _recipients(db)  # two sends, ONE recipient row
    assert r["rfq_send_id"] == "s-new"
    assert r["sent_at"] == "2026-08-05T00:00:00+00:00"
    assert r["state"] == "replied_no_quote"
    assert r["replied_at"] == "2026-08-02T00:00:00+00:00"


def test_failed_resend_still_targets_the_send_that_went_out():
    # The newest row is a failed re-send: the contact reads failed (their
    # latest send), and the nudge target falls back to the last send that
    # actually left with a Graph id - but a failed state is never nudgeable.
    db = FakeDB(
        {
            "rfq_sends": [
                _send("s1", sent_at="2026-08-01T00:00:00+00:00"),
                _send("s2", status="failed", graph_message_id=None, sent_at=None,
                      created_at="2026-08-05T00:00:00+00:00", error="boom"),
            ]
        }
    )
    [r] = _recipients(db)
    assert r["send_status"] == "failed"
    assert r["state"] == "failed"
    assert r["rfq_send_id"] == "s1"
    assert not r["nudgeable"]


def test_last_nudge_is_the_newest_across_the_contacts_sends():
    db = FakeDB(
        {
            "rfq_sends": [
                _send("s-old", sent_at="2026-08-01T00:00:00+00:00"),
                _send("s-new", sent_at="2026-08-05T00:00:00+00:00"),
            ],
            "rfq_nudges": [
                {"id": "n1", "rfq_send_id": "s-old",
                 "created_at": "2026-08-02T00:00:00+00:00",
                 "sent_at": "2026-08-02T00:00:05+00:00", "status": "sent", "error": None},
                {"id": "n2", "rfq_send_id": "s-new",
                 "created_at": "2026-08-06T00:00:00+00:00",
                 "sent_at": None, "status": "failed", "error": "graph down"},
            ],
        }
    )
    [r] = _recipients(db)
    assert r["last_nudge"] == {
        "created_at": "2026-08-06T00:00:00+00:00",
        "sent_at": None,
        "status": "failed",
        "error": "graph down",
    }


def test_no_sends_means_no_recipients():
    assert rfq_nudges.recipients_status(FakeDB(), RFQ_ID) == []


# ── The nudge send loop (Graph mocked) ─────────────────────────────────────


@pytest.fixture
def nudge_env(monkeypatch):
    """No pacing sleeps, per-nudge audits recorded instead of hitting the DB."""
    monkeypatch.setattr(rfq_nudges.time, "sleep", lambda s: None)
    audits = []
    monkeypatch.setattr(rfq_nudges, "audit", lambda *a, **k: audits.append(a))
    return audits


def _draft(n):
    # What createReplyAll actually hands back: the vendor recipients survive,
    # but the replying mailbox's own address (bids@) is STRIPPED from the
    # lines it builds - the nudge path has to put the desk CC back itself.
    return {
        "id": f"draft-{n}",
        "conversationId": "conv-1",
        "internetMessageId": f"<nudge-{n}@g3>",
        "subject": "RE: 26-104 - Riverside Plaza - BOM",
        "body": {
            "contentType": "html",
            "content": "<html><head></head><body><div>quoted history</div></body></html>",
        },
        "toRecipients": [{"emailAddress": {"address": "jane@vendor.com"}}],
        "ccRecipients": [{"emailAddress": {"address": "coworker@vendor.com"}}],
    }


def _patch_graph(monkeypatch, *, fail_send_for=frozenset()):
    """Stub the four Graph calls; drafts come back as draft-1, draft-2, ... in
    target order. Returns the recorded calls."""
    calls = {"create": [], "body": [], "attach": [], "send": []}
    counter = {"n": 0}

    def create(message_id):
        counter["n"] += 1
        draft = _draft(counter["n"])
        calls["create"].append((message_id, draft["id"]))
        return draft

    def send(message_id):
        if message_id in fail_send_for:
            raise RuntimeError("graph send blew up")
        calls["send"].append(message_id)

    monkeypatch.setattr(rfq_nudges.graph_email, "create_reply_all_draft", create)
    monkeypatch.setattr(
        rfq_nudges.graph_email,
        "update_message_body",
        lambda mid, html, cc=None: calls["body"].append((mid, html, cc)),
    )
    monkeypatch.setattr(
        rfq_nudges.graph_email,
        "add_attachment",
        lambda mid, name, content, ctype, content_id=None: calls["attach"].append(
            (mid, name, content_id)
        ),
    )
    monkeypatch.setattr(rfq_nudges.graph_email, "send_draft", send)
    return calls


def test_nudge_sends_and_records(nudge_env, monkeypatch):
    db = FakeDB({"rfq_sends": [_send("s1")]})
    calls = _patch_graph(monkeypatch)

    [res] = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID,
        [{"rfq_send_id": "s1", "message": "Hey Jane, quote yet?"}], "u1",
    )

    assert res["status"] == "sent"
    assert res["error"] is None
    [nudge] = db.tables["rfq_nudges"]
    assert res["nudge_id"] == nudge["id"]
    assert nudge["status"] == "sent"
    assert nudge["internet_message_id"] == "<nudge-1@g3>"
    assert nudge["graph_message_id"] == "draft-1"
    assert nudge["message"] == "Hey Jane, quote yet?"
    assert nudge["sent_by"] == "u1"
    [log] = db.tables["email_log"]
    assert nudge["email_log_id"] == log["id"]
    assert log["to_addrs"] == "jane@vendor.com, coworker@vendor.com, bids@g3electrical.com"
    assert log["rfq_id"] == RFQ_ID and log["project_id"] == PROJECT_ID
    assert log["subject"].startswith("RE:")
    # The reply-all draft was created on the ORIGINAL send's Graph message.
    assert calls["create"] == [("graph-s1", "draft-1")]
    # Branded body: the reminder card first, the quoted thread below it. The
    # same PATCH rewrites the CC line: createReplyAll dropped the replying
    # mailbox, so bids@ is appended after the thread's surviving CCs.
    [(mid, html, cc)] = calls["body"]
    assert mid == "draft-1"
    assert "G3 ELECTRICAL" in html
    assert html.index("Hey Jane, quote yet?") < html.index("quoted history")
    assert cc == ["coworker@vendor.com", "bids@g3electrical.com"]
    # Logo inline, then the draft went out.
    assert calls["attach"] == [("draft-1", "g3-logo.jpg", "g3-logo")]
    assert calls["send"] == ["draft-1"]
    # Per-nudge audit fired.
    assert nudge_env[-1][1] == "rfq.nudge"


def test_nudge_never_duplicates_an_already_present_desk_cc(nudge_env, monkeypatch):
    """If the reply-all draft already carries bids@ (tenant behavior varies),
    the CC line is left alone - PATCHing cc=None - so nobody is copied twice."""
    db = FakeDB({"rfq_sends": [_send("s1")]})
    calls = _patch_graph(monkeypatch)
    create = rfq_nudges.graph_email.create_reply_all_draft

    def create_with_desk_cc(message_id):
        draft = create(message_id)
        draft["ccRecipients"] = [{"emailAddress": {"address": "Bids@G3Electrical.com"}}]
        return draft

    monkeypatch.setattr(
        rfq_nudges.graph_email, "create_reply_all_draft", create_with_desk_cc
    )
    [res] = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID, [{"rfq_send_id": "s1", "message": "ping"}], "u1"
    )

    assert res["status"] == "sent"
    [(_, _, cc)] = calls["body"]
    assert cc is None
    [log] = db.tables["email_log"]
    assert log["to_addrs"] == "jane@vendor.com, Bids@G3Electrical.com"


def test_nudge_row_exists_before_the_draft_is_sent(nudge_env, monkeypatch):
    """The pending row (carrying the draft's internetMessageId) must be in the
    DB BEFORE send: that is what closes the race with the inbox poller."""
    db = FakeDB({"rfq_sends": [_send("s1")]})
    calls = _patch_graph(monkeypatch)
    seen = []

    def send(message_id):
        seen.extend(dict(n) for n in db.tables.get("rfq_nudges", []))
        calls["send"].append(message_id)

    monkeypatch.setattr(rfq_nudges.graph_email, "send_draft", send)
    rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID, [{"rfq_send_id": "s1", "message": "ping"}], "u1"
    )
    [claimed] = seen
    assert claimed["status"] == "pending"
    assert claimed["internet_message_id"] == "<nudge-1@g3>"


def test_nudge_skips_when_the_send_already_has_a_quote(nudge_env, monkeypatch):
    db = FakeDB(
        {"rfq_sends": [_send("s1", quote_received_at="2026-08-02T00:00:00+00:00")]}
    )
    monkeypatch.setattr(
        rfq_nudges.graph_email,
        "create_reply_all_draft",
        lambda mid: pytest.fail("must never email a contact who already quoted"),
    )
    [res] = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID, [{"rfq_send_id": "s1", "message": "x"}], "u1"
    )
    assert res == {
        "rfq_send_id": "s1",
        "nudge_id": None,
        "status": "skipped_quote_received",
        "error": None,
    }
    assert db.tables.get("rfq_nudges", []) == []


def test_nudge_skips_on_a_vendor_level_quote(nudge_env, monkeypatch):
    # The quote arrived through a coworker's send at the same vendor company.
    db = FakeDB(
        {
            "rfq_sends": [_send("s1")],
            "quotes": [
                {"id": "q1", "rfq_id": RFQ_ID, "vendor_id": "v1", "origin": "vendor"}
            ],
        }
    )
    monkeypatch.setattr(
        rfq_nudges.graph_email,
        "create_reply_all_draft",
        lambda mid: pytest.fail("must never email a vendor who already quoted"),
    )
    [res] = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID, [{"rfq_send_id": "s1", "message": "x"}], "u1"
    )
    assert res["status"] == "skipped_quote_received"


def test_per_target_failure_never_stops_the_batch(nudge_env, monkeypatch):
    db = FakeDB(
        {"rfq_sends": [_send("s1", cid="c1"), _send("s2", cid="c2", vendor_id="v2")]}
    )
    _patch_graph(monkeypatch, fail_send_for={"draft-1"})

    results = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID,
        [{"rfq_send_id": "s1", "message": "a"}, {"rfq_send_id": "s2", "message": "b"}],
        "u1",
    )

    assert [r["status"] for r in results] == ["failed", "sent"]
    assert results[0]["error"] == "graph send blew up"
    by_send = {n["rfq_send_id"]: n for n in db.tables["rfq_nudges"]}
    assert by_send["s1"]["status"] == "failed"
    assert by_send["s1"]["error"] == "graph send blew up"
    assert by_send["s2"]["status"] == "sent"
    # The failed target still produced no email_log row; the sent one did.
    assert len(db.tables["email_log"]) == 1


def test_bookkeeping_failure_after_send_still_reports_sent(nudge_env, monkeypatch):
    """Once send_draft succeeds the vendor HAS the email: a DB blip in the
    post-send ledger must never surface as 'failed', or the user retries and
    double-emails. The row is still stamped sent, just without a ledger link."""
    db = FakeDB({"rfq_sends": [_send("s1")]})
    calls = _patch_graph(monkeypatch)
    db.raise_on_insert["email_log"] = RuntimeError("db blip")

    [res] = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID, [{"rfq_send_id": "s1", "message": "ping"}], "u1"
    )

    assert res["status"] == "sent"
    assert res["error"] is None
    assert calls["send"] == ["draft-1"]
    [nudge] = db.tables["rfq_nudges"]
    assert nudge["status"] == "sent"
    assert nudge.get("email_log_id") is None
    assert db.tables.get("email_log", []) == []


def test_even_a_failed_sent_stamp_still_reports_sent(nudge_env, monkeypatch):
    """Worst case: the email_log insert AND the rfq_nudges 'sent' update both
    fail after a successful send. The target must still report 'sent' - the
    stuck pending row is the stale-claim reclaim's problem, not a reason to
    tell the user to retry."""
    db = FakeDB({"rfq_sends": [_send("s1")]})
    calls = _patch_graph(monkeypatch)
    db.raise_on_insert["email_log"] = RuntimeError("db blip")
    real_table = db.table

    def table(name):
        q = real_table(name)
        if name == "rfq_nudges":
            def update(payload):
                raise RuntimeError("update blip")
            q.update = update
        return q

    monkeypatch.setattr(db, "table", table)
    [res] = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID, [{"rfq_send_id": "s1", "message": "ping"}], "u1"
    )

    assert res["status"] == "sent"
    assert res["error"] is None
    assert calls["send"] == ["draft-1"]
    [nudge] = db.tables["rfq_nudges"]
    assert nudge["status"] == "pending"  # stuck, reclaimable after 10 minutes


def test_concurrent_pending_nudge_blocks_with_in_progress_error(nudge_env, monkeypatch):
    """rfq_nudges_one_pending_per_send is the lock: a second batch racing the
    same send gets a per-target failure, never a second email."""
    db = FakeDB(
        {
            "rfq_sends": [_send("s1")],
            "rfq_nudges": [
                {
                    "id": "n1",
                    "rfq_send_id": "s1",
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }
    )
    calls = _patch_graph(monkeypatch)
    db.raise_on_insert["rfq_nudges"] = RuntimeError(
        'duplicate key value violates unique constraint '
        '"rfq_nudges_one_pending_per_send"'
    )

    [res] = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID, [{"rfq_send_id": "s1", "message": "ping"}], "u1"
    )

    assert res["status"] == "failed"
    assert res["error"] == "A nudge for this vendor is already in progress"
    assert calls["send"] == []  # nothing was emailed
    [row] = db.tables["rfq_nudges"]  # the racer's claim is untouched
    assert row["status"] == "pending"


class _RaiseOnce(dict):
    """FakeDB.raise_on_insert that trips only the FIRST insert into a table,
    so the retry after a stale-claim reclaim goes through."""

    def get(self, key, default=None):
        return self.pop(key, default)


def test_stale_pending_nudge_is_reclaimed_and_the_nudge_proceeds(nudge_env, monkeypatch):
    """A pending claim older than 10 minutes is a crashed run's leftover, not
    a nudge in flight: it gets marked failed 'abandoned' and this nudge takes
    over the lock and sends."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    db = FakeDB(
        {
            "rfq_sends": [_send("s1")],
            "rfq_nudges": [
                {"id": "n1", "rfq_send_id": "s1", "status": "pending",
                 "created_at": stale}
            ],
        }
    )
    calls = _patch_graph(monkeypatch)
    db.raise_on_insert = _RaiseOnce(
        {"rfq_nudges": RuntimeError("23505: duplicate key value")}
    )

    [res] = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID, [{"rfq_send_id": "s1", "message": "ping"}], "u1"
    )

    assert res["status"] == "sent"
    assert calls["send"] == ["draft-1"]
    by_id = {r["id"]: r for r in db.tables["rfq_nudges"]}
    assert by_id["n1"]["status"] == "failed"
    assert by_id["n1"]["error"] == "abandoned"
    [claimed] = [r for r in db.tables["rfq_nudges"] if r["id"] != "n1"]
    assert claimed["status"] == "sent"
    assert res["nudge_id"] == claimed["id"]


def test_nudge_validates_each_target_without_reaching_graph(nudge_env, monkeypatch):
    other_rfq = _send("s-other")
    other_rfq["rfq_id"] = "rfq-2"
    other_project = _send("s-foreign", cid="c3")
    other_project["rfqs"] = {"project_id": "p2"}
    never_sent = _send(
        "s-pending", cid="c2", status="pending", graph_message_id=None, sent_at=None
    )
    db = FakeDB({"rfq_sends": [other_rfq, other_project, never_sent]})
    monkeypatch.setattr(
        rfq_nudges.graph_email,
        "create_reply_all_draft",
        lambda mid: pytest.fail("no invalid target may reach Graph"),
    )

    results = rfq_nudges.send_nudges(
        db, PROJECT_ID, RFQ_ID,
        [
            {"rfq_send_id": "missing", "message": "x"},
            {"rfq_send_id": "s-other", "message": "x"},
            {"rfq_send_id": "s-foreign", "message": "x"},
            {"rfq_send_id": "s-pending", "message": "x"},
        ],
        "u1",
    )

    assert [r["status"] for r in results] == ["failed"] * 4
    assert results[0]["error"] == "Send not found on this RFQ"
    assert results[1]["error"] == "Send not found on this RFQ"
    assert results[2]["error"] == "Send not found on this RFQ"
    assert "never went out" in results[3]["error"]
    assert db.tables.get("rfq_nudges", []) == []


def test_splice_keeps_the_branded_card_above_the_quoted_history():
    branded = rfq_nudges.email_branding.render_vendor_email(
        "Hey Jane, quote yet?", subtitle="QUOTE REMINDER"
    )
    out = rfq_nudges._splice_quoted_history(
        branded, "<html><body><div>original RFQ text</div></body></html>"
    )
    assert out.index("Hey Jane, quote yet?") < out.index("original RFQ text")
    assert out.rstrip().endswith("</html>")
    # A draft with no quoted content leaves the branded card untouched.
    assert rfq_nudges._splice_quoted_history(branded, "") == branded


# ── Request validation + routes ────────────────────────────────────────────


def test_nudge_schema_bounds():
    ok = RFQNudgeIn(targets=[{"rfq_send_id": "s1", "message": "hello"}])
    assert ok.targets[0].message == "hello"
    with pytest.raises(ValidationError):
        RFQNudgeIn(targets=[])  # at least one target
    with pytest.raises(ValidationError):
        RFQNudgeIn(targets=[{"rfq_send_id": "s1", "message": "   "}])  # blank
    with pytest.raises(ValidationError):
        RFQNudgeIn(targets=[{"rfq_send_id": "s1", "message": "x" * 5001}])
    with pytest.raises(ValidationError):
        RFQNudgeIn(
            targets=[{"rfq_send_id": f"s{i}", "message": "x"} for i in range(51)]
        )


def _writer():
    return CurrentUser(
        id="u1", email="mats@g3.com", role=Role.ESTIMATING_ENGINEER_MATERIALS,
        is_active=True,
    )


def test_recipients_route_rejects_external_roles():
    estimator = CurrentUser(
        id="e1", email="ext@vendor.com", role=Role.ESTIMATOR, is_active=True
    )
    with pytest.raises(HTTPException) as exc:
        rfqs_router.list_rfq_recipients(PROJECT_ID, RFQ_ID, estimator)
    assert exc.value.status_code == 403


def test_recipients_route_404s_an_rfq_outside_the_project(monkeypatch):
    db = FakeDB({"rfqs": [{"id": RFQ_ID, "project_id": "another-project"}]})
    monkeypatch.setattr(rfqs_router, "get_supabase", lambda: db)
    with pytest.raises(HTTPException) as exc:
        rfqs_router.list_rfq_recipients(PROJECT_ID, RFQ_ID, _writer())
    assert exc.value.status_code == 404


def test_nudges_route_404s_an_rfq_outside_the_project(monkeypatch):
    db = FakeDB({"rfqs": [{"id": RFQ_ID, "project_id": "another-project"}]})
    monkeypatch.setattr(rfqs_router, "get_supabase", lambda: db)
    body = RFQNudgeIn(targets=[{"rfq_send_id": "s1", "message": "x"}])
    with pytest.raises(HTTPException) as exc:
        rfqs_router.send_rfq_nudges(PROJECT_ID, RFQ_ID, body, _writer())
    assert exc.value.status_code == 404


def test_recipients_route_returns_the_contract_shape(monkeypatch):
    db = FakeDB(
        {
            "rfqs": [{"id": RFQ_ID, "project_id": PROJECT_ID}],
            "rfq_sends": [_send("s1")],
        }
    )
    monkeypatch.setattr(rfqs_router, "get_supabase", lambda: db)
    out = rfqs_router.list_rfq_recipients(PROJECT_ID, RFQ_ID, _writer())
    [r] = out["recipients"]
    assert r["rfq_send_id"] == "s1"
    assert r["vendor_name"] == "Acme Supply"
    assert r["state"] == "no_reply"
    assert r["nudgeable"] is True
    assert r["last_nudge"] is None


def test_nudges_route_audits_a_batch_summary(nudge_env, monkeypatch):
    db = FakeDB(
        {
            "rfqs": [{"id": RFQ_ID, "project_id": PROJECT_ID}],
            "rfq_sends": [
                _send("s1"),
                _send("s2", cid="c2", vendor_id="v2",
                      quote_received_at="2026-08-02T00:00:00+00:00"),
            ],
        }
    )
    monkeypatch.setattr(rfqs_router, "get_supabase", lambda: db)
    batches = []
    monkeypatch.setattr(rfqs_router, "audit", lambda *a, **k: batches.append(a))
    _patch_graph(monkeypatch)

    body = RFQNudgeIn(
        targets=[
            {"rfq_send_id": "s1", "message": "hello"},
            {"rfq_send_id": "s2", "message": "hello"},
        ]
    )
    out = rfqs_router.send_rfq_nudges(PROJECT_ID, RFQ_ID, body, _writer())

    assert [r["status"] for r in out["results"]] == ["sent", "skipped_quote_received"]
    assert batches[-1][1] == "rfq.nudge_batch"
    assert batches[-1][4] == {"targets": 2, "sent": 1, "failed": 0, "skipped": 1}


# ── Ingestion guard: a nudge must never become a vendor reply ──────────────

NUDGE_SEND = {
    "id": "send-1",
    "conversation_id": "conv-1",
    "vendor_contacts": {
        "id": "c1", "name": "Jane", "email": "jane@vendor.com", "vendor_id": "v1",
    },
    "rfqs": {
        "id": RFQ_ID,
        "project_id": PROJECT_ID,
        "material_category_id": "mc1",
        "material_categories": {"name": "Switchgear"},
        "projects": {"id": PROJECT_ID, "name": "Riverside Plaza", "number": "26-104"},
    },
}


def _inbound(imid, from_addr="jane@vendor.com"):
    return {
        "id": "msg-1",
        "conversationId": "conv-1",
        "internetMessageId": imid,
        "from": {"emailAddress": {"address": from_addr}},
        "subject": "RE: 26-104 - Riverside Plaza - BOM",
        "bodyPreview": "hi",
        "receivedDateTime": "2026-08-10T12:00:00+00:00",
        "hasAttachments": False,
    }


def test_ingest_skips_a_message_matching_a_recorded_nudge(monkeypatch):
    db = FakeDB({"rfq_nudges": [{"id": "n1", "internet_message_id": "<nudge-1@g3>"}]})
    monkeypatch.setattr(rfq_inbox, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        graph_inbox,
        "get_message",
        lambda mid, **k: pytest.fail("a recognized nudge must never be fetched"),
    )
    # allow_sender_mismatch=True is the check_project_quotes path - the guard
    # must protect it too, whatever address the message appears to come from.
    assert (
        rfq_inbox._ingest_message(
            db, _inbound("<nudge-1@g3>"), {"conv-1": NUDGE_SEND},
            allow_sender_mismatch=True,
        )
        is None
    )
    assert db.tables.get("rfq_messages", []) == []


def test_ingest_still_stores_a_real_vendor_reply(monkeypatch):
    # The guard matches on internetMessageId, not on the conversation: a real
    # reply on the nudged thread still comes in.
    db = FakeDB({"rfq_nudges": [{"id": "n1", "internet_message_id": "<nudge-1@g3>"}]})
    monkeypatch.setattr(rfq_inbox, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        graph_inbox, "get_message", lambda mid, **k: {"body": {"content": "<p>on it</p>"}}
    )
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfq_inbox, "notify_role", lambda *a, **k: None)
    rfq_inbox._ingest_message(db, _inbound("<real-reply@vendor>"), {"conv-1": NUDGE_SEND})
    [message] = db.tables["rfq_messages"]
    assert message["graph_message_id"] == "msg-1"


def test_missing_internet_message_id_is_treated_as_no_match(monkeypatch):
    # No internetMessageId -> the guard must not even query; the message walks
    # on to the idempotency check exactly as before (sb=None proves the first
    # DB touch is that check, same trick as test_rfq_inbox).
    monkeypatch.setattr(rfq_inbox, "get_settings", lambda: Settings(_env_file=None))
    msg = _inbound(None)
    del msg["internetMessageId"]
    with pytest.raises(AttributeError):
        rfq_inbox._ingest_message(None, msg, {"conv-1": NUDGE_SEND})


def test_inbox_selects_carry_internet_message_id():
    # Both the poller's delta $select and the on-demand check's conversation
    # $select must fetch the field the guard matches on.
    assert "internetMessageId" in graph_inbox._DELTA_SELECT
    assert "internetMessageId" in rfq_inbox._CONVERSATION_SELECT

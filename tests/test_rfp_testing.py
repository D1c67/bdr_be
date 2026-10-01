"""The RFP Ingestion test bench (docs/RFP_TESTING.md section 12):
services/rfp_test, routers/rfp_testing and the test-mode branches of the
intake poller, the project email filer, the NGEM tick, the harvester's
`eml:` locator and the two senders, against the in-memory fake Supabase of
tests/test_rfp_email_ingest.py.

Pinned here:

- gates: 404 with the bare body while the switch is off (before auth), 401
  with it on; every route carries require_dev_it_admin and the catch-all
  limiter; a non-dev and a dev in another role are 403 rfp_testing_forbidden;
  the features map reports `rfp_testing`; the boot validators and the prod
  guard refuse a bad configuration.
- sessions: each preflight 422 code, the 409 on a second activation, the
  delta reset and the `session.activated` event, idempotent end, the
  auto-create PATCH refused on an ended session, `active_session` free
  while the switch is off.
- events: `record` never raises; the 64 KB detail cap truncates strings and
  wraps non-dicts.
- intake: in test mode the session's mailbox is the only one synced, the
  sender and started_at filters decide inserts (tagged, `listed`) versus
  `ignored` events with their reason, the heartbeat lands and the interval
  shortens; normal mode is unchanged; the sweep touches tagged rows only in
  test mode and untagged rows only in normal mode (ended-session rows are
  frozen).
- unwrap: the Outlook, Original Message, Gmail and Apple blocks, repeated
  FW prefixes, the wall-clock date forms and the fallback to the forward's
  date, `none` when no block is found, the fetch step's writes and the 5.3
  re-check (`failed` / `test_listing_skip`), the `.eml` path and the
  harvester's `eml:` locator and listing.
- redirect: `send_mail` rewrites recipients, subject, body (text, header
  with resolved identities), keeps file attachments and drops inline
  images, tags the email_log row and records the event; an empty redirect
  address refuses the send; no session means no change; `send_draft`
  PATCHes the draft before the send and never touches Supabase while the
  switch is off.
- cleanup: refused on the active session, deletes only tagged rows, keeps a
  GC another project links.
- filer: the mailbox switch, Inbox only, the sender and time filters, the
  tag, the sweep filter.
- the NGEM tick is a no-op while a session is active.
- the step strip states.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.core.deps import CurrentUser
from app.core.roles import Role
from app.routers import rfp_testing as rr
from app.services import email_ingest as filer
from app.services import graph_email
from app.services import rfp_email_harvest as eh
from app.services import rfp_email_ingest as ingest
from app.services import rfp_portal_ingest as portal
from app.services import rfp_test
from tests.test_rfp_email_ingest import (
    EXO_PASS,
    MAILBOX,
    MAILBOX2,
    RULES,
    FakeDB,
    _Query,
    _Snapshot,
)
from tests.test_rfp_email_ingest import _settings as _ingest_settings

TEST_MAILBOX = "symone@g3electrical.com"
SENDER = "t.moorejr@g3electrical.com"
REDIRECT = "baseballtom33@gmail.com"
SID = "aaaaaaaa-0000-4000-8000-000000000001"
SID_ENDED = "aaaaaaaa-0000-4000-8000-000000000002"
DEV = "dddddddd-0000-4000-8000-000000000001"
P1 = "5a000000-0000-4000-8000-000000000001"
STARTED = "2026-09-16T20:00:00+00:00"
BEFORE = "2026-09-16T19:59:00Z"
AFTER = "2026-09-16T20:01:00Z"


# ── Fakes ─────────────────────────────────────────────────────────────────


class _Q(_Query):
    """The shared fake plus integer event ids and an `at` default, so the
    cursor (`id > after`) and the newest-first orders behave."""

    def execute(self):
        if self._op == "insert" and self.table == rfp_test.TABLE_EVENTS:
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            for p in payloads:
                self.db.event_seq += 1
                p["id"] = self.db.event_seq
                p.setdefault("at", datetime.now(timezone.utc).isoformat())
        return super().execute()


class DB(FakeDB):
    def __init__(self, tables=None):
        super().__init__(tables)
        self.event_seq = 0

    def table(self, name):
        return _Q(self, name)

    def events(self, kind=None, source=None):
        rows = self.tables.get(rfp_test.TABLE_EVENTS, [])
        return [
            r for r in rows
            if (kind is None or r["kind"] == kind) and (source is None or r["source"] == source)
        ]


def _session(**over):
    row = {
        "id": SID, "status": "active", "name": "Wave 1", "started_by": DEV,
        "started_at": STARTED, "ended_at": None, "mailbox": TEST_MAILBOX, "sender": SENDER,
        "redirect_to": REDIRECT, "auto_create": False, "intake_last_tick_at": None,
        "filer_last_tick_at": None, "cleanup_started_at": None, "cleanup_finished_at": None,
        "cleanup_report": None, "created_at": STARTED,
    }
    row.update(over)
    return row


def _settings(**over):
    """The namespace every module under test reads (the intake's own
    settings shape plus the bench's fields)."""
    base = dict(vars(_ingest_settings()))
    base.update(
        rfp_ingest_enabled=True,
        rfp_testing_enabled=True,
        rfp_testing_mailbox=TEST_MAILBOX,
        rfp_testing_sender=SENDER,
        rfp_testing_redirect_to=REDIRECT,
        rfp_testing_poll_seconds=15,
        rfp_testing_unwrap_scan_chars=4000,
        inbound_attachment_max_bytes=25 * 1024 * 1024,
        rfp_email_ingestion_inboxes=[MAILBOX, MAILBOX2],
        rfp_email_ingestion_poll_interval_seconds=120,
        rfp_email_ingestion_lookback_days=3,
        rfp_email_ingestion_reset_lookback_days=7,
        rfp_email_ingestion_internal_domain_set={"g3electrical.com"},
        rfp_email_ingestion_blocked_domain_set={"buildingconnected.com", "ionwave.net"},
        rfp_email_ingestion_confidence_threshold=0.85,
        rfp_email_ingestion_classify_max_body_chars=12000,
        rfp_email_ingestion_classify_retry_seconds=300,
        rfp_email_ingestion_classify_max_attempts=8,
        rfp_email_ingestion_lease_seconds=600,
        rfp_create_auto_enabled=False,
        rfp_create_poll_seconds=30,
        rfp_create_claim_seconds=600,
        rfp_create_notes_max_chars=4000,
        rfp_create_files_queue_priority=160,
        rfp_match_sibling_window_minutes=0,
        email_body_max_chars=100_000,
        email_ingest_mailbox="pm@g3electrical.com",
        email_ingest_enabled=True,
        email_ingest_poll_interval_seconds=120,
        email_ingest_lookback_days=1,
        email_ingest_reset_lookback_days=7,
        email_match_max_attempts=5,
        ms_client_id="client",
        ms_sender=MAILBOX,
        rfp_ngem_enabled=False,
        rfp_ngem_active=True,
        default_rate_limit_per_min=240,
        full_self_hosted_llms_enabled=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def db():
    return DB({
        rfp_test.TABLE_SESSIONS: [_session()],
        rfp_test.TABLE_EVENTS: [],
        "rfp_authorized_senders": RULES,
        "gc_contacts": [{"id": "c1", "gc_id": "gc-1", "email": "pm@gc.example", "name": "Sam Lee", "created_at": "x"}],
        "general_contractors": [{"id": "gc-1", "name": "Meridian Builders"}],
        "vendor_contacts": [{"id": "v1", "vendor_id": "ven-1", "email": "quotes@graybar.example", "name": "Pat Q", "created_at": "x"}],
        "vendors": [{"id": "ven-1", "name": "Graybar"}],
        "profiles": [{"id": DEV, "full_name": "Tom Moore", "email": SENDER, "role": "it_admin"},
                     {"id": "u-exec", "full_name": "Jane Doe", "email": "jane@g3electrical.com", "role": "executive"}],
        "notifications": [],
        "rfq_sends": [],
        "projects": [],
        "project_gcs": [],
        "proposal_sends": [],
        "rfp_project_matches": [],
        "graph_sync_state": [],
        "email_log": [],
    })


@pytest.fixture(autouse=True)
def _defaults(monkeypatch, db):
    settings = _settings()
    for module in (rfp_test, ingest, filer, graph_email, portal, rr, eh):
        monkeypatch.setattr(module, "get_settings", lambda s=settings: s)
        if hasattr(module, "get_supabase"):
            monkeypatch.setattr(module, "get_supabase", lambda d=db: d)
    monkeypatch.setattr(ingest, "audit", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(filer, "audit", lambda *a, **k: None)
    monkeypatch.setattr(ingest.llm, "is_configured", lambda feature, settings=None: True)
    monkeypatch.setattr(ingest.llm, "active_model", lambda feature, settings=None: "test-model")
    monkeypatch.setattr(ingest.llm_health, "cached", lambda settings=None, force=False: _Snapshot())
    monkeypatch.setattr(ingest, "_list_attachment_meta", lambda mailbox, mid, **k: [])
    return settings


def _user(role=Role.IT_ADMIN, is_dev=True, uid=DEV):
    return CurrentUser(id=uid, email="dev@g3.com", role=role, is_active=True, is_dev=is_dev)


# ── Gates (section 2) ─────────────────────────────────────────────────────


@contextmanager
def _flag(enabled: bool):
    previous = {k: os.environ.get(k) for k in (
        "RFP_TESTING_ENABLED", "RFP_TESTING_MAILBOX", "RFP_TESTING_SENDER", "RFP_TESTING_REDIRECT_TO",
    )}
    os.environ["RFP_TESTING_ENABLED"] = "true" if enabled else "false"
    os.environ["RFP_TESTING_MAILBOX"] = TEST_MAILBOX
    os.environ["RFP_TESTING_SENDER"] = SENDER
    os.environ["RFP_TESTING_REDIRECT_TO"] = REDIRECT
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.fixture(scope="module")
def client() -> TestClient:
    import app.main

    return TestClient(app.main.app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/rfp-testing/state"),
        ("POST", "/rfp-testing/sessions"),
        ("POST", f"/rfp-testing/sessions/{SID}/end"),
        ("PATCH", f"/rfp-testing/sessions/{SID}"),
        ("POST", f"/rfp-testing/sessions/{SID}/cleanup"),
        ("GET", f"/rfp-testing/sessions/{SID}/emails"),
        ("GET", f"/rfp-testing/sessions/{SID}/ignored"),
        ("GET", f"/rfp-testing/sessions/{SID}/filed"),
        ("GET", f"/rfp-testing/sessions/{SID}/mail-out"),
        ("GET", f"/rfp-testing/sessions/{SID}/projects"),
        ("GET", f"/rfp-testing/projects/{P1}"),
    ],
)
def test_flag_off_404s_before_auth_and_flag_on_asks_for_a_token(client, monkeypatch, method, path):
    # The module fixture patched the router's get_settings; the TestClient
    # must see the real, env-driven one for this test.
    monkeypatch.setattr(rr, "get_settings", get_settings)
    with _flag(False):
        resp = client.request(method, path)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}
    with _flag(True):
        assert client.request(method, path).status_code == 401


def test_every_route_carries_the_gates_and_the_catch_all_limiter():
    assert rr.router.prefix == "/rfp-testing"
    found = {(m, r.path) for r in rr.router.routes for m in r.methods}
    assert found == {
        ("GET", "/rfp-testing/state"),
        ("POST", "/rfp-testing/sessions"),
        ("POST", "/rfp-testing/sessions/{session_id}/end"),
        ("PATCH", "/rfp-testing/sessions/{session_id}"),
        ("POST", "/rfp-testing/sessions/{session_id}/cleanup"),
        ("GET", "/rfp-testing/sessions/{session_id}/emails"),
        ("GET", "/rfp-testing/sessions/{session_id}/ignored"),
        ("GET", "/rfp-testing/sessions/{session_id}/filed"),
        ("GET", "/rfp-testing/sessions/{session_id}/mail-out"),
        ("GET", "/rfp-testing/sessions/{session_id}/projects"),
        ("GET", "/rfp-testing/projects/{project_id}"),
    }
    for route in rr.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert rr.require_rfp_testing in calls, route.path
        assert rr.require_dev_it_admin in calls, route.path
        assert rr.rfp_testing_rate_limit in calls, route.path


def test_require_dev_it_admin_refuses_non_dev_and_other_roles():
    assert asyncio.run(rr.require_dev_it_admin(_user())).id == DEV
    for user in (_user(is_dev=False), _user(role=Role.EXECUTIVE), _user(role=Role.EXECUTIVE, is_dev=False)):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(rr.require_dev_it_admin(user))
        assert exc.value.status_code == 403
        assert exc.value.detail == "rfp_testing_forbidden"
        assert exc.value.headers["X-Error-Code"] == "rfp_testing_forbidden"


def test_features_map_reports_rfp_testing_under_the_master_switch(monkeypatch):
    from app.core import features

    monkeypatch.setattr(features, "get_settings", lambda: _settings(rfp_testing_enabled=True, rfp_ingest_enabled=True,
                                                                       bidding_enabled=True, pm_enabled=True,
                                                                       certified_payroll_enabled=True,
                                                                       bid_file_splitter_enabled=False,
                                                                       rfp_email_ingestion_enabled=False))
    assert features.enabled_map()["rfp_testing"] is True
    monkeypatch.setattr(features, "get_settings", lambda: _settings(rfp_testing_enabled=True, rfp_ingest_enabled=False,
                                                                       bidding_enabled=True, pm_enabled=True,
                                                                       certified_payroll_enabled=True,
                                                                       bid_file_splitter_enabled=False,
                                                                       rfp_email_ingestion_enabled=False))
    assert features.enabled_map()["rfp_testing"] is False


def _boot(**over):
    base = dict(
        _env_file=None, rfp_testing_enabled=True, rfp_testing_mailbox=TEST_MAILBOX,
        rfp_testing_sender=SENDER, rfp_testing_redirect_to=REDIRECT,
        rfp_email_ingestion_inboxes_allowed=f"{MAILBOX},{MAILBOX2}", email_ingest_mailbox="pm@g3electrical.com",
    )
    base.update(over)
    return Settings(**base)


def test_boot_validators_refuse_a_bad_bench_configuration():
    _boot()  # the good one
    with pytest.raises(ValueError, match="RFP_TESTING_MAILBOX is empty"):
        _boot(rfp_testing_mailbox="")
    with pytest.raises(ValueError, match="not an email address"):
        _boot(rfp_testing_redirect_to="nobody")
    with pytest.raises(ValueError, match="dedicated"):
        _boot(rfp_testing_mailbox=MAILBOX)
    with pytest.raises(ValueError, match="dedicated"):
        _boot(rfp_testing_mailbox="pm@g3electrical.com")
    with pytest.raises(ValueError, match="RFP_TESTING_POLL_SECONDS"):
        _boot(rfp_testing_poll_seconds=2)
    # Off: the addresses are not checked at all.
    Settings(_env_file=None, rfp_testing_enabled=False, rfp_testing_mailbox="")


def test_prod_guard_refuses_the_bench():
    with pytest.raises(ValueError, match="RFP_TESTING_ENABLED=true in production"):
        _boot(environment="production", supabase_service_role_key="k", mfa_required=True,
              preview_engine="graph")


# ── Sessions (section 3) ──────────────────────────────────────────────────


def test_active_session_is_free_while_the_switch_is_off(monkeypatch):
    class Boom:
        def table(self, name):
            raise AssertionError("the session table must not be read while the switch is off")

    monkeypatch.setattr(rfp_test, "get_settings", lambda: _settings(rfp_testing_enabled=False))
    assert rfp_test.active_session(Boom()) is None


def test_activate_preflight_codes(db, monkeypatch):
    db.tables[rfp_test.TABLE_SESSIONS] = []
    cases = [
        (_settings(rfp_ingest_enabled=False), "rfp_testing_ingest_off"),
        (_settings(ms_client_id=""), "rfp_testing_graph_off"),
        (_settings(rfp_testing_redirect_to="not an address"), "rfp_testing_redirect_invalid"),
    ]
    for settings, code in cases:
        monkeypatch.setattr(rfp_test, "get_settings", lambda s=settings: s)
        with pytest.raises(rfp_test.RfpTestError) as exc:
            rfp_test.activate(db, actor_id=DEV, name=None, auto_create=False)
        assert exc.value.code == code and exc.value.status == 422
    monkeypatch.setattr(rfp_test, "get_settings", lambda: _settings())

    def missing(method, path, **k):
        request = httpx.Request("GET", "https://graph.microsoft.com" + path)
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
    monkeypatch.setattr(rfp_test, "graph_request", missing)
    with pytest.raises(rfp_test.RfpTestError) as exc:
        rfp_test.activate(db, actor_id=DEV, name=None, auto_create=False)
    assert exc.value.code == "rfp_testing_mailbox_unreachable"
    assert str(exc.value) == "rfp_testing_mailbox_unreachable: 404"

    def down(method, path, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(rfp_test, "graph_request", down)
    with pytest.raises(rfp_test.RfpTestError) as exc:
        rfp_test.activate(db, actor_id=DEV, name=None, auto_create=False)
    assert str(exc.value) == "rfp_testing_mailbox_unreachable: ConnectError"
    assert db.tables[rfp_test.TABLE_SESSIONS] == []


def _graph_ok(monkeypatch):
    calls = []

    def ok(method, path, **k):
        calls.append((method, path))
        return SimpleNamespace(status_code=200, json=lambda: {})
    monkeypatch.setattr(rfp_test, "graph_request", ok)
    return calls


def test_activate_inserts_resets_delta_links_and_records(db, monkeypatch):
    db.tables[rfp_test.TABLE_SESSIONS] = []
    db.tables["graph_sync_state"] = [
        {"id": f"rfp-mail:{TEST_MAILBOX}:inbox", "delta_link": "old"},
        {"id": f"pm-mail:{TEST_MAILBOX}:inbox", "delta_link": "old"},
        {"id": f"rfp-mail:{MAILBOX}:inbox", "delta_link": "keep"},
    ]
    calls = _graph_ok(monkeypatch)
    session = rfp_test.activate(db, actor_id=DEV, name="  Wave 1 ", auto_create=True)
    assert calls == [("GET", f"/users/{TEST_MAILBOX}/mailFolders/inbox")]
    assert session["status"] == "active" and session["name"] == "Wave 1"
    assert session["mailbox"] == TEST_MAILBOX and session["sender"] == SENDER
    assert session["redirect_to"] == REDIRECT and session["auto_create"] is True
    assert [r["id"] for r in db.tables["graph_sync_state"]] == [f"rfp-mail:{MAILBOX}:inbox"]
    events = db.events(kind="activated")
    assert len(events) == 1 and events[0]["source"] == "session"
    assert events[0]["detail"]["auto_create"] is True
    # A second activation is the 409.
    with pytest.raises(rfp_test.RfpTestError) as exc:
        rfp_test.activate(db, actor_id=DEV, name=None, auto_create=False)
    assert exc.value.code == "rfp_testing_already_active" and exc.value.status == 409


def test_end_session_is_idempotent_and_records(db):
    ended = rfp_test.end_session(db, SID, actor_id=DEV)
    assert ended["status"] == "ended" and ended["ended_by"] == DEV and ended["ended_at"]
    again = rfp_test.end_session(db, SID, actor_id="someone-else")
    assert again["ended_by"] == DEV
    assert len(db.events(kind="ended")) == 1
    with pytest.raises(LookupError):
        rfp_test.end_session(db, SID_ENDED, actor_id=DEV)


def test_set_auto_create_refuses_an_ended_session(db):
    assert rfp_test.set_auto_create(db, SID, True)["auto_create"] is True
    rfp_test.end_session(db, SID, actor_id=DEV)
    with pytest.raises(rfp_test.RfpTestError) as exc:
        rfp_test.set_auto_create(db, SID, False)
    assert exc.value.code == "rfp_testing_session_ended"


def test_sync_prefixes_match_the_pollers():
    assert rfp_test.INTAKE_SYNC_PREFIX == ingest._SYNC_PREFIX
    assert rfp_test.FILER_SYNC_PREFIX == filer._SYNC_PREFIX


def test_message_filter_reasons():
    session = _session()
    assert rfp_test.message_filter(session, "T.MooreJr@g3electrical.com", AFTER) is None
    assert rfp_test.message_filter(session, SENDER, STARTED) is None  # at started_at counts
    assert rfp_test.message_filter(session, SENDER, BEFORE) == "before_session"
    assert rfp_test.message_filter(session, "pm@gc.example", AFTER) == "not_test_sender"
    assert rfp_test.message_filter(session, None, AFTER) == "not_test_sender"


def test_poll_seconds_shortens_only_in_test_mode():
    assert rfp_test.poll_seconds(_settings(), 120, True) == 15
    assert rfp_test.poll_seconds(_settings(), 120, False) == 120
    assert rfp_test.poll_seconds(_settings(rfp_testing_enabled=False), 120, True) == 120


# ── Events (section 7) ────────────────────────────────────────────────────


def test_record_never_raises():
    class Boom:
        def table(self, name):
            raise RuntimeError("PostgREST is away")

    rfp_test.record(Boom(), session_id=SID, source="intake", kind="x", title="t")
    rfp_test.record(Boom(), session_id=None, source="intake", kind="x", title="t")


def test_record_shapes_the_row(db):
    rfp_test.record(db, session_id=SID, source="intake", kind="auth", title="t" * 400, level="bogus",
                    detail={"when": datetime(2026, 9, 16, tzinfo=timezone.utc)}, rfp_email_id="e1")
    row = db.events()[0]
    assert row["level"] == "info" and len(row["title"]) == 300 and row["rfp_email_id"] == "e1"
    assert row["detail"] == {"when": "2026-09-16 00:00:00+00:00"}


def test_cap_detail_truncates_long_strings_and_wraps_non_dicts():
    assert rfp_test.cap_detail(None) == {}
    assert rfp_test.cap_detail("x") == {"value": "x"}
    big = {"prompt": "a" * 100_000, "keep": "short", "nested": {"body": "b" * 70_000}}
    capped = rfp_test.cap_detail(big)
    assert capped["truncated"] is True and capped["keep"] == "short"
    assert capped["prompt"].endswith("[truncated]") and capped["nested"]["body"].endswith("[truncated]")
    assert len(json.dumps(capped).encode()) <= 64 * 1024
    huge = {f"k{i}": "v" * 300 for i in range(2000)}
    capped = rfp_test.cap_detail(huge)
    assert capped["truncated"] is True and len(json.dumps(capped).encode()) <= 64 * 1024


# ── Intake sync (section 4.1) ─────────────────────────────────────────────


def _msg(**over):
    m = {
        "id": "g1",
        "conversationId": "conv-1",
        "internetMessageId": "<M1@GC.example>",
        "from": {"emailAddress": {"name": "Tom Moore", "address": SENDER}},
        "toRecipients": [{"emailAddress": {"address": TEST_MAILBOX}}],
        "ccRecipients": [],
        "subject": "FW: ITB",
        "receivedDateTime": AFTER,
        "hasAttachments": False,
    }
    m.update(over)
    return m


def test_poll_once_in_test_mode_syncs_only_the_test_mailbox_and_filters(db, monkeypatch):
    synced = []

    def delta(delta_link, *, mailbox, folder, since_days, select):
        synced.append((mailbox, since_days))
        return [
            _msg(),
            _msg(id="g2", internetMessageId="<M2@x>", receivedDateTime=BEFORE),
            _msg(id="g3", internetMessageId="<M3@x>",
                 **{"from": {"emailAddress": {"address": "pm@gc.example"}}}),
            _msg(id="g4", internetMessageId="<M4@x>", conversationId="conv-rfq"),
            {"@removed": {"reason": "deleted"}, "id": "g5"},
        ], "delta-new"
    monkeypatch.setattr(ingest.graph_inbox, "delta_inbox", delta)
    db.tables["rfq_sends"] = [{"id": "s1", "conversation_id": "conv-rfq"}]
    swept = []
    monkeypatch.setattr(ingest, "_sweep", lambda sb, **k: swept.append(k) or ingest._TickStats())

    assert ingest.poll_once() is True
    assert synced == [(TEST_MAILBOX, 1)]
    rows = db.tables["rfp_emails"]
    assert [r["internet_message_id"] for r in rows] == ["m1@gc.example"]
    assert rows[0]["test_session_id"] == SID and rows[0]["from_address"] == SENDER
    ignored = {e["detail"]["graph_message_id"]: e["detail"]["reason"] for e in db.events(kind="ignored")}
    assert ignored == {"g2": "before_session", "g3": "not_test_sender", "g4": "rfq_thread"}
    listed = db.events(kind="listed")
    assert len(listed) == 1 and listed[0]["rfp_email_id"] == rows[0]["id"]
    assert swept and swept[0]["session"]["id"] == SID
    session = db.tables[rfp_test.TABLE_SESSIONS][0]
    assert session["intake_last_tick_at"] is not None
    assert next(r for r in db.tables["graph_sync_state"] if r["id"] == f"rfp-mail:{TEST_MAILBOX}:inbox")["delta_link"] == "delta-new"


def test_poll_once_normal_mode_is_unchanged_without_a_session(db, monkeypatch):
    db.tables[rfp_test.TABLE_SESSIONS] = [_session(status="ended")]
    synced = []

    def delta(delta_link, *, mailbox, folder, since_days, select):
        synced.append((mailbox, since_days))
        return [_msg(**{"from": {"emailAddress": {"address": "pm@gc.example"}}})], "d"
    monkeypatch.setattr(ingest.graph_inbox, "delta_inbox", delta)
    monkeypatch.setattr(ingest, "_sweep", lambda sb, **k: ingest._TickStats())
    assert ingest.poll_once() is False
    assert synced == [(MAILBOX, 3), (MAILBOX2, 3)]
    assert db.tables["rfp_emails"][0].get("test_session_id") is None
    assert db.events() == []


def test_poll_once_runs_in_test_mode_with_no_real_mailbox_listed(db, monkeypatch):
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_email_ingestion_inboxes=[]))
    synced = []
    monkeypatch.setattr(ingest.graph_inbox, "delta_inbox",
                        lambda *a, mailbox, **k: synced.append(mailbox) or ([], "d"))
    monkeypatch.setattr(ingest, "_sweep", lambda sb, **k: ingest._TickStats())
    assert ingest.poll_once() is True and synced == [TEST_MAILBOX]
    db.tables[rfp_test.TABLE_SESSIONS] = []
    assert ingest.poll_once() is False  # nothing to watch at all


def test_should_skip_sender_allow_addresses_bypasses_internal_only():
    kw = dict(watched=[MAILBOX], internal_domains={"g3electrical.com"}, blocked_domains={"ionwave.net"})
    assert ingest.should_skip_sender(SENDER, **kw) is True
    assert ingest.should_skip_sender(SENDER, allow_addresses={SENDER}, **kw) is False
    assert ingest.should_skip_sender("other@g3electrical.com", allow_addresses={SENDER}, **kw) is True
    assert ingest.should_skip_sender("x@ionwave.net", allow_addresses={"x@ionwave.net"}, **kw) is True


def _email(**over):
    row = {
        "id": "e1", "internet_message_id": "m1@x", "primary_mailbox": TEST_MAILBOX,
        "from_address": SENDER, "from_name": "Tom Moore", "subject": "FW: Invitation to Bid: Riverside Plaza",
        "body_text": None, "received_at": AFTER, "has_attachments": False, "attachments_meta": [],
        "status": "received", "attempts": 0, "last_error": None, "next_attempt_at": None,
        "excluded_project_ids": [], "test_session_id": SID,
    }
    row.update(over)
    return row


def _seed(db, row):
    db.tables.setdefault("rfp_emails", []).append(row)
    db.tables.setdefault("rfp_email_sightings", []).append(
        {"id": "s-" + row["id"], "rfp_email_id": row["id"], "mailbox": row["primary_mailbox"],
         "graph_message_id": "g-" + row["id"], "created_at": "x"})
    return row


def _row(db, email_id="e1"):
    return next(r for r in db.tables["rfp_emails"] if r["id"] == email_id)


def test_sweep_touches_tagged_rows_only_in_test_mode_and_untagged_only_in_normal_mode(db, monkeypatch):
    _seed(db, _email(id="e-real", test_session_id=None, internet_message_id="a@x"))
    _seed(db, _email(id="e-active", internet_message_id="b@x"))
    _seed(db, _email(id="e-ended", test_session_id=SID_ENDED, internet_message_id="c@x"))
    seen = []
    monkeypatch.setattr(ingest, "_process_email", lambda sb, row, **k: seen.append((row["id"], k.get("auto_create"))))

    ingest._sweep(db, lease_key=None, stats=ingest._TickStats(), session=None)
    assert seen == [("e-real", False)]
    seen.clear()
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats(), session=_session(auto_create=True))
    assert seen == [("e-active", True)]
    # With the switch off the normal sweep never names the column.
    seen.clear()
    monkeypatch.setattr(ingest, "get_settings", lambda: _settings(rfp_testing_enabled=False))
    ingest._sweep(db, lease_key=None, stats=ingest._TickStats(), session=None)
    assert sorted(s[0] for s in seen) == ["e-active", "e-ended", "e-real"]


# ── Forward unwrap (section 5) ────────────────────────────────────────────


OUTLOOK = (
    "Please take a look.\n\nFrom: Jane PM <jane@gc.example>\n"
    "Sent: Tuesday, September 15, 2026 9:12 AM\nTo: Tom Moore <t.moorejr@g3electrical.com>\n"
    "Subject: Invitation to Bid: Riverside Plaza\n\nYou are invited to bid.\n"
)
ORIGINAL = (
    "-----Original Message-----\nFrom: pm@gc.example\nSent: Tue, 15 Sep 2026 09:12:00 -0700\n"
    "Subject: FW: FW : Riverside Plaza\n\nBody here.\n"
)
GMAIL = (
    "---------- Forwarded message ---------\nFrom: Jane PM <jane@gc.example>\n"
    "Date: Tue, Sep 15, 2026 at 9:12 AM\nSubject: Fwd: Riverside Plaza\nTo: <t.moorejr@g3electrical.com>\n\n"
    "Body here.\n"
)
APPLE = (
    "Begin forwarded message:\n\nFrom: Jane PM <jane@gc.example>\nSubject: Riverside Plaza\n"
    "Date: September 15, 2026 at 9:12:34 AM PDT\nTo: Tom <t.moorejr@g3electrical.com>\n\nBody here.\n"
)


def test_parse_inline_forward_recognises_the_four_blocks():
    outlook = rfp_test.parse_inline_forward(OUTLOOK, 4000)
    assert outlook.marker == "from" and outlook.note == "Please take a look."
    assert outlook.headers["from"] == "Jane PM <jane@gc.example>" and outlook.body == "You are invited to bid."
    original = rfp_test.parse_inline_forward(ORIGINAL, 4000)
    assert original.marker == "original message" and original.note is None and original.body == "Body here."
    gmail = rfp_test.parse_inline_forward(GMAIL, 4000)
    assert gmail.marker == "forwarded message" and gmail.headers["date"] == "Tue, Sep 15, 2026 at 9:12 AM"
    apple = rfp_test.parse_inline_forward(APPLE, 4000)
    assert apple.marker == "begin forwarded message" and apple.headers["subject"] == "Riverside Plaza"
    # Prose "From:" without a subject / date line is not a block; a block
    # beyond the scan window is quoted history.
    assert rfp_test.parse_inline_forward("From: nobody in particular\nnothing else", 4000) is None
    assert rfp_test.parse_inline_forward("x" * 5000 + "\n" + OUTLOOK, 4000) is None
    assert rfp_test.parse_inline_forward("", 4000) is None


def test_strip_forward_prefixes_and_parse_address():
    assert rfp_test.strip_forward_prefixes("FW: Fwd: FW : TR: Riverside") == "Riverside"
    assert rfp_test.strip_forward_prefixes("RE: Riverside") == "RE: Riverside"
    assert rfp_test.strip_forward_prefixes(None) is None
    assert rfp_test.parse_address("Jane PM <jane@gc.example>") == ("Jane PM", "jane@gc.example")
    assert rfp_test.parse_address("pm@GC.example") == (None, "pm@gc.example")
    assert rfp_test.parse_address("Jane PM [mailto:jane@gc.example]") == ("Jane PM", "jane@gc.example")
    assert rfp_test.parse_address("Jane PM") == ("Jane PM", None)


def test_parse_forward_date_forms():
    rfc = rfp_test.parse_forward_date("Tue, 15 Sep 2026 09:12:00 -0700")
    assert rfc.isoformat() == "2026-09-15T09:12:00-07:00"
    outlook = rfp_test.parse_forward_date("Tuesday, September 15, 2026 9:12 AM")
    assert outlook.astimezone(timezone.utc).isoformat() == "2026-09-15T16:12:00+00:00"
    gmail = rfp_test.parse_forward_date("Tue, Sep 15, 2026 at 9:12 AM")
    assert gmail.astimezone(timezone.utc).isoformat() == "2026-09-15T16:12:00+00:00"
    apple = rfp_test.parse_forward_date("September 15, 2026 at 9:12:34 AM PDT")
    assert apple.astimezone(timezone.utc).isoformat() == "2026-09-15T16:12:34+00:00"
    # Afternoon forms: the lenient RFC parser reads these but drops the PM
    # marker (the live smoke showed "2:10 PM" landing as 2:10 AM), so the
    # explicit wall-clock forms must win over it.
    outlook_pm = rfp_test.parse_forward_date("Wednesday, September 16, 2026 2:10 PM")
    assert outlook_pm.astimezone(timezone.utc).isoformat() == "2026-09-16T21:10:00+00:00"
    gmail_pm = rfp_test.parse_forward_date("Wed, Sep 16, 2026 at 4:40 PM")
    assert gmail_pm.astimezone(timezone.utc).isoformat() == "2026-09-16T23:40:00+00:00"
    assert rfp_test.parse_forward_date("yesterday-ish") is None
    assert rfp_test.parse_forward_date("") is None


def test_unwrap_forward_inline_none_and_fields():
    row = _email()
    full = {"body": {"content": OUTLOOK}}
    out = rfp_test.unwrap_forward(row, full, [], TEST_MAILBOX, "g1")
    assert out.method == "inline" and out.from_address == "jane@gc.example" and out.from_name == "Jane PM"
    assert out.subject == "Invitation to Bid: Riverside Plaza"
    assert out.received_at == "2026-09-15T09:12:00-07:00" and out.forwarder_note == "Please take a look."
    fields = rfp_test.unwrap_fields(row, out)
    assert fields["from_address"] == "jane@gc.example" and fields["body_text"] == "You are invited to bid."
    assert fields["forward_meta"]["raw_from_address"] == SENDER and fields["forward_meta"]["unwrap"] == "inline"
    assert fields["forward_meta"]["raw_subject"] == row["subject"] and "attachments_meta" not in fields
    # An unparseable date keeps the forward's own receivedDateTime.
    body = OUTLOOK.replace("Sent: Tuesday, September 15, 2026 9:12 AM", "Sent: some day")
    out = rfp_test.unwrap_forward(row, {"body": {"content": body}}, [], TEST_MAILBOX, "g1")
    assert out.method == "inline" and out.received_at == AFTER
    # No block: the forwarder stays the sender, with the FW prefix kept.
    out = rfp_test.unwrap_forward(row, {"body": {"content": "Just a note."}}, [], TEST_MAILBOX, "g1")
    assert out.method == "none" and out.from_address == SENDER and out.subject == row["subject"]
    assert rfp_test.unwrap_fields(row, out)["forward_meta"]["unwrap"] == "none"


def _eml_bytes(*, html=False, attachments=1) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Jane PM <jane@gc.example>"
    msg["To"] = "Tom <t.moorejr@g3electrical.com>"
    msg["Subject"] = "FW: Riverside Plaza"
    msg["Date"] = "Tue, 15 Sep 2026 09:12:00 -0700"
    if html:
        msg.set_content("<p>You are <b>invited</b>.</p><br><p>Bid Friday.</p>", subtype="html")
    else:
        msg.set_content("You are invited to bid.")
    for i in range(attachments):
        msg.add_attachment(b"%PDF-1.4 " + bytes([i]) * 10, maintype="application", subtype="pdf",
                           filename=f"Plans-{i}.pdf")
    return bytes(msg)


def test_parse_eml_and_part_payload():
    headers, body, parts = rfp_test.parse_eml(_eml_bytes(attachments=2))
    assert headers["from"] == "Jane PM <jane@gc.example>" and headers["subject"] == "FW: Riverside Plaza"
    assert body.strip() == "You are invited to bid."
    assert [p["name"] for p in parts] == ["Plans-0.pdf", "Plans-1.pdf"] and parts[1]["index"] == 1
    assert parts[0]["content_type"] == "application/pdf" and parts[0]["size"] == 19
    name, payload = rfp_test.eml_part_payload(_eml_bytes(attachments=2), 1)
    assert name == "Plans-1.pdf" and payload.startswith(b"%PDF-1.4 \x01")
    with pytest.raises(LookupError):
        rfp_test.eml_part_payload(_eml_bytes(), 5)
    _, html_body, _ = rfp_test.parse_eml(_eml_bytes(html=True))
    assert "You are invited." in html_body and "Bid Friday." in html_body and "<b>" not in html_body


def test_unwrap_forward_eml_path_lists_the_parts(monkeypatch):
    monkeypatch.setattr(rfp_test, "fetch_eml_bytes",
                        lambda mailbox, mid, att_id, *, max_bytes: _eml_bytes(attachments=2))
    row = _email(has_attachments=True)
    meta = [{"id": "att-1", "name": "Invitation.eml", "contentType": "message/rfc822", "size": 900,
             "kind": "file", "inline": False}]
    out = rfp_test.unwrap_forward(row, {"body": {"content": "See attached.\n"}}, meta, TEST_MAILBOX, "g1")
    assert out.method == "eml" and out.from_address == "jane@gc.example"
    assert out.subject == "Riverside Plaza" and out.received_at == "2026-09-15T09:12:00-07:00"
    assert out.eml_attachment_id == "att-1" and [p["name"] for p in out.eml_parts] == ["Plans-0.pdf", "Plans-1.pdf"]
    assert out.forwarder_note == "See attached."
    fields = rfp_test.unwrap_fields(row, out)
    assert [a["name"] for a in fields["attachments_meta"]] == ["Plans-0.pdf", "Plans-1.pdf"]
    assert fields["has_attachments"] is True and fields["forward_meta"]["eml_attachment_id"] == "att-1"
    # An unreadable attached message falls through to the inline block / none.
    monkeypatch.setattr(rfp_test, "fetch_eml_bytes",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gone")))
    out = rfp_test.unwrap_forward(row, {"body": {"content": OUTLOOK}}, meta, TEST_MAILBOX, "g1")
    assert out.method == "inline"


def test_eml_locator_download_and_listing(monkeypatch, tmp_path):
    monkeypatch.setattr(rfp_test, "fetch_eml_bytes",
                        lambda mailbox, mid, att_id, *, max_bytes: _eml_bytes(attachments=2))
    locator = eh.eml_locator(TEST_MAILBOX, "g1", "att-1", 1)
    assert locator == f"eml:{TEST_MAILBOX}|g1|att-1|1"
    session = eh.EmailFileSession(tmp_path)
    dest = tmp_path / "part.bin"
    assert session.download(locator, dest, max_bytes=10_000) == 19
    assert dest.read_bytes().startswith(b"%PDF-1.4 \x01")
    with pytest.raises(eh.cloud_folders.CloudForbidden):
        session.download("eml:bad", dest, max_bytes=10)
    with pytest.raises(eh.cloud_folders.CloudForbidden):
        session.download(locator, tmp_path / "small.bin", max_bytes=5)
    # An unwrapped .eml row lists the parts instead of the forward's listing.
    db = DB({"rfp_email_sightings": [
        {"id": "s1", "rfp_email_id": "e1", "mailbox": TEST_MAILBOX, "graph_message_id": "g1", "created_at": "x"}]})
    email = _email(forward_meta={"unwrap": "eml", "eml_attachment_id": "att-1",
                                 "eml_parts": [{"index": 0, "name": "Plans-0.pdf", "content_type": "application/pdf", "size": 19},
                                               {"index": 1, "name": "Plans-1.pdf", "content_type": "application/pdf", "size": 19}]})
    meta, mailbox, message_id = eh._list_attachments(db, email)
    assert (mailbox, message_id) == (TEST_MAILBOX, "g1")
    assert [m["id"] for m in meta] == [eh.eml_locator(TEST_MAILBOX, "g1", "att-1", 0), locator]
    assert all(m["kind"] == "file" and m["inline"] is False for m in meta)


def test_step_received_unwraps_a_test_row_and_records(db, monkeypatch):
    monkeypatch.setattr(
        ingest.graph_inbox, "get_message",
        lambda *a, **k: {"body": {"content": OUTLOOK}, "bodyPreview": "Please",
                         "internetMessageHeaders": [{"name": "Authentication-Results", "value": EXO_PASS}]},
    )
    _seed(db, _email())
    assert ingest._step_received(db, dict(_row(db))) == "auth"
    row = _row(db)
    assert row["status"] == "auth" and row["from_address"] == "jane@gc.example"
    assert row["subject"] == "Invitation to Bid: Riverside Plaza" and row["body_text"] == "You are invited to bid."
    assert row["forward_meta"]["raw_from_address"] == SENDER and row["received_at"] == "2026-09-15T09:12:00-07:00"
    fetched = db.events(kind="fetched")
    assert len(fetched) == 1 and fetched[0]["detail"]["effective"]["from_address"] == "jane@gc.example"
    assert fetched[0]["detail"]["raw"]["from_address"] == SENDER and fetched[0]["level"] == "info"


def test_step_received_no_block_warns_and_keeps_the_forwarder(db, monkeypatch):
    monkeypatch.setattr(
        ingest.graph_inbox, "get_message",
        lambda *a, **k: {"body": {"content": "Just a note."},
                         "internetMessageHeaders": [{"name": "Authentication-Results", "value": EXO_PASS}]},
    )
    _seed(db, _email())
    # The forwarder is internal: production would never give this a row.
    assert ingest._step_received(db, dict(_row(db))) is None
    row = _row(db)
    assert row["status"] == "failed" and row["flag_reason"] == "test_listing_skip"
    assert row["decided_at_step"] == "fetch" and row["forward_meta"]["unwrap"] == "none"
    assert row["last_error"].startswith("In production this message would never get a row: ")
    assert "internal domain" in row["last_error"]
    fetched = db.events(kind="fetched")
    assert fetched[0]["level"] == "warn" and "no forwarded header" in fetched[0]["title"]
    assert db.events(kind="listing_skip")[0]["level"] == "warn"
    assert db.events(kind="terminal")[0]["detail"]["status"] == "failed"


def test_step_received_recheck_drops_a_vendor_sender(db, monkeypatch):
    body = OUTLOOK.replace("jane@gc.example", "quotes@graybar.example")
    monkeypatch.setattr(
        ingest.graph_inbox, "get_message",
        lambda *a, **k: {"body": {"content": body},
                         "internetMessageHeaders": [{"name": "Authentication-Results", "value": EXO_PASS}]},
    )
    _seed(db, _email())
    assert ingest._step_received(db, dict(_row(db))) is None
    row = _row(db)
    assert row["status"] == "failed" and row["flag_reason"] == "test_listing_skip"
    assert row["from_address"] == "quotes@graybar.example"
    assert "vendor contact" in db.events(kind="listing_skip")[0]["detail"]["reason"]


def test_normal_rows_never_unwrap_or_record(db, monkeypatch):
    monkeypatch.setattr(
        ingest.graph_inbox, "get_message",
        lambda *a, **k: {"body": {"content": OUTLOOK},
                         "internetMessageHeaders": [{"name": "Authentication-Results", "value": EXO_PASS}]},
    )
    _seed(db, _email(test_session_id=None, from_address="pm@gc.example"))
    assert ingest._step_received(db, dict(_row(db))) == "auth"
    row = _row(db)
    assert row["from_address"] == "pm@gc.example" and row["body_text"] == OUTLOOK
    assert "forward_meta" not in row and db.events() == []


def test_walk_to_done_records_every_step_for_a_test_row(db, monkeypatch):
    from tests.test_rfp_email_ingest import EXTRACT_OK, MATCH_NONE

    monkeypatch.setattr(
        ingest.graph_inbox, "get_message",
        lambda *a, **k: {"body": {"content": OUTLOOK.replace("jane@gc.example", "pm@gc.example")},
                         "internetMessageHeaders": [{"name": "Authentication-Results", "value": EXO_PASS}]},
    )
    by_feature = {
        "rfp_classify": {"answer": "yes", "confidence": 0.95, "reasoning": "invite"},
        "rfp_extract": EXTRACT_OK, "rfp_match": MATCH_NONE,
    }
    monkeypatch.setattr(ingest.llm, "complete_json",
                        lambda feature, **kw: copy.deepcopy(by_feature[feature]))
    monkeypatch.setattr(ingest.rfp_harvest, "harvester_for", lambda row, settings=None: None)
    _seed(db, _email())
    ingest._process_email(db, dict(_row(db)))
    row = _row(db)
    assert row["status"] == "done" and row["invitation_method"] == "organic"
    kinds = [e["kind"] for e in db.events()]
    assert kinds == ["fetched", "auth", "keywords", "classify", "authorize", "method", "extract",
                     "match", "parked", "terminal"]
    classify = db.events(kind="classify")[0]["detail"]
    assert classify["answer"] == "yes" and classify["system_prompt"] and classify["messages"]
    assert db.events(kind="auth")[0]["detail"]["note"] == rfp_test.AUTH_FORWARD_NOTE
    assert db.events(kind="match")[0]["detail"]["decision"] == "new"
    assert db.events(kind="parked")[0]["source"] == "create"


def test_step_chips_states():
    chips = {c["step"]: c for c in rfp_test.step_chips({"status": "classify", "next_attempt_at": None})}
    assert chips["received"]["state"] == "done" and chips["keywords"]["state"] == "done"
    assert chips["classify"]["state"] == "current" and chips["authorize"]["state"] == "pending"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "harvest", "next_attempt_at": "soon"})}
    assert chips["harvest"] == "waiting" and chips["match"] == "done"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "review_llm"})}
    assert chips["classify"] == "waiting"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "flagged_auth"})}
    assert chips["auth"] == "failed" and chips["received"] == "done" and chips["keywords"] == "pending"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "failed", "decided_at_step": "fetch"})}
    assert chips["received"] == "failed"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "merged"})}
    assert chips["match"] == "done" and chips["harvest"] == "skipped" and chips["create"] == "skipped"
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "created", "harvest_id": "h1"})}
    assert all(v == "done" for v in chips.values())
    chips = {c["step"]: c["state"] for c in rfp_test.step_chips({"status": "done", "harvest_id": None})}
    assert chips["harvest"] == "skipped" and chips["create"] == "done"
    events = [{"id": 3, "source": "intake", "kind": "classify"}, {"id": 9, "source": "harvest", "kind": "file"},
              {"id": 4, "source": "intake", "kind": "retry", "detail": {"step": "fetch"}}]
    chips = {c["step"]: c for c in rfp_test.step_chips({"status": "done"}, events)}
    assert chips["classify"]["event_id"] == 3 and chips["harvest"]["event_id"] == 9
    assert chips["received"]["event_id"] == 4 and "event_id" not in chips["auth"]


# ── Outbound redirect (section 6) ─────────────────────────────────────────


def _send_fakes(monkeypatch, captured):
    monkeypatch.setattr(graph_email, "_acquire_token", lambda: "tok")

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return SimpleNamespace(raise_for_status=lambda: None, headers={"request-id": "req-1"})
    monkeypatch.setattr(graph_email.httpx, "post", fake_post)


def test_send_mail_redirects_while_a_session_is_active(db, monkeypatch):
    captured: dict = {}
    _send_fakes(monkeypatch, captured)
    db.tables["projects"] = [{"id": P1, "number": "26.9.7124", "name": "Cimarron Elementary"}]
    log = graph_email.send_mail(
        to=["jane@g3electrical.com", "pm@gc.example", "stranger@example.com"], cc=[SENDER],
        subject="Proposal: Cimarron", body_html="<p>Hello<br>there</p><img src=\"cid:logo\" alt=\"G3\">",
        attachments=[("Plans.pdf", b"x" * 2048)], inline_images=[("logo", "logo.jpg", b"y", "image/jpeg")],
        project_id=P1, rfq_id="r1", sent_by="u-exec",
    )
    message = captured["message"]
    assert [r["emailAddress"]["address"] for r in message["toRecipients"]] == [REDIRECT]
    assert message["ccRecipients"] == [] and message["bccRecipients"] == []
    assert message["subject"] == "[TEST for Jane Doe +2] Proposal: Cimarron"
    assert message["body"]["contentType"] == "Text"
    text = message["body"]["content"]
    assert text.startswith('BDR TEST MODE  (session "Wave 1", started Sep 16, 2026 1:00 PM PT)')
    assert "  To:  Jane Doe, Executive <jane@g3electrical.com>" in text
    assert "  To:  Meridian Builders: Sam Lee, GC contact <pm@gc.example>" in text
    assert "  To:  stranger@example.com (unknown)" in text
    assert f"  CC:  Tom Moore, IT Admin <{SENDER}>" in text
    assert f"From mailbox: {MAILBOX}" in text and "Project: 26.9.7124 Cimarron Elementary" in text
    assert "Attachments kept: Plans.pdf (2 KB)" in text
    assert text.rstrip().endswith("Hello\nthere") and "G3" not in text.split("-" * 62)[1]
    assert [a["name"] for a in message["attachments"]] == ["Plans.pdf"]
    # The ledger keeps the INTENDED To line and says where it went.
    row = db.tables["email_log"][0]
    assert row["to_addrs"] == "jane@g3electrical.com, pm@gc.example, stranger@example.com"
    assert row["test_session_id"] == SID and row["redirected_to"] == REDIRECT and row["status"] == "sent"
    assert log["status"] == "sent"
    event = db.events(kind="redirected")[0]
    assert event["source"] == "mail_out" and event["project_id"] == P1 and event["detail"]["rfq_id"] == "r1"
    assert event["detail"]["email_log_id"] == row["id"] and event["detail"]["status"] == "sent"
    assert event["detail"]["to"][0]["display"] == "Jane Doe, Executive <jane@g3electrical.com>"
    assert event["detail"]["inline_dropped"] == ["logo.jpg"] and event["detail"]["preview"].startswith("Hello")


def test_send_mail_refuses_when_the_redirect_address_is_empty(db, monkeypatch):
    captured: dict = {}
    _send_fakes(monkeypatch, captured)
    db.tables[rfp_test.TABLE_SESSIONS][0]["redirect_to"] = ""
    with pytest.raises(rfp_test.RedirectRefused):
        graph_email.send_mail(to=["pm@gc.example"], subject="s", body_html="<p>x</p>")
    assert captured == {}
    row = db.tables["email_log"][0]
    assert row["status"] == "failed" and row["redirected_to"] is None
    refused = db.events(kind="refused")
    assert len(refused) == 1 and refused[0]["level"] == "error"


def test_send_mail_is_unchanged_without_a_session(db, monkeypatch):
    captured: dict = {}
    _send_fakes(monkeypatch, captured)
    db.tables[rfp_test.TABLE_SESSIONS] = [_session(status="ended")]
    graph_email.send_mail(to=["pm@gc.example"], cc=["x@y.com"], subject="s", body_html="<p>x</p>")
    message = captured["message"]
    assert [r["emailAddress"]["address"] for r in message["toRecipients"]] == ["pm@gc.example"]
    assert message["body"]["contentType"] == "HTML" and message["subject"] == "s"
    row = db.tables["email_log"][0]
    assert "test_session_id" not in row and "redirected_to" not in row and db.events() == []


def test_send_draft_patches_the_draft_before_the_send(db, monkeypatch):
    calls = []

    def fake_request(method, path, *, json=None, params=None, **k):
        calls.append((method, path, json, params))
        if method == "GET":
            return SimpleNamespace(json=lambda: {
                "id": "d1", "subject": "RFQ 26.9.7124 Lighting BOM",
                "body": {"contentType": "HTML", "content": "<div>Please quote</div><div>Thanks</div>"},
                "toRecipients": [{"emailAddress": {"address": "quotes@graybar.example"}}],
                "ccRecipients": [{"emailAddress": {"address": MAILBOX}}],
                "bccRecipients": [],
                "attachments": [
                    {"id": "a-logo", "name": "logo.jpg", "size": 10, "isInline": True},
                    {"id": "a-bom", "name": "BOM.pdf", "size": 3 * 1024 * 1024, "isInline": False},
                ],
            })
        return SimpleNamespace(json=lambda: {})
    monkeypatch.setattr(graph_email, "graph_request", fake_request)
    graph_email.send_draft("d1", project_id=P1, rfq_id="r1")
    methods = [c[0] for c in calls]
    assert methods == ["GET", "PATCH", "DELETE", "POST"]
    patch = calls[1][2]
    assert patch["subject"] == "[TEST for Graybar: Pat Q] RFQ 26.9.7124 Lighting BOM"
    assert [r["emailAddress"]["address"] for r in patch["toRecipients"]] == [REDIRECT]
    assert patch["ccRecipients"] == [] and patch["body"]["contentType"] == "Text"
    assert "Attachments kept: BOM.pdf (3.0 MB)" in patch["body"]["content"]
    assert "Please quote\nThanks" in patch["body"]["content"]
    assert calls[2][1].endswith("/attachments/a-logo") and calls[3][1].endswith("/messages/d1/send")
    event = db.events(kind="redirected")[0]
    assert event["detail"]["rfq_id"] == "r1" and event["detail"]["email_log_id"] is None


def test_send_draft_never_touches_supabase_while_the_switch_is_off(monkeypatch):
    monkeypatch.setattr(graph_email, "get_settings", lambda: _settings(rfp_testing_enabled=False))
    monkeypatch.setattr(graph_email, "get_supabase",
                        lambda: (_ for _ in ()).throw(AssertionError("no Supabase")))
    sent = []
    monkeypatch.setattr(graph_email, "graph_request", lambda method, path, **k: sent.append((method, path)))
    graph_email.send_draft("d1")
    assert sent == [("POST", f"/users/{MAILBOX}/messages/d1/send")]


def test_send_draft_refuses_before_sending_on_an_empty_redirect(db, monkeypatch):
    db.tables[rfp_test.TABLE_SESSIONS][0]["redirect_to"] = ""
    calls = []

    def fake_request(method, path, **k):
        calls.append(method)
        return SimpleNamespace(json=lambda: {"subject": "s", "body": {}, "toRecipients": [], "attachments": []})
    monkeypatch.setattr(graph_email, "graph_request", fake_request)
    with pytest.raises(rfp_test.RedirectRefused):
        graph_email.send_draft("d1")
    assert calls == ["GET"]


def test_resolve_recipients_and_display():
    db = DB({
        "profiles": [{"full_name": "Jane Doe", "email": "jane@g3electrical.com", "role": "executive"}],
        "gc_contacts": [{"name": "Sam Lee", "email": "sam@m.com", "gc_id": "gc-1"}],
        "general_contractors": [{"id": "gc-1", "name": "Meridian"}],
        "vendor_contacts": [{"name": "Pat", "email": "pat@v.com", "vendor_id": "v-1"},
                            {"name": "Dup", "email": "jane@g3electrical.com", "vendor_id": "v-1"}],
        "vendors": [{"id": "v-1", "name": "Graybar"}],
    })
    resolved = rfp_test.resolve_recipients(db, ["jane@g3electrical.com", "sam@m.com", "pat@v.com", "x@y.com"])
    assert resolved == {
        "jane@g3electrical.com": "Jane Doe, Executive",
        "sam@m.com": "Meridian: Sam Lee, GC contact",
        "pat@v.com": "Graybar: Pat, vendor contact",
    }
    assert rfp_test.display_for("x@y.com", resolved) == "x@y.com (unknown)"
    assert rfp_test.resolve_recipients(db, []) == {}


# ── Cleanup (section 9) ───────────────────────────────────────────────────


def test_cleanup_refuses_the_active_session_and_deletes_only_tagged_rows(db, monkeypatch):
    with pytest.raises(rfp_test.RfpTestError) as exc:
        rfp_test.cleanup(db, SID, actor_id=DEV)
    assert exc.value.code == "rfp_testing_session_active"
    rfp_test.end_session(db, SID, actor_id=DEV)

    db.tables["projects"] = [
        {"id": "p-test", "number": "26.9.7201", "test_session_id": SID},
        {"id": "p-real", "number": "26.9.7100", "test_session_id": None},
    ]
    db.tables["general_contractors"] = [
        {"id": "gc-new", "name": "New GC", "test_session_id": SID},
        {"id": "gc-shared", "name": "Shared GC", "test_session_id": SID},
        {"id": "gc-real", "name": "Real GC"},
    ]
    db.tables["gc_contacts"] = [
        {"id": "c-new", "gc_id": "gc-new", "test_session_id": SID},
        {"id": "c-real", "gc_id": "gc-real"},
    ]
    db.tables["project_gcs"] = [{"id": "l1", "project_id": "p-real", "gc_id": "gc-shared"}]
    db.tables["project_gc_contacts"] = []
    db.tables["rfp_harvests"] = [{"id": "h-test", "sandbox_run_id": "run-1", "test_session_id": SID},
                                 {"id": "h-real", "sandbox_run_id": "run-2"}]
    db.tables["rfp_ingest_runs"] = [{"id": "run-1", "status": "done", "test_session_id": SID},
                                    {"id": "run-3", "status": "running", "test_session_id": SID},
                                    {"id": "run-2", "status": "done"}]
    db.tables["rfp_emails"] = [_email(id="e-test"), _email(id="e-real", test_session_id=None)]
    db.tables["ingested_emails"] = [{"id": "i-test", "test_session_id": SID}, {"id": "i-real"}]
    db.tables["ingested_email_attachments"] = [{"id": "a1", "email_id": "i-test", "storage_path": "emails/i-test/x.pdf"}]
    db.tables["email_log"] = [
        {"id": "m-tagged", "test_session_id": SID},
        {"id": "m-project", "project_id": "p-test"},
        {"id": "m-real", "project_id": "p-real"},
    ]
    deleted_runs, swept, files = [], [], []
    from app.services import rfp_ingest, storage

    def delete_run(run_id):
        if run_id == "run-3":
            raise RuntimeError("Cancel the run and wait for it to stop before deleting it.")
        deleted_runs.append(run_id)
        db.tables["rfp_ingest_runs"] = [r for r in db.tables["rfp_ingest_runs"] if r["id"] != run_id]
    monkeypatch.setattr(rfp_ingest, "delete_run", delete_run)
    monkeypatch.setattr(storage, "delete_project_prefix", lambda pid: swept.append(pid))
    monkeypatch.setattr(storage, "delete_file", lambda path: files.append(path))

    report = rfp_test.cleanup(db, SID, actor_id=DEV)
    assert report["deleted"] == {
        "projects": 1, "gcs": 1, "gc_contacts": 1, "split_jobs": 0, "harvests": 1, "ingest_runs": 1,
        "rfp_emails": 1, "ingested_emails": 1, "email_log": 2,
    }
    assert {k["id"] for k in report["kept"]} == {"gc-shared", "run-3"}
    assert report["errors"] == []
    assert deleted_runs == ["run-1"] and swept == ["p-test"] and files == ["emails/i-test/x.pdf"]
    assert [p["id"] for p in db.tables["projects"]] == ["p-real"]
    assert {g["id"] for g in db.tables["general_contractors"]} == {"gc-shared", "gc-real"}
    assert [c["id"] for c in db.tables["gc_contacts"]] == ["c-real"]
    assert [h["id"] for h in db.tables["rfp_harvests"]] == ["h-real"]
    assert {r["id"] for r in db.tables["rfp_ingest_runs"]} == {"run-3", "run-2"}
    assert [e["id"] for e in db.tables["rfp_emails"]] == ["e-real"]
    assert [e["id"] for e in db.tables["ingested_emails"]] == ["i-real"]
    assert [m["id"] for m in db.tables["email_log"]] == ["m-real"]
    session = db.tables[rfp_test.TABLE_SESSIONS][0]
    assert session["cleanup_finished_at"] and session["cleanup_report"]["deleted"]["projects"] == 1
    assert [e["kind"] for e in db.events(source="session")] == ["ended", "cleanup_started", "cleanup_finished"]


# ── The filer (section 4.2) ───────────────────────────────────────────────


def test_filer_poll_once_switches_to_the_test_mailbox_inbox_only_and_filters(db, monkeypatch):
    synced = []

    def delta(delta_link, *, mailbox, folder, since_days, select):
        synced.append((mailbox, folder, since_days))
        return [
            _msg(),
            _msg(id="g2", receivedDateTime=BEFORE),
            _msg(id="g3", **{"from": {"emailAddress": {"address": "pm@gc.example"}}}),
        ], "d"
    monkeypatch.setattr(filer.graph_inbox, "delta_inbox", delta)
    pending = []
    monkeypatch.setattr(filer, "process_pending", lambda sb, lease_key=None, **k: pending.append(k))
    assert filer.poll_once() is True
    assert synced == [(TEST_MAILBOX, "inbox", 1)]
    rows = db.tables["ingested_emails"]
    assert [r["graph_message_id"] for r in rows] == ["g1"] and rows[0]["test_session_id"] == SID
    ignored = {e["detail"]["graph_message_id"]: e["detail"]["reason"] for e in db.events(kind="ignored", source="filer")}
    assert ignored == {"g2": "before_session", "g3": "not_test_sender"}
    assert db.events(kind="listed", source="filer")[0]["ingested_email_id"] == rows[0]["id"]
    assert pending[0]["session"]["id"] == SID
    assert db.tables[rfp_test.TABLE_SESSIONS][0]["filer_last_tick_at"] is not None
    assert any(r["id"] == f"pm-mail:{TEST_MAILBOX}:lease" for r in db.tables["graph_sync_state"])


def test_filer_normal_mode_is_unchanged_without_a_session(db, monkeypatch):
    db.tables[rfp_test.TABLE_SESSIONS] = []
    synced = []
    monkeypatch.setattr(filer.graph_inbox, "delta_inbox",
                        lambda *a, mailbox, folder, since_days, **k: synced.append((mailbox, folder, since_days)) or ([], "d"))
    monkeypatch.setattr(filer, "process_pending", lambda sb, lease_key=None, **k: None)
    assert filer.poll_once() is False
    assert synced == [("pm@g3electrical.com", "inbox", 1), ("pm@g3electrical.com", "sentitems", 1)]
    # Filer off and no session: nothing at all.
    monkeypatch.setattr(filer, "get_settings", lambda: _settings(email_ingest_enabled=False))
    synced.clear()
    assert filer.poll_once() is False and synced == []


def test_filer_process_pending_filters_by_tag(db, monkeypatch):
    db.tables["ingested_emails"] = [
        {"id": "i-real", "status": "received", "created_at": "1", "test_session_id": None},
        {"id": "i-active", "status": "received", "created_at": "2", "test_session_id": SID},
        {"id": "i-ended", "status": "received", "created_at": "3", "test_session_id": SID_ENDED},
    ]
    seen = []
    monkeypatch.setattr(filer, "_process_email", lambda sb, email, **k: seen.append(email["id"]))
    filer.process_pending(db)
    assert seen == ["i-real"]
    seen.clear()
    filer.process_pending(db, session=_session())
    assert seen == ["i-active"]


# ── NGEM (section 4.3) ────────────────────────────────────────────────────


def test_ngem_tick_is_a_no_op_while_a_session_is_active(db, monkeypatch):
    slots = []
    monkeypatch.setattr(portal, "due_slots", lambda now, settings: slots.append(now) or [])
    monkeypatch.setattr(portal, "_requeue_parked_runs", lambda *a: slots.append("requeue"))
    monkeypatch.setattr(portal, "_fail_stale_runs", lambda *a: None)
    monkeypatch.setattr(portal.rfp_email_ingest, "acquire_lease", lambda sb, key: False)
    portal.poll_once()
    assert slots == []
    db.tables[rfp_test.TABLE_SESSIONS] = []
    portal.poll_once()
    assert len(slots) == 2


# ── Router reads (section 8) ──────────────────────────────────────────────


def test_state_route_shape_and_cursor(db):
    rfp_test.record(db, session_id=SID, source="intake", kind="listed", title="one")
    rfp_test.record(db, session_id=SID, source="intake", kind="ignored", title="two", detail={"reason": "x"})
    rfp_test.record(db, session_id=SID, source="mail_out", kind="redirected", title="three")
    db.tables["rfp_emails"] = [_email(id="e1"), _email(id="e2", test_session_id=None)]
    db.tables["projects"] = [{"id": P1, "test_session_id": SID}]
    state = rr.get_state(session_id=None, after=0, user=_user())
    assert state["enabled"] is True and state["config"]["mailbox"] == TEST_MAILBOX
    assert state["config"]["poll_seconds"] == 15 and state["config"]["auto_create_env"] is False
    assert state["active"]["id"] == SID and state["selected"]["id"] == SID
    assert state["active"]["started_by"] == {"id": DEV, "name": "Tom Moore"}
    assert state["paused"] == {"rfp_mailboxes": [MAILBOX, MAILBOX2], "ngem": False,
                               "filer_mailbox": "pm@g3electrical.com",
                               "note": "harvests already queued keep running"}
    assert state["counts"] == {"emails": 1, "ignored": 1, "projects": 1, "filed": 0, "mail_out": 1, "events": 3}
    assert [e["id"] for e in state["events"]] == [1, 2, 3]
    assert [e["id"] for e in rr.get_state(session_id=None, after=2, user=_user())["events"]] == [3]
    with pytest.raises(HTTPException) as exc:
        rr.get_state(session_id=SID_ENDED, after=0, user=_user())
    assert exc.value.status_code == 404


def test_session_routes_map_the_service_errors(db, monkeypatch):
    _graph_ok(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        rr.create_session(rr.SessionCreateIn(name="x"), user=_user())
    assert exc.value.status_code == 409 and exc.value.detail == "rfp_testing_already_active"
    out = rr.patch_session(SID, rr.SessionPatchIn(auto_create=True), user=_user())
    assert out["auto_create"] is True and db.events(kind="auto_create")
    with pytest.raises(HTTPException) as exc:
        rr.cleanup_session(SID, user=_user())
    assert exc.value.status_code == 409 and exc.value.detail == "rfp_testing_session_active"
    ended = rr.end_session(SID, user=_user())
    assert ended["status"] == "ended"
    with pytest.raises(HTTPException) as exc:
        rr.patch_session(SID, rr.SessionPatchIn(auto_create=False), user=_user())
    assert exc.value.detail == "rfp_testing_session_ended"
    db.tables[rfp_test.TABLE_SESSIONS] = []
    created = rr.create_session(rr.SessionCreateIn(name="Wave 2", auto_create=True), user=_user())
    assert created["status"] == "active" and created["name"] == "Wave 2"
    with pytest.raises(HTTPException) as exc:
        rr.end_session("not-a-uuid", user=_user())
    assert exc.value.status_code == 404


def test_emails_route_shapes_rows_with_chips(db):
    db.tables["rfp_emails"] = [
        _email(id="e1", status="done", decided_at_step="create", invitation_method="organic",
               llm_answer="yes", llm_confidence=0.95, extracted_project_name="Riverside Plaza",
               harvest_id="h1", created_project_id=P1, forward_meta={"unwrap": "inline"}),
        _email(id="e2", test_session_id=None),
    ]
    db.tables["rfp_harvests"] = [{"id": "h1", "status": "complete"}]
    db.tables["projects"] = [{"id": P1, "number": "26.9.7201", "name": "Riverside Plaza"}]
    rfp_test.record(db, session_id=SID, source="intake", kind="classify", title="c", rfp_email_id="e1")
    rows = rr.list_session_emails(SID, user=_user())["rows"]
    assert [r["id"] for r in rows] == ["e1"]
    row = rows[0]
    assert row["method"] == "organic" and row["harvest_status"] == "complete"
    assert row["created_project_number"] == "26.9.7201" and row["extracted"]["project_name"] == "Riverside Plaza"
    assert row["forward_meta"] == {"unwrap": "inline"}
    chips = {c["step"]: c for c in row["steps"]}
    assert chips["classify"]["event_id"] == 1 and chips["create"]["state"] == "done"
    ignored = rr.list_session_ignored(SID, user=_user())["rows"]
    assert ignored == []


# ── Harvest tagging (section 4.4) ─────────────────────────────────────────


def test_harvest_step_passes_the_tag_only_for_test_rows(monkeypatch):
    from app.services import rfp_harvest as h

    calls = []
    monkeypatch.setattr(h, "get_settings", lambda: _settings(rfp_harvest_poll_seconds=60))
    monkeypatch.setattr(h, "harvester_for", lambda row, settings=None: "procore")
    monkeypatch.setattr(h, "reference_for", lambda row: SimpleNamespace(external_key="k"))
    monkeypatch.setattr(h, "availability_for", lambda row, settings: (True, None, None))
    monkeypatch.setattr(h, "active_job", lambda email_id: None)
    monkeypatch.setattr(h, "enqueue", lambda email_id, **k: calls.append((email_id, k)))
    parked = []
    h.step(None, _email(), park=lambda s, e: parked.append((s, e)), finish=lambda: True)
    h.step(None, _email(id="e-real", test_session_id=None), park=lambda s, e: None, finish=lambda: True)
    assert calls[0][1]["test_session_id"] == SID and "test_session_id" not in calls[1][1]
    assert parked == [(60, None)]


def test_harvest_file_and_run_tagging(db, monkeypatch):
    from app.services import rfp_harvest as h

    h._record_file(db, {"id": "h1", "test_session_id": SID}, "e1",
                   {"file_path": "Plans.pdf", "status": "accepted", "size": 10, "origin": "attachment"})
    h._record_file(db, {"id": "h2"}, "e2", {"file_path": "x", "status": "rejected"})
    (event,) = db.events(kind="file")
    assert event["harvest_id"] == "h1" and event["detail"]["source"] == "attachment"
    created = []
    monkeypatch.setattr(h.rfp_ingest, "create_harvest_run",
                        lambda **k: created.append(k) or {"id": "run-1", "status": "staging"})
    monkeypatch.setattr(h, "_existing_run", lambda sb, run_id: None)
    monkeypatch.setattr(h, "_update_claimed", lambda *a, **k: None)
    monkeypatch.setattr(h, "_scratch_dir", lambda settings: (_ for _ in ()).throw(RuntimeError("stop here")))
    for harvest in ({"id": "h1", "test_session_id": SID}, {"id": "h2"}):
        with pytest.raises(RuntimeError, match="stop here"):
            h._harvest_files(db, None, _settings(), harvest, "tok", "e1", [{"file_path": "a"}], [None])
    assert created[0]["test_session_id"] == SID and "test_session_id" not in created[1]


def test_auth_step_passes_a_test_row_on_the_tenants_trust_and_says_so(db):
    _seed(db, _email(status="auth", auth_raw=None, auth_spf=None, auth_dkim=None, auth_dmarc=None))
    assert ingest._step_auth(db, dict(_row(db))) == "keywords"
    assert _row(db)["status"] == "keywords" and _row(db)["auth_verdict"] == "pass"
    (event,) = db.events(kind="auth")
    assert event["detail"]["tenant_header_present"] is False and "tenant's trust" in event["detail"]["note"]
    # A real row with no tenant header still fails at auth, as before.
    _seed(db, _email(id="e-real", test_session_id=None, status="auth", auth_raw=None))
    assert ingest._step_auth(db, dict(_row(db, "e-real"))) is None
    assert _row(db, "e-real")["status"] == "flagged_auth" and len(db.events(kind="auth")) == 1

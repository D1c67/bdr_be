"""Browser v2 retention and IT Admin inspection exclusions (WI-18;
docs/ASSISTANT_BROWSER_V2_PLAN.md 10.7 and 17.5).

Invariants pinned here:

  retention   purge_expired_records is unchanged with ASSISTANT_BROWSER_V2_ENABLED
              off; with it on the v2 retention rides along (purge_v2, staged
              download objects past 7 days, ended holds and download metadata
              past 90 days) and a failing step is counted and never stops the
              rest; legal hold keeps a person's rows;
  inspection  an IT Admin never gets a record a hold points at, nor any
              screenshot of a job that held a final action (or whose holds
              cannot be read); the conversation read drops the held-action
              labels and the card detail of the four private browser cards
              and leaves every other block alone.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import supabase_client
from app.core.deps import CurrentUser, get_current_user
from app.core.roles import Role
from app.routers import assistant_inspection as routes
from app.services import assistant_browser_records as records
from app.services import assistant_browser_vault as vault
from app.services import assistant_staging
from tests.assistant_fakes import USER, FakeDB, FakeStorage

# The purge readers in app/assistant/repo.py use the real clock, so the
# fixtures are dated from it too; a fixed date drifts past their windows.
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
OLD = (NOW - timedelta(days=200)).isoformat()
FRESH = (NOW - timedelta(days=1)).isoformat()
OTHER = "12121212-1212-4121-8121-121212121212"


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(now=NOW)
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    fake.bv2_rpc_results["assistant_browser_purge_v2"] = {"sessions": 1}
    return fake


def _v2(monkeypatch, on: bool) -> None:
    import app.core.config as config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(assistant_browser_v2_enabled=on))


# ── Retention ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_schema_probe(monkeypatch):
    """The flag-off schema probe's backoff is module state: reset per test."""
    monkeypatch.setattr(records, "_v2_absent_until", 0.0)


def test_purge_expired_records_is_unchanged_with_v2_off_and_no_0192(monkeypatch):
    _v2(monkeypatch, False)
    monkeypatch.setattr(records.repo, "browser_records_purge", lambda owner, **kw: {"records": 2})
    calls: list[dict] = []
    monkeypatch.setattr(records, "purge_v2_retention", lambda **kw: calls.append(kw) and None)
    assert records.purge_expired_records(limit=10) == {"records": 2}
    assert calls == [{"limit": 10, "require_schema": True}]
    # A database without 0192 is probed again at most once an hour.
    assert records.purge_expired_records(limit=10) == {"records": 2}
    assert len(calls) == 1


def test_purge_expired_records_adds_the_v2_counts_with_v2_on(monkeypatch):
    _v2(monkeypatch, True)
    monkeypatch.setattr(records.repo, "browser_records_purge", lambda owner, **kw: {"records": 2})
    monkeypatch.setattr(records, "purge_v2_retention", lambda **kw: {"errors": 0, "holds_deleted": 1})
    assert records.purge_expired_records(limit=10) == {"records": 2, "v2": {"errors": 0, "holds_deleted": 1}}


def test_unreadable_settings_leave_the_v1_sweep_alone(monkeypatch):
    import app.core.config as config

    def boom():
        raise RuntimeError("settings")

    monkeypatch.setattr(config, "get_settings", boom)
    monkeypatch.setattr(records.repo, "browser_records_purge", lambda owner, **kw: {"records": 0})
    assert records.purge_expired_records() == {"records": 0}


def _seed_retention(db) -> FakeStorage:
    store = FakeStorage()
    t = db.tables
    t["assistant_browser_downloads"] = [
        {"id": "d-expired", "user_id": USER, "status": "staged", "purged_at": None, "created_at": FRESH,
         "storage_path": f"assistant-files/{USER}/browser/d1", "expires_at": (NOW - timedelta(hours=1)).isoformat()},
        {"id": "d-live", "user_id": USER, "status": "staged", "purged_at": None, "created_at": FRESH,
         "storage_path": f"assistant-files/{USER}/browser/d2", "expires_at": (NOW + timedelta(days=3)).isoformat()},
        {"id": "d-old-stored", "user_id": OTHER, "status": "stored", "purged_at": None, "created_at": OLD},
        {"id": "d-old-staged", "user_id": OTHER, "status": "staged", "purged_at": None, "created_at": OLD,
         "storage_path": f"assistant-files/{OTHER}/browser/d4", "expires_at": (NOW + timedelta(days=3)).isoformat()},
    ]
    t["assistant_browser_holds"] = [
        {"id": "h-old-done", "user_id": OTHER, "status": "consumed", "created_at": OLD},
        {"id": "h-old-open", "user_id": OTHER, "status": "open", "created_at": OLD},
        {"id": "h-new-done", "user_id": OTHER, "status": "declined", "created_at": FRESH},
    ]
    for row in t["assistant_browser_downloads"]:
        if row.get("storage_path"):
            store.objects[row["storage_path"]] = b"x"
    return store


def test_v2_retention_runs_every_step_once(db, monkeypatch):
    store = _seed_retention(db)
    store.install(monkeypatch)
    out = records.purge_v2_retention(limit=100)
    assert out["errors"] == 0 and out["purge"] == {"sessions": 1}
    assert out["downloads_purged"] == 1 and out["holds_deleted"] == 1 and out["downloads_deleted"] == 1
    t = db.tables
    # Only the expired staged object went; the staged row stays (purged), the live one is untouched.
    assert f"assistant-files/{USER}/browser/d1" not in store.objects and f"assistant-files/{USER}/browser/d2" in store.objects
    assert {r["id"]: r["purged_at"] is not None for r in t["assistant_browser_downloads"]} == {
        "d-expired": True, "d-live": False, "d-old-staged": False}
    assert sorted(r["id"] for r in t["assistant_browser_holds"]) == ["h-new-done", "h-old-open"]
    # A second run finds nothing.
    again = records.purge_v2_retention(limit=100)
    assert again["downloads_purged"] == 0 and again["holds_deleted"] == 0 and again["downloads_deleted"] == 0


def test_v2_retention_a_failing_step_is_counted_and_the_rest_run(db, monkeypatch):
    store = _seed_retention(db)
    store.install(monkeypatch)
    db.rpc_errors["assistant_browser_purge_v2"] = RuntimeError("rpc down")
    monkeypatch.setattr(assistant_staging, "purge_browser_downloads", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("storage down")))
    out = records.purge_v2_retention(limit=100)
    assert out["errors"] == 2 and "purge" not in out and "downloads_purged" not in out
    assert out["holds_deleted"] == 1 and out["downloads_deleted"] == 1


def test_v2_retention_legal_hold_keeps_the_rows_and_the_objects(db, monkeypatch):
    store = _seed_retention(db)
    store.install(monkeypatch)
    db.tables["assistant_profiles"] = [{"user_id": OTHER, "legal_hold": True}, {"user_id": USER, "legal_hold": True}]
    out = records.purge_v2_retention(limit=100)
    assert out["holds_deleted"] == 0 and out["downloads_deleted"] == 0 and out["downloads_purged"] == 0
    assert len(db.tables["assistant_browser_holds"]) == 3 and f"assistant-files/{USER}/browser/d1" in store.objects


# ── IT Admin inspection of job records ───────────────────────────────────────


def _png() -> str:
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 16).decode()


@pytest.fixture
def job(monkeypatch):
    owner, jid = str(uuid4()), str(uuid4())
    monkeypatch.setattr(vault, "get_settings", lambda: SimpleNamespace(
        user_cred_kek=base64.b64encode(b"k" * 32).decode(), user_cred_kek_id="u1", user_cred_old_keys=""))

    def row(kind, payload, **extra):
        packed = records.pack_record(owner, jid, kind, payload, record_id=str(uuid4()), seq=1)
        return {**packed, "user_id": owner, "job_id": jid, "help_id": None, "created_at": "2026-11-02T00:00:00Z", **extra}

    step = row("step", {"summary": "Opened the RFI list"})
    shot = row("screenshot", {"data": _png(), "mime_type": "image/png"})
    held = row("request", {"hold": {"method": "POST"}})
    monkeypatch.setattr(records.repo, "browser_inspect", lambda *a, **k: {
        "ok": True, "audit_id": str(uuid4()), "records": [step, shot, held]})
    return SimpleNamespace(owner=owner, id=jid, step=step, shot=shot, held=held, holds=[])


def _holds(monkeypatch, job, rows):
    monkeypatch.setattr(records.repo, "browser_holds", lambda user_id, job_id, **k: (
        rows if (user_id, job_id) == (job.owner, job.id) else []))


def test_a_job_with_no_holds_shows_its_trace_and_screenshots(job, monkeypatch):
    _holds(monkeypatch, job, [])
    out = records.inspect(str(uuid4()), job.owner, job.id, "Checking a reported failure")
    assert [i["kind"] for i in out["items"]] == ["step", "screenshot"]      # the hold request is skipped by its marker
    blob, mime = records.inspect_screenshot(str(uuid4()), job.owner, job.id, job.shot["id"], "Checking a reported failure")
    assert mime == "image/png" and blob.startswith(b"\x89PNG")


def test_a_final_action_hold_withholds_every_screenshot_of_the_job(job, monkeypatch):
    _holds(monkeypatch, job, [{"kind": "final_action", "summary_record_id": None, "screenshot_record_id": None}])
    out = records.inspect(str(uuid4()), job.owner, job.id, "Checking a reported failure")
    assert [i["kind"] for i in out["items"]] == ["step"]
    with pytest.raises(records.BrowserContentUnavailable):
        records.inspect_screenshot(str(uuid4()), job.owner, job.id, job.shot["id"], "Checking a reported failure")


def test_records_a_hold_points_at_are_dropped_by_id(job, monkeypatch):
    _holds(monkeypatch, job, [{"kind": "site", "summary_record_id": job.step["id"], "screenshot_record_id": None}])
    out = records.inspect(str(uuid4()), job.owner, job.id, "Checking a reported failure")
    assert [i["kind"] for i in out["items"]] == ["screenshot"]              # a site hold does not hide screenshots
    _holds(monkeypatch, job, [{"kind": "site", "summary_record_id": None, "screenshot_record_id": job.shot["id"]}])
    with pytest.raises(records.BrowserContentUnavailable):
        records.inspect_screenshot(str(uuid4()), job.owner, job.id, job.shot["id"], "Checking a reported failure")


def test_unreadable_holds_withhold_the_screenshots_and_keep_the_text(job, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(records.repo, "browser_holds", boom)
    out = records.inspect(str(uuid4()), job.owner, job.id, "Checking a reported failure")
    assert [i["kind"] for i in out["items"]] == ["step"]
    with pytest.raises(records.BrowserContentUnavailable):
        records.inspect_screenshot(str(uuid4()), job.owner, job.id, job.shot["id"], "Checking a reported failure")


def test_help_secrets_and_help_messages_are_never_returned(job, monkeypatch):
    _holds(monkeypatch, job, [])
    secret = records.pack_record(job.owner, job.id, "help_secret", {"secret": "123456"}, record_id=str(uuid4()), seq=1)
    message = records.pack_record(job.owner, job.id, "help_message", {"text": "code 123456"}, record_id=str(uuid4()),
                                  seq=2)
    extra = [{**r, "user_id": job.owner, "job_id": job.id, "help_id": None, "created_at": "2026-11-02T00:00:00Z"}
             for r in (secret, message)]
    monkeypatch.setattr(records.repo, "browser_inspect", lambda *a, **k: {
        "ok": True, "audit_id": "audit", "records": [job.step, *extra]})
    out = records.inspect(str(uuid4()), job.owner, job.id, "Checking a reported failure")
    assert [i["kind"] for i in out["items"]] == ["step"] and "123456" not in str(out)


# ── IT Admin inspection of a conversation ────────────────────────────────────

ADMIN = "11111111-1111-4111-8111-111111111111"
CONV = "33333333-3333-4333-8333-333333333333"
BODY = {"owner_id": OTHER, "reason_kind": "support", "reason": "Investigate the reported issue"}

HELD_LABEL = "Submit RFI 12 answer to the GC"


@pytest.fixture
def api(monkeypatch):
    state = SimpleNamespace(result={})

    def inspect(user_id, owner_id, conversation_id, **kwargs):
        return state.result

    monkeypatch.setattr(routes.repo, "admin_conversation_read", inspect)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=ADMIN, email="admin@example.test", role=Role.IT_ADMIN, is_active=True, via_assistant_run_id=None)
    return SimpleNamespace(client=TestClient(app), state=state)


def _conversation():
    return {"ok": True, "conversations": [], "bounded": True, "tool_results": [
        {"id": "t1", "tool_name": "browser_task_status", "payload": {
            "status": "running", "release_token": "tok", "released_actions": [{"label": HELD_LABEL}]}},
        {"id": "t2", "tool_name": "find_projects", "payload": {"secret": "kept", "released_actions": ["x"]}},
    ], "messages": [
        {"id": "m1", "role": "assistant", "content": "A task needs you", "blocks": [
            {"type": "text", "text": "hello"},
            {"type": "browser_final_action", "hold_id": "h", "job_id": "j", "status": "open", "label": HELD_LABEL,
             "body_summary": [{"name": "ssn", "value": "123-45-6789"}]},
            {"type": "browser_code_request", "job_id": "j", "help_id": "x", "site_label": "Procore", "status": "open"},
            {"type": "browser_credential_request", "request_id": "r", "site_key": "procore", "status": "requested"},
            {"type": "browser_live_signin", "job_id": "j", "status": "open"},
            {"type": "browser_site_access", "request_id": "r2", "status": "requested", "hold_id": None},
        ]},
        {"id": "m2", "role": "assistant", "content": "done", "blocks": [
            {"type": "browser_result", "job_id": "j", "status": "succeeded", "text": "Found 3 RFIs", "facts": [],
             "files": [], "released_actions": [{"hold_id": "h", "method": "POST", "label": HELD_LABEL}]},
        ]},
        {"id": "m3", "role": "user", "content": "hi", "blocks": None},
    ]}


@pytest.mark.parametrize("action", ["read", "export"])
def test_the_conversation_read_hides_what_the_owner_approved_in_browser_cards(api, action):
    api.state.result = _conversation()
    response = api.client.post(f"/assistant/inspection/conversations/{CONV}/{action}", json=BODY)
    assert response.status_code == 200
    text = response.text
    assert HELD_LABEL not in text and "123-45-6789" not in text and '"tok"' not in text
    messages = {m["id"]: m for m in response.json()["messages"]}
    cards = messages["m1"]["blocks"]
    assert cards[0] == {"type": "text", "text": "hello"}
    assert cards[1] == {"type": "browser_final_action", "status": "open", "private": True}
    assert cards[2] == {"type": "browser_code_request", "status": "open", "private": True}
    assert cards[3] == {"type": "browser_credential_request", "status": "requested", "private": True}
    assert cards[4] == {"type": "browser_live_signin", "status": "open", "private": True}
    # A site access card holds ids and a status only: shown.
    assert cards[5]["request_id"] == "r2" and cards[5]["status"] == "requested"
    result = messages["m2"]["blocks"][0]
    assert result["text"] == "Found 3 RFIs" and result["released_action_count"] == 1
    assert "released_actions" not in result
    assert messages["m3"]["blocks"] is None
    tools = {t["id"]: t["payload"] for t in response.json()["tool_results"]}
    assert tools["t1"] == {"status": "running", "released_action_count": 1}
    assert tools["t2"] == {"secret": "kept", "released_actions": ["x"]}      # other tools pass unchanged


def test_an_empty_or_odd_result_passes_through(api):
    api.state.result = {"ok": True, "conversations": [], "messages": [], "tool_results": [], "bounded": True}
    assert api.client.post(f"/assistant/inspection/conversations/{CONV}/read", json=BODY).status_code == 200
    api.state.result = {"ok": True, "messages": [{"id": "m", "blocks": "not a list"}, "odd"], "tool_results": None}
    response = api.client.post(f"/assistant/inspection/conversations/{CONV}/read", json=BODY)
    assert response.status_code == 200 and response.json()["messages"][0]["blocks"] == "not a list"


# ── Review fixes (WI-18 follow-up) ───────────────────────────────────────────


def test_v2_retention_keeps_running_when_the_flag_is_turned_off(db, monkeypatch):
    """The flag is a kill switch for new work, not for retention: with it off
    and 0192 present, the 7 and 90 day purges still run."""
    _v2(monkeypatch, False)
    store = _seed_retention(db)
    store.install(monkeypatch)
    monkeypatch.setattr(records.repo, "browser_records_purge", lambda owner, **kw: {"records": 0})
    out = records.purge_expired_records(limit=100)
    assert out["v2"]["errors"] == 0 and out["v2"]["downloads_purged"] == 1 and out["v2"]["holds_deleted"] == 1
    assert f"assistant-files/{USER}/browser/d1" not in store.objects


def test_flag_off_retention_stops_at_a_failed_schema_probe(db, monkeypatch):
    _v2(monkeypatch, False)
    _seed_retention(db).install(monkeypatch)
    db.rpc_errors["assistant_browser_purge_v2"] = RuntimeError("function does not exist")
    monkeypatch.setattr(records.repo, "browser_records_purge", lambda owner, **kw: {"records": 0})
    assert records.purge_expired_records(limit=100) == {"records": 0}
    assert len(db.tables["assistant_browser_holds"]) == 3         # nothing after the probe ran
    assert records._v2_absent_until > 0
    # With the flag on, a failing purge is counted and the rest still run.
    assert records.purge_v2_retention(limit=100)["holds_deleted"] == 1


def test_hold_retention_keeps_a_hold_while_its_job_still_has_records(db):
    """The hold row is the only fence between IT Admin inspection and the
    original final-action screenshot: it outlives the job's records."""
    t = db.tables
    t["assistant_browser_holds"] = [
        {"id": "h-records", "user_id": OTHER, "job_id": "job-a", "status": "consumed", "created_at": OLD},
        {"id": "h-clean", "user_id": OTHER, "job_id": "job-b", "status": "declined", "created_at": OLD},
    ]
    t["assistant_browser_records"] = [{"id": "r1", "user_id": OTHER, "job_id": "job-a", "kind": "screenshot"}]
    assert records.repo.browser_holds_retention_delete(None) == 1
    assert [r["id"] for r in t["assistant_browser_holds"]] == ["h-records"]
    t["assistant_browser_records"] = []                       # the records purge caught up
    assert records.repo.browser_holds_retention_delete(None) == 1
    assert t["assistant_browser_holds"] == []


def test_jobs_with_records_fails_closed_when_the_reads_run_out(db, monkeypatch):
    db.tables["assistant_browser_records"] = [{"id": f"r{i}", "job_id": "job-a"} for i in range(1000)]
    assert records.repo._bv2_jobs_with_records(None, ["job-a", "job-b"], rounds=1) == {"job-a", "job-b"}
    assert records.repo._bv2_jobs_with_records(None, ["job-a", "job-b"]) == {"job-a"}


def _stuck_claim(**extra) -> dict:
    """A row whose "Add to project" claim died: stored, no file, object kept."""
    return {"id": "d-stuck", "user_id": OTHER, "status": "stored", "file_id": None, "purged_at": None,
            "storage_path": f"assistant-files/{OTHER}/browser/stuck", "created_at": OLD,
            "expires_at": (NOW - timedelta(days=150)).isoformat(), **extra}


def test_a_dead_claim_keeps_its_row_until_the_object_is_purged(db, monkeypatch):
    store = FakeStorage()
    store.objects[f"assistant-files/{OTHER}/browser/stuck"] = b"x"
    store.install(monkeypatch)
    db.tables["assistant_browser_downloads"] = [_stuck_claim()]
    assert records.repo.browser_downloads_retention_delete(None) == 0
    # The 7-day purge takes it (a day past expiry) and marks the row purged.
    assert assistant_staging.purge_browser_downloads(None, 10) == 1
    assert f"assistant-files/{OTHER}/browser/stuck" not in store.objects
    assert db.tables["assistant_browser_downloads"][0]["purged_at"] is not None
    assert records.repo.browser_downloads_retention_delete(None) == 1


def test_a_fresh_claim_is_not_purged_while_a_click_may_still_be_copying_it(db):
    db.tables["assistant_browser_downloads"] = [_stuck_claim(expires_at=(NOW + timedelta(hours=1)).isoformat())]
    assert records.repo.staged_downloads_to_purge(None, limit=10) == []


def test_offboarding_reads_a_dead_claim_with_the_staged_downloads(db):
    db.tables["assistant_browser_downloads"] = [
        _stuck_claim(expires_at=(NOW + timedelta(days=3)).isoformat()),
        {"id": "d-done", "user_id": OTHER, "status": "stored", "file_id": "f1", "purged_at": FRESH,
         "storage_path": "x", "created_at": OLD},
    ]
    assert [r["id"] for r in records.repo.browser_offboard_staged_downloads(OTHER)] == ["d-stuck"]


def test_the_orphan_reader_finds_saved_logins_and_cookie_jars(db):
    t = db.tables
    t["assistant_credentials"] = [{"id": "c1", "user_id": OTHER, "status": "active"},
                                  {"id": "c2", "user_id": USER, "status": "revoked"}]
    assert records.repo.browser_v2_owners_with_open_state(None) == [OTHER]
    t["assistant_credentials"] = []
    t["assistant_browser_sessions"] = [{"id": "s1", "user_id": USER}]
    assert records.repo.browser_v2_owners_with_open_state(None) == [USER]
    assert records.repo.browser_v2_owners_with_open_state(OTHER) == []

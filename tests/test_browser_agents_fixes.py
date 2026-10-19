"""Browser agent fixes after the two-skeptic review (migration 0158 plus the
Python halves). One test per confirmed defect or open item; the FakeDB twins
in tests/browser_agents_fake.py mirror the 0158 SQL."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import procore_creds
from app.services.browser_agents import admin, identities, queue, redaction
from app.services.browser_agents import help as help_flow
from tests.browser_agents_fake import iso
from tests.browser_agents_harness import API, Harness, error_code
from tests.browser_agents_human import HumanHarness

MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/0158_browser_agents_fixes.sql"


@pytest.fixture
def h(monkeypatch):
    harness = HumanHarness(monkeypatch)
    yield harness
    harness.close()


def _rfi_job(h):
    return h.active_with_job("rfi_from_email")


def _lease(worker: dict, job: dict):
    return SimpleNamespace(worker=SimpleNamespace(id=str(worker["id"]), name=worker.get("name")),
                           epoch=int(job["lease_epoch"]), job_id=str(job["id"]))


# ── 0158 SQL text ──────────────────────────────────────────────────────────────


def test_0158_migration_text():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.startswith("-- 0158: ") and "Apply after 0157." in sql.splitlines()[0]
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    claim = sql[sql.index("function public.claim_browser_agent_jobs("):sql.index("function public.browser_agent_heartbeat(")]
    assert "from browser_agent_workers where id = p_worker for share;" in claim
    assert "status = 'queued' and cancel_requested_at is null" in claim
    hb = sql[sql.index("function public.browser_agent_heartbeat("):sql.index("browser_agent_append_rfi_supplement(")]
    assert "for no key update" in hb
    assert "w.job_id = any (v_renewed_ids)" in hb and "w.job_id = any (v_listed)" not in hb
    for name in ("claim_browser_agent_jobs", "browser_agent_heartbeat", "browser_agent_append_rfi_supplement"):
        m = re.search(rf"create or replace function public\.{name}\((.*?)\$\$", sql, re.S)
        assert m and "security invoker" in m.group(1) and "set search_path = pg_catalog, public" in m.group(1)
        assert re.search(rf"revoke all on function public\.{name}\([^)]*\) from anon, authenticated;", sql)
        assert re.search(rf"grant execute on function public\.{name}\([^)]*\) to service_role;", sql)
    assert set(re.findall(r"raise exception '([a-z_]+)'", sql)) <= {
        "unauthorized", "revoked", "stale_boot", "invalid_request"}


# ── claim, heartbeat, sweeper ──────────────────────────────────────────────────


def test_a_lease_under_a_boot_the_worker_no_longer_runs_is_reclaimed_while_it_heartbeats(monkeypatch):
    h = Harness(monkeypatch)
    worker, token, boot, job = h.active_with_job()
    # The claim committed under the old boot after a newer /hello registered.
    worker["boot_id"] = str(uuid.uuid4())
    h.db.advance(200)
    worker["last_seen_at"] = iso(h.db.now)                    # the new boot keeps heartbeating
    assert queue.reclaim_expired(h.db.now, {}) == 1
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["resume_reason"], row["lease_owner"], row["attempts"]) == (
        "queued", "interrupted", None, 1)                     # uncharged


def test_a_held_page_under_a_stale_boot_is_parked(monkeypatch):
    h = Harness(monkeypatch)
    worker, _, _, job = h.active_with_job()
    job.update(status="waiting_help", hold_until=iso(h.db.now + timedelta(minutes=15)))
    worker["boot_id"] = str(uuid.uuid4())
    h.db.advance(120)
    worker["last_seen_at"] = iso(h.db.now)
    assert queue.park_lapsed_holds(h.db.now, {}) == 1
    assert h.db.one("browser_agent_jobs", id=job["id"])["lease_owner"] is None


def test_the_sweeper_cas_misses_a_lease_a_heartbeat_just_renewed(monkeypatch):
    h = Harness(monkeypatch)
    _, _, _, job = h.active_with_job()
    h.db.advance(200)
    read = dict(h.db.one("browser_agent_jobs", id=job["id"]))      # what the sweeper read: expired
    h.db.one("browser_agent_jobs", id=job["id"])["lease_expires_at"] = iso(h.db.now + timedelta(seconds=90))
    assert queue._cas_job(read, {"status": "queued"}, status="running", expired_before=h.db.now) is False
    assert h.db.one("browser_agent_jobs", id=job["id"])["status"] == "running"


def test_heartbeat_help_updates_cover_only_jobs_the_worker_holds(h):
    _, token, boot, job = _rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    _, other_token = h.worker()
    _, other_boot = h.hello(other_token)
    body = {"boot_id": other_boot, "config_version": 1,
            "jobs": [{"job_id": job["id"], "epoch": h.db.one("browser_agent_jobs", id=job["id"])["lease_epoch"]}]}
    resp = h.client.post(f"{API}/heartbeat", json=body, headers=h.auth(other_token, **{"X-Agent-Boot": other_boot}))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["lost"] == [job["id"]] and data["help_updates"] == []
    mine = {"boot_id": boot, "config_version": 1,
            "jobs": [{"job_id": job["id"], "epoch": h.db.one("browser_agent_jobs", id=job["id"])["lease_epoch"]}]}
    own = h.client.post(f"{API}/heartbeat", json=mine, headers=h.auth(token, **{"X-Agent-Boot": boot})).json()
    assert [u["help_id"] for u in own["help_updates"]] == [hid]


def test_a_resumed_claim_starts_after_the_last_issued_turn(monkeypatch):
    h = Harness(monkeypatch)
    worker, token = h.worker()
    _, boot = h.hello(token)
    job = h.job()
    for n in (1, 2, 3):
        h.db.tables.setdefault("browser_agent_turns", []).append(
            {"job_id": job["id"], "turn_no": n, "epoch": 1, "status": "issued", "created_at": iso(h.db.now)})
    job.update(turn_count=2)                                    # the counter write for turn 3 was lost
    [claimed] = h.claim(token, boot).json()["jobs"]
    assert claimed["turn_count"] == 3


def test_step_count_counts_every_ledger_row(h):
    _, token, boot, job = _rfi_job(h)
    ledger = len(h.db.rows("browser_agent_steps", job_id=job["id"]))
    assert ledger >= 2 and h.db.one("browser_agent_jobs", id=job["id"])["step_count"] == 0
    assert admin.ledger_step_count(job["id"]) == ledger
    detail = h.get(f"/jobs/{job['id']}", who="it")
    assert detail.status_code == 200, detail.text
    assert detail.json()["step_count"] == ledger


# ── login help ─────────────────────────────────────────────────────────────────


def test_a_login_auto_resolve_returns_held_waiters_to_running(h):
    worker, token, boot, j1 = _rfi_job(h)
    j2 = h.second_job(token, boot)
    hid = h.open_help(token, boot, j1, reason_code="mfa").json()["help_id"]
    assert h.open_help(token, boot, j2, reason_code="mfa").json()["joined_existing"] is True
    identities._auto_resolve_login_help("procore", h.db.one("browser_agent_jobs", id=j1["id"]), _lease(worker, j1))
    assert h.db.one("browser_agent_help_requests", id=hid)["status"] == "resolved"
    for jid in (j1["id"], j2["id"]):
        row = h.db.one("browser_agent_jobs", id=jid)
        assert (row["status"], row["waiting_help_id"], row["lease_owner"]) == ("running", None, worker["id"])
    # j2 can ask for help again (open_help fences on running).
    assert h.open_help(token, boot, j2, reason_code="not_found").status_code == 200


def test_a_parked_waiter_on_a_closed_help_is_requeued(h):
    _, token, boot, job = _rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    assert h.wcall("post", token, boot, job, "/park").status_code == 204
    h.db.one("browser_agent_help_requests", id=hid).update(status="resolved", resolution="auto_resolved",
                                                            resolved_at=iso(h.db.now))
    assert help_flow.sweep_parked_closed_help(h.db.now, {}) == 0          # the closing path's grace
    h.db.advance(300)
    assert help_flow.sweep_parked_closed_help(h.db.now, {}) == 1
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["resume_reason"], row["attempts"]) == ("queued", "help_answered", 1)


def test_a_code_with_no_agent_at_the_login_page_is_refused(h):
    _, token, boot, job = _rfi_job(h)
    hid = h.open_help(token, boot, job, reason_code="mfa").json()["help_id"]
    h.db.table("browser_agent_login_attempts").insert({
        "identity": "procore", "host": "app.procore.com", "kind": "browser", "job_id": job["id"],
        "epoch": job["lease_epoch"], "granted": True, "outcome": "mfa"}).execute()
    assert h.wcall("post", token, boot, job, "/park").status_code == 204
    resp = h.say(hid, is_secret=True, secret_id=str(uuid.uuid4()), secret_value="482913")
    assert resp.status_code == 409 and error_code(resp) == "no_agent_at_login"
    assert "Retry now" in resp.json()["detail"]
    assert h.db.rows("browser_agent_help_messages", help_id=hid) == []


def test_a_code_alone_keeps_the_request_open(h):
    _, token, boot, job = _rfi_job(h)
    hid = h.open_help(token, boot, job, reason_code="mfa").json()["help_id"]
    h.db.table("browser_agent_login_attempts").insert({
        "identity": "procore", "host": "app.procore.com", "kind": "browser", "job_id": job["id"],
        "epoch": job["lease_epoch"], "granted": True, "outcome": "mfa"}).execute()
    assert h.say(hid, is_secret=True, secret_id=str(uuid.uuid4()), secret_value="482913").status_code == 200
    assert h.db.one("browser_agent_help_requests", id=hid)["status"] == "open"   # reminders keep running


def test_parked_waiters_of_a_login_help_whose_holder_ended_are_requeued(h):
    _, token, boot, holder = _rfi_job(h)
    waiter = h.second_job(token, boot)
    hid = h.open_help(token, boot, holder, reason_code="captcha").json()["help_id"]
    h.open_help(token, boot, waiter, reason_code="captcha")
    assert h.wcall("post", token, boot, waiter, "/park").status_code == 204
    assert help_flow.sweep_dead_login_holders(h.db.now, {}) == 0              # holder alive
    h.db.one("browser_agent_jobs", id=holder["id"]).update(status="canceled", lease_owner=None)
    assert help_flow.sweep_dead_login_holders(h.db.now, {}) == 1
    assert h.db.one("browser_agent_jobs", id=waiter["id"])["status"] == "queued"
    assert h.db.one("browser_agent_help_requests", id=hid)["status"] == "open"


# ── helper input reaches the job ───────────────────────────────────────────────


def test_a_message_then_retry_now_still_reaches_the_resumed_job(h):
    _, token, boot, job = _rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    assert h.wcall("post", token, boot, job, "/park").status_code == 204
    h.say(hid, body="here is the file")
    h.say(hid, action="retry")
    assert h.db.one("browser_agent_help_requests", id=hid)["status"] == "resolved"
    [claimed] = h.claim(token, boot, {"rfi_from_email": 1}).json()["jobs"]
    [item] = claimed["unconsumed_help"]
    assert item["status"] == "resolved"
    assert [m["body"] for m in item["messages"] if m["action"] == "message"] == ["here is the file"]


def test_an_old_resolved_or_given_up_help_is_not_delivered(h):
    _, token, boot, job = _rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    h.say(hid, body="note")
    row = h.db.one("browser_agent_help_requests", id=hid)
    row.update(status="resolved", resolution="give_up", resolved_at=iso(h.db.now))
    assert queue.unconsumed_help([job["id"]]) == {}
    row.update(resolution="retry", resolved_at=iso(h.db.now - timedelta(hours=49)))
    assert queue.unconsumed_help([job["id"]]) == {}
    row.update(resolved_at=iso(h.db.now - timedelta(hours=1)))
    assert list(queue.unconsumed_help([job["id"]])) == [job["id"]]


def test_a_re_ask_after_an_answer_reopens_and_announces_again(h):
    _, token, boot, job = _rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    row = h.db.one("browser_agent_help_requests", id=hid)
    mid = h.say(hid, body="I added the agent to the project").json()["id"]
    row.update(reminder_count=3, claimed_by="00000000-0000-4000-8000-0000000000e1",
               expires_at=iso(h.db.now + timedelta(hours=8)))
    assert row["status"] == "answered"
    h.wcall("post", token, boot, job, f"/help/{hid}/consume", json={"message_ids": [mid]})
    before = len([n for n in h.notices if n["type"] == "browser_agent.help_needed"])
    again = h.open_help(token, boot, job)
    assert again.status_code == 200 and again.json()["help_id"] == hid
    row = h.db.one("browser_agent_help_requests", id=hid)
    assert (row["status"], row["claimed_by"], row["reminder_count"]) == ("open", None, 0)
    assert queue.parse_ts(row["expires_at"]) >= h.db.now + timedelta(hours=47)
    assert len([n for n in h.notices if n["type"] == "browser_agent.help_needed"]) > before


def test_contributions_must_name_the_jobs_own_rfi(h):
    _, token, boot, job = _rfi_job(h)
    hid = h.open_help(token, boot, job).json()["help_id"]
    mid = h.say(hid, body="Use the EF-2 circuit").json()["id"]
    body = {"help_id": hid, "external_rfi_id": "999",
            "items": [{"kind": "note", "message_ids": [mid]}]}
    bad = h.wcall("post", token, boot, job, "/contributions", json=body)
    assert bad.status_code == 403 and error_code(bad) == "idor"
    assert h.db.rows("browser_agent_rfi_contributions") == []
    ok = h.wcall("post", token, boot, job, "/contributions", json={**body, "external_rfi_id": "456"})
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["accepted"]) == 1


# ── secrets, config (the login_released removal is pinned in test_procore_login_lock) ──


def test_the_scrub_learns_cookie_values_of_every_procore_host(h):
    h.db.tables["rfp_harvest_sessions"] = [
        {"provider": "procore", "cookies": [{"name": "a", "value": "app-cookie-value-123456"}]},
        {"provider": "procore:us02.procore.com", "cookies": [{"name": "b", "value": "us02-cookie-value-123456"}]},
        {"provider": "smartbid", "cookies": [{"name": "c", "value": "other-cookie-value-123456"}]},
    ]
    redaction.reset_for_tests()
    values = redaction._cookie_values()
    assert "app-cookie-value-123456" in values and "us02-cookie-value-123456" in values
    assert "other-cookie-value-123456" not in values


def test_rfp_harvest_active_counts_the_stored_procore_password(monkeypatch):
    base = dict(supabase_url="", rfp_ingest_enabled=True, rfp_harvest_enabled=True,
                procore_login_email="bot@example.com", procore_login_password="",
                pipelinesuite_enabled=False, smartbid_enabled=False, rfp_harvest_email_enabled=False)
    s = Settings(**base)
    assert s.rfp_harvest_active is False
    monkeypatch.setattr(procore_creds, "configured", lambda settings=None: True)
    assert s.rfp_harvest_active is True


"""Browser agent sweeper and hard-shutdown recovery (SPEC.md 3.3, 3.4).

Invariants pinned here:

  reclaim     an expired running lease is reclaimed only when its worker has
              been silent for a lease AND the sweeper is past its startup
              grace; the reclaim is uncharged ('interrupted'), bumps the
              epoch (the old holder's late heartbeat then finds nothing) and
              fails interrupted_repeatedly past max_interruptions.
  park        a waiting_help lease that lapsed is released once the hold is
              over or the worker is gone; help stays open.
  applying    follows its pm_rfi_sync_runs row (done / failed / gone).
  retries     a queued retry already at max_attempts fails crashed_repeatedly.
  expiry      queued jobs past expires_at become expired.
  cancel      requested cancels close once no live lease remains.
  spend       one 50% and one 100% IT Admin alert per window, re-armed by a
              reset or the next Pacific day.
  offline     one bell, then one email, re-armed once a worker is seen.
  isolation   a failing step never stops the others; later phases plug in
              through register_sweep_step.
  recovery    service stop, power pull with the PC coming back, and the PC
              staying off each end with the job queued again, uncharged.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

from app.services.browser_agents import queue

from tests.browser_agents_fake import iso
from tests.browser_agents_harness import API, Harness


def _sweep(h: Harness) -> dict[str, int]:
    return queue.sweep_once(h.db.now)


# ── 1. reclaim ─────────────────────────────────────────────────────────────


def test_no_reclaim_while_the_worker_is_still_seen(monkeypatch):
    h = Harness(monkeypatch)
    worker, _, _, job = h.active_with_job()
    h.db.advance(120)                      # lease (90 s) expired
    worker["last_seen_at"] = iso(h.db.now - timedelta(seconds=10))
    assert _sweep(h)["reclaim_expired"] == 0
    assert h.db.one("browser_agent_jobs", id=job["id"])["status"] == "running"


def test_no_reclaim_during_startup_grace(monkeypatch):
    h = Harness(monkeypatch)
    _, _, _, job = h.active_with_job()
    h.db.advance(600)                      # lease long expired, worker silent
    monkeypatch.setattr(queue, "_started_monotonic", time.monotonic())
    assert _sweep(h)["reclaim_expired"] == 0
    assert h.db.one("browser_agent_jobs", id=job["id"])["status"] == "running"


def test_reclaim_after_silence_is_uncharged_and_fences_the_old_holder(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    h.db.advance(200)
    assert _sweep(h)["reclaim_expired"] == 1
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["resume_reason"], row["interrupted_count"], row["lease_epoch"]) == (
        "queued", "interrupted", 1, 2)
    assert row["lease_owner"] is None and row["attempts"] == 1
    # The PC wakes up and beats with its old epoch: lost, not renewed.
    data = h.client.post(f"{API}/heartbeat", json={"boot_id": boot, "config_version": 1,
                                                   "jobs": [{"job_id": job["id"], "epoch": 1}]},
                         headers=h.auth(token)).json()
    assert data["lost"] == [job["id"]]
    kinds = [s["kind"] for s in h.db.rows("browser_agent_steps", job_id=job["id"])]
    assert "job.reclaimed" in kinds


def test_reclaim_fails_past_max_interruptions(monkeypatch):
    h = Harness(monkeypatch)
    _, _, _, job = h.active_with_job()
    h.db.one("browser_agent_types", key="canary")["max_interruptions"] = 2
    job["interrupted_count"] = 2
    h.db.advance(200)
    _sweep(h)
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["error_kind"], row["interrupted_count"]) == ("failed", "interrupted_repeatedly", 3)
    assert [n["type"] for n in h.notices] == ["browser_agent.job_failed"]


def test_two_sweepers_reclaim_once(monkeypatch):
    h = Harness(monkeypatch)
    _, _, _, job = h.active_with_job()
    h.db.advance(200)
    stale_view = dict(job)
    assert queue.reclaim_expired(h.db.now, {}) == 1
    # A rival sweeper acting on the row it read before the first write: no-op.
    assert queue._cas_job(stale_view, {"status": "queued"}, status="running") is False
    assert h.db.one("browser_agent_jobs", id=job["id"])["interrupted_count"] == 1


# ── 2. park lapsed holds ───────────────────────────────────────────────────


def test_park_waits_for_the_hold_unless_the_worker_is_gone(monkeypatch):
    h = Harness(monkeypatch)
    worker, _, _, job = h.active_with_job()
    help_id = str(uuid.uuid4())
    h.db.tables.setdefault("browser_agent_help_requests", []).append(h.db.defaults("browser_agent_help_requests", {
        "id": help_id, "scope_key": f"job:{job['id']}", "reason_code": "other", "title": "Help needed",
        "holder_job_id": job["id"], "opened_by_job_id": job["id"], "expires_at": iso(h.db.now + timedelta(hours=48))}))
    job.update(status="waiting_help", waiting_help_id=help_id, hold_until=iso(h.db.now + timedelta(minutes=15)))
    h.db.advance(120)
    worker["last_seen_at"] = iso(h.db.now)
    assert _sweep(h)["park_lapsed_holds"] == 0                # hold not over, worker alive
    h.db.advance(200)                                          # worker silent now
    assert _sweep(h)["park_lapsed_holds"] == 1
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["status"] == "waiting_help" and row["lease_owner"] is None and row["attempts"] == 1


# ── 3. applying ────────────────────────────────────────────────────────────


def _applying(h, run_status, stats=None):
    job = h.job("rfi_from_email")
    run_id = str(uuid.uuid4())
    h.db.tables.setdefault("pm_rfi_sync_runs", []).append(
        {"id": run_id, "status": run_status, "stats": stats or {}, "error": "boom" if run_status == "failed" else None})
    job.update(status="applying", outcome="pending", sync_run_id=run_id)
    return job


def test_applying_follows_its_run(monkeypatch):
    h = Harness(monkeypatch)
    done = _applying(h, "done")
    stale = _applying(h, "done", {"agent_outcome": "skipped_stale"})
    failed = _applying(h, "failed")
    waiting = _applying(h, "running")
    gone = h.job("rfi_from_email")
    gone.update(status="applying", outcome="pending", sync_run_id=None)
    assert _sweep(h)["reconcile_applying"] == 4
    assert (done["status"], done["outcome"]) == ("succeeded", "applied")
    assert (stale["status"], stale["outcome"]) == ("succeeded", "skipped_stale")
    assert (failed["status"], failed["outcome"], failed["error"]) == ("failed", "apply_failed", "boom")
    assert waiting["status"] == "applying"
    assert (gone["status"], gone["error_kind"]) == ("failed", "apply_failed")
    assert len([n for n in h.notices if n["type"] == "browser_agent.job_failed"]) == 2


# ── 4. exhausted retries, 5. expiry, 6. cancels ────────────────────────────


def test_queued_retry_at_the_cap_fails(monkeypatch):
    h = Harness(monkeypatch)
    at_cap = h.job(resume_reason="retry", attempts=3)
    below = h.job(resume_reason="retry", attempts=2)
    _sweep(h)
    assert (at_cap["status"], at_cap["error_kind"]) == ("failed", "crashed_repeatedly")
    assert below["status"] == "queued"


def test_queued_jobs_past_expiry_expire(monkeypatch):
    h = Harness(monkeypatch)
    old = h.job(expires_at=iso(h.db.now - timedelta(minutes=1)))
    fresh = h.job()
    assert _sweep(h)["expire_stale"] == 1
    assert (old["status"], old["error_kind"]) == ("expired", "expired") and fresh["status"] == "queued"


def test_cancels_close_once_no_live_lease_remains(monkeypatch):
    h = Harness(monkeypatch)
    _, _, _, running = h.active_with_job()
    queued = h.job(cancel_requested_at=iso(h.db.now))
    running["cancel_requested_at"] = iso(h.db.now)
    _sweep(h)
    assert queued["status"] == "canceled"
    assert running["status"] == "running"                     # the heartbeat delivers it first
    h.db.advance(200)
    _sweep(h)
    # reclaim runs first and requeues; the cancel step closes it in the same tick
    assert running["status"] == "canceled" and running["lease_owner"] is None


# ── 7. spend alerts ─────────────────────────────────────────────────────────


def _spend(h, usd):
    job = h.job()
    h.db.tables.setdefault("browser_agent_turns", []).append(
        {"job_id": job["id"], "turn_no": len(h.db.tables["browser_agent_turns"]) + 1, "cost_usd": usd,
         "created_at": iso(h.db.now)})


def test_spend_alerts_once_per_window_and_rearm_after_a_reset(monkeypatch):
    h = Harness(monkeypatch)
    _spend(h, 13)
    _sweep(h)
    _sweep(h)
    alerts = [n for n in h.notices if n["type"] == "browser_agent.spend_alert"]
    assert [a["metadata"]["fraction"] for a in alerts] == [0.5]
    _spend(h, 13)
    _sweep(h)
    alerts = [n for n in h.notices if n["type"] == "browser_agent.spend_alert"]
    assert [a["metadata"]["fraction"] for a in alerts] == [0.5, 1.0] and alerts[1]["email"] is True
    # IT Admin resets the window (the RPC nulls the flags); new spend re-alerts.
    rs = h.db.tables["browser_agent_runtime_state"][0]
    h.db.advance(60)
    rs.update(cost_window_started_at=iso(h.db.now), half_cap_notified_at=None, cap_hit_notified_at=None)
    h.db.advance(60)
    _spend(h, 14)
    _sweep(h)
    assert [a["metadata"]["fraction"] for a in h.notices if a["type"] == "browser_agent.spend_alert"] == [
        0.5, 1.0, 0.5]


def test_spend_flags_from_yesterday_rearm(monkeypatch):
    h = Harness(monkeypatch)
    rs = h.db.tables["browser_agent_runtime_state"][0]
    rs["half_cap_notified_at"] = iso(h.db.now - timedelta(days=1))
    _spend(h, 13)
    _sweep(h)
    assert [n["metadata"]["fraction"] for n in h.notices] == [0.5]


# ── 8. worker offline ───────────────────────────────────────────────────────


def test_offline_bell_then_email_then_rearm(monkeypatch):
    h = Harness(monkeypatch)
    worker, token = h.worker()
    worker["last_seen_at"] = iso(h.db.now)
    h.job()
    h.db.advance(61 * 60)
    _sweep(h)
    _sweep(h)
    offline = [n for n in h.notices if n["type"] == "browser_agent.worker_offline"]
    assert [n["email"] for n in offline] == [False]
    h.db.advance(4 * 3600)
    _sweep(h)
    _sweep(h)
    offline = [n for n in h.notices if n["type"] == "browser_agent.worker_offline"]
    assert [n["email"] for n in offline] == [False, True]
    h.hello(token)                                             # back online
    _sweep(h)
    rs = h.db.tables["browser_agent_runtime_state"][0]
    assert rs["offline_notified_at"] is None and rs["offline_emailed_at"] is None


def test_no_offline_alert_without_waiting_jobs(monkeypatch):
    h = Harness(monkeypatch)
    worker, _ = h.worker()
    worker["last_seen_at"] = iso(h.db.now)
    h.db.advance(10 * 3600)
    _sweep(h)
    assert not h.notices


# ── isolation and plug-ins ──────────────────────────────────────────────────


def test_a_failing_step_never_stops_the_others(monkeypatch):
    h = Harness(monkeypatch)
    old = h.job(expires_at=iso(h.db.now - timedelta(minutes=1)))

    def broken(now, settings_row):
        raise RuntimeError("boom")

    monkeypatch.setattr(queue, "_EXTRA_STEPS", [])
    queue.register_sweep_step("broken", broken)
    seen = []
    queue.register_sweep_step("later_phase", lambda now, s: seen.append(now) or 3)
    report = _sweep(h)
    assert report["broken"] == -1 and report["later_phase"] == 3 and old["status"] == "expired"
    queue.register_sweep_step("later_phase", lambda now, s: 7)       # re-registering replaces
    assert _sweep(h)["later_phase"] == 7 and len(queue._EXTRA_STEPS) == 2


# ── hard shutdown recovery, end to end ───────────────────────────────────────


def test_service_stop_requeues_uncharged_and_resumes(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    resp = h.client.post(f"{API}/jobs/{job['id']}/requeue", json={"reason": "shutdown", "service_stop": True},
                         headers=h.lease_headers(token, job["id"], 1, boot))
    assert resp.status_code == 204
    _, new_boot = h.hello(token)                               # service restarts
    claimed = h.claim(token, new_boot).json()["jobs"][0]
    assert claimed["attempts"] == 1 and claimed["last_resume_reason"] == "shutdown" and claimed["epoch"] == 3


def test_power_pull_then_pc_returns(monkeypatch):
    h = Harness(monkeypatch)
    _, token, old_boot, job = h.active_with_job()
    h.db.advance(30)                                           # power cut; no requeue was sent
    resp, new_boot = h.hello(token)
    assert resp.json()["requeued"] == 1
    claimed = h.claim(token, new_boot).json()["jobs"][0]
    assert claimed["attempts"] == 1 and claimed["last_resume_reason"] == "interrupted"
    late = h.client.post(f"{API}/jobs/{job['id']}/steps",
                         json={"steps": [{"idem_key": "spooled-1", "kind": "nav", "actor": "worker",
                                          "summary": "spooled before the power cut"}]},
                         headers=h.lease_headers(token, job["id"], 1, old_boot))
    assert late.json()["late"] == 1                            # the spool still lands, as late


def test_pc_stays_off_then_the_sweeper_reclaims(monkeypatch):
    h = Harness(monkeypatch)
    _, token, _, job = h.active_with_job()
    h.db.advance(3600)
    _sweep(h)
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["status"] == "queued" and row["attempts"] == 1
    _, other = h.worker()                                      # a second PC picks it up
    _, boot = h.hello(other)
    claimed = h.claim(other, boot).json()["jobs"][0]
    assert claimed["job_id"] == job["id"] and claimed["attempts"] == 1


def test_bdr_outage_alone_never_kills_a_live_job(monkeypatch):
    h = Harness(monkeypatch)
    worker, token, boot, job = h.active_with_job()
    h.db.advance(600)                                          # BDR down 10 min; no beats landed
    monkeypatch.setattr(queue, "_started_monotonic", time.monotonic())   # BDR just restarted
    _sweep(h)
    beat = h.client.post(f"{API}/heartbeat", json={"boot_id": boot, "config_version": 1,
                                                   "jobs": [{"job_id": job["id"], "epoch": 1}]},
                         headers=h.auth(token)).json()
    assert [r["job_id"] for r in beat["renewed"]] == [job["id"]]
    monkeypatch.setattr(queue, "_started_monotonic", time.monotonic() - 100_000)
    _sweep(h)
    assert h.db.one("browser_agent_jobs", id=job["id"])["status"] == "running"

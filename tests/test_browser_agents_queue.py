"""Browser agent queue and worker API (services/browser_agents/queue.py,
routers/agent_worker.py) over the 0157 RPC twins in browser_agents_fake.

Invariants pinned here:

  enqueue     one job per (agent_type, dedupe_key); a bad seed or an
              unknown type is refused at the call site.
  claim       kill switch, drain, allowed and advertised types, enablement,
              per-type max_concurrency, max_slots, next_attempt_at and expiry
              all hold; the FIRST claim charges 1 attempt and every claim
              bumps the epoch; prepare() closes jobs that need no browser and
              a prepare that raises releases the lease uncharged.
  fencing     a stale epoch is 409 lease_lost and a stale boot is 409
              stale_boot on every fenced call; late steps from an epoch the
              worker held are kept as late=true.
  hello       a new boot requeues the old boot's running leases uncharged
              (interrupted) and parks its waiting ones; >6 per hour suspends
              and bells IT Admin once.
  heartbeat   renews an expired lease nobody reclaimed; reports lost,
              cancel and orphans; never bumps config_version.
  requeue     shutdown/drain need service_stop (or a draining worker) and
              are uncharged; crash is charged and fails at max_attempts.
  writes      steps idempotent and redacted, checkpoint monotonic and
              type-shaped, finish outcomes per type, artifacts capped,
              hashed, sniffed and idempotent; releases signature-checked.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from datetime import timedelta

import pytest

from app.services.browser_agents import protocol, queue, registry
from app.services.browser_agents.registry import AgentTypeSpec, PrepareResult
from app.services.browser_agents.types import canary

from tests.browser_agents_fake import iso
from tests.browser_agents_harness import API, Harness, agent_settings, error_code

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ── enqueue ─────────────────────────────────────────────────────────────────


def test_enqueue_is_idempotent_on_the_dedupe_key(monkeypatch):
    h = Harness(monkeypatch)
    first = queue.enqueue("canary", "canary:one", {"expect_text": "BDR canary ok"})
    second = queue.enqueue("canary", "canary:one", {"expect_text": "BDR canary ok"})
    assert first.created and not second.created and first.job["id"] == second.job["id"]
    assert len(h.db.tables["browser_agent_jobs"]) == 1
    job = h.db.tables["browser_agent_jobs"][0]
    assert job["status"] == "queued" and job["attempts"] == 0 and job["lease_epoch"] == 0
    assert job["expires_at"] > iso(h.db.now + timedelta(hours=167))
    assert [s["kind"] for s in h.db.rows("browser_agent_steps", job_id=job["id"])] == ["job.enqueued"]


def test_enqueue_refuses_bad_seeds_and_unknown_types(monkeypatch):
    Harness(monkeypatch)
    with pytest.raises(ValueError):
        queue.enqueue("canary", "k", {"expect_text": "x" * 500})
    with pytest.raises(ValueError):
        queue.enqueue("rfi_from_email", "k", {"platform": "procore"})
    with pytest.raises(registry.UnknownAgentType):
        queue.enqueue("nope_type", "k", {})


def test_enqueue_canary_helper(monkeypatch):
    h = Harness(monkeypatch)
    job = canary.enqueue_canary(created_by="admin-1")
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["priority"] == canary.CANARY_PRIORITY and row["created_by"] == "admin-1"
    assert canary.CANARY_TEXT in canary.canary_page_html()


# ── claim ────────────────────────────────────────────────────────────────────


def test_first_claim_charges_one_attempt_and_bumps_the_epoch(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    assert job["status"] == "running" and job["attempts"] == 1 and job["lease_epoch"] == 1
    assert job["lease_boot_id"] == boot and job["config_snapshot"]["per_job_cost_cap_usd"] == 2
    claims = [s for s in h.db.rows("browser_agent_steps", job_id=job["id"]) if s["kind"] == "claim"]
    assert len(claims) == 1 and claims[0]["epoch"] == 1


def test_claim_response_shape(monkeypatch):
    h = Harness(monkeypatch)
    _, token = h.worker()
    _, boot = h.hello(token)
    job = h.job()
    data = h.claim(token, boot).json()
    parsed = protocol.ClaimResponse.model_validate(data)
    assert parsed.jobs[0].job_id == uuid.UUID(job["id"]) and parsed.jobs[0].attempts == 1
    assert parsed.jobs[0].input == {"expect_text": "BDR canary ok"}


def test_claim_returns_nothing_with_the_kill_switch_off_or_draining(monkeypatch):
    h = Harness(monkeypatch)
    worker, token = h.worker()
    _, boot = h.hello(token)
    h.job()
    h.db.tables["browser_agent_settings"][0]["enabled"] = False
    assert h.claim(token, boot).json() == {"jobs": []}
    h.db.tables["browser_agent_settings"][0]["enabled"] = True
    worker["draining"] = True
    assert h.claim(token, boot).json() == {"jobs": []}
    worker["draining"] = False
    assert len(h.claim(token, boot).json()["jobs"]) == 1


def test_claim_respects_allowed_advertised_and_enabled_types(monkeypatch):
    h = Harness(monkeypatch)
    _, token = h.worker(allowed_types=("canary",))
    _, boot = h.hello(token, capabilities={"types": {"canary": "1.0.0"}, "slots": 3})
    h.enable_type("rfi_from_email")
    h.job("rfi_from_email")
    assert h.claim(token, boot, {"rfi_from_email": 1}).json() == {"jobs": []}   # not allowed
    _, token2 = h.worker(allowed_types=("canary", "rfi_from_email"))
    _, boot2 = h.hello(token2, capabilities={"types": {"canary": "1.0.0"}, "slots": 3})
    assert h.claim(token2, boot2, {"rfi_from_email": 1}).json() == {"jobs": []}  # not advertised
    h.db.one("browser_agent_types", key="rfi_from_email")["enabled"] = False
    _, token3 = h.worker()
    _, boot3 = h.hello(token3)
    assert h.claim(token3, boot3, {"rfi_from_email": 1}).json() == {"jobs": []}  # disabled


def test_claim_respects_max_concurrency_across_workers(monkeypatch):
    h = Harness(monkeypatch)
    h.db.one("browser_agent_types", key="canary")["max_concurrency"] = 2
    for _ in range(4):
        h.job()
    _, t1 = h.worker()
    _, b1 = h.hello(t1)
    _, t2 = h.worker()
    _, b2 = h.hello(t2)
    assert len(h.claim(t1, b1, {"canary": 3}).json()["jobs"]) == 2
    assert h.claim(t2, b2, {"canary": 3}).json()["jobs"] == []


def test_claim_trims_capacity_to_free_slots(monkeypatch):
    h = Harness(monkeypatch)
    h.db.one("browser_agent_types", key="canary")["max_concurrency"] = 8
    for _ in range(5):
        h.job()
    _, token = h.worker(max_slots=2)
    _, boot = h.hello(token)
    assert len(h.claim(token, boot, {"canary": 5}).json()["jobs"]) == 2
    assert h.claim(token, boot, {"canary": 5}).json()["jobs"] == []         # both slots held


def test_trim_capacity_is_round_robin():
    assert queue.trim_capacity({"canary": 3, "rfi_from_email": 3}, max_slots=3, held=0) == {
        "canary": 2, "rfi_from_email": 1}
    assert queue.trim_capacity({"canary": 3, "unknown_type": 3}, max_slots=8, held=6) == {"canary": 2}


def test_claim_skips_future_and_expired_jobs(monkeypatch):
    h = Harness(monkeypatch)
    later = h.job(next_attempt_at=h.db.now + timedelta(minutes=5))
    h.job(expires_at=iso(h.db.now - timedelta(seconds=1)))
    _, token = h.worker()
    _, boot = h.hello(token)
    assert h.claim(token, boot, {"canary": 3}).json()["jobs"] == []
    h.db.advance(301)
    assert [j["job_id"] for j in h.claim(token, boot, {"canary": 3}).json()["jobs"]] == [later["id"]]


def test_claim_orders_by_priority_then_age(monkeypatch):
    h = Harness(monkeypatch)
    h.db.one("browser_agent_types", key="canary")["max_concurrency"] = 1
    old = h.job()
    h.db.advance(5)
    urgent = h.job(priority=10)
    _, token = h.worker()
    _, boot = h.hello(token)
    assert [j["job_id"] for j in h.claim(token, boot).json()["jobs"]] == [urgent["id"]]
    assert old["status"] == "queued"


def test_prepare_finishes_a_job_that_needs_no_browser(monkeypatch):
    h = Harness(monkeypatch)
    bad = h.job(input={"expect_text": "something the page never shows"})
    _, token = h.worker()
    _, boot = h.hello(token)
    assert h.claim(token, boot).json()["jobs"] == []
    row = h.db.one("browser_agent_jobs", id=bad["id"])
    assert row["status"] == "failed" and row["error_kind"] == "unsupported" and row["lease_owner"] is None
    assert [n["type"] for n in h.notices] == ["browser_agent.job_failed"]


def test_prepare_that_raises_releases_the_lease_uncharged(monkeypatch):
    h = Harness(monkeypatch)

    def boom(job):
        raise RuntimeError("db hiccup")

    monkeypatch.setitem(registry._TYPES, "canary", AgentTypeSpec(**{**canary.SPEC.__dict__, "prepare": boom}))
    job = h.job()
    _, token = h.worker()
    _, boot = h.hello(token)
    assert h.claim(token, boot).json()["jobs"] == []
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["status"] == "queued" and row["resume_reason"] == "interrupted"
    assert row["next_attempt_at"] and row["lease_epoch"] == 2
    monkeypatch.setitem(registry._TYPES, "canary", canary.SPEC)
    h.db.advance(61)
    h.claim(token, boot)
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["status"] == "running" and row["attempts"] == 1      # the release was free


def test_prepare_can_short_circuit_as_success(monkeypatch):
    h = Harness(monkeypatch)
    spec = AgentTypeSpec(**{**canary.SPEC.__dict__,
                            "prepare": lambda job: PrepareResult.succeed("self_test_ok", "nothing to do")})
    monkeypatch.setitem(registry._TYPES, "canary", spec)
    job = h.job()
    _, token = h.worker()
    _, boot = h.hello(token)
    assert h.claim(token, boot).json()["jobs"] == []
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["outcome"]) == ("succeeded", "self_test_ok") and not h.notices


def test_claim_carries_unconsumed_help_without_secret_values(monkeypatch):
    h = Harness(monkeypatch)
    job = h.job()
    help_id = str(uuid.uuid4())
    h.db.tables["browser_agent_help_requests"] = [{"id": help_id, "status": "answered"}]
    h.db.tables["browser_agent_help_waiters"] = [{"help_id": help_id, "job_id": job["id"]}]
    art = h.db.table("browser_agent_artifacts").insert({
        "help_id": help_id, "kind": "helper_upload", "storage_path": "help/x.pdf", "filename": "x.pdf",
        "mime_type": "application/pdf", "size_bytes": 10, "sha256": "a" * 64, "attachable": True,
    }).execute().data[0]
    h.db.tables["browser_agent_help_messages"] = [
        {"id": 1, "help_id": help_id, "author_kind": "agent", "body": "need help", "is_secret": False,
         "artifact_ids": [], "created_at": iso(h.db.now)},
        {"id": 2, "help_id": help_id, "author_kind": "helper", "author_label": "Pat (Estimating Admin)",
         "action": "message", "body": "try again", "is_secret": False, "artifact_ids": [art["id"]],
         "created_at": iso(h.db.now)},
        {"id": 3, "help_id": help_id, "author_kind": "helper", "body": None, "is_secret": True,
         "secret_ciphertext": "\\xdeadbeef", "artifact_ids": [], "created_at": iso(h.db.now)},
        {"id": 4, "help_id": help_id, "author_kind": "helper", "body": "old", "is_secret": False,
         "artifact_ids": [], "created_at": iso(h.db.now)},
    ]
    h.db.tables["browser_agent_help_reads"] = [{"job_id": job["id"], "message_id": 4}]
    _, token = h.worker()
    _, boot = h.hello(token)
    data = h.claim(token, boot).json()
    helps = data["jobs"][0]["unconsumed_help"]
    assert len(helps) == 1 and helps[0]["help_id"] == help_id
    msgs = {m["id"]: m for m in helps[0]["messages"]}
    assert set(msgs) == {2, 3}
    assert msgs[2]["files"][0]["filename"] == "x.pdf" and msgs[2]["files"][0]["attachable"] is True
    assert msgs[3]["body"] is None and msgs[3]["secret_ref"] == {"message_id": 3, "consumed": False}
    assert "deadbeef" not in json.dumps(data)


# ── fencing ──────────────────────────────────────────────────────────────────


def _step(idem="s1", **over):
    return {"idem_key": idem, "kind": "tool.navigate", "actor": "model", "summary": "opened the page", **over}


FENCED_CALLS = [
    ("post", "steps", {"steps": [_step()]}),
    ("put", "checkpoint", {"seq": 1, "data": {}}),
    ("post", "finish", {"status": "failed", "error_kind": "other", "summary": "x"}),
    ("post", "requeue", {"reason": "provider_outage"}),
    ("post", "park", None),
]


@pytest.mark.parametrize("method, path, body", FENCED_CALLS)
def test_stale_epoch_is_lease_lost(monkeypatch, method, path, body):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    headers = h.lease_headers(token, job["id"], job["lease_epoch"] + 1, boot)
    resp = getattr(h.client, method)(f"{API}/jobs/{job['id']}/{path}", json=body, headers=headers)
    assert resp.status_code == 409 and error_code(resp) == "lease_lost"
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method, path, body", FENCED_CALLS)
def test_stale_boot_is_stale_boot(monkeypatch, method, path, body):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    epoch = job["lease_epoch"]
    h.hello(token)                                   # a newer process registered
    headers = h.lease_headers(token, job["id"], epoch, boot)
    resp = getattr(h.client, method)(f"{API}/jobs/{job['id']}/{path}", json=body, headers=headers)
    if path == "steps":
        # the old boot really held this epoch (claim step): kept as late
        assert resp.status_code == 200, (resp.headers.get("x-error-code"), resp.text)
        assert resp.json() == {"appended": 0, "duplicates": 0, "late": 1}
    else:
        assert resp.status_code == 409 and error_code(resp) == "stale_boot"


def test_claim_and_heartbeat_from_a_stale_boot(monkeypatch):
    h = Harness(monkeypatch)
    _, token = h.worker()
    _, old = h.hello(token)
    h.hello(token)
    resp = h.claim(token, old)
    assert resp.status_code == 409 and error_code(resp) == "stale_boot"
    beat = h.client.post(f"{API}/heartbeat", json={"boot_id": old, "config_version": 1}, headers=h.auth(token))
    assert beat.status_code == 409 and error_code(beat) == "stale_boot"


def test_boot_header_must_agree_with_the_body(monkeypatch):
    h = Harness(monkeypatch)
    _, token = h.worker()
    _, boot = h.hello(token)
    resp = h.client.post(f"{API}/claim", json={"boot_id": boot, "capacity": {"canary": 1}},
                         headers=h.auth(token, **{"X-Agent-Boot": str(uuid.uuid4())}))
    assert resp.status_code == 422 and error_code(resp) == "invalid_request"


def test_late_steps_need_a_claim_step_for_that_epoch(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    url = f"{API}/jobs/{job['id']}/steps"
    h.client.post(f"{API}/jobs/{job['id']}/requeue", json={"reason": "provider_outage"},
                  headers=h.lease_headers(token, job["id"], 1, boot))
    late = h.client.post(url, json={"steps": [_step("late-1")]}, headers=h.lease_headers(token, job["id"], 1, boot))
    assert late.json() == {"appended": 0, "duplicates": 0, "late": 1}
    assert h.db.one("browser_agent_steps", idem_key="late-1")["late"] is True
    never = h.client.post(url, json={"steps": [_step("x")]}, headers=h.lease_headers(token, job["id"], 7, boot))
    assert never.status_code == 409 and error_code(never) == "lease_lost"


def test_a_job_held_by_another_worker_is_lease_lost(monkeypatch):
    h = Harness(monkeypatch)
    _, _, _, job = h.active_with_job()
    _, intruder = h.worker()
    _, ib = h.hello(intruder)
    resp = h.client.post(f"{API}/jobs/{job['id']}/park", headers=h.lease_headers(intruder, job["id"], 1, ib))
    assert resp.status_code == 409 and error_code(resp) == "lease_lost"


# ── hello ────────────────────────────────────────────────────────────────────


def test_hello_from_a_new_boot_requeues_running_leases_uncharged(monkeypatch):
    h = Harness(monkeypatch)
    _, token, old_boot, job = h.active_with_job()
    resp, new_boot = h.hello(token)
    assert resp.status_code == 200 and resp.json()["requeued"] == 1
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["resume_reason"], row["interrupted_count"], row["lease_epoch"]) == (
        "queued", "interrupted", 1, 2)
    h.claim(token, new_boot)
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["attempts"] == 1 and row["lease_epoch"] == 3 and row["last_resume_reason"] == "interrupted"


def test_hello_parks_a_waiting_job_of_an_old_boot(monkeypatch):
    h = Harness(monkeypatch)
    _, token, _, job = h.active_with_job()
    job.update(status="waiting_help", hold_until=iso(h.db.now + timedelta(minutes=10)))
    resp, _ = h.hello(token)
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["status"] == "waiting_help" and row["lease_owner"] is None and resp.json()["requeued"] == 0


def test_hello_suspends_after_six_per_hour_and_bells_once(monkeypatch):
    h = Harness(monkeypatch)
    worker, token = h.worker()
    statuses = [h.hello(token)[0].json()["status"] for _ in range(8)]
    assert statuses[:6] == ["active"] * 6 and statuses[6:] == ["suspended", "suspended"]
    assert worker["draining"] is True
    assert [n["type"] for n in h.notices] == ["browser_agent.worker_suspicious"]


def test_hello_reports_the_desired_release(monkeypatch):
    h = Harness(monkeypatch)
    _publish_release(h, "1.2.0", min_version="1.1.0")
    h.db.tables["browser_agent_settings"][0]["desired_worker_version"] = "1.2.0"
    _, token = h.worker()
    data = h.hello(token)[0].json()
    assert data["desired_version"] == "1.2.0" and data["min_version"] == "1.1.0"


# ── heartbeat ─────────────────────────────────────────────────────────────────


def _beat(h, token, boot, jobs):
    return h.client.post(f"{API}/heartbeat", json={"boot_id": boot, "config_version": 1, "jobs": jobs},
                         headers=h.auth(token))


def test_heartbeat_renews_an_expired_but_unreclaimed_lease(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    h.db.advance(300)                                  # BDR was down; lease long expired
    data = _beat(h, token, boot, [{"job_id": job["id"], "epoch": 1, "active_seconds_delta": 500}]).json()
    assert [r["job_id"] for r in data["renewed"]] == [job["id"]] and data["lost"] == []
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["lease_expires_at"] > iso(h.db.now) and row["active_seconds"] == 30   # clamped per beat


def test_heartbeat_reports_lost_cancel_and_orphans(monkeypatch):
    h = Harness(monkeypatch)
    h.db.one("browser_agent_types", key="canary")["max_concurrency"] = 3
    _, token = h.worker()
    _, boot = h.hello(token)
    a, b = h.job(), h.job()
    h.claim(token, boot, {"canary": 2})
    h.db.one("browser_agent_jobs", id=a["id"])["cancel_requested_at"] = iso(h.db.now)
    data = _beat(h, token, boot, [{"job_id": a["id"], "epoch": 1},
                                  {"job_id": str(uuid.uuid4()), "epoch": 1}]).json()
    assert data["cancel"] == [a["id"]] and len(data["lost"]) == 1
    assert data["orphans"] == [{"job_id": b["id"], "epoch": 1}] and data["controls"] == []


def test_heartbeat_never_bumps_config_version(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    before = h.db.tables["browser_agent_settings"][0]["config_version"]
    for _ in range(3):
        _beat(h, token, boot, [{"job_id": job["id"], "epoch": 1}])
    assert h.db.tables["browser_agent_settings"][0]["config_version"] == before


# ── config ────────────────────────────────────────────────────────────────────


def test_config_has_no_provider_model_or_keys_and_honors_the_etag(monkeypatch):
    h = Harness(monkeypatch)
    _, token = h.worker()
    resp = h.client.get(f"{API}/config", headers=h.auth(token))
    body = resp.json()
    protocol.ConfigResponse.model_validate(body)
    text = json.dumps(body)
    for forbidden in ("gpt-test", "openai", "price", "api_key", "password"):
        assert forbidden not in text
    assert body["types"]["canary"]["enabled"] is True and resp.headers["etag"] == "1"
    same = h.client.get(f"{API}/config", headers=h.auth(token, **{"If-None-Match": "1"}))
    assert same.status_code == 304 and same.headers["cache-control"] == "no-store"
    h.db.table("browser_agent_types").update({"max_concurrency": 1}).eq("key", "canary").execute()
    changed = h.client.get(f"{API}/config", headers=h.auth(token, **{"If-None-Match": "1"}))
    assert changed.status_code == 200 and changed.json()["config_version"] == 2


# ── requeue ───────────────────────────────────────────────────────────────────


def _requeue(h, token, boot, job, **body):
    return h.client.post(f"{API}/jobs/{job['id']}/requeue", json=body,
                         headers=h.lease_headers(token, job["id"], job["lease_epoch"], boot))


def test_shutdown_requeue_needs_service_stop_and_is_uncharged(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    refused = _requeue(h, token, boot, job, reason="shutdown")
    assert refused.status_code == 422 and error_code(refused) == "invalid_request"
    assert _requeue(h, token, boot, job, reason="shutdown", service_stop=True).status_code == 204
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["resume_reason"], row["interrupted_count"]) == ("queued", "shutdown", 1)
    h.claim(token, boot)
    assert h.db.one("browser_agent_jobs", id=job["id"])["attempts"] == 1


def test_drain_requeue_is_accepted_from_a_draining_worker(monkeypatch):
    h = Harness(monkeypatch)
    worker, token, boot, job = h.active_with_job()
    worker["draining"] = True
    assert _requeue(h, token, boot, job, reason="drain").status_code == 204


def test_crash_is_charged_and_fails_at_max_attempts(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    for attempt in (1, 2):
        assert _requeue(h, token, boot, job, reason="crash").status_code == 204
        h.claim(token, boot)
        job = h.db.one("browser_agent_jobs", id=job["id"])
        assert job["attempts"] == attempt + 1
    assert _requeue(h, token, boot, job, reason="crash").status_code == 204
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["error_kind"]) == ("failed", "crashed_repeatedly")
    assert [n["type"] for n in h.notices] == ["browser_agent.job_failed"]


def test_provider_outage_requeue_is_uncharged_with_a_capped_retry_at(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    far = iso(h.db.now + timedelta(days=9))
    assert _requeue(h, token, boot, job, reason="provider_outage", retry_at=far).status_code == 204
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["resume_reason"] == "provider_outage" and row["interrupted_count"] == 0
    assert row["next_attempt_at"] <= iso(h.db.now + timedelta(hours=24, seconds=1))


# ── steps ─────────────────────────────────────────────────────────────────────


def test_steps_are_idempotent_redacted_and_counted(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    headers = h.lease_headers(token, job["id"], 1, boot)
    step = _step(url_before="https://app.procore.com/1/project/rfi?token=SECRET#x",
                 result={"note": "see https://u:p@files.example.com/a?sig=abc"})
    first = h.client.post(f"{API}/jobs/{job['id']}/steps", json={"steps": [step]}, headers=headers)
    again = h.client.post(f"{API}/jobs/{job['id']}/steps", json={"steps": [step, _step("s2")]}, headers=headers)
    assert first.json() == {"appended": 1, "duplicates": 0, "late": 0}
    assert again.json() == {"appended": 1, "duplicates": 1, "late": 0}
    row = h.db.one("browser_agent_steps", idem_key="s1")
    assert row["url_before"] == "https://app.procore.com/1/project/rfi"
    assert "SECRET" not in json.dumps(row) and "sig=abc" not in json.dumps(row) and "u:p@" not in json.dumps(row)
    assert len(row["payload_sha256"]) == 64 and row["seq"] > 1
    assert h.db.one("browser_agent_jobs", id=job["id"])["step_count"] == 2


def test_steps_refuse_bdr_kinds_and_oversized_batches(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    headers = h.lease_headers(token, job["id"], 1, boot)
    bad_kind = h.client.post(f"{API}/jobs/{job['id']}/steps", json={"steps": [_step(kind="claim")]}, headers=headers)
    assert bad_kind.status_code == 422 and error_code(bad_kind) == "invalid_request"
    too_many = {"steps": [_step(f"k{i}") for i in range(protocol.MAX_STEPS_PER_POST + 1)]}
    assert h.client.post(f"{API}/jobs/{job['id']}/steps", json=too_many, headers=headers).status_code == 422


def test_steps_truncate_a_huge_payload(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    big = {"text": "x" * (protocol.STEP_PAYLOAD_MAX_BYTES + 10)}
    h.client.post(f"{API}/jobs/{job['id']}/steps", json={"steps": [_step(result=big)]},
                  headers=h.lease_headers(token, job["id"], 1, boot))
    row = h.db.one("browser_agent_steps", idem_key="s1")
    assert row["result"]["truncated"] is True


def test_steps_refuse_a_screenshot_of_another_job(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    other = h.job()
    art = h.db.table("browser_agent_artifacts").insert({
        "job_id": other["id"], "kind": "screenshot", "storage_path": "jobs/o/s.png", "mime_type": "image/png",
        "size_bytes": 1, "sha256": "b" * 64}).execute().data[0]
    resp = h.client.post(f"{API}/jobs/{job['id']}/steps",
                         json={"steps": [_step(screenshot_artifact_id=art["id"])]},
                         headers=h.lease_headers(token, job["id"], 1, boot))
    assert resp.status_code == 403 and error_code(resp) == "idor"


# ── checkpoint ────────────────────────────────────────────────────────────────


def test_checkpoint_is_monotonic_and_shaped_per_type(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job("rfi_from_email")
    url = f"{API}/jobs/{job['id']}/checkpoint"
    headers = h.lease_headers(token, job["id"], 1, boot)
    assert h.client.put(url, json={"seq": 2, "data": {"phase": "located", "rfi_id": "456"}},
                        headers=headers).status_code == 204
    assert h.client.put(url, json={"seq": 1, "data": {"phase": "seeded"}}, headers=headers).status_code == 204
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["checkpoint"]["phase"] == "located" and row["checkpoint_seq"] == 2
    bad = h.client.put(url, json={"seq": 3, "data": {"phase": "teleported"}}, headers=headers)
    assert bad.status_code == 422 and error_code(bad) == "invalid_request"


# ── finish ────────────────────────────────────────────────────────────────────


def _finish(h, token, boot, job, **body):
    return h.client.post(f"{API}/jobs/{job['id']}/finish", json=body,
                         headers=h.lease_headers(token, job["id"], job["lease_epoch"], boot))


def test_finish_outcomes_are_per_type_and_idempotent(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    wrong = _finish(h, token, boot, job, status="succeeded", outcome="nothing_to_do", summary="x")
    assert wrong.status_code == 422 and error_code(wrong) == "invalid_request"
    assert _finish(h, token, boot, job, status="succeeded", outcome="self_test_ok", summary="ok").status_code == 204
    assert _finish(h, token, boot, job, status="succeeded", outcome="self_test_ok", summary="ok").status_code == 204
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["outcome"], row["lease_owner"]) == ("succeeded", "self_test_ok", None)
    assert row["result"] == {"summary": "ok"} and not h.notices


def test_rfi_finish_allows_only_nothing_to_do(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job("rfi_from_email")
    assert _finish(h, token, boot, job, status="succeeded", outcome="self_test_ok", summary="x").status_code == 422
    assert _finish(h, token, boot, job, status="succeeded", outcome="nothing_to_do", summary="draft").status_code == 204


def test_finish_failed_bells_it_admin_and_redacts(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    resp = _finish(h, token, boot, job, status="failed", error_kind="cannot_complete",
                   error="boom at https://x.test/p?token=abc", summary="gave up")
    assert resp.status_code == 204
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["error"] == "boom at https://x.test/p" and row["error_kind"] == "cannot_complete"
    assert [n["type"] for n in h.notices] == ["browser_agent.job_failed"]


# ── park ──────────────────────────────────────────────────────────────────────


def test_park_needs_waiting_help(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    url = f"{API}/jobs/{job['id']}/park"
    resp = h.client.post(url, headers=h.lease_headers(token, job["id"], 1, boot))
    assert resp.status_code == 409 and error_code(resp) == "job_not_running"
    job.update(status="waiting_help")
    assert h.client.post(url, headers=h.lease_headers(token, job["id"], 1, boot)).status_code == 204
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["status"] == "waiting_help" and row["lease_owner"] is None


# ── artifacts ─────────────────────────────────────────────────────────────────


def _upload(h, token, boot, job, data: bytes, kind="screenshot", sha=None, filename="shot.png"):
    return h.client.post(
        f"{API}/jobs/{job['id']}/artifacts",
        data={"kind": kind, "sha256": sha or hashlib.sha256(data).hexdigest(), "filename": filename},
        files={"file": (filename, io.BytesIO(data), "application/octet-stream")},
        headers=h.lease_headers(token, job["id"], job["lease_epoch"], boot),
    )


def test_screenshot_upload_is_hashed_sniffed_and_idempotent(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    first = _upload(h, token, boot, job, PNG, filename="../../etc/shot.png")
    again = _upload(h, token, boot, job, PNG)
    assert first.status_code == 200 and first.json()["existed"] is False
    assert again.json() == {"artifact_id": first.json()["artifact_id"], "existed": True}
    row = h.db.one("browser_agent_artifacts", id=first.json()["artifact_id"])
    sha = hashlib.sha256(PNG).hexdigest()
    assert row["storage_path"] == f"jobs/{job['id']}/screenshot/{sha}.png" and row["filename"] == "shot.png"
    assert row["uploaded_by_worker"] and row["size_bytes"] == len(PNG)
    assert h.db.storage.store[("browser-agent", row["storage_path"])] == PNG


@pytest.mark.parametrize("data, kind, sha, needle", [
    (PNG, "screenshot", "0" * 64, "sha256"),
    (b"GIF89a....", "screenshot", None, "PNG or JPEG"),
    (b"{not json", "rfi_detail_json", None, "not valid JSON"),
    (b'{"status": 200}', "rfi_detail_json", None, "envelope"),
    (b"\xff\xfe\x00", "page_snapshot", None, "UTF-8"),
])
def test_bad_uploads_are_artifact_invalid(monkeypatch, data, kind, sha, needle):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    resp = _upload(h, token, boot, job, data, kind=kind, sha=sha)
    assert resp.status_code == 422 and error_code(resp) == "artifact_invalid" and needle in resp.json()["detail"]
    assert not h.db.tables.get("browser_agent_artifacts")


def test_upload_over_the_kind_cap_is_refused(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    big = PNG + b"\x00" * protocol.ARTIFACT_RULES["screenshot"]["max_bytes"]
    resp = _upload(h, token, boot, job, big)
    assert resp.status_code == 422 and error_code(resp) == "artifact_invalid" and "too large" in resp.json()["detail"]


def test_rfi_detail_envelope_upload(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job("rfi_from_email")
    env = {"request_path": "/rest/v1.0/projects/123/rfis/456", "status": 200,
           "captured_at": iso(h.db.now), "company_id": "9", "body": {"id": 456}}
    resp = _upload(h, token, boot, job, json.dumps(env).encode(), kind="rfi_detail_json", filename="d.json")
    assert resp.status_code == 200
    assert h.db.one("browser_agent_artifacts", id=resp.json()["artifact_id"])["mime_type"] == "application/json"


def test_upload_is_fenced_and_kinds_are_worker_only(monkeypatch):
    h = Harness(monkeypatch)
    _, token, boot, job = h.active_with_job()
    job["lease_epoch"] = 1
    stale = h.client.post(
        f"{API}/jobs/{job['id']}/artifacts",
        data={"kind": "screenshot", "sha256": hashlib.sha256(PNG).hexdigest(), "filename": "s.png"},
        files={"file": ("s.png", io.BytesIO(PNG), "image/png")},
        headers=h.lease_headers(token, job["id"], 5, boot))
    assert stale.status_code == 409 and error_code(stale) == "lease_lost"
    helper_kind = _upload(h, token, boot, job, PNG, kind="helper_upload")
    assert helper_kind.status_code == 422 and error_code(helper_kind) == "invalid_request"


# ── releases ──────────────────────────────────────────────────────────────────


def _keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(pub).decode()


def _publish_release(h, version, *, min_version="1.0.0", key=None, tamper=False):
    sha = hashlib.sha256(version.encode()).hexdigest()
    manifest = {"version": version, "sha256": sha, "min_version": min_version, "created_at": iso(h.db.now)}
    signature = ""
    if key is not None:
        signed = protocol.canonical_manifest_bytes(protocol.ReleaseManifest.model_validate(manifest))
        signature = base64.b64encode(key.sign(signed)).decode()
    if tamper:
        manifest = {**manifest, "min_version": "0.0.1"}
    h.db.tables.setdefault("browser_agent_worker_releases", []).append({
        "version": version, "storage_path": f"releases/{version}.zip", "sha256": sha,
        "min_version": min_version if not tamper else "0.0.1", "manifest": manifest, "signature": signature,
    })


def test_release_is_served_only_with_a_valid_signature(monkeypatch):
    key, pub = _keypair()
    h = Harness(monkeypatch, browser_agent_release_pubkey=pub)
    _publish_release(h, "1.2.0", key=key)
    _publish_release(h, "1.3.0", key=_keypair()[0])          # signed by someone else
    _, token = h.worker()
    ok = h.client.get(f"{API}/releases/1.2.0", headers=h.auth(token))
    assert ok.status_code == 200
    body = protocol.ReleaseResponse.model_validate(ok.json())
    assert body.manifest.version == "1.2.0" and "ttl=900" in body.url
    forged = h.client.get(f"{API}/releases/1.3.0", headers=h.auth(token))
    assert forged.status_code == 422 and error_code(forged) == "invalid_request"
    assert h.client.get(f"{API}/releases/latest", headers=h.auth(token)).status_code == 422
    h.db.tables["browser_agent_settings"][0]["desired_worker_version"] = "1.3.0"
    assert h.client.get(f"{API}/config", headers=h.auth(token)).json()["release"] is None
    h.db.tables["browser_agent_settings"][0]["desired_worker_version"] = "1.2.0"
    release = h.client.get(f"{API}/config", headers=h.auth(token)).json()["release"]
    assert release["version"] == "1.2.0" and release["signature"]


def test_release_manifest_must_match_its_row(monkeypatch):
    h = Harness(monkeypatch)
    _publish_release(h, "2.0.0", tamper=True)
    h.db.tables["browser_agent_worker_releases"][0]["manifest"]["min_version"] = "1.9.0"
    _, token = h.worker()
    assert h.client.get(f"{API}/releases/2.0.0", headers=h.auth(token)).status_code == 422


# ── canary page ───────────────────────────────────────────────────────────────


def test_canary_page_is_static_and_needs_no_token(monkeypatch):
    h = Harness(monkeypatch)
    resp = h.client.get(f"{API}/canary/page")
    assert resp.status_code == 200 and canary.CANARY_TEXT in resp.text
    assert "<script" not in resp.text.lower() and "<form" not in resp.text.lower()
    assert resp.headers["content-security-policy"].startswith("default-src 'none'")
    assert resp.headers["cache-control"] == "no-store"


def test_registry_contract():
    assert set(registry.type_keys()) >= {"canary", "rfi_from_email"}
    assert registry.get_type("canary").tools == protocol.TOOLS_BY_TYPE["canary"]
    assert registry.get_type("rfi_from_email").identity == "procore"
    assert registry.get_pm_app("procore").hosts == protocol.PROCORE_HOSTS
    with pytest.raises(ValueError):
        registry.register_type(canary.SPEC)                 # no silent double registration
    seed = {"platform": "procore", "host": "app.procore.com", "external_project_id": "1",
            "rfi_id": "2", "email_message_at": "2026-10-19T00:00:00Z"}
    # No BDR project (or no live link): failed quietly as no_link (SPEC.md 5.2;
    # the full prepare rules are in tests/test_rfi_agent.py).
    unlinked = registry.get_type("rfi_from_email").prepare({"input": seed})
    assert (unlinked.error_kind, unlinked.notify) == ("no_link", False)
    assert registry.get_type("rfi_from_email").prepare({"input": {}}).error_kind == "unsupported"


def test_settings_helper_builds():
    assert agent_settings().browser_agents_active


def test_a_job_canceled_while_queued_is_closed_not_handed_out(monkeypatch):
    h = Harness(monkeypatch)
    job = h.job(cancel_requested_at=iso(h.db.now))
    _, token = h.worker()
    _, boot = h.hello(token)
    assert h.claim(token, boot).json()["jobs"] == []
    row = h.db.one("browser_agent_jobs", id=job["id"])
    # 0158: the claim RPC skips cancel-requested jobs (never leased) ...
    assert (row["status"], row["lease_owner"], row["lease_epoch"]) == ("queued", None, 0)
    assert not h.db.rows("browser_agent_steps", job_id=job["id"], kind="claim")
    # ... and the sweeper's cancel step closes them.
    assert queue.finish_cancels(h.db.now, {}) == 1
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["status"], row["error_kind"], row["lease_owner"]) == ("canceled", "canceled", None)
    assert not h.notices

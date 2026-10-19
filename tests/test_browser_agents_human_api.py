"""/browser-agents human API (docs/browser_agents/HUMAN_API.md, C2): summary,
jobs, artifacts, admin; notices and deep links (C3); RFI provenance in
_enrich_rfis. FakeDB, no network."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import Settings
from app.services import notification_email, notifications
from app.services.browser_agents import admin
from app.services.browser_agents.llm import models as llm_models
from tests.browser_agents_fake import iso
from tests.browser_agents_harness import agent_settings, error_code
from tests.browser_agents_human import PROFILES, HumanHarness


@pytest.fixture
def make(monkeypatch):
    made: list[HumanHarness] = []

    def _make(**kw) -> HumanHarness:
        h = HumanHarness(monkeypatch, **kw)
        made.append(h)
        return h

    yield _make
    for h in made:
        h.close()


@pytest.fixture
def h(make):
    return make()


def dump(h) -> str:
    return json.dumps(h.db.tables, default=str)


# ── Gate and roles ─────────────────────────────────────────────────────────────


def test_every_route_is_a_bare_404_while_agents_are_off(h):
    h.settings = agent_settings(browser_agents_enabled=False)
    for path in ("/summary", "/jobs", "/help", "/admin/state"):
        resp = h.get(path, who="it")
        assert resp.status_code == 404 and resp.json() == {"detail": "Not Found"}


def test_roles(h):
    assert h.get("/summary", who="exec").status_code == 200
    assert h.get("/summary", who="acct").status_code == 403
    assert h.get("/admin/state", who="ea").status_code == 403
    assert h.get("/admin/state", who="exec").status_code == 403
    assert h.get("/admin/state", who="dev").status_code == 200       # a dev account is an admin
    resp = h.get("/admin/state", who="it")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    job = h.job("canary")
    assert h.post(f"/jobs/{job['id']}/cancel", who="exec").status_code == 403


# ── Summary and jobs ────────────────────────────────────────────────────────────


def test_summary_counts_and_spend(h):
    worker, token = h.worker()
    h.hello(token)
    h.job("canary")
    h.db.table("browser_agent_turns").insert({"job_id": str(uuid.uuid4()), "turn_no": 1, "epoch": 1,
                                              "status": "issued", "tool_results": {}, "cost_usd": 1.25}).execute()
    s = h.get("/summary", who="exec").json()
    assert s == {"enabled": True, "open_help": 0, "waiting_jobs": 0, "running_jobs": 0, "queued_jobs": 1,
                 "worker_online": True, "workers_online": 1, "spend_today_usd": 1.25, "daily_cap_usd": 25.0,
                 "per_job_cap_usd": 2.0}


def test_jobs_list_filters_and_pages_newest_first(h):
    ids = []
    for i in range(5):
        ids.append(h.job("canary")["id"])
        h.db.advance(1)
    rfi = h.job("rfi_from_email")
    page1 = h.get("/jobs", params={"type": "canary", "limit": 2}).json()
    assert [j["id"] for j in page1["items"]] == ids[::-1][:2] and page1["next_cursor"]
    page2 = h.get("/jobs", params={"type": "canary", "limit": 2, "cursor": page1["next_cursor"]}).json()
    page3 = h.get("/jobs", params={"type": "canary", "limit": 2, "cursor": page2["next_cursor"]}).json()
    assert [j["id"] for j in page2["items"] + page3["items"]] == ids[::-1][2:] and page3["next_cursor"] is None
    one = h.get("/jobs", params={"type": "rfi_from_email"}).json()["items"]
    assert one[0]["rfi_label"] == "RFI 456" and one[0]["project_name"] == "Linked"
    assert one[0]["id"] == rfi["id"] and one[0]["cost_usd"] == 0
    h.db.one("browser_agent_jobs", id=ids[0]).update(status="expired")
    assert [j["id"] for j in h.get("/jobs", params={"status": "finished"}).json()["items"]] == [ids[0]]
    assert h.get("/jobs", params={"status": "bogus"}).status_code == 422
    assert h.get("/jobs", params={"cursor": "%%%"}).status_code == 422


def test_rfi_labels_use_the_procore_number_or_the_locate_hint(h):
    job = h.job("rfi_from_email")
    h.db.tables["rfis"] = [{"id": "r1", "project_id": job["project_id"], "external_id": "456",
                            "external_number": "Const-198"}]
    locate = h.job("rfi_from_email", input={"platform": "procore", "host": "app.procore.com",
                                            "external_project_id": "123", "rfi_number_hint": "198",
                                            "email_message_at": iso(h.db.now)})
    items = {j["id"]: j["rfi_label"] for j in h.get("/jobs").json()["items"]}
    assert items[job["id"]] == "RFI Const-198" and items[locate["id"]] == "Locate RFI #198"


def test_job_detail_steps_and_turns(h):
    _, token, boot, job = h.active_with_job("rfi_from_email")
    h.db.table("browser_agent_turns").insert({
        "job_id": job["id"], "turn_no": 1, "epoch": job["lease_epoch"], "status": "issued", "tool_results": {},
        "action": {"call_id": "c1", "tool": "navigate", "args": {"url": "https://app.procore.com/1/project/home"},
                   "rejected": [{"why": "x"}], "replay": {"sig": "s"}},
        "provider": "openai", "model": "gpt-test", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01,
    }).execute()
    detail = h.get(f"/jobs/{job['id']}", who="exec").json()
    assert detail["input_summary"]["rfi_id"] == "456" and detail["input_summary"]["host"] == "app.procore.com"
    assert detail["config_snapshot"]["model"] == "gpt-test" and detail["config_snapshot"]["per_job_cap_usd"] == 2.0
    assert detail["tokens_in"] == 10 and detail["cost_usd"] == 0.01 and detail["can_retry"] is False
    turns = h.get(f"/jobs/{job['id']}/turns").json()["items"]
    assert turns[0]["action"] == {"call_id": "c1", "tool": "navigate",
                                  "args": {"url": "https://app.procore.com/1/project/home"}}
    steps = h.get(f"/jobs/{job['id']}/steps", params={"limit": 1}).json()
    assert len(steps["items"]) == 1 and steps["next_after_seq"] == steps["items"][0]["seq"]
    rest = h.get(f"/jobs/{job['id']}/steps", params={"after_seq": steps["next_after_seq"]}).json()
    assert rest["next_after_seq"] is None and all(s["seq"] > steps["next_after_seq"] for s in rest["items"])
    assert h.get(f"/jobs/{uuid.uuid4()}").status_code == 404


def test_cancel_queued_at_once_and_running_by_request(h):
    queued = h.job("canary")
    out = h.post(f"/jobs/{queued['id']}/cancel").json()
    assert out["status"] == "canceled" and out["error_kind"] == "canceled"
    _, token, boot, job = h.active_with_job("canary")
    out = h.post(f"/jobs/{job['id']}/cancel").json()
    assert out["status"] == "running" and out["cancel_requested_at"]
    beat = h.client.post("/agent-worker/v1/heartbeat", headers=h.auth(token, **{"X-Agent-Boot": boot}),
                         json={"boot_id": boot, "config_version": 1, "jobs": [
                             {"job_id": job["id"], "epoch": job["lease_epoch"], "phase": "x", "step_count": 0,
                              "active_seconds_delta": 1}]})
    assert beat.json()["cancel"] == [job["id"]]
    assert [a["action"] for a in h.audits] == ["browser_agent.job_cancel", "browser_agent.job_cancel"]


def test_retry_rules(h):
    _, token, boot, job = h.active_with_job("canary")
    refused = h.post(f"/jobs/{job['id']}/retry")
    assert refused.status_code == 409 and error_code(refused) == "job_not_retryable"
    h.db.one("browser_agent_jobs", id=job["id"]).update(status="failed", error_kind="crashed_repeatedly",
                                                        attempts=3, lease_owner=None, lease_expires_at=None,
                                                        finished_at=iso(h.db.now))
    out = h.post(f"/jobs/{job['id']}/retry").json()
    assert out["status"] == "queued" and out["attempts"] == 0
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert (row["resume_reason"], row["priority"], row["finished_at"]) == ("manual", 10, None)
    assert h.audits[-1]["action"] == "browser_agent.job_retry"


def test_cap_reached_retry_once_for_a_helper_then_it_admin_only(h):
    job = h.job("canary")
    h.db.one("browser_agent_jobs", id=job["id"]).update(status="failed", error_kind="cap_reached", cost_usd=2.0,
                                                        tokens_in=5000, turn_count=4)
    assert h.post(f"/jobs/{job['id']}/retry").status_code == 200
    row = h.db.one("browser_agent_jobs", id=job["id"])
    assert row["helper_retry_used"] is True and row["cost_usd"] == 0 and row["result"]["cap_reset"]["after_turn_no"] == 4
    row.update(status="failed", error_kind="cap_reached")
    assert h.get(f"/jobs/{job['id']}").json()["can_retry"] is False
    again = h.post(f"/jobs/{job['id']}/retry")
    assert again.status_code == 403 and error_code(again) == "retry_admin_only"
    assert h.get(f"/jobs/{job['id']}", who="it").json()["can_retry"] is True
    assert h.post(f"/jobs/{job['id']}/retry", who="it").status_code == 200
    listed = h.get("/jobs").json()["items"][0]
    assert listed["cost_usd"] == 2.0                                     # spent before the reset still shows


def test_artifact_urls_and_model_transcripts_are_admin_only(h):
    job = h.job("canary")
    shot = h.db.table("browser_agent_artifacts").insert({
        "job_id": job["id"], "kind": "screenshot", "storage_path": "jobs/x/s.png", "mime_type": "image/png",
        "size_bytes": 3, "sha256": "1" * 64, "filename": "s.png"}).execute().data[0]
    req = h.db.table("browser_agent_artifacts").insert({
        "job_id": job["id"], "kind": "llm_request", "storage_path": "jobs/x/r.json", "mime_type": "application/json",
        "size_bytes": 3, "sha256": "2" * 64}).execute().data[0]
    ok = h.get(f"/artifacts/{shot['id']}/url", who="exec").json()
    assert ok["expires_in"] == 300 and "ttl=300" in ok["url"] and ok["filename"] == "s.png"
    assert h.get(f"/artifacts/{req['id']}/url", who="ea").status_code == 403
    assert h.get(f"/artifacts/{req['id']}/url", who="it").status_code == 200


# ── Admin ───────────────────────────────────────────────────────────────────────


def test_admin_state_shape_and_no_secret_values(h):
    worker, token = h.worker()
    h.put("/admin/secrets/procore_password", json={"value": "Stored-Secret-77"})
    state = h.get("/admin/state", who="it").json()
    assert set(state) == {"settings", "runtime", "caps", "types", "workers", "secrets", "procore", "releases",
                          "providers", "ceilings", "pm_rfi_sync_active"}
    assert {t["key"] for t in state["types"]} == {"canary", "rfi_from_email"}
    assert state["ceilings"]["rfi_from_email"] == ["app.procore.com", "us02.procore.com"]
    assert state["secrets"][0]["set"] is True and len(state["secrets"][0]["fingerprint"]) == 8
    assert state["procore"]["email"] == "agent@g3.test"
    assert state["workers"][0]["id"] == worker["id"] and state["workers"][0]["online"] is False
    text = json.dumps(state)
    assert "Stored-Secret-77" not in text and "Procore-Pass-9!" not in text and token not in text


def test_patch_settings_is_audited_and_checks_the_desired_version(h):
    out = h.patch("/admin/settings", json={"daily_cost_cap_usd": 40, "help_reminder_minutes": 60})
    assert out.status_code == 200, out.text
    assert out.json()["settings"]["daily_cost_cap_usd"] == 40.0
    assert out.json()["settings"]["updated_by_label"] == "Ian Tech (IT Admin)"
    changes = h.db.rows("browser_agent_settings_changes", scope="global")
    assert {c["field"] for c in changes} == {"daily_cost_cap_usd", "help_reminder_minutes"}
    assert h.db.rows("audit_log", action="browser_agent.settings_update")
    unknown = h.patch("/admin/settings", json={"desired_worker_version": "9.9.9"})
    assert unknown.status_code == 409 and error_code(unknown) == "below_min_version"
    for v, mv in (("1.0.0", "1.0.0"), ("1.2.0", "1.1.0")):
        h.db.table("browser_agent_worker_releases").insert({"version": v, "min_version": mv, "sha256": "a" * 64,
                                                            "storage_path": "p", "manifest": {},
                                                            "signature": "s"}).execute()
    low = h.patch("/admin/settings", json={"desired_worker_version": "1.0.0"})
    assert low.status_code == 409 and error_code(low) == "below_min_version"
    assert h.patch("/admin/settings", json={"desired_worker_version": "1.2.0"}).status_code == 200
    assert h.patch("/admin/settings", json={"desired_worker_version": None}).json()["settings"][
        "desired_worker_version"] is None
    bad = h.patch("/admin/settings", json={"per_job_cost_cap_usd": 99})
    assert bad.status_code == 422 and error_code(bad) == "invalid_request"
    assert h.patch("/admin/settings", json={"config_version": 5}).status_code == 422
    assert h.patch("/admin/settings", who="ea", json={"enabled": False}).status_code == 403


def test_spend_reset_rearms_alerts_and_is_logged(h):
    h.db.tables["browser_agent_runtime_state"][0].update(half_cap_notified_at=iso(h.db.now))
    caps = h.post("/admin/spend/reset", who="it", json={"note": "budget raised"}).json()
    assert caps["window_reset_by_label"] == "Ian Tech (IT Admin)" and caps["spend_today_usd"] == 0
    assert h.db.tables["browser_agent_runtime_state"][0]["half_cap_notified_at"] is None
    [change] = h.db.rows("browser_agent_settings_changes", scope="spend")
    assert change["new_value"]["note"] == "budget raised"


def test_type_patch_rules(h, monkeypatch):
    monkeypatch.setattr(llm_models, "recent_probe", lambda *a, **k: None)
    change = h.patch("/admin/types/canary", json={"model": "gpt-other"})
    assert change.status_code == 409 and error_code(change) == "probe_failed"
    anyway = h.patch("/admin/types/canary", json={"model": "gpt-other", "save_anyway": True})
    assert anyway.status_code == 200 and anyway.json()["model"] == "gpt-other"
    assert anyway.json()["model_probe_ok_at"] is None
    [row] = [c for c in h.db.rows("browser_agent_settings_changes", field="model")]
    assert row["probe"]["save_anyway"] is True
    monkeypatch.setattr(llm_models, "recent_probe", lambda *a, **k: {"ok": True, "probed_at": iso(h.db.now)})
    probed = h.patch("/admin/types/canary", json={"provider": "anthropic", "model": "claude-x"}).json()
    assert probed["provider"] == "anthropic" and probed["model_probe_ok_at"]
    needs = h.patch("/admin/types/rfi_from_email", json={"enabled": True})
    assert needs.status_code == 422 and error_code(needs) == "type_needs_model"
    real = h.settings

    class SyncOff:
        pm_rfi_sync_active = False

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(admin, "get_settings", lambda: SyncOff())
    off = h.patch("/admin/types/rfi_from_email", json={"enabled": True, "provider": "openai", "model": "gpt-x",
                                                       "price_in_per_mtok": 1, "price_out_per_mtok": 2})
    assert off.status_code == 409 and error_code(off) == "pm_rfi_sync_off"
    wide = h.patch("/admin/types/rfi_from_email", json={"host_narrowing": ["evil.example.com"]})
    assert wide.status_code == 422 and error_code(wide) == "host_not_in_ceiling"
    narrow = h.patch("/admin/types/rfi_from_email", json={"host_narrowing": ["app.procore.com"]})
    assert narrow.json()["host_narrowing"] == ["app.procore.com"]
    assert h.patch("/admin/types/nope", json={"label": "x"}).status_code == 404


def test_models_and_probe_need_the_provider_key(h, monkeypatch):
    missing = h.get("/admin/models", who="it", params={"provider": "google"})
    assert missing.status_code == 409 and error_code(missing) == "provider_key_missing"
    monkeypatch.setattr(llm_models, "probe", lambda provider, model, **k: {
        "ok": True, "latency_ms": 12, "error": None, "probed_at": iso(h.db.now)})
    out = h.post("/admin/types/canary/probe", who="it", json={"provider": "openai", "model": "gpt-test"})
    assert out.status_code == 200 and out.json()["ok"] is True
    assert h.db.rows("browser_agent_settings_changes", field="model_probe")


def test_secrets_are_write_only(h):
    out = h.put("/admin/secrets/procore_password", json={"value": "New-Secret-123"})
    assert out.status_code == 200 and out.json()["set"] is True and "New-Secret-123" not in out.text
    assert "New-Secret-123" not in dump(h)
    assert h.put("/admin/secrets/other_password", json={"value": "x"}).status_code == 404
    assert h.delete("/admin/secrets/procore_password").status_code == 204
    assert h.get("/admin/state", who="it").json()["secrets"][0]["set"] is False
    actions = [a["action"] for a in h.audits]
    assert "browser_agent.secret_set" in actions and "browser_agent.secret_cleared" in actions


def test_enrollment_code_install_command_and_fingerprint(h):
    key = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw,
                                                                 serialization.PublicFormat.Raw)
    h.settings = agent_settings(procore_login_password="Procore-Pass-9!",
                                browser_agent_release_pubkey=base64.b64encode(key).decode(),
                                browser_agent_api_base_url="https://api.g3.test")
    out = h.post("/admin/enrollments", who="it", json={"worker_name": "Office PC"}).json()
    assert len(out["code"]) == 20 and out["code"].isalnum()
    assert out["install_command"].startswith("irm https://api.g3.test/agent-worker/v1/installer.ps1")
    assert len(out["key_fingerprint"].replace(" ", "")) == 64
    [row] = h.db.rows("browser_agent_enrollments")
    assert out["code"] not in dump(h) and row["worker_name"] == "Office PC"
    enrolled = h.client.post("/agent-worker/v1/enroll",
                             json={"code": out["code"], "hostname": "PC1", "os_info": {}, "version": "1.0.0"})
    assert enrolled.status_code == 200 and enrolled.json()["status"] == "pending"
    assert h.audits[0]["action"] == "browser_agent.enrollment_created"


def test_worker_approve_patch_revoke(h):
    worker, token = h.worker(status="pending")
    approved = h.post(f"/admin/workers/{worker['id']}/approve", who="it").json()
    assert approved["status"] == "active" and approved["approved_by_label"] == "Ian Tech (IT Admin)"
    # Approval opens every built-in type (an empty list would never claim).
    assert approved["allowed_types"] == ["canary", "rfi_from_email"]
    patched = h.patch(f"/admin/workers/{worker['id']}", json={"max_slots": 5, "draining": True,
                                                              "allowed_ips": ["10.0.0.1", "192.168.1.0/24"],
                                                              "allowed_types": ["canary"]}).json()
    assert patched["max_slots"] == 5 and patched["draining"] is True
    assert patched["allowed_ips"] == ["10.0.0.1/32", "192.168.1.0/24"] and patched["allowed_types"] == ["canary"]
    bad = h.patch(f"/admin/workers/{worker['id']}", json={"allowed_ips": ["not-an-ip"]})
    assert bad.status_code == 422
    assert h.patch(f"/admin/workers/{worker['id']}", json={"allowed_types": ["nope"]}).status_code == 422
    revoked = h.post(f"/admin/workers/{worker['id']}/revoke", who="it").json()
    assert revoked["status"] == "revoked"
    assert h.db.one("browser_agent_workers", id=worker["id"])["token_hash"] is None
    refused = h.client.get("/agent-worker/v1/config", headers=h.auth(token))
    assert refused.status_code == 401
    actions = [a["action"] for a in h.audits]
    assert "browser_agent.worker_approved" in actions and "browser_agent.worker_revoked" in actions


def test_history_pages_newest_first(h):
    for cap in (10, 11, 12):
        h.patch("/admin/settings", json={"daily_cost_cap_usd": cap})
    first = h.get("/admin/history", who="it", params={"limit": 2}).json()
    assert [c["new_value"] for c in first["items"]] == [12.0, 11.0] and first["next_cursor"]
    second = h.get("/admin/history", who="it", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [c["new_value"] for c in second["items"]] == [10.0] and second["next_cursor"] is None


def test_canary_and_stop_running(h):
    worker, token = h.worker()
    out = h.post("/admin/canary", who="it", json={"worker_id": worker["id"]}).json()
    job = h.db.one("browser_agent_jobs", id=out["job_id"])
    assert job["agent_type"] == "canary" and job["created_by"] == PROFILES["it"][0]
    _, boot = h.hello(token)
    assert [j["job_id"] for j in h.claim(token, boot).json()["jobs"]] == [out["job_id"]]
    stopped = h.post("/admin/stop-running", who="it").json()
    assert stopped == {"cancel_requested": 1}
    assert h.db.one("browser_agent_jobs", id=out["job_id"])["cancel_requested_at"]
    assert h.post("/admin/stop-running", who="it").json() == {"cancel_requested": 0}


# ── Notices (C3) ────────────────────────────────────────────────────────────────


def test_notify_role_returns_the_count_and_help_id_scopes_a_dismissal(monkeypatch):
    calls = []

    class Q:
        def __init__(self, table):
            self.table = table

        def __getattr__(self, name):
            def _f(*a, **k):
                calls.append((self.table, name, a))
                return self
            return _f

        def execute(self):
            from types import SimpleNamespace

            if self.table == "profiles":
                return SimpleNamespace(data=[{"id": "u1"}, {"id": "u2"}])
            return SimpleNamespace(data=[{"id": "n1"}, {"id": "n2"}])

    class DB:
        def table(self, name):
            return Q(name)

    monkeypatch.setattr(notifications, "get_supabase", lambda: DB())
    from app.core.roles import Role

    assert notifications.notify_role(Role.IT_ADMIN, None, "browser_agent.spend_alert", "m", mirror_email=False) == 2
    notifications.dismiss_notifications(types=["browser_agent.help_needed"], metadata_eq={"help_id": "h1"})
    assert ("notifications", "eq", ("metadata->>help_id", "h1")) in calls
    with pytest.raises(ValueError):
        notifications.dismiss_notifications(types=["x"], metadata_eq={"gc_id": "g"})


def test_email_deep_links_and_titles(monkeypatch):
    monkeypatch.setattr(notification_email, "get_settings", lambda: Settings(frontend_url="https://bdr.test"))
    link = notification_email._deep_link
    assert link("p1", "estimating_admin", "browser_agent.help_needed", {"help_id": "h1"}) == "https://bdr.test/agents/help/h1"
    assert link(None, "it_admin", "browser_agent.job_failed", {"job_id": "j1"}) == "https://bdr.test/agents/jobs/j1"
    assert link(None, "it_admin", "browser_agent.spend_alert", {}) == "https://bdr.test/settings/browser-agents"
    for t in notifications.BROWSER_AGENT_NOTIFICATION_TYPES:
        assert notification_email.heading_for(t) != "BDR notification"
        assert "\u2014" not in notification_email.heading_for(t)


def test_notification_types_match_the_contract():
    from app.services.browser_agents import protocol

    assert set(notifications.BROWSER_AGENT_NOTIFICATION_TYPES) == set(protocol.NOTIFICATION_TYPES)


# ── RFI provenance ──────────────────────────────────────────────────────────────


def test_enrich_rfis_passes_agent_provenance(monkeypatch):
    from app.routers import pm_field

    monkeypatch.setattr(pm_field, "_latest_sends", lambda ids: {})
    monkeypatch.setattr(pm_field, "_names", lambda *a, **k: {})

    class DB:
        def table(self, name):
            from types import SimpleNamespace

            class Q:
                def __getattr__(self, n):
                    return lambda *a, **k: self

                def execute(self):
                    return SimpleNamespace(data=[])

            return Q()

    monkeypatch.setattr(pm_field, "get_supabase", lambda: DB())
    rows = pm_field._enrich_rfis("p", [
        {"id": "r1", "source": "procore", "import_method": "agent", "imported_by_agent_job_id": "j1",
         "external_supplement": [{"text": "use 3/4", "by": "Erin", "at": "2026-10-19", "help_id": "h"}, "junk"]},
        {"id": "r2"},
    ])
    assert rows[0]["import_method"] == "agent" and rows[0]["imported_by_agent_job_id"] == "j1"
    assert rows[0]["external_supplement"] == [{"text": "use 3/4", "by": "Erin", "at": "2026-10-19"}]
    assert rows[1]["import_method"] is None and rows[1]["external_supplement"] == []


def test_cursor_round_trip():
    c = admin.encode_cursor({"t": "2026-10-19T17:00:00+00:00", "id": "x"})
    assert admin.decode_cursor(c) == {"t": "2026-10-19T17:00:00+00:00", "id": "x"}
    assert admin.decode_cursor(None) is None
    assert admin.clamp_limit(1000) == admin.LIST_MAX and admin.clamp_limit(0) == admin.LIST_DEFAULT
    assert timedelta(seconds=admin.ONLINE_SECONDS) == timedelta(seconds=30)

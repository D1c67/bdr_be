"""Phase 6 boundaries on the existing org platform, with real HTTP edges."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.services.browser_agents import admin, help as help_flow, protocol, queue, registry
from app.services.browser_agents.types import rfi_from_email, user_browser_read
from tests.browser_agents_harness import agent_settings
from tests.browser_agents_human import HumanHarness, PROFILES


@pytest.fixture
def h(monkeypatch):
    harness = HumanHarness(monkeypatch)
    yield harness
    harness.close()


def personal_job(h):
    row = h.job("canary")
    row.update(principal_kind="user", principal_user_id=PROFILES["it"][0])
    return row


@pytest.mark.parametrize("who", ["ea", "it", "exec", "dev"])
@pytest.mark.parametrize("suffix", ["", "/steps", "/turns"])
def test_org_views_refuse_personal_jobs_even_for_owner_admin(h, who, suffix):
    job = personal_job(h)
    response = h.get(f"/jobs/{job['id']}{suffix}", who=who)
    assert response.status_code == 404
    assert h.get("/jobs", who=who).json()["items"] == []


@pytest.mark.parametrize("action", ["cancel", "retry"])
def test_org_mutations_cannot_requeue_personal_job(h, action):
    job = personal_job(h)
    assert h.post(f"/jobs/{job['id']}/{action}", who="it").status_code == 404
    assert job["status"] == "queued"


@pytest.mark.parametrize("kind", ["screenshot", "llm_request", "llm_response", "page_snapshot"])
def test_org_artifact_url_cannot_disclose_personal_artifact(h, kind):
    job = personal_job(h)
    artifact = h.db.table("browser_agent_artifacts").insert({
        "job_id": job["id"], "kind": kind, "storage_path": "private-user-content",
    }).execute().data[0]
    assert h.get(f"/artifacts/{artifact['id']}/url", who="it").status_code == 404


def personal_help(h):
    return h.db.table("browser_agent_help_requests").insert({
        "principal_kind": "user", "principal_user_id": PROFILES["it"][0],
        "scope_key": "login:u-owner-procore", "reason_code": "mfa", "title": "Private help",
    }).execute().data[0]


@pytest.mark.parametrize("suffix", ["", "/messages"])
def test_org_help_views_exclude_personal_help(h, suffix):
    row = personal_help(h)
    assert h.get(f"/help/{row['id']}{suffix}", who="it").status_code == 404
    assert h.get("/help", who="it").json()["items"] == []


def test_org_help_write_refuses_before_idempotency_lookup(h, monkeypatch):
    row = personal_help(h)
    monkeypatch.setattr(help_flow, "_existing_message", lambda *_: pytest.fail("Private message queried"))
    with pytest.raises(HTTPException) as exc:
        help_flow.post_message(row, h.user("it"), help_flow.HumanMessage(client_msg_id=str(uuid.uuid4())))
    assert exc.value.status_code == 404


def test_personal_help_notices_only_go_to_owner(h, monkeypatch):
    row = personal_help(h)
    notices = []
    monkeypatch.setattr(help_flow.notifications, "notify_user", lambda *a, **kw: notices.append((a, kw)))
    help_flow.notify_helpers(row, "browser_agent.help_needed", "Private page text")
    assert not h.notices
    assert len(notices) == 1 and notices[0][0][0] == PROFILES["it"][0]
    assert "Private page text" not in str(notices)


def test_user_pool_cannot_claim_org_job_or_access_job_protocol(h):
    worker, token, boot, job = h.active_with_job()
    worker["principal_kind"] = "user"
    assert h.claim(token, boot, {"canary": 8}).json() == {"jobs": []}
    response = h.wcall("post", token, boot, job, "/steps", json={"steps": []})
    assert response.status_code == 403
    assert response.json()["detail"] == "user_browser_execution_unavailable"


@pytest.mark.parametrize("path,body", [
    ("/steps", {"steps": []}),
    ("/turns", {"prev_turn_no": 0}),
    ("/help", {"reason_code": "mfa"}),
    ("/identities/procore/session", {"host": "app.procore.com"}),
    ("/identities/procore/login/begin", {"host": "app.procore.com"}),
])
def test_personal_job_refused_before_plaintext_protocol_even_with_org_worker(h, path, body):
    _, token, boot, job = h.active_with_job()
    job["principal_kind"] = "user"
    response = h.wcall("post", token, boot, job, path, json=body)
    assert response.status_code == 403
    assert response.json()["detail"] == "user_browser_execution_unavailable"


def test_user_type_cannot_use_org_enqueue_or_worker_capability(h):
    spec = registry.get_type("user_browser_read")
    assert spec.principal_kind == "user" and spec.identity is None
    assert set(spec.tools) == {"ensure_login", "navigate", "read_page", "find_text", "scroll", "go_back", "wait_for", "request_help", "reply_help", "finish"}
    assert user_browser_read.prepare({}).summary == "user_browser_execution_unavailable"
    with pytest.raises(ValueError, match="assistant approval"):
        queue.enqueue("user_browser_read", "attempt", {})
    worker, token = h.worker()
    _, boot = h.hello(token)
    assert h.claim(token, boot, {"user_browser_read": 8}).json() == {"jobs": []}
    with pytest.raises(HTTPException) as exc:
        admin.patch_worker(worker["id"], {"allowed_types": ["user_browser_read"]}, h.user("it"))
    assert exc.value.status_code == 422


def test_approve_worker_only_assigns_matching_pool(h):
    worker, _ = h.worker(status="pending", allowed_types=())
    admin.approve_worker(worker["id"], h.user("it"))
    assert "user_browser_read" not in worker["allowed_types"]


def test_admin_type_options_expose_the_pool_used_by_worker_validation(h):
    h.db.table("browser_agent_types").insert({
        "key": "user_browser_read", "label": "Personal browser reads", "enabled": False,
    }).execute()
    response = h.get("/admin/state", who="it")
    assert response.status_code == 200
    types = {row["key"]: row for row in response.json()["types"]}
    assert types["user_browser_read"]["principal_kind"] == "user"
    assert types["canary"]["principal_kind"] == "org"
    assert types["rfi_from_email"]["principal_kind"] == "org"


@pytest.mark.parametrize("over", [{"procore_login_email": ""}, {"pm_rfi_sync_enabled": False}])
def test_org_rfi_prepare_keeps_its_own_configuration_gate(h, over):
    job = h.job("rfi_from_email")
    h.settings = agent_settings(**over)
    result = rfi_from_email.prepare(job)
    assert result.action == "finish" and result.error_kind == "unsupported" and not result.notify


def test_user_seed_forbids_model_supplied_credentials():
    with pytest.raises(ValueError):
        protocol.UserBrowserReadInput(service="procore", url="https://app.procore.com/1/project/rfi",
                                      task="Read status", credential_id=str(uuid.uuid4()))


def test_unresolvable_artifact_parent_never_mints_a_url(h):
    artifact = h.db.table("browser_agent_artifacts").insert({
        "job_id": str(uuid.uuid4()), "kind": "screenshot", "storage_path": "orphan-private-object",
    }).execute().data[0]
    assert h.get(f"/artifacts/{artifact['id']}/url", who="it").status_code == 404


def test_private_failure_does_not_send_details_to_organization_roles(h, monkeypatch):
    job = personal_job(h)
    notices = []
    monkeypatch.setattr(queue.notifications, "notify_user", lambda *a, **kw: notices.append((a, kw)))
    queue.notify_job_failed(job, "unsupported", "Private page content")
    assert not h.notices and len(notices) == 1
    assert notices[0][0][0] == PROFILES["it"][0]
    assert "Private page content" not in str(notices)


def test_organization_stop_running_does_not_mutate_or_log_personal_jobs(h):
    personal = personal_job(h)
    personal.update(status="running")
    org = h.job("canary", status="running")
    before_steps = len(h.db.rows("browser_agent_steps", job_id=personal["id"]))
    response = h.post("/admin/stop-running", who="it")
    assert response.status_code == 200 and response.json() == {"cancel_requested": 1}
    assert org["cancel_requested_at"] and not personal.get("cancel_requested_at")
    assert len(h.db.rows("browser_agent_steps", job_id=personal["id"])) == before_steps


@pytest.mark.parametrize("fn", [admin.cancel_job, admin.retry_job])
def test_direct_org_mutation_service_rejects_personal_job(h, fn):
    personal = personal_job(h)
    with pytest.raises(HTTPException) as exc:
        fn(personal, h.user("it"))
    assert exc.value.status_code == 404


@pytest.mark.parametrize("name,status", [
    ("reclaim_expired", "running"), ("park_lapsed_holds", "waiting_help"),
    ("reconcile_applying", "applying"), ("fail_exhausted_retries", "queued"),
    ("expire_stale", "queued"), ("finish_cancels", "running"),
])
def test_org_queue_sweep_does_not_change_personal_jobs(h, name, status):
    from datetime import timedelta
    from copy import deepcopy
    from tests.browser_agents_fake import iso
    row = personal_job(h)
    row.update(status=status, resume_reason="retry", attempts=100, lease_owner=str(uuid.uuid4()),
               lease_expires_at=iso(h.db.now - timedelta(hours=1)),
               expires_at=iso(h.db.now - timedelta(hours=1)), cancel_requested_at=iso(h.db.now))
    before = deepcopy(row)
    assert getattr(queue, name)(h.db.now, queue.load_settings()) == 0
    assert row == before


@pytest.mark.parametrize("name", ["sweep_reminders", "sweep_claim_lapse", "sweep_resolve_finished",
                                  "sweep_expire", "sweep_dead_login_holders"])
def test_org_help_sweep_ignores_personal_help(h, name):
    from datetime import timedelta
    from copy import deepcopy
    from tests.browser_agents_fake import iso
    row = personal_help(h)
    row.update(expires_at=iso(h.db.now - timedelta(days=1)),
               notified_at=iso(h.db.now - timedelta(days=1)),
               claimed_by=PROFILES["it"][0], claimed_at=iso(h.db.now - timedelta(days=1)))
    before = deepcopy(row)
    assert getattr(help_flow, name)(h.db.now, {}) == 0
    assert row == before and not h.notices


def test_org_help_secret_purge_does_not_mutate_personal_ciphertext(h):
    from datetime import timedelta
    from tests.browser_agents_fake import iso
    parent = personal_help(h)
    row = h.db.table("browser_agent_help_messages").insert({
        "help_id": parent["id"], "is_secret": True, "secret_id": str(uuid.uuid4()),
        "secret_ciphertext": "private-envelope", "secret_nonce": "private-nonce", "secret_hmac": "private-hmac",
        "created_at": iso(h.db.now - timedelta(days=10)),
    }).execute().data[0]
    assert help_flow.sweep_secret_purge(h.db.now, {}) == 0
    assert row["secret_ciphertext"] == "private-envelope" and row["secret_hmac"] == "private-hmac"


def test_org_unreported_login_sweep_cannot_lock_shared_identity_for_personal_job(h, monkeypatch):
    from datetime import timedelta
    from tests.browser_agents_fake import iso
    from app.services.browser_agents import identities
    job = personal_job(h)
    attempt = h.db.table("browser_agent_login_attempts").insert({
        "job_id": job["id"], "kind": "browser", "granted": True, "identity": "procore", "epoch": 1,
        "created_at": iso(h.db.now - timedelta(minutes=10)),
    }).execute().data[0]
    monkeypatch.setattr(identities, "get_settings", lambda: h.settings)
    monkeypatch.setattr(identities, "_shared_store", lambda *_: pytest.fail("Shared credential accessed"))
    assert identities.sweep_unreported_logins(h.db.now, {}) == 0
    assert attempt["reported_at"] is None


def test_org_job_cannot_join_personal_help_even_with_a_waiter_row(h):
    job = h.job("canary")
    help_row = personal_help(h)
    h.db.table("browser_agent_help_waiters").insert({"job_id": job["id"], "help_id": help_row["id"]}).execute()
    with pytest.raises(HTTPException) as exc:
        help_flow._waited_help(job["id"], help_row["id"])
    assert exc.value.status_code == 403


def test_org_canary_cannot_target_personal_pool(h):
    worker, _ = h.worker()
    worker["principal_kind"] = "user"
    with pytest.raises(HTTPException) as exc:
        admin.run_canary(worker["id"], h.user("it"))
    assert exc.value.status_code == 409
    assert not h.db.tables.get("browser_agent_jobs")


def test_artifact_without_any_parent_cannot_mint_url(h):
    artifact = h.db.table("browser_agent_artifacts").insert({
        "kind": "screenshot", "storage_path": "unknown-private-object",
    }).execute().data[0]
    assert h.get(f"/artifacts/{artifact['id']}/url", who="it").status_code == 404


@pytest.mark.parametrize('pool',['org','user'])
def test_enrollment_code_authoritatively_assigns_matching_worker_pool(h,pool):
    minted=h.post('/admin/enrollments',who='it',json={'worker_name':'Pool PC','principal_kind':pool})
    assert minted.status_code==200,minted.text
    code=minted.json()['code']
    assert minted.json()['principal_kind']==pool
    assert f'-Pool {pool}' in minted.json()['install_command']
    path='/agent-worker/v1/enroll'
    body={'code':code,'hostname':'POOL-PC','version':'1.0.0','expected_pool':'user' if pool=='org' else 'org'}
    mismatch=h.client.post(path,json=body)
    assert mismatch.status_code==400
    enrollment=h.db.tables['browser_agent_enrollments'][-1]
    assert not enrollment.get('used_at')
    body['expected_pool']=pool
    enrolled=h.client.post(path,json=body)
    assert enrolled.status_code==200,enrolled.text
    row=h.db.one('browser_agent_workers',id=enrolled.json()['worker_id'])
    assert row['principal_kind']==pool==enrolled.json()['principal_kind']
    assert row['status']=='pending'
    assert admin.workers_view([row])[0]['principal_kind']==pool
    admin.approve_worker(row['id'],h.user('it'))
    assert ('user_browser_read' in row['allowed_types']) is (pool=='user')
    assert ('rfi_from_email' in row['allowed_types']) is (pool=='org')


def test_user_claim_trims_private_authority_and_skips_org_ledgers(h,monkeypatch):
    from app.core import agent_auth
    from app.services import assistant_browser_vault
    row=personal_job(h)
    row.update(agent_type='user_browser_read',lease_epoch=1,lease_expires_at='2026-10-27T15:00:00Z',attempts=1,
        input={'service':'procore','url':'https://app.procore.com/123/project/rfi/list','objective':'Read RFIs',
               'credential_id':str(uuid.uuid4()),'credential_version':7,'client_request_id':'private','max_cost_usd':'2'},
        checkpoint={'private':'should not reach worker'},turn_count=3)
    monkeypatch.setattr(user_browser_read,'enabled',lambda:True)
    # Browser v2 (WI-6): the personal claim gate is the v2 type's enabled().
    from app.services.browser_agents.types import user_browser
    monkeypatch.setattr(user_browser,'enabled',lambda:True)
    monkeypatch.setattr(assistant_browser_vault,'job_credential',lambda job:{})
    monkeypatch.setattr(queue,'_held_leases',lambda wid:0)
    monkeypatch.setattr(agent_auth,'rpc',lambda *a:[row])
    monkeypatch.setattr(queue,'unconsumed_help',lambda *a:pytest.fail('org help queried'))
    monkeypatch.setattr(queue,'issued_turn_counts',lambda *a:pytest.fail('org turns queried'))
    claimed=queue.claim(str(uuid.uuid4()),str(uuid.uuid4()),{'user_browser_read':1,'rfi_from_email':1},
                        max_slots=1,principal_kind='user')
    assert len(claimed)==1
    assert set(claimed[0].input)=={'service','url','objective'}
    assert claimed[0].checkpoint=={} and claimed[0].turn_count==3 and claimed[0].unconsumed_help==[]

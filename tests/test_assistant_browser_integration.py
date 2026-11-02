"""Browser tools through real approval, principal, shim and HTTP contracts.

Only persistence admission/worker execution are faked. In particular, trusted
user message provenance must survive the fresh run used for approval dispatch.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI

from app.assistant import access, approvals, approvals_job, jobs, principal, registry, repo, shim, spend
from app.assistant.executor import Executor
from app.core import deps, features, supabase_client
from app.core.roles import INTERNAL_ROLES, WRITER_ROLES, Role
from app.routers import assistant_browser
from app.services import assistant_browser as service
from app.services import notifications
from tests.assistant_fakes import (
    CONV, MSG_ASSISTANT, RUN, SECRET, USER, FakeDB, conversation, delegation,
    message, pending_answer, profile, settings_row,
)

URL = "https://app.procore.com/123/project/rfi/list"
TEXT = f"Read the RFI status at {URL}"
ARGS = {"service": "procore", "url": URL, "objective": "Read the RFI status", "response_format": "concise", "limit": 5}


@pytest.fixture
def env(monkeypatch):
    settings = SimpleNamespace(assistant_enabled=True, assistant_browser_enabled=True, assistant_token_secret=SECRET,
                               assistant_daily_cap_usd=5, assistant_global_daily_cap_usd=50)
    for module in (access, principal, service, assistant_browser, spend):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "EXECUTION_READY", True)
    db = FakeDB(profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[delegation()], assistant_approvals=[], assistant_tool_calls=[],
        assistant_tool_results=[], assistant_messages=[message(content=TEXT), pending_answer(status="waiting_approval")],
        assistant_conversations=[conversation()])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: db)
    monkeypatch.setattr(deps, "get_supabase", lambda: db)
    monkeypatch.setattr(features, "enabled_map", lambda role=None: {"bidding": True, "pm": True, "certified_payroll": True})
    monkeypatch.setattr(jobs, "_renew_lease", lambda: True)
    monkeypatch.setattr(jobs, "_lease_owner", lambda: "worker:test")
    monkeypatch.setattr(notifications, "notify_user", lambda *a, **k: None)
    credential = {"id": str(uuid4()), "version": 1, "status": "active", "label": "Personal Procore",
                  "origins": ["https://app.procore.com", "https://login.procore.com"]}
    monkeypatch.setattr(repo, "browser_credential", lambda user, site: credential)
    platform = {"principal_kind": "user", "enabled": True, "provider": "openai", "model": "test-model",
                "model_probe_ok_at": "2026-10-27T00:00:00Z", "price_in_per_mtok": 1, "price_out_per_mtok": 2}
    monkeypatch.setattr(repo, "browser_platform_config", lambda owner: (platform, {"enabled": True, "per_job_cost_cap_usd": 2}))
    budget = SimpleNamespace(user_remaining=Decimal("5"), global_remaining=Decimal("50"))
    monkeypatch.setattr(spend, "state", lambda owner: budget)
    app = FastAPI()
    app.include_router(assistant_browser.router)
    monkeypatch.setattr(shim, "served_app", lambda: app)
    state = SimpleNamespace(db=db, credential=credential, platform=platform, budget=budget, admitted={}, calls=[])
    def admit(user_id, name, params):
        assert name == "assistant_browser_task_enqueue" and user_id == USER
        card = repo.get_approval(user_id, params["p_approval_id"])
        assert card["status"] == "executing"
        assert card["payload_sha256"] == params["p_payload_sha256"]
        assert card["payload"]["request"]["json"] == params["p_input"]
        assert repo.mail_user_texts(user_id, params["p_run_id"]) == [TEXT]
        assert repo.conversation_for_run(user_id, params["p_run_id"]) == CONV
        state.calls.append(params)
        row = state.admitted.setdefault(card["id"], {"id": str(uuid4()), "input": params["p_input"], "status": "queued"})
        return {"ok": True, "job": row}
    monkeypatch.setattr(repo, "browser_rpc", admit)
    return state


def propose(env, args=None):
    spec = registry.get("user_browser_task")
    executor = Executor(user_id=USER, user_label="Tom Moore", run_id=RUN, specs={spec.name: spec},
        lease_owner="worker:turn", conversation_id=CONV, message_id=MSG_ASSISTANT, role=Role.EXECUTIVE)
    return approvals.propose(executor, spec.name, args or ARGS, user_texts=(TEXT,), conversation_id=CONV, message_id=MSG_ASSISTANT)


def approve(env):
    result = propose(env)
    assert result.ok, result.model_output
    card = result.approval
    decision = repo.decide_approval(USER, card["id"], decision="approve", payload_sha256=card["payload_sha256"],
                                   precondition=card["precondition"], session_aal="aal2")
    assert decision["ok"]
    return decision["row"]


def test_browser_approval_preserves_typed_destination_through_real_shim(env):
    card = approve(env)
    assert not env.calls
    assert repo.mail_user_texts(USER, RUN) == [TEXT]
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    result = repo.get_approval(USER, card["id"])
    assert result["status"] == "executed", result.get("result")
    assert len(env.calls) == 1
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    assert len(env.calls) == 1


def test_credential_change_supersedes_without_admission(env):
    card = approve(env)
    env.credential["version"] = 2
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    result = repo.get_approval(USER, card["id"])
    assert result["status"] == "expired" and result.get("superseded_by"), result
    assert not env.calls
    # The superseding preview also needed the original human message.
    newer = [row for row in env.db.tables["assistant_approvals"] if row["id"] != card["id"]]
    assert newer and newer[-1]["payload"]["request"]["json"]["credential_version"] == 2


def test_page_url_that_only_appears_in_model_args_is_refused(env):
    result = propose(env, {**ARGS, "url": "https://app.procore.com/456/project/rfi/list"})
    assert not result.ok and not env.calls


def test_mailbox_review_never_reads_browser_credentials(env, monkeypatch):
    env.db.tables["assistant_conversations"][0]["mode"] = "mailbox_review"
    monkeypatch.setattr(repo, "browser_credential", lambda *a: pytest.fail("must not read credential"))
    assert not propose(env).ok


def test_budget_at_cap_refuses_before_approval(env):
    env.budget.user_remaining = Decimal("2")
    assert not propose(env).ok


@pytest.mark.parametrize("change", [{"principal_kind": "org"}, {"enabled": False}, {"provider": "other"},
                                    {"price_in_per_mtok": None}, {"model_probe_ok_at": None}])
def test_platform_must_be_user_scoped_enabled_and_priced(env, change):
    env.platform.update(change)
    assert not propose(env).ok


@pytest.mark.parametrize("role", list(INTERNAL_ROLES))
def test_tool_roles_and_review_mode(role):
    user = SimpleNamespace(id=USER, role=role, is_active=True)
    tools = {spec.name for spec in registry.toolset_for(user, {"bidding": True, "pm": True, "certified_payroll": True, "ai_estimating": True})}
    assert ("user_browser_task" in tools) == (role in WRITER_ROLES)
    assert ("user_browser_status" in tools) == (role in WRITER_ROLES)
    review = {spec.name for spec in registry.toolset_for(user, {"bidding": True, "pm": True, "certified_payroll": True}, mode="mailbox_review")}
    assert "user_browser_task" not in review and "user_browser_status" not in review


def test_approval_card_freezes_site_account_version_destination_and_cost(env):
    # Establish trusted context using the actual proposal before inspecting its frozen card.
    result = propose(env)
    assert result.ok, result.model_output
    card = result.approval
    payload = card["payload"]["request"]["json"]
    assert payload["credential_version"] == 1 and payload["url"] == URL and payload["max_cost_usd"] == "2"
    assert payload["authority"]["model"] == "test-model"
    assert payload["authority"]["credential_version"] == 1
    assert payload["authority"]["conversation_id"] == CONV
    assert payload["client_request_id"]
    account = next(row for row in card["preview"]["rows"] if row["field"] == "credential")
    assert account["after"] == "Personal Procore (version 1)"
    assert card["payload"]["request"]["headers"]["X-Assistant-Idempotency-Key"] == card["id"]


def test_model_change_supersedes_without_admission(env):
    card = approve(env)
    env.platform["model"] = "changed-model"
    approvals_job.run_approval_execute({"user_id": USER, "approval_id": card["id"]})
    result = repo.get_approval(USER, card["id"])
    assert result["status"] == "expired" and result.get("superseded_by"), result
    assert not env.calls


def test_enqueue_rechecks_frozen_authority_even_after_preview(env):
    card = approve(env)
    env.db.tables["assistant_approvals"][0]["status"] = "executing"
    env.platform["model"] = "changed-model"
    with pytest.raises(ValueError, match="authority changed"):
        service.enqueue(USER, RUN, card["id"], card["payload"]["request"]["json"])
    assert not env.calls


def test_admitted_job_replay_does_not_recheck_budget_or_reserve(env, monkeypatch):
    card = approve(env)
    env.db.tables["assistant_approvals"][0]["status"] = "executing"
    job = {"id": str(uuid4()), "status": "queued", "input": card["payload"]["request"]["json"]}
    monkeypatch.setattr(repo, "browser_job_by_approval", lambda owner, approval_id: job if owner == USER and approval_id == card["id"] else None)
    monkeypatch.setattr(service, "prepare", lambda *a: pytest.fail("replay must not inspect remaining budget"))
    result = service.enqueue(USER, RUN, card["id"], card["payload"]["request"]["json"])
    assert result["id"] == job["id"] and result["status"] == "queued"
    assert not env.calls
    with pytest.raises(ValueError, match="approval changed"):
        service.enqueue(USER, RUN, card["id"], {**card["payload"]["request"]["json"], "objective": "changed"})


def test_regional_preview_requires_explicit_credential_resave(env, monkeypatch):
    url = ('https://us02.procore.com/webclients/host/companies/562949953430361/'
           'projects/562949955106559/tools/hubs/project-hub/views/overview')
    monkeypatch.setattr(repo, 'mail_user_texts', lambda *a: [f'Read the project overview at {url}'])
    monkeypatch.setattr(repo, 'conversation_for_run', lambda *a: CONV)
    args = {**ARGS, 'url': url}
    with pytest.raises(ValueError, match='sign-in again'):
        service.prepare(USER, RUN, args)
    assert env.credential['version'] == 1
    env.credential.update(version=2, origins=['https://app.procore.com', 'https://login.procore.com',
                                             'https://us02.procore.com'])
    preview = service.prepare(USER, RUN, args)
    assert preview['url'] == url and preview['credential_version'] == 2


def test_project_overview_rfi_request_reaches_approval_and_admission(env, monkeypatch):
    url = ('https://us02.procore.com/webclients/host/companies/562949953430361/'
           'projects/562949955106559/tools/hubs/project-hub/views/overview')
    objective = 'Check recent RFIs for Mater Academy with DC Building Group'
    text = f'{objective} at {url}'
    monkeypatch.setitem(globals(), 'TEXT', text)
    for row in env.db.tables['assistant_messages']:
        if row.get('role') == 'user':
            row['content'] = text
    env.credential.update(version=2, origins=['https://app.procore.com', 'https://login.procore.com',
                                             'https://us02.procore.com'])
    result = propose(env, {**ARGS, 'url': url, 'objective': objective})
    assert result.ok, result.model_output
    card = result.approval
    assert card['payload']['request']['json']['url'] == url
    assert card['payload']['request']['json']['objective'] == objective
    assert not env.calls
    decided = repo.decide_approval(USER, card['id'], decision='approve',
        payload_sha256=card['payload_sha256'], precondition=card['precondition'], session_aal='aal2')
    assert decided['ok']
    approvals_job.run_approval_execute({'user_id': USER, 'approval_id': card['id']})
    assert repo.get_approval(USER, card['id'])['status'] == 'executed'
    assert len(env.calls) == 1
    assert env.calls[0]['p_input']['url'] == url


def test_overview_discovery_and_skill_describe_supported_browser_scope():
    from app.assistant import find, skills_index
    skill = skills_index.get('reading-websites', Role.IT_ADMIN, {})
    assert skill.version == '2'
    assert 'project overviews' in skill.description
    assert 'https://us02.procore.com' in skill.body
    assert '/tools/hubs/project-hub/views/overview' in skill.body
    assert 'only for an exact supported Procore RFI URL' not in skill.body
    specs = list(registry.all_tools().values())
    found = find.resolve('read Procore project overview page and summarize visible content', specs, limit=6)
    assert 'user_browser_task' in {spec.name for spec in found}
    assert 'project overview' in registry.get('user_browser_task').description

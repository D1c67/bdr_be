"""The `user_browser` agent type and its server plumbing (docs/ASSISTANT_BROWSER_V2_PLAN.md
3.4): registration, enabled() on the v2 flag plus the type row, prepare(),
the claim gate and claimed input, and the worker GET /config TypeConfig."""
import uuid
from types import SimpleNamespace

import pytest

from app.routers import agent_worker
from app.services.browser_agents import protocol, queue, registry
from app.services.browser_agents.types import user_browser, user_browser_read


def _settings(monkeypatch, *, v1=True, v2=True):
    config = SimpleNamespace(assistant_browser_enabled=v1, assistant_browser_v2_enabled=v2,
                             browser_agent_lease_seconds=90)
    monkeypatch.setattr(user_browser, "get_settings", lambda: config)
    return config


def _type_row(monkeypatch, **over):
    row = {"key": "user_browser", "enabled": True, "principal_kind": "user"}
    row.update(over)
    from app.assistant import repo
    monkeypatch.setattr(repo, "browser_platform_config_v2", lambda *a: (row, {"enabled": True}))
    return row


def _job(**over):
    adm, conv = str(uuid.uuid4()), str(uuid.uuid4())
    row = {"id": str(uuid.uuid4()), "agent_type": "user_browser", "principal_kind": "user",
           "principal_user_id": str(uuid.uuid4()), "assistant_admission_id": adm,
           "assistant_conversation_id": conv, "user_credential_id": None,
           "input": {"admission_id": adm, "goal": "Download the bid set", "start_url": "https://acc.autodesk.com/x",
                     "start_site_key": "autodesk", "project_id": None,
                     "authority": {"role": "Engineer", "model": "gpt-test", "conversation_id": conv,
                                   "max_cost_usd": 5, "admission_id": adm}}}
    row.update(over)
    return row


def test_registered_as_a_user_type_with_the_v2_tools():
    spec = registry.get_type("user_browser")
    assert spec.principal_kind == "user" and spec.identity is None and spec.prompt_version == "user_browser.v1"
    assert spec.input_model is protocol.UserBrowserInput
    assert set(spec.tools) == set(protocol.TOOLS_BY_TYPE["user_browser"])
    assert {"fill_credential", "user_finish", "user_request_help", "switch_tab"} <= set(spec.tools)
    assert "hold" in spec.help_reasons and "mfa_step_up" in spec.help_reasons
    # The retired read type stays registered for its rows.
    assert registry.get_type("user_browser_read").principal_kind == "user"


def test_enabled_needs_both_flags_and_the_type_row(monkeypatch):
    _settings(monkeypatch)
    row = _type_row(monkeypatch)
    assert user_browser.enabled() is True
    row["enabled"] = False
    assert user_browser.enabled() is False
    row.update(enabled=True, principal_kind="org")
    assert user_browser.enabled() is False
    row["principal_kind"] = "user"
    _settings(monkeypatch, v2=False)
    assert user_browser.enabled() is False
    _settings(monkeypatch, v1=False)
    assert user_browser.enabled() is False


def test_enabled_fails_closed_on_a_read_error(monkeypatch):
    _settings(monkeypatch)
    from app.assistant import repo

    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(repo, "browser_platform_config_v2", boom)
    assert user_browser.enabled() is False


def test_prepare_runs_an_admitted_job(monkeypatch):
    _settings(monkeypatch)
    assert user_browser.prepare(_job()).action == "run"


@pytest.mark.parametrize("change", [
    {"assistant_admission_id": str(uuid.uuid4())},
    {"assistant_conversation_id": str(uuid.uuid4())},
    {"user_credential_id": str(uuid.uuid4())},
    {"principal_kind": "org"},
    {"agent_type": "user_browser_read"},
    {"input": {"goal": "x"}},
])
def test_prepare_refuses_a_job_that_does_not_match_its_admission(monkeypatch, change):
    _settings(monkeypatch)
    out = user_browser.prepare(_job(**change))
    assert out.action == "finish" and out.status == "failed" and out.notify is False
    assert out.summary == "user_browser_authority_unavailable"


def test_prepare_refuses_extra_input_keys(monkeypatch):
    _settings(monkeypatch)
    job = _job()
    job["input"]["credential_id"] = str(uuid.uuid4())
    assert user_browser.prepare(job).status == "failed"


def test_prepare_when_flags_are_off(monkeypatch):
    _settings(monkeypatch, v2=False)
    out = user_browser.prepare(_job())
    assert out.status == "failed" and out.summary == user_browser.EXECUTION_UNAVAILABLE


def test_personal_claim_gate_is_the_v2_type(monkeypatch):
    monkeypatch.setattr(user_browser, "enabled", lambda: False)
    monkeypatch.setattr(user_browser_read, "enabled", lambda: True)
    from app.core import agent_auth
    monkeypatch.setattr(agent_auth, "rpc", lambda *a: pytest.fail("claimed with v2 off"))
    assert queue.claim(str(uuid.uuid4()), str(uuid.uuid4()), {"user_browser": 1}, max_slots=3,
                       principal_kind="user") == []


def test_claimed_v2_job_carries_exactly_its_admitted_input(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setattr(user_browser, "enabled", lambda: True)
    row = _job(lease_epoch=2, lease_expires_at="2026-11-02T18:00:00Z", attempts=1, turn_count=0,
               checkpoint={"private": "x"})
    from app.core import agent_auth
    monkeypatch.setattr(agent_auth, "rpc", lambda *a: [row])
    monkeypatch.setattr(queue, "_held_leases", lambda wid: 0)
    monkeypatch.setattr(queue, "unconsumed_help", lambda *a: pytest.fail("org help queried"))
    monkeypatch.setattr(queue, "issued_turn_counts", lambda *a: pytest.fail("org turns queried"))
    claimed = queue.claim(str(uuid.uuid4()), str(uuid.uuid4()), {"user_browser": 2, "rfi_from_email": 1},
                          max_slots=3, principal_kind="user")
    assert len(claimed) == 1 and claimed[0].agent_type == "user_browser"
    assert protocol.UserBrowserInput.model_validate(claimed[0].input).goal == "Download the bid set"
    assert claimed[0].checkpoint == {} and claimed[0].unconsumed_help == []


def test_config_enables_the_user_type_only_through_the_v2_gate(monkeypatch):
    monkeypatch.setattr(user_browser, "enabled", lambda: True)
    monkeypatch.setattr(user_browser_read, "enabled", lambda: False)
    assert agent_worker._user_execution_enabled() is True
    assert agent_worker._user_type_enabled("user_browser") is True
    assert agent_worker._user_type_enabled("user_browser_read") is False
    monkeypatch.setattr(user_browser, "enabled", lambda: False)
    assert agent_worker._user_type_enabled("user_browser") is False

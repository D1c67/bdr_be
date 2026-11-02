"""The delegation assurance rule (docs/ASSISTANT.md, Permissions item 2) and
migration 0171.

  repo     aal2 always creates the grant; aal1 only while
           settings.mfa_required is False, recorded as created_session_aal
           'aal1'; anything else is refused; the setting is read on every
           call; while mfa_required is True a live aal1 grant is revoked
           (reason mfa_required) and replaced when the person sends at aal2;
  router   a refused send answers 403 mfa_step_up_required when the person
           has a factor and mfa_enrollment_required when they have none
           (the same choice core/deps makes); an aal1 send succeeds with
           mfa_required off;
  0171     widens the check to ('aal1','aal2') inside one do block through
           execute, is idempotent, has no top-level drop, comments the
           column and ends with the PostgREST reload.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.assistant import access, repo
from app.core import config, supabase_client
from app.core.deps import CurrentUser, get_current_user, mfa_challenge_code
from app.core.roles import Role
from app.routers import assistant as router_mod
from app.services import llm_queue

from tests.assistant_fakes import CONV, USER, FakeDB, conversation, delegation, profile, settings_row

MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/0171_assistant_delegation_aal.sql"


@pytest.fixture
def flags(monkeypatch):
    state = {"mfa_required": True}
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(mfa_required=state["mfa_required"]))
    return state


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB(profiles=[profile()], assistant_delegations=[])
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    return fake


# ── repo ─────────────────────────────────────────────────────────────────────


def test_aal1_accepted_and_recorded_when_mfa_not_required(db, flags):
    flags["mfa_required"] = False
    row = repo.ensure_delegation(USER, "aal1")
    rows = db.tables["assistant_delegations"]
    assert len(rows) == 1 and rows[0]["created_session_aal"] == "aal1"
    assert row["created_session_aal"] == "aal1"


def test_aal1_refused_when_mfa_required(db, flags):
    flags["mfa_required"] = True
    with pytest.raises(PermissionError):
        repo.ensure_delegation(USER, "aal1")
    assert db.tables["assistant_delegations"] == []


@pytest.mark.parametrize("mfa_required", [True, False])
def test_aal2_always_accepted_and_recorded(db, flags, mfa_required):
    flags["mfa_required"] = mfa_required
    repo.ensure_delegation(USER, "aal2")
    assert [r["created_session_aal"] for r in db.tables["assistant_delegations"]] == ["aal2"]


@pytest.mark.parametrize("aal", ["", "aal0", "aal3", "AAL1", None])
def test_other_levels_refused_even_with_mfa_off(db, flags, aal):
    flags["mfa_required"] = False
    with pytest.raises(PermissionError):
        repo.ensure_delegation(USER, aal)
    assert db.tables["assistant_delegations"] == []


def test_rule_is_read_per_call_not_cached(db, flags):
    flags["mfa_required"] = True
    assert repo.delegation_session_aal_allowed("aal1") is False
    flags["mfa_required"] = False
    assert repo.delegation_session_aal_allowed("aal1") is True
    flags["mfa_required"] = True
    assert repo.delegation_session_aal_allowed("aal1") is False


def test_live_aal1_grant_is_reused_while_mfa_off(db, flags):
    flags["mfa_required"] = False
    db.tables["assistant_delegations"] = [delegation(created_session_aal="aal1")]
    assert repo.ensure_delegation(USER, "aal1")["id"] == "del-1"
    assert repo.ensure_delegation(USER, "aal2")["id"] == "del-1"
    assert len(db.tables["assistant_delegations"]) == 1


def test_live_aal1_grant_is_revoked_and_replaced_once_mfa_required(db, flags):
    flags["mfa_required"] = True
    db.tables["assistant_delegations"] = [delegation(created_session_aal="aal1")]
    row = repo.ensure_delegation(USER, "aal2")
    rows = db.tables["assistant_delegations"]
    old = [r for r in rows if r["id"] == "del-1"][0]
    assert old["revoked_at"] is not None and old["revoked_reason"] == "mfa_required"
    live = [r for r in rows if r.get("revoked_at") is None]
    assert len(live) == 1 and live[0]["created_session_aal"] == "aal2"
    assert row["created_session_aal"] == "aal2"


# ── router ───────────────────────────────────────────────────────────────────


@pytest.fixture
def api(monkeypatch, flags):
    fake = FakeDB(
        profiles=[profile()], assistant_settings=[settings_row()], assistant_profiles=[],
        assistant_delegations=[], assistant_conversations=[conversation()], assistant_messages=[],
        llm_call_log=[],
    )
    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)
    monkeypatch.setattr(access, "env_enabled", lambda: True)
    monkeypatch.setattr(router_mod, "_model_available", lambda: True)
    monkeypatch.setattr(router_mod, "_queue_available", lambda: True)
    monkeypatch.setattr(llm_queue, "enqueue", lambda job_type, **kw: {"id": "job-1"})
    who = {"aal": "aal1", "mfa_enrolled": False}
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=USER, email="tom@g3.test", role=Role.EXECUTIVE, is_active=True,
        aal=who["aal"], mfa_enrolled=who["mfa_enrolled"],
    )
    return SimpleNamespace(client=TestClient(app), db=fake, who=who, flags=flags)


def _send(api):
    return api.client.post(f"/assistant/conversations/{CONV}/messages", json={"content": "what is due"})


def test_router_aal1_send_succeeds_when_mfa_not_required(api):
    api.flags["mfa_required"] = False
    res = _send(api)
    assert res.status_code == 202, res.text
    assert [r["created_session_aal"] for r in api.db.tables["assistant_delegations"]] == ["aal1"]


def test_router_aal1_unenrolled_gets_enrollment_code(api):
    api.flags["mfa_required"] = True
    api.who["mfa_enrolled"] = False
    res = _send(api)
    assert res.status_code == 403 and res.json()["detail"] == "mfa_enrollment_required"
    assert api.db.tables["assistant_delegations"] == []
    assert api.db.tables["assistant_messages"] == []


def test_router_aal1_enrolled_gets_step_up_code(api):
    api.flags["mfa_required"] = True
    api.who["mfa_enrolled"] = True
    res = _send(api)
    assert res.status_code == 403 and res.json()["detail"] == "mfa_step_up_required"


def test_challenge_code_matches_deps():
    assert mfa_challenge_code(True) == "mfa_step_up_required"
    assert mfa_challenge_code(False) == "mfa_enrollment_required"


# ── migration 0171 ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _outside_do_blocks(text: str) -> str:
    return re.sub(r"do \$\$.*?end \$\$;", "", _code(text), flags=re.S)


def test_exactly_one_0171_file():
    assert len(list(MIGRATION.parent.glob("0171_*.sql"))) == 1


def test_0171_header_reload_and_no_dash_characters(sql):
    assert sql.startswith("-- 0171: ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql


def test_0171_has_no_top_level_drop(sql):
    assert not re.search(r"\bdrop\b", _outside_do_blocks(sql), re.I)
    assert len(re.findall(r"do \$\$", _code(sql))) == 1


def test_0171_swaps_the_check_through_execute_idempotently(sql):
    code = _code(sql)
    block = re.search(r"do \$\$(.*?)end \$\$;", code, re.S).group(1)
    assert "'public.assistant_delegations'::regclass" in block
    # Skips when a created_session_aal check already admits aal1.
    assert re.search(r"if exists \(.*?created_session_aal.*?'''aal1'''.*?\) then.*?return;", block, re.S)
    assert re.search(r"execute format\('alter table public\.assistant_delegations drop constraint %I'", block)
    assert "check (created_session_aal in (''aal1'', ''aal2''))" in block
    assert "add constraint assistant_delegations_created_session_aal_check" in block


def test_0171_comments_the_column_with_the_rule(sql):
    m = re.search(r"comment on column public\.assistant_delegations\.created_session_aal is\s+'([^']+)';", sql)
    assert m and "mfa_required" in m.group(1) and "aal1" in m.group(1)

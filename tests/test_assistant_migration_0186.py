"""Phase 6 DDL guard rails. Real transactional coverage lives in the SQL check.

These prevent accidentally reopening runtime while future schema scaffolding
exists; they do not claim a live database or Windows execution was exercised.
"""
from pathlib import Path
import re

import pytest

SQL = (Path(__file__).parents[1] / "supabase/migrations/0186_assistant_browser_foundation.sql").read_text()
CHECK = (Path(__file__).parents[1] / "scripts/check_assistant_browser_foundation.sql").read_text()


def function(name: str) -> str:
    match = re.search(r"create or replace function public\." + name + r"\(.*?\$\$;", SQL, re.S)
    assert match, name
    return match.group(0)


def test_hard_runtime_gate_does_not_depend_on_settings():
    ready = function("assistant_browser_runtime_ready")
    assert "select false" in ready
    assert "settings" not in ready
    for name in ("assistant_browser_task_enqueue", "assistant_browser_session_put"):
        body = function(name)
        assert body.index("assistant_browser_runtime_ready()") < body.index("assistant_browser_owner_eligible")
        assert "runtime_unavailable" in body
    claim = function("claim_browser_agent_jobs")
    assert claim.index("assistant_browser_runtime_ready()") < claim.index("update browser_agent_jobs")
    fence = function("browser_agent_fence")
    assert "v_job.principal_kind='user'" in fence
    assert "browser_runtime_unavailable" in fence
    assert "tg_op='INSERT'" in function("assistant_browser_job_guard")


def test_principal_constraints_fail_closed_on_null_version():
    assert "user_credential_version is not null and user_credential_version>0" in SQL
    assert "principal_kind='org' and principal_user_id is null" in SQL
    guard = function("assistant_browser_job_guard")
    assert "browser_principal_immutable" in guard
    for field in ("principal_user_id", "user_credential_id", "user_credential_version", "assistant_approval_id"):
        assert f"new.{field} is distinct from old.{field}" in guard
    assert "browser_pool_mismatch" in guard
    assert "browser_pool_immutable" in function("assistant_browser_pool_guard")
    claim = function("claim_browser_agent_jobs")
    assert "enabled and principal_kind=v_worker.principal_kind" in claim
    assert "status = 'queued' and principal_kind=v_worker.principal_kind" in claim
    assert "where id = p_worker for share" in claim
    assert "cancel_requested_at is null" in claim


def test_credential_cas_origin_and_destruction_order():
    put = function("assistant_browser_credential_put")
    assert "c.version<>p_expected_version" in put
    assert "c.id is not null and v_id<>c.id" in put
    assert "array['https://app.procore.com','https://login.procore.com']" in put
    assert "p_record->'origins' is distinct from to_jsonb(v_origins)" in put
    assert put.index("assistant_browser_cancel_credentials") < put.index("insert into public.assistant_credentials")
    revoke = function("assistant_browser_credential_revoke")
    assert revoke.index("assistant_browser_cancel_credentials") < revoke.index("wrapped_dek=null")
    assert "version=version+1" in revoke
    assert "delete from public.assistant_credentials" not in revoke
    assert "before update or delete on public.profiles" not in SQL
    assert "before update on public.profiles" in SQL
    assert "for each row when (new.role is distinct from old.role" in SQL
    cancel = function("assistant_browser_cancel_credentials")
    assert "principal_kind='user' and principal_user_id=p_user_id" in cancel
    assert "actual_usd=j.cost_usd" in cancel
    assert "status='active'" in cancel
    profile = function("assistant_browser_profile_changed")
    for field in ("role", "is_active", "email", "mfa_enrolled"):
        assert f"new.{field} is distinct from old.{field}" in profile


@pytest.mark.parametrize("table", ["assistant_credentials", "assistant_browser_sessions", "assistant_browser_access"])
def test_private_tables_and_service_only_rpcs(table):
    assert f"alter table public.{table} enable row level security" in SQL
    assert f"alter table public.{table} force row level security" in SQL
    assert "from public,anon,authenticated" in SQL
    assert "create policy" not in SQL.lower()
    assert "security definer" not in SQL.lower()
    assert "revoke all on function %s from public,anon,authenticated" in SQL
    assert "grant execute on function %s to service_role" in SQL
    assert "assistant_browser_access_no_truncate" in SQL


def test_cost_admission_is_frozen_and_transactional():
    enqueue = function("assistant_browser_task_enqueue")
    assert "a.payload#>'{request,json}' is distinct from p_input" in enqueue
    assert "a.payload_sha256 is distinct from p_payload_sha256" in enqueue
    assert "a.decided_by is distinct from p_user_id" in enqueue
    assert "credential_changed" in enqueue
    assert enqueue.index("pg_advisory_xact_lock") < enqueue.index("concurrency_cap")
    assert enqueue.index("assistant_spend_reserve") < enqueue.index("insert into public.browser_agent_jobs")
    assert "now()+interval '8 hours'" in enqueue
    assert "'browser_task'" in function("assistant_spend_state")
    assert "'browser_task'" in function("assistant_spend_reserve")


def test_sql_check_always_rolls_back_and_covers_real_lifecycle():
    assert CHECK.strip().endswith("rollback;")
    assert "begin;" in CHECK
    assert "fixture-ciphertext" in CHECK
    for text in ("version_conflict", "MFA profile change", "deactivation", "Accountant", "claim_browser_agent_jobs",
                 "browser_pool_immutable", "has_function_privilege", "has_table_privilege", "GoTrue", "name-only update",
                 "organization claim", "organization lease fence", "stale organization epoch"):
        assert text in CHECK

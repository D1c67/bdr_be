"""Migration 0157 (docs/browser_agents/SPEC.md section 2 + SPEC_OVERRIDES.md):
the SQL text carries every table, the override adjustments (no attestations,
no escalation flag, caps on settings, worker-run login), deny-by-default RLS,
append-only triggers, service-role-only functions with a pinned search_path,
and the house style (header, reload, no dashes). The vocabularies are pinned
against the protocol so the DB and the wire contract cannot drift."""

import re
from pathlib import Path

import pytest

from app.services.browser_agents import protocol as p

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase/migrations"
FILE = "0157_browser_agents.sql"

TABLES = (
    "browser_agent_settings", "browser_agent_runtime_state", "browser_agent_types",
    "browser_agent_settings_changes", "browser_agent_secrets", "browser_agent_workers",
    "browser_agent_enrollments", "browser_agent_enroll_attempts", "browser_agent_worker_releases",
    "browser_agent_jobs", "browser_agent_turns", "browser_agent_artifacts", "browser_agent_steps",
    "browser_agent_login_attempts", "browser_agent_help_requests", "browser_agent_help_waiters",
    "browser_agent_help_messages", "browser_agent_help_reads", "browser_agent_rfi_contributions",
)

FUNCTIONS = (
    "browser_agent_settings_bump_version", "browser_agent_config_touch", "browser_agent_steps_assign_seq",
    "browser_agent_steps_guard_update", "browser_agent_refuse_change", "browser_agent_help_messages_guard_update",
    "procore_reserve_login", "procore_record_login_failure", "browser_agent_fence", "browser_agent_log_step",
    "claim_browser_agent_jobs", "browser_agent_hello", "browser_agent_heartbeat", "browser_agent_append_steps",
    "browser_agent_checkpoint", "browser_agent_open_help", "browser_agent_park", "browser_agent_submit_rfi_capture",
    "browser_agent_finish", "browser_agent_requeue", "browser_agent_update_settings", "browser_agent_spend_today",
    "browser_agent_reset_spend_window",
)


@pytest.fixture(scope="module")
def sql() -> str:
    return (MIGRATIONS / FILE).read_text(encoding="utf-8")


def _table_body(sql: str, table: str) -> str:
    m = re.search(rf"create table if not exists {table} \((.+?)\n\);", sql, re.S)
    assert m, f"create table if not exists {table}"
    return m.group(1)


def _check_values(body: str, column: str) -> set[str]:
    m = re.search(rf"\b{column}\b[^\n]*?check \({column} in\s*\(([^)]+)\)\)", body, re.S)
    assert m, f"{column} check"
    return {v.strip().strip("'") for v in m.group(1).replace("\n", " ").split(",") if v.strip()}


def test_exactly_one_0157_file():
    assert len(list(MIGRATIONS.glob("0157_*.sql"))) == 1


def test_header_notify_and_no_dash_characters(sql):
    assert sql.startswith("-- 0157: ")
    assert "Apply after 0156." in sql.splitlines()[0]
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql


@pytest.mark.parametrize("table", TABLES)
def test_every_table_exists_with_forced_rls(sql, table):
    _table_body(sql, table)
    assert f"alter table {table} enable row level security;" in sql
    assert f"alter table {table} force row level security;" in sql
    # No policies: the service-role backend is the only client.
    assert "create policy" not in sql


def _code(sql: str) -> str:
    """The SQL with -- comments removed."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_overrides_applied(sql):
    code = _code(sql)
    # O5: no attestation table or code.
    assert "attestation" not in code
    # O1: no escalation flag; an enabled type needs a priced model.
    assert "llm_escalation_enabled" not in code
    assert "browser_agent_types_enabled_needs_model" in code
    # O2: no broker login, no fallback gate or release cap.
    assert "broker" not in code
    assert "browser_login_fallback_enabled" not in code
    assert "browser_login_releases_per_day" not in code
    assert "p_bypass_after_broker" not in code
    # O4: $25/day and $2/job on settings; the reset is audited.
    body = _table_body(sql, "browser_agent_settings")
    assert "daily_cost_cap_usd            numeric(10,2) not null default 25" in body
    assert "per_job_cost_cap_usd          numeric(8,2) not null default 2" in body
    assert "max_cost_usd" not in _table_body(sql, "browser_agent_types")
    assert "'browser_agent.spend_reset'" in sql
    assert "'cost_window_reset'" in sql
    # O5: worker IP allowlist optional.
    assert "allowed_ips            cidr[] not null default '{}'" in sql


def test_vocabularies_match_the_protocol(sql):
    jobs = _table_body(sql, "browser_agent_jobs")
    assert _check_values(jobs, "status") == set(p.JobStatus.__args__)
    assert _check_values(jobs, "outcome") == set(p.JobOutcome.__args__)
    assert _check_values(jobs, "resume_reason") == set(p.ResumeReason.__args__)
    art = _table_body(sql, "browser_agent_artifacts")
    assert _check_values(art, "kind") == set(p.ArtifactKind.__args__)
    steps = _table_body(sql, "browser_agent_steps")
    assert _check_values(steps, "actor") == set(p.StepActor.__args__)
    assert p.STEP_KIND_RE.pattern.replace("\\", "") in steps.replace("\\", "")
    help_ = _table_body(sql, "browser_agent_help_requests")
    assert _check_values(help_, "reason_code") == set(p.HelpReasonCode.__args__)
    assert _check_values(help_, "status") == set(p.HelpStatus.__args__)
    assert _check_values(help_, "resolution") == set(p.HelpResolution.__args__)
    msgs = _table_body(sql, "browser_agent_help_messages")
    assert _check_values(msgs, "author_kind") == set(p.HelpAuthorKind.__args__)
    assert _check_values(msgs, "action") == set(p.HelpMessageAction.__args__)
    types = _table_body(sql, "browser_agent_types")
    assert _check_values(types, "provider") == set(p.PROVIDERS)
    login = _table_body(sql, "browser_agent_login_attempts")
    assert set(p.LoginOutcome.__args__) <= _check_values(login, "outcome")
    assert set(p.LoginRefusal.__args__) <= _check_values(login, "refused_reason")


def test_requeue_reasons_match_the_protocol(sql):
    m = re.search(r"if p_reason not in \(([^)]+)\) then", sql)
    assert m
    assert {v.strip().strip("'") for v in m.group(1).split(",")} == set(p.RequeueReason.__args__)


def test_rpc_error_messages_are_protocol_codes(sql):
    raised = set(re.findall(r"raise exception '([a-z_]+)'", sql))
    assert raised <= {c.value for c in p.ErrorCode}
    assert {"stale_boot", "lease_lost", "job_not_running", "revoked", "unauthorized"} <= raised


def test_unique_indexes(sql):
    for idx in (
        "browser_agent_jobs_dedupe_uidx on browser_agent_jobs (agent_type, dedupe_key)",
        "browser_agent_steps_seq_uidx on browser_agent_steps (job_id, seq)",
        "browser_agent_steps_idem_uidx on browser_agent_steps (job_id, idem_key) where idem_key is not null",
        "browser_agent_help_open_uidx on browser_agent_help_requests (scope_key)",
        "pm_rfi_sync_runs_agent_uidx",
        "browser_agent_artifacts_job_sha_uidx",
        "browser_agent_rfi_contrib_uidx",
    ):
        assert f"create unique index if not exists {idx}" in sql


def test_append_only_triggers(sql):
    for table in ("browser_agent_steps", "browser_agent_settings_changes", "browser_agent_help_messages"):
        assert f"before delete on {table}" in sql
        assert f"before truncate on {table}" in sql
        assert f"before update on {table}" in sql
    assert "'{\"redacted\":true}'::jsonb" in sql


def test_pm_rfi_sync_runs_trigger_widened(sql):
    assert "alter table pm_rfi_sync_runs drop constraint if exists pm_rfi_sync_runs_trigger_check;" in sql
    assert "check (trigger in ('daily', 'manual', 'email', 'agent'))" in sql


def test_existing_tables_additive_only(sql):
    assert not re.search(r"\bdrop table\b", sql)
    assert not re.search(r"\bdrop column\b", sql)
    assert "delete from" not in sql
    for col in ("import_method", "imported_by_agent_job_id", "external_supplement"):
        assert f"alter table rfis add column if not exists {col}" in sql
    assert "alter table rfp_harvest_sessions add column if not exists last_login_source text;" in sql
    assert "alter table pm_rfi_email_scan_state add column if not exists subject_checkpoint timestamptz;" in sql


def test_immutable_tables_have_no_profile_fks(sql):
    for table in ("browser_agent_steps", "browser_agent_settings_changes", "browser_agent_help_messages"):
        assert "references profiles" not in _table_body(sql, table)


@pytest.mark.parametrize("name", FUNCTIONS)
def test_functions_invoker_pinned_and_service_role_only(sql, name):
    m = re.search(rf"create or replace function public\.{name}\((.*?)\$\$", sql, re.S)
    assert m, name
    head = m.group(1)
    assert "security invoker" in head
    assert "set search_path = pg_catalog, public" in head
    assert "security definer" not in head
    assert re.search(rf"revoke all on function public\.{name}\([^)]*\) from public;", sql)
    assert re.search(rf"revoke all on function public\.{name}\([^)]*\) from anon, authenticated;", sql)
    assert re.search(rf"grant execute on function public\.{name}\([^)]*\) to service_role;", sql)


def test_no_pgcrypto(sql):
    assert "pgcrypto" not in sql and "digest(" not in sql and "crypt(" not in sql

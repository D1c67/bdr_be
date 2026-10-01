"""Migration 0136 (docs/RFP_BUILDINGCONNECTED.md section 5): the SQL text
carries the BuildingConnected columns, the widened checks, the new tables
with deny-by-default RLS, and the house style (header, reload, no dashes)."""

import re
from pathlib import Path

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase/migrations"
FILE = "0136_rfp_buildingconnected.sql"

HEADER = (
    "-- 0136 - RFP Ingestion: BuildingConnected Bid Board (docs/RFP_BUILDINGCONNECTED.md section 5, "
    "scratchpad BUILD_CONTRACT.md section 2). Apply after 0135. DEV ONLY until the release steps in "
    "section 9 run."
)

# The literal vocabulary 0136 writes. Pinned here on purpose (not read from
# rfp_portal_ingest.ALL_STATUSES) so this test stays frozen history.
STATUSES_0136 = {
    "match", "harvest", "split", "create", "review_match", "exists", "done",
    "created", "ignored", "historical", "expired", "withdrawn",
}
GC_PLANS_0136 = {"resolved", "likely", "created", "none", "provisional"}

NEW_TABLES = ("rfp_portal_state", "rfp_oauth_connections", "rfp_oauth_states", "gc_external_aliases")
TABLES_WITH_UPDATED_AT = ("rfp_portal_state", "rfp_oauth_connections", "gc_external_aliases")


@pytest.fixture(scope="module")
def sql() -> str:
    return (MIGRATIONS / FILE).read_text(encoding="utf-8")


def _values(body: str) -> set[str]:
    return {v.strip().strip("'") for v in body.replace("\n", ",").split(",") if v.strip()}


def _create_table_body(sql: str, table: str) -> str:
    m = re.search(rf"create table if not exists {table} \((.+?)\n\);", sql, re.S)
    assert m, f"create table if not exists {table}"
    return m.group(1)


def test_exactly_one_0136_file():
    assert len(list(MIGRATIONS.glob("0136_*.sql"))) == 1
    assert (MIGRATIONS / FILE).is_file()


def test_header_notify_and_no_dash_characters(sql):
    assert sql.splitlines()[0] == HEADER
    assert sql.startswith("-- 0136 - ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert "—" not in sql and "–" not in sql   # em dash, en dash


def test_additive_only(sql):
    assert "delete from" not in sql
    assert not re.search(r"\bupdate \w+\s+set\b", sql)
    assert "drop table" not in sql
    assert "drop column" not in sql
    # No new queue job type (contract D2): the job type check is untouched.
    assert "alter table llm_jobs" not in sql
    assert "rfp_bc_full_sync" not in sql


def test_status_check_is_the_twelve_values(sql):
    check = re.search(
        r"add constraint rfp_portal_invitations_status_check\s+check \(status in \(([^)]+)\)\)", sql, re.S
    )
    assert check
    assert _values(check.group(1)) == STATUSES_0136
    # The 0132 DO-block pattern: looked up by column, so a re-run is safe.
    assert "c.conrelid = 'public.rfp_portal_invitations'::regclass" in sql
    assert "a.attname = 'status'" in sql


def test_gc_plan_check_is_the_five_values(sql):
    check = re.search(
        r"add constraint rfp_created_projects_gc_plan_check\s+check \(gc_plan in \(([^)]+)\)\)", sql, re.S
    )
    assert check
    assert _values(check.group(1)) == GC_PLANS_0136
    assert "c.conrelid = 'public.rfp_created_projects'::regclass" in sql
    assert "a.attname = 'gc_plan'" in sql
    # Two DO blocks, each closed.
    assert sql.count("do $$") == 2 and sql.count("end\n$$;") == 2
    assert sql.count("from pg_constraint c") == 2


def test_named_checks_on_the_invitation_and_run_columns(sql):
    for table, name, body in (
        ("rfp_portal_invitations", "rfp_portal_invitations_gc_kind_check",
         "check (gc_kind in ('alias', 'contact', 'domain', 'provisional', 'none'))"),
        ("rfp_portal_invitations", "rfp_portal_invitations_ignore_source_check",
         "check (ignore_source in ('user', 'system'))"),
        ("rfp_portal_runs", "rfp_portal_runs_kind_check", "check (kind in ('incremental', 'full'))"),
    ):
        assert f"alter table {table} drop constraint if exists {name};" in sql, name
        assert re.search(rf"add constraint {name}\s+{re.escape(body)}", sql), name
    assert "constraint rfp_oauth_connections_status_check check (status in ('connected', 'disconnected'))" in sql


def test_invitation_columns(sql):
    alter = re.search(r"alter table rfp_portal_invitations\n(.+?);", sql, re.S)
    assert alter
    cols = dict(re.findall(r"add column if not exists (\w+)\s+([^,\n]+)", alter.group(1)))
    expected = {
        "external_id": "text", "external_url": "text", "payload": "jsonb", "payload_hash": "text",
        "bc_updated_at": "timestamptz", "invited_at": "timestamptz", "job_walk_at": "timestamptz",
        "expected_start_at": "timestamptz", "expected_finish_at": "timestamptz", "rfis_due_at": "timestamptz",
        "address": "text", "trade_name": "text", "submission_state": "text", "workflow_bucket": "text",
        "source": "text", "request_type": "text", "is_archived": "boolean", "is_nda_required": "boolean",
        "is_sealed": "boolean", "gc_external_id": "text", "gc_external_name": "text",
        "gc_id": "uuid references general_contractors(id) on delete set null",
        "gc_kind": "text", "gc_candidates": "jsonb", "gc_confirmed_at": "timestamptz",
        "gc_confirmed_by": "uuid references profiles(id) on delete set null",
        "lead": "jsonb", "ignore_source": "text",
        "project_gc_id": "uuid references project_gcs(id) on delete set null",
        "sibling_of": "uuid references rfp_portal_invitations(id) on delete set null",
    }
    assert {k: v.strip() for k, v in cols.items()} == expected
    # All nullable, so NGEM rows are untouched; created_project_id is 0130's.
    assert "not null" not in alter.group(1)
    assert "created_project_id" not in alter.group(1)


def test_invitation_indexes(sql):
    for needle in (
        "create index if not exists rfp_portal_invitations_external_idx\n"
        "  on rfp_portal_invitations (portal, external_id) where external_id is not null;",
        "create index if not exists rfp_portal_invitations_status_close_idx\n"
        "  on rfp_portal_invitations (portal, status, close_at);",
        "create index if not exists rfp_portal_invitations_gc_external_idx\n"
        "  on rfp_portal_invitations (gc_external_id) where gc_external_id is not null;",
        "create index if not exists rfp_portal_invitations_project_gc_idx\n"
        "  on rfp_portal_invitations (project_gc_id) where project_gc_id is not null;",
    ):
        assert needle in sql, needle


def test_runs_columns_and_the_slot_index(sql):
    alter = re.search(r"alter table rfp_portal_runs\n(.+?);", sql, re.S)
    assert alter
    cols = {k: v.strip() for k, v in re.findall(r"add column if not exists (\w+)\s+([^,\n]+)", alter.group(1))}
    assert cols == {
        "kind": "text not null default 'incremental'",
        "high_water_before": "timestamptz",
        "rows_pulled": "int not null default 0",
        "rows_pipeline": "int not null default 0",
        "rows_expired": "int not null default 0",
        "rows_withdrawn": "int not null default 0",
    }
    drop_at = sql.index("drop index if exists rfp_portal_runs_slot_uidx;")
    create = re.search(
        r"create unique index rfp_portal_runs_slot_uidx\s+on rfp_portal_runs \(portal, kind, scheduled_for\) "
        r"where scheduled_for is not null;",
        sql,
    )
    assert create and create.start() > drop_at
    # The kind column exists before the index that names it.
    assert sql.index("add column if not exists kind") < create.start()
    # The active-run index is not touched.
    assert "rfp_portal_runs_active_uidx" not in sql


@pytest.mark.parametrize("table", NEW_TABLES)
def test_new_tables_are_idempotent_and_deny_by_default(sql, table):
    assert f"create table if not exists {table} (" in sql
    assert f"alter table {table} enable row level security;" in sql
    assert f"alter table {table} force row level security;" in sql


def test_updated_at_triggers_only_where_updated_at_exists(sql):
    for table in NEW_TABLES:
        body = _create_table_body(sql, table)
        has_col = re.search(r"^\s*updated_at\s+timestamptz not null default now\(\)", body, re.M) is not None
        has_trigger = (
            f"drop trigger if exists {table}_updated_at on {table};" in sql
            and f"create trigger {table}_updated_at before update on {table}\n"
                "  for each row execute function set_updated_at();" in sql
        )
        assert has_col == (table in TABLES_WITH_UPDATED_AT), table
        assert has_trigger == has_col, table
    assert "rfp_oauth_states_updated_at" not in sql


def test_new_table_columns(sql):
    state = _create_table_body(sql, "rfp_portal_state")
    for needle in ("portal              text primary key", "high_water_at       timestamptz",
                   "last_full_sync_at   timestamptz", "last_incremental_at timestamptz"):
        assert needle in state, needle

    conn = _create_table_body(sql, "rfp_oauth_connections")
    names = set(re.findall(r"^\s{2}(\w+)\s", conn, re.M)) - {"constraint"}
    assert names == {
        "provider", "status", "access_token", "refresh_token", "expires_at", "scope", "connected_by",
        "connected_at", "external_user_id", "external_user_name", "external_user_email",
        "external_company_id", "view_all", "last_refresh_at", "last_used_at", "last_error",
        "refresh_lock_until", "refresh_lock_owner", "disconnected_at", "created_at", "updated_at",
    }
    assert "provider            text primary key" in conn
    assert "status              text not null default 'disconnected'" in conn
    assert "connected_by        uuid references profiles(id) on delete set null" in conn
    assert "Tokens are plain text; RLS forced, service role only" in sql

    states = _create_table_body(sql, "rfp_oauth_states")
    names = set(re.findall(r"^\s{2}(\w+)\s", states, re.M))
    assert names == {"state", "provider", "actor_id", "created_at", "expires_at"}
    assert "state      text primary key" in states
    assert "actor_id   uuid references profiles(id) on delete cascade" in states
    assert "expires_at timestamptz not null" in states

    aliases = _create_table_body(sql, "gc_external_aliases")
    for needle in (
        "id            uuid primary key default gen_random_uuid()",
        "source        text not null",
        "external_id   text not null",
        "external_name text not null",
        "gc_id         uuid not null references general_contractors(id) on delete cascade",
        "confirmed_by  uuid references profiles(id) on delete set null",
        "confirmed_at  timestamptz not null default now()",
    ):
        assert needle in aliases, needle
    assert "create unique index if not exists gc_external_aliases_uidx\n  on gc_external_aliases (source, external_id);" in sql
    assert "create index if not exists gc_external_aliases_gc_idx\n  on gc_external_aliases (gc_id);" in sql


def test_every_projects_column(sql):
    alter = re.search(r"alter table projects\n(.+?);", sql, re.S)
    assert alter
    cols = {k: v.strip() for k, v in re.findall(r"add column if not exists (\w+)\s+([^,\n]+)", alter.group(1))}
    assert cols == {
        "job_walk_at": "timestamptz",
        "project_information": "text",
        "trade_instructions": "text",
        "gc_confirm_pending": "boolean not null default false",
        "files_needed_source": "text",
        "files_needed_url": "text",
        "files_needed_set_at": "timestamptz",
        "files_needed_cleared_at": "timestamptz",
        "files_needed_cleared_by": "uuid references profiles(id) on delete set null",
    }
    # Contract D14: neither of these columns exists.
    assert "invitation_method" not in alter.group(1) and "is_budgetary" not in alter.group(1)
    assert re.search(
        r"create index if not exists projects_files_needed_idx\s+on projects \(files_needed_set_at\)\s+"
        r"where files_needed_set_at is not null and files_needed_cleared_at is null;",
        sql,
    )

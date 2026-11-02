"""Migration 0174 (docs/ASSISTANT_PHASE2_PLAN.md, section 2): staged file
page counts and purge marks, the split task columns, the per-file assistant
scope on bid_split_files, the spend hold on llm_call_log, and the
assistant_spend_reservations ledger with its two service-role RPCs.

Static checks on the SQL text. PHASE2_TABLES is imported by other tests (every
new assistant table must have forced RLS and revoked privileges)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0174_assistant_phase2.sql"
SQL_PATH = MIGRATIONS / FILE

PHASE2_TABLES = ("assistant_spend_reservations",)

FUNCTIONS = (
    "assistant_spend_state(uuid, numeric, numeric)",
    "assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid)",
)


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _fn(sql: str, name: str) -> str:
    m = re.search(rf"create or replace function public\.{name}\(.+?\n\$\$;", sql, re.S)
    assert m, name
    return m.group(0)


def _statements(code: str) -> list[str]:
    return [s.strip() for s in re.split(r";[ \t]*(?:\n|\Z)", code) if s.strip()]


def test_exactly_one_0174_file():
    assert len(list(MIGRATIONS.glob("0174_*.sql"))) == 1


def test_header_notify_last_and_no_dash_characters(sql):
    assert sql.startswith("-- 0174: ")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    assert "\\u2014" not in sql
    assert _statements(_code(sql))[-1] == "notify pgrst, 'reload schema'"


def test_no_drop_statement_anywhere(sql):
    code = _code(sql)
    assert not re.search(r"^\s*drop\s", code, re.M | re.I)
    assert not re.search(r"\bdrop\s+(table|function|trigger|index|constraint|policy|column)\b", code, re.I)


@pytest.mark.parametrize("table,column", [
    ("assistant_files", "page_count int"),
    ("assistant_files", "purged_at timestamptz"),
    ("assistant_files", "consumed_task_id uuid"),
    ("assistant_tasks", "approval_id uuid"),
    ("assistant_tasks", "run_id uuid"),
    ("assistant_tasks", "spend_reservation_id uuid"),
    ("assistant_tasks", "result_message_id uuid"),
    ("bid_split_jobs", "spend_reservation_id uuid"),
    ("bid_split_files", "assistant_run_id uuid"),
    ("bid_split_files", "spend_reservation_id uuid"),
    ("llm_call_log", "spend_reservation_id uuid"),
])
def test_added_columns_are_idempotent(sql, table, column):
    assert f"alter table public.{table} add column if not exists {column};" in _code(sql)


def test_page_count_check_is_guarded_not_dropped(sql):
    code = _code(sql)
    assert "if not exists (select 1 from pg_constraint where conname = 'assistant_files_page_count_chk') then" in code
    assert "check (page_count is null or page_count > 0) not valid;" in code
    assert "validate constraint assistant_files_page_count_chk;" in code


def test_partial_indexes(sql):
    code = _code(sql)
    assert re.search(
        r"create unique index if not exists assistant_tasks_approval_uq\s+"
        r"on public\.assistant_tasks \(approval_id\) where approval_id is not null;", code)
    assert re.search(
        r"create index if not exists assistant_files_object_live_idx\s+"
        r"on public\.assistant_files \(expires_at\) where purged_at is null;", code)
    assert re.search(
        r"create unique index if not exists assistant_spend_reservations_task_uq\s+"
        r"on public\.assistant_spend_reservations \(task_id\) where task_id is not null and kind = 'split_task';",
        code)


def test_every_created_table_is_listed(sql):
    created = set(re.findall(r"create table if not exists public\.([a-z_]+)", sql))
    assert created == set(PHASE2_TABLES)


@pytest.mark.parametrize("table", PHASE2_TABLES)
def test_new_table_has_forced_rls_and_revokes(sql, table):
    assert f"alter table public.{table} enable row level security;" in sql
    assert f"alter table public.{table} force row level security;" in sql
    assert f"revoke all on table public.{table} from anon, authenticated;" in sql
    assert "create policy" not in _code(sql)


def test_reservations_table_shape(sql):
    m = re.search(r"create table if not exists public\.assistant_spend_reservations \((.+?)\n\);", sql, re.S)
    assert m
    body = m.group(1)
    assert "kind         text not null check (kind in ('turn_call', 'split_task'))" in body
    assert "status       text not null default 'active' check (status in ('active', 'settled', 'released'))" in body
    assert "amount_usd   numeric(12,6) not null check (amount_usd >= 0)" in body
    assert "expires_at   timestamptz not null" in body
    # The ledger outlives the person: no FK on user_id.
    assert "references" not in body


@pytest.mark.parametrize("sig", FUNCTIONS)
def test_rpcs_are_service_role_only(sql, sig):
    assert f"revoke all on function public.{sig} from public;" in sql
    assert f"revoke all on function public.{sig} from anon, authenticated;" in sql
    assert f"grant execute on function public.{sig} to service_role;" in sql
    name = sig.split("(", 1)[0]
    grants = re.findall(rf"grant execute on function public\.{name}\([^)]*\) to ([a-z_, ]+);", sql)
    assert grants == ["service_role"]


def test_every_function_is_invoker_and_pinned(sql):
    names = re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
    assert set(names) == {"assistant_spend_state", "assistant_spend_reserve"}
    for name in names:
        fn = _fn(sql, name)
        assert "security invoker" in fn and "set search_path = pg_catalog, public" in fn, name
        assert "security definer" not in fn, name


def test_spend_state_counts_pacific_day_attributed_calls_and_live_holds(sql):
    fn = _fn(sql, "assistant_spend_state")
    assert "now() at time zone 'America/Los_Angeles'" in fn
    assert "l.feature in ('assistant', 'assistant_extract') or l.assistant_run_id is not null" in fn
    assert "where r.status = 'active' and r.expires_at > now()" in fn
    assert "l.spend_reservation_id = r.id" in fn
    for key in ("user_cap", "user_spent", "user_held", "global_cap", "global_spent", "global_held"):
        assert f"'{key}', v_{key}" in fn, key


def test_reserve_locks_and_admits_with_ge(sql):
    fn = _fn(sql, "assistant_spend_reserve")
    assert "perform pg_advisory_xact_lock(hashtext('assistant_spend'));" in fn
    assert fn.index("pg_advisory_xact_lock") < fn.index("public.assistant_spend_state(")
    assert fn.count(">= (v_state->>'user_cap')::numeric") == 1
    assert fn.count(">= (v_state->>'global_cap')::numeric") == 1
    assert "'reason', 'user_cap'" in fn and "'reason', 'global_cap'" in fn
    assert "p_ttl_seconds < 30 or p_ttl_seconds > 43200" in fn
    assert "errcode = 'check_violation'" in fn


def test_job_type_guard_reads_constraintdef_and_never_relists(sql):
    code = _code(sql)
    assert "position('assistant_continue' in v_def) = 0" in code
    assert not re.search(r"add\s+constraint\s+llm_jobs_job_type_check", code, re.I)


_INDEX_RE = re.compile(r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?([a-z0-9_]+)", re.I)


def test_no_0174_index_name_exists_in_another_migration(sql):
    ours = set(_INDEX_RE.findall(_code(sql)))
    assert ours == {
        "assistant_files_object_live_idx", "assistant_tasks_approval_uq", "assistant_tasks_split_job_idx",
        "llm_call_log_spend_reservation_idx", "assistant_spend_reservations_live_idx",
        "assistant_spend_reservations_task_uq",
    }
    others: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("0*.sql")):
        if path.name == FILE:
            continue
        for name in _INDEX_RE.findall(_code(path.read_text(encoding="utf-8"))):
            others.setdefault(name.lower(), path.name)
    clashes = {n: others[n] for n in ours if n.lower() in others}
    assert not clashes, f"0174 index names already used: {clashes}"


def test_every_statement_parses():
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(SQL_PATH.read_text(encoding="utf-8"))

"""Migration 0170 (docs/ASSISTANT.md, Data model, "0170, assistant core") and
the llm_jobs_job_type_check re-list guard (scripts/check_llm_jobs_constraint_relists.py).

Static checks on the SQL text: every assistant table has forced RLS, no
policies and its privileges revoked from anon and authenticated; the
append-only, immutability, fence and cap triggers are bound; the phase ledger
RPC is service-role only with a pinned search_path; the job type widening is
the defensive pg_get_constraintdef form, never a literal full list; and the
house style holds (header, reload, no dash characters)."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0170_assistant_core.sql"

TABLES = (
    "assistant_settings", "assistant_profiles", "assistant_delegations", "assistant_conversations",
    "assistant_messages", "assistant_tool_results", "assistant_tool_calls", "assistant_approvals",
    "assistant_memories", "assistant_memory_events", "assistant_memory_revisions", "assistant_schedules",
    "assistant_schedule_runs", "assistant_tasks", "assistant_files", "assistant_events",
    "assistant_subscriptions", "assistant_inbox", "assistant_conversation_views", "assistant_eval_lock",
)

LEDGERS = ("assistant_tool_calls", "assistant_approvals", "assistant_schedule_runs", "assistant_conversation_views")
APPEND_ONLY = ("assistant_tool_calls", "assistant_memory_events", "assistant_conversation_views")


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_llm_jobs_constraint_relists", ROOT / "scripts/check_llm_jobs_constraint_relists.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sql() -> str:
    return (MIGRATIONS / FILE).read_text(encoding="utf-8")


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _table_body(sql: str, table: str) -> str:
    m = re.search(rf"create table if not exists public\.{table} \((.+?)\n\);", sql, re.S)
    assert m, f"create table if not exists public.{table}"
    return m.group(1)


def test_exactly_one_0170_file():
    assert len(list(MIGRATIONS.glob("0170_*.sql"))) == 1


def test_header_notify_and_no_dash_characters(sql):
    assert sql.startswith("-- 0170: ")
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql


def test_every_create_table_is_listed(sql):
    created = set(re.findall(r"create table if not exists public\.([a-z_]+)", sql))
    assert created == set(TABLES)


@pytest.mark.parametrize("table", TABLES)
def test_every_table_has_forced_rls_revokes_and_uuid_or_singleton_pk(sql, table):
    body = _table_body(sql, table)
    assert f"alter table public.{table} enable row level security;" in sql
    assert f"alter table public.{table} force row level security;" in sql
    assert f"revoke all on table public.{table} from anon, authenticated;" in sql
    first = body.strip().splitlines()[0]
    assert ("uuid primary key" in first) or ("smallint primary key default 1 check (id = 1)" in first)


def test_no_policies(sql):
    assert "create policy" not in _code(sql).lower()


@pytest.mark.parametrize("table", LEDGERS)
def test_ledgers_store_person_as_plain_uuid_plus_label(sql, table):
    body = _table_body(sql, table)
    assert "references public.profiles" not in body
    assert re.search(r"\b(user_label|viewer_label)\s+text not null", body)


def test_private_rows_cascade_with_the_person(sql):
    for table in ("assistant_profiles", "assistant_conversations", "assistant_messages", "assistant_memories",
                  "assistant_schedules", "assistant_tasks", "assistant_files", "assistant_inbox"):
        assert "references public.profiles(id) on delete cascade" in _table_body(sql, table), table


def test_project_id_never_has_an_fk(sql):
    assert not re.search(r"project_id\s+uuid[^,\n]*references", _code(sql))


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_append_only_triggers(sql, table):
    for op, level in (("update", "row"), ("delete", "row"), ("truncate", "statement")):
        assert re.search(
            rf"create (or replace )?trigger {table}_no_{op} before {op} on public\.{table}\s+for each {level} "
            rf"execute function public\.assistant_refuse_change\(",
            sql,
        ), f"{table} {op}"


def test_refuse_change_is_our_own_and_never_the_browser_agent_one(sql):
    assert "create or replace function public.assistant_refuse_change()" in sql
    assert "browser_agent_refuse_change" not in _code(sql)


def test_approval_immutable_trigger(sql):
    assert re.search(r"create (or replace )?trigger assistant_approval_immutable before update on public\.assistant_approvals", sql)
    fn = re.search(r"function public\.assistant_approval_immutable\(\).+?\$\$;", sql, re.S).group(0)
    for col in ("payload", "payload_sha256", "preview", "precondition", "user_id", "action_kind"):
        assert f"new.{col} is distinct from old.{col}" in fn


def test_partial_unique_index_on_active_messages(sql):
    assert re.search(
        r"create unique index if not exists \w+\s+on public\.assistant_messages \(conversation_id\) "
        r"where status in \('pending', 'running'\);",
        sql,
    )


def test_tool_calls_unique_key_phase_and_rpc(sql):
    assert "unique (idempotency_key, phase)" in _table_body(sql, "assistant_tool_calls")
    m = re.search(r"create or replace function public\.assistant_tool_call_begin\((.+?)\)\nreturns jsonb(.+?)\$\$;",
                  sql, re.S)
    assert m
    params, rest = m.group(1), m.group(2)
    assert params.strip().startswith("p_idempotency_key text,\n  p_lease_owner text")
    assert "security invoker" in rest and "set search_path = pg_catalog, public" in rest
    assert "on conflict (idempotency_key, phase) do nothing" in rest
    sig = ("assistant_tool_call_begin(text, text, uuid, text, uuid, integer, text, text, uuid, uuid, "
           "text, text, text, text, jsonb, text, jsonb, uuid, uuid, boolean)")
    assert f"revoke all on function public.{sig} from public;" in sql
    assert f"revoke all on function public.{sig} from anon, authenticated;" in sql
    assert f"grant execute on function public.{sig} to service_role;" in sql


def test_every_function_is_invoker_pinned_and_revoked(sql):
    names = re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
    assert len(names) == 9
    for name in names:
        fn = re.search(rf"function public\.{name}\(.+?\$\$;", sql, re.S).group(0)
        assert "security invoker" in fn and "set search_path = pg_catalog, public" in fn, name
        assert re.search(rf"revoke all on function public\.{name}\([^)]*\) from public;", sql), name
        assert re.search(rf"revoke all on function public\.{name}\([^)]*\) from anon, authenticated;", sql), name


def test_caps(sql):
    mem = re.search(r"function public\.assistant_memories_caps\(\).+?\$\$;", sql, re.S).group(0)
    assert "> 300" in mem and "> 40" in mem and "> 6000" in mem
    sch = re.search(r"function public\.assistant_schedules_caps\(\).+?\$\$;", sql, re.S).group(0)
    assert "> 10" in sch and "'HOURLY', 'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'" in sch
    assert re.search(r"create (or replace )?trigger assistant_memories_caps before insert or update", sql)
    assert re.search(r"create (or replace )?trigger assistant_schedules_caps before insert or update", sql)


def test_job_type_widening_is_defensive(sql):
    code = _code(sql)
    assert "pg_get_constraintdef" in code
    for t in ("assistant_turn", "assistant_continue", "assistant_approval_execute"):
        assert f"'{t}'" in code
    # Never a literal full list: that is exactly what the re-list guard forbids.
    assert not _load_checker().relists_full_list(sql)


def test_existing_table_alters(sql):
    code = _code(sql)
    for stmt in (
        "alter table public.llm_call_log add column if not exists user_id uuid;",
        "alter table public.llm_call_log add column if not exists assistant_run_id uuid;",
        "alter table public.projects add column if not exists assistant_idempotency_key text;",
        "alter table public.bid_split_jobs add column if not exists assistant_task_id uuid;",
        "alter table public.todos add column if not exists due_at timestamptz;",
        "alter table public.todos add column if not exists project_id uuid;",
        "alter table public.todos add column if not exists source text not null default 'user';",
        "alter table public.todos add column if not exists assistant_task_id uuid;",
    ):
        assert stmt in code
    assert re.search(r"create unique index if not exists \w+\s+on public\.projects \(assistant_idempotency_key\) "
                     r"where assistant_idempotency_key is not null;", code)


# --- the inbox schema: the phase 7 contract (docs/ASSISTANT_PHASE3_PLAN.md 3.12) ---------
# Phase 3 adds no inbox DDL or code; these pin the shape the phase 7 router
# will code against.

INBOX_COLUMNS = {
    "assistant_events": (
        "id               uuid primary key default gen_random_uuid()",
        "event_type       text not null",
        "dedupe_key       text not null unique",
        "project_id       uuid",
        "subject_user_id  uuid",
        "payload          jsonb not null default '{}'::jsonb",
        "occurred_at      timestamptz not null default now()",
        "processed_at     timestamptz",
        "created_at       timestamptz not null default now()",
    ),
    "assistant_subscriptions": (
        "id          uuid primary key default gen_random_uuid()",
        "role        text",
        "user_id     uuid references public.profiles(id) on delete cascade",
        "event_type  text not null",
        "enabled     boolean not null default true",
        "severity    text not null default 'normal' check (severity in ('low', 'normal', 'high', 'urgent'))",
        "created_at  timestamptz not null default now()",
        "updated_at  timestamptz not null default now()",
    ),
    "assistant_inbox": (
        "id            uuid primary key default gen_random_uuid()",
        "user_id       uuid not null references public.profiles(id) on delete cascade",
        "dedupe_key    text not null",
        "event_id      uuid references public.assistant_events(id) on delete set null",
        "severity      text not null default 'normal' check (severity in ('low', 'normal', 'high', 'urgent'))",
        "title         text not null",
        "why           text",
        "payload       jsonb not null default '{}'::jsonb",
        "project_id    uuid",
        "delivered_at  timestamptz",
        "read_at       timestamptz",
        "feedback      text check (feedback in ('useful', 'not_useful', 'mute'))",
        "feedback_at   timestamptz",
        "created_at    timestamptz not null default now()",
    ),
}


@pytest.mark.parametrize("table", sorted(INBOX_COLUMNS))
def test_inbox_table_columns(sql, table):
    body = _code(_table_body(sql, table))
    lines = [line.strip().rstrip(",") for line in body.splitlines() if line.strip()]
    columns = [line for line in lines if not line.startswith("constraint ")]
    assert [re.sub(r"\s+", " ", c) for c in columns] == [re.sub(r"\s+", " ", c) for c in INBOX_COLUMNS[table]]


def test_inbox_dedupe_keys(sql):
    assert "dedupe_key       text not null unique" in _table_body(sql, "assistant_events")
    assert "constraint assistant_inbox_dedupe_uq unique (user_id, dedupe_key)" in _table_body(sql, "assistant_inbox")
    assert re.search(r"create index if not exists assistant_events_unprocessed_idx\s+"
                     r"on public\.assistant_events \(occurred_at\) where processed_at is null;", sql)
    assert re.search(r"create index if not exists assistant_inbox_undelivered_idx\s+"
                     r"on public\.assistant_inbox \(user_id, created_at\) where delivered_at is null;", sql)


def test_subscriptions_are_either_role_or_user(sql):
    body = _table_body(sql, "assistant_subscriptions")
    assert "constraint assistant_subscriptions_target_chk check ((role is null) <> (user_id is null))" in body
    assert re.search(r"create unique index if not exists assistant_subscriptions_role_uq\s+"
                     r"on public\.assistant_subscriptions \(role, event_type\) where user_id is null;", sql)
    assert re.search(r"create unique index if not exists assistant_subscriptions_user_uq\s+"
                     r"on public\.assistant_subscriptions \(user_id, event_type\) where role is null;", sql)


@pytest.mark.parametrize("table", sorted(INBOX_COLUMNS))
def test_inbox_tables_have_forced_rls(sql, table):
    assert f"alter table public.{table} enable row level security;" in sql
    assert f"alter table public.{table} force row level security;" in sql
    assert f"revoke all on table public.{table} from anon, authenticated;" in sql


# --- the re-list guard ---------------------------------------------------------------

FULL = ("alter table llm_jobs add constraint llm_jobs_job_type_check\n"
        "  check (job_type in ('boq_extraction', 'general_material', 'x'));\n")


def _write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body, encoding="utf-8")


def test_guard_allows_history_and_the_known_baseline(tmp_path):
    chk = _load_checker()
    _write(tmp_path, "0111_a.sql", FULL)
    _write(tmp_path, "0152_b.sql", FULL)
    _write(tmp_path, "0154_c.sql", FULL)
    # 0165 (AI Estimating) accepted as baseline on 2026-10-22; the release
    # note orders it before 0170 everywhere.
    _write(tmp_path, "0165_c2.sql", FULL)
    _write(tmp_path, "0170_d.sql", "do $$ begin perform pg_get_constraintdef(1); end $$;\n")
    report = chk.scan(tmp_path)
    assert report.ok
    assert report.historical == ["0111_a.sql"]
    assert report.baseline == ["0152_b.sql", "0154_c.sql", "0165_c2.sql"]
    assert chk.main(["x", str(tmp_path)]) == 0


def test_guard_fails_on_a_third_relist(tmp_path):
    chk = _load_checker()
    _write(tmp_path, "0152_b.sql", FULL)
    _write(tmp_path, "0154_c.sql", FULL)
    _write(tmp_path, "0171_e.sql", FULL)
    report = chk.scan(tmp_path)
    assert report.violations == ["0171_e.sql"]
    assert chk.main(["x", str(tmp_path)]) == 1


def test_guard_ignores_comments_and_partial_lists(tmp_path):
    chk = _load_checker()
    _write(tmp_path, "0180_f.sql", "-- " + FULL.replace("\n", " ") + "\n")
    _write(tmp_path, "0181_g.sql", "alter table llm_jobs add constraint llm_jobs_job_type_check "
                                   "check (job_type in ('only_new'));\n")
    assert chk.scan(tmp_path).ok


def test_guard_sees_the_repo_baseline():
    report = _load_checker().scan(MIGRATIONS)
    # 0152 and 0154 re-list for good; 0165 is accepted baseline while it
    # re-lists, and drops out once the AI Estimating build rewrites it to
    # widen the live list (as it did on 2026-10-22), so it is optional here.
    assert report.baseline[:2] == ["0152_plans_reader.sql", "0154_pm_email_classification.sql"]
    assert set(report.baseline) <= {
        "0152_plans_reader.sql", "0154_pm_email_classification.sql",
        "0165_plans_reader_autonomy_core.sql",
    }
    assert report.ok
    assert FILE not in report.violations

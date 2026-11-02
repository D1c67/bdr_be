"""Migration 0176 (docs/ASSISTANT_PHASE3_PLAN.md, section 2): schedule
anchor, occurrence queue and bookkeeping columns, the run ledger guards, the
claim and delivery claim RPCs, the strengthened schedule caps, the memory
RPCs (apply, near duplicate, touch, transcript redaction), the pin guard and
the content-only updated_at trigger, one pending revision per memory, the
housekeeping ledger effect, the holiday seed and the assistant_sweeps claim
table.

Static checks on the SQL text. PHASE3_TABLES is exported for other tests
(every new assistant table must have forced RLS and revoked privileges)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0176_assistant_phase3.sql"
SQL_PATH = MIGRATIONS / FILE

PHASE3_TABLES = ("assistant_sweeps",)

FUNCTIONS = (
    "assistant_schedules_caps()",
    "assistant_schedule_run_forward()",
    "claim_due_assistant_schedules(text, int, int)",
    "assistant_schedule_run_channel(uuid, uuid, text, text, jsonb)",
    "assistant_memories_pin_guard()",
    "assistant_memory_similar(uuid, text, real, int)",
    "assistant_redact_memory_items(jsonb, text)",
    "assistant_memory_apply(uuid, text, uuid, timestamptz, jsonb, text, uuid, uuid, text)",
    "assistant_memory_touch(uuid, uuid[])",
)

EFFECTS_0170 = ("read", "sensitive_read", "write_internal", "external_send", "expensive")
EVENTS_0170 = ("created", "updated", "approved", "rejected", "pinned", "unpinned", "archived", "restored",
               "revision_proposed", "revision_approved", "revision_rejected")


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


def _branch(fn: str, op: str) -> str:
    """The body of one `elsif p_op = '<op>' then` branch of the apply RPC."""
    start = fn.index(f"elsif p_op = '{op}' then")
    end = fn.find("\n    elsif p_op", start + 1)
    if end < 0:
        end = fn.index("\n  end if;\n\n  insert into public.assistant_memory_events", start)
    return fn[start:end]


def test_exactly_one_0176_file():
    assert len(list(MIGRATIONS.glob("0176_*.sql"))) == 1


def test_header_notify_last_and_no_dash_characters(sql):
    assert sql.startswith("-- 0176: ")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    assert "\\u2014" not in sql
    assert _statements(_code(sql))[-1] == "notify pgrst, 'reload schema'"


def test_no_drop_statement_anywhere(sql):
    code = _code(sql)
    assert not re.search(r"^\s*drop\s", code, re.M | re.I)
    assert not re.search(r"\bdrop\s+(table|function|trigger|index|policy|column)\b", code, re.I)
    # The two constraint swaps run through execute inside a do block.
    drops = [line.strip() for line in code.splitlines() if re.search(r"\bdrop\s+constraint\b", line, re.I)]
    assert len(drops) == 2
    assert all(line.startswith("execute format('alter table public.") for line in drops)


@pytest.mark.parametrize("table,column", [
    ("assistant_schedules", "dtstart timestamptz"),
    ("assistant_schedules", "after_next_run_at timestamptz"),
    ("assistant_schedules", "approval_id uuid"),
    ("assistant_schedules", "paused_at timestamptz"),
    ("assistant_schedules", "ended_reason text"),
    ("assistant_schedules", "last_delivered_at timestamptz"),
    ("assistant_schedules", "last_seen_at timestamptz"),
    ("assistant_schedule_runs", "kind text not null default 'scheduled'"),
    ("assistant_schedule_runs", "stats jsonb"),
    ("assistant_memories", "idempotency_key text"),
    ("assistant_memory_revisions", "base_updated_at timestamptz"),
    ("assistant_memory_revisions", "idempotency_key text"),
    ("assistant_tool_results", "memory_ids uuid[]"),
    ("assistant_messages", "notes_tainted boolean not null default false"),      # D1 (B8)
])
def test_added_columns_are_idempotent(sql, table, column):
    assert f"alter table public.{table} add column if not exists {column};" in _code(sql)


@pytest.mark.parametrize("table,name,check", [
    ("assistant_schedules", "assistant_schedules_timezone_chk", "check (timezone = 'America/Los_Angeles')"),
    ("assistant_schedules", "assistant_schedules_params_chk", "check (jsonb_typeof(params) = 'object')"),
    ("assistant_schedules", "assistant_schedules_active_next_chk",
     "check (status <> 'active' or (next_run_at is not null and dtstart is not null))"),
    ("assistant_schedules", "assistant_schedules_ended_chk", "check ((status = 'ended') = (ended_at is not null))"),
    ("assistant_schedule_runs", "assistant_schedule_runs_kind_chk", "check (kind in ('scheduled', 'run_now'))"),
])
def test_new_checks_are_guarded_and_validated(sql, table, name, check):
    code = _code(sql)
    assert f"if not exists (select 1 from pg_constraint where conname = '{name}') then" in code
    assert re.search(rf"add constraint {name}\s+{re.escape(check)} not valid;", code), name
    assert f"alter table public.{table} validate constraint {name};" in code


def test_partial_and_unique_indexes(sql):
    code = _code(sql)
    for pattern in (
        r"create unique index if not exists assistant_schedules_approval_uq\s+"
        r"on public\.assistant_schedules \(approval_id\) where approval_id is not null;",
        r"create index if not exists assistant_schedules_refill_idx\s+"
        r"on public\.assistant_schedules \(next_run_at\) where status = 'active' and after_next_run_at is null;",
        r"create unique index if not exists assistant_memories_idem_uq\s+"
        r"on public\.assistant_memories \(user_id, idempotency_key\) where idempotency_key is not null;",
        r"create unique index if not exists assistant_memory_revisions_one_pending_uq\s+"
        r"on public\.assistant_memory_revisions \(memory_id\) where decided_at is null;",
        r"create index if not exists assistant_tool_results_memory_ids_idx\s+"
        r"on public\.assistant_tool_results using gin \(memory_ids\) where memory_ids is not null;",
        r"create index if not exists assistant_schedule_runs_manual_idx\s+"
        r"on public\.assistant_schedule_runs \(created_at\) where kind = 'run_now' and status = 'claimed' "
        r"and claimed_by is null;",
    ):
        assert re.search(pattern, code), pattern


def test_every_created_table_is_listed(sql):
    created = set(re.findall(r"create table if not exists public\.([a-z_]+)", sql))
    assert created == set(PHASE3_TABLES)


@pytest.mark.parametrize("table", PHASE3_TABLES)
def test_new_table_has_forced_rls_and_revokes(sql, table):
    assert f"alter table public.{table} enable row level security;" in sql
    assert f"alter table public.{table} force row level security;" in sql
    assert f"revoke all on table public.{table} from anon, authenticated;" in sql
    assert "create policy" not in _code(sql)


def test_sweeps_table_shape(sql):
    m = re.search(r"create table if not exists public\.assistant_sweeps \((.+?)\n\);", sql, re.S)
    assert m
    body = m.group(1)
    assert "check (step in ('nightly', 'orphans'))" in body
    assert "primary key (step, sweep_date)" in body
    assert "references" not in body


@pytest.mark.parametrize("sig", FUNCTIONS)
def test_functions_are_service_role_only(sql, sig):
    assert f"revoke all on function public.{sig} from public;" in sql
    assert f"revoke all on function public.{sig} from anon, authenticated;" in sql
    assert f"grant execute on function public.{sig} to service_role;" in sql
    name = sig.split("(", 1)[0]
    grants = re.findall(rf"grant execute on function public\.{name}\([^)]*\) to ([a-z_, ]+);", sql)
    assert grants == ["service_role"]


def test_every_function_is_invoker_pinned_and_listed(sql):
    names = re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
    assert set(names) == {sig.split("(", 1)[0] for sig in FUNCTIONS}
    assert len(names) == len(FUNCTIONS)
    for name in names:
        fn = _fn(sql, name)
        assert "security invoker" in fn and "set search_path = pg_catalog, public" in fn, name
        assert "security definer" not in fn, name


# --- schedules -------------------------------------------------------------------------


def test_triggers_are_bound(sql):
    code = _code(sql)
    for pattern in (
        r"create or replace trigger assistant_schedules_caps before insert or update of rrule, status, user_id\s+"
        r"on public\.assistant_schedules\s+for each row execute function public\.assistant_schedules_caps\(\);",
        r"create or replace trigger assistant_schedule_runs_forward before update on public\.assistant_schedule_runs"
        r"\s+for each row execute function public\.assistant_schedule_run_forward\(\);",
        r"create or replace trigger assistant_schedule_runs_no_delete before delete on public\.assistant_schedule_runs"
        r"\s+for each row execute function public\.assistant_refuse_change\(\);",
        r"create or replace trigger assistant_schedule_runs_no_truncate before truncate on "
        r"public\.assistant_schedule_runs\s+for each statement execute function public\.assistant_refuse_change\(\);",
        r"create or replace trigger assistant_memories_pin_guard before insert or update of pinned\s+"
        r"on public\.assistant_memories\s+for each row execute function public\.assistant_memories_pin_guard\(\);",
    ):
        assert re.search(pattern, code), pattern


def test_memories_updated_at_moves_only_on_content_or_state(sql):
    m = re.search(r"create or replace trigger assistant_memories_updated_at\s+before update of ([a-z_, ]+)\n", sql)
    assert m
    cols = {c.strip() for c in m.group(1).split(",")}
    assert cols == {"kind", "title", "body", "project_id", "status", "pinned", "source", "origin_tainted"}
    assert not cols & {"last_used_at", "use_count", "updated_at"}


def _caps_refuses(fn: str, rule: str) -> bool:
    """Replays the caps function's rule refusals (the regexes in its text)
    against one rule string, the way the trigger reads upper(new.rrule)."""
    v_rule = rule.upper()
    freq_pat = re.search(r"substring\(v_rule from '([^']+)'\)", fn).group(1)
    m = re.search(freq_pat, v_rule)
    if not m or m.group(1) not in ("HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return True
    if any(re.search(p, v_rule) for p in re.findall(r"v_rule ~ '([^']+)'", fn)):
        return True
    bym = re.search(r"BYMINUTE=([0-9,]+)", v_rule)
    return bool(bym and "," in bym.group(1))


def test_caps_still_refuse_any_bysecond_and_the_new_parts(sql):
    fn = _fn(sql, "assistant_schedules_caps")
    assert "v_rule ~ 'BYSECOND='" in fn
    assert "> 10" in fn and "pg_advisory_xact_lock(hashtext('assistant_schedules:'" in fn
    for rule in (
        "FREQ=HOURLY;BYMINUTE=0;BYSECOND=0,30",
        "FREQ=HOURLY;BYMINUTE=0;BYSECOND=0",
        "FREQ=MINUTELY;BYMINUTE=0",
        "FREQ=HOURLY;BYMINUTE=0,30",
        "FREQ=DAILY;RDATE=20261101T070000",
        "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;COUNT=3",
        "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;UNTIL=20271231T000000Z",
        "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1;BYHOUR=7;BYMINUTE=0",
        "FREQ=YEARLY;BYWEEKNO=1;BYHOUR=7;BYMINUTE=0",
        "FREQ=YEARLY;BYYEARDAY=1;BYHOUR=7;BYMINUTE=0",
        "freq=daily;byhour=7;byminute=0;exdate=20261225",
    ):
        assert _caps_refuses(fn, rule), rule
    for rule in (
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7;BYMINUTE=0",
        "FREQ=DAILY;BYHOUR=7;BYMINUTE=0",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;BYHOUR=7;BYMINUTE=0",
        "FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=8;BYMINUTE=0",
        "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7,8,9,10,11,12,13,14,15,16,17;BYMINUTE=0",
    ):
        assert not _caps_refuses(fn, rule), rule


def test_run_forward_guard(sql):
    fn = _fn(sql, "assistant_schedule_run_forward")
    for col in ("schedule_id", "user_id", "scheduled_for", "kind", "user_label"):
        assert f"new.{col} <> old.{col}" in fn, col
    assert "when 'claimed' then 0 when 'running' then 1 else 2" in fn
    assert "v_new < v_old or (v_old = 2 and new.status <> old.status)" in fn


def test_claim_is_one_skip_locked_statement_with_the_occurrence_key(sql):
    fn = _fn(sql, "claim_due_assistant_schedules")
    assert "returns setof public.assistant_schedule_runs" in fn
    assert "language sql" in fn
    assert fn.count("for update skip locked") == 2
    assert "on conflict (schedule_id, scheduled_for) do nothing" in fn
    assert "'missed'" in fn and "'overlap'" in fn
    assert "and s.after_next_run_at is not null" in fn
    assert "set next_run_at = d.after_next_run_at,\n           after_next_run_at = null" in fn
    manual = fn[fn.index("manual as ("):]
    assert "r2.kind = 'run_now' and r2.status = 'claimed' and r2.claimed_by is null" in manual
    assert "and not exists (select 1 from public.assistant_schedule_runs r3" in manual
    assert "r3.status in ('claimed', 'running')" in manual
    # SCHED-1: an older unclaimed Run now sibling blocks too (one row per schedule).
    assert ("and (r3.claimed_by is not null\n"
            "                                               or (r3.kind = 'run_now' and (r3.created_at, r3.id) < "
            "(r2.created_at, r2.id))))") in manual
    assert "least(coalesce(p_limit, 20), 50)" in fn
    # SCHED-4: an occurrence whose next slot is already due is superseded.
    assert "d.after_next_run_at as next_slot" in fn
    assert "when a.next_slot <= now()\n               then 'missed'" in fn


def test_one_pending_run_now_row_per_schedule(sql):
    """SCHED-1: the partial unique index covers only Run now rows nobody took,
    so the scheduled insert of the claim never hits it."""
    assert ("create unique index if not exists assistant_schedule_runs_one_pending_run_now_uq\n"
            "  on public.assistant_schedule_runs (schedule_id)\n"
            "  where kind = 'run_now' and status = 'claimed' and claimed_by is null;") in _code(sql)


def test_run_channel_claims_each_channel_once(sql):
    fn = _fn(sql, "assistant_schedule_run_channel")
    assert "p_channel not in ('chat', 'bell', 'email')" in fn
    assert "p_action not in ('claim', 'done', 'failed')" in fn
    assert "and not (coalesce(r.delivered, '{}'::jsonb) ? p_channel)" in fn
    assert "and r.delivered ->> p_channel = 'claimed'" in fn
    assert fn.count("r.status = 'running'") == 2


# --- memory ----------------------------------------------------------------------------


def test_event_check_widening_reads_the_live_definition(sql):
    code = _code(sql)
    assert "pg_get_constraintdef(c.oid) like '%revision_rejected%'" in code
    m = re.search(r"add constraint assistant_memory_events_event_chk\s+check \(event in \((.+?)\)\) not valid;",
                  code, re.S)
    assert m
    events = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert events == set(EVENTS_0170) | {"confirmed", "moved"}


def test_effect_widening_keeps_every_0170_effect(sql):
    code = _code(sql)
    assert "pg_get_constraintdef(c.oid) like '%sensitive_read%'" in code
    assert "if position('''housekeeping''' in v_def) > 0 then" in code
    m = re.search(r"add constraint assistant_tool_calls_effect_chk\s+check \(effect in \((.+?)\)\)\s+not valid;",
                  code, re.S)
    assert m
    assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == set(EFFECTS_0170) | {"housekeeping"}


def test_pin_guard_reads_the_transaction_flag(sql):
    fn = _fn(sql, "assistant_memories_pin_guard")
    assert "current_setting('bdr.assistant_memory_pin', true)" in fn
    assert "memory_pin_user_only" in fn
    apply_fn = _fn(sql, "assistant_memory_apply")
    assert "set_config('bdr.assistant_memory_pin', 'user', true)" in apply_fn
    # Only the user's pin op sets it.
    assert apply_fn.count("set_config(") == 1
    assert apply_fn.index("set_config(") > apply_fn.index("elsif p_op in ('pin', 'unpin') then")


def test_similar_uses_trigram_and_bounds(sql):
    fn = _fn(sql, "assistant_memory_similar")
    assert "set search_path = pg_catalog, public, extensions" in fn
    assert "similarity(m.title, p_title)" in fn
    assert "m.status <> 'archived'" in fn
    assert "least(coalesce(p_limit, 5), 10)" in fn


def test_redaction_matches_memory_calls_by_call_id(sql):
    fn = _fn(sql, "assistant_redact_memory_items")
    assert "immutable" in fn
    assert "i->>'name' like 'memory\\_%'" in fn
    assert "'arguments', '{\"redacted\":true}'" in fn
    assert "'output', '{\"redacted\":true}'" in fn
    assert "order by o" in fn


def test_apply_actor_op_matrix(sql):
    fn = _fn(sql, "assistant_memory_apply")
    assert ("p_actor = 'assistant' and p_op in ('create', 'revise_pending', 'propose_revision', 'forget')") in fn
    assert "p_actor = 'system' and p_op in ('archive', 'move', 'delete')" in fn
    user_ops = re.search(r"p_actor = 'user' and p_op in \((.+?)\)\)", fn, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", user_ops)) == {
        "create", "edit", "approve", "confirm", "reject", "archive", "restore", "pin", "unpin",
        "approve_revision", "reject_revision", "delete",
    }


def test_create_op_replays_by_key_and_caps_pending(sql):
    fn = _fn(sql, "assistant_memory_apply")
    create = fn[fn.index("if p_op = 'create' then"):fn.index("returning * into v_row;")]
    assert create.index("m.idempotency_key = p_idempotency_key") < create.index("insert into public.assistant_memories")
    assert "raise exception 'memory_cap_pending:" in create
    assert "if v_pending + 1 > 50 then" in create
    assert "pg_advisory_xact_lock(hashtext('assistant_memories:' || p_user_id::text))" in create
    # The model never sets pinned and the user's create is user_settings.
    assert "pinned" not in create.split("insert into", 1)[1]
    assert "coalesce(v_f->>'source', '') = 'user_settings'" in create


def test_propose_revision_supersedes_the_pending_one(sql):
    branch = _branch(_fn(sql, "assistant_memory_apply"), "propose_revision")
    assert branch.index("set decision = 'rejected', decided_at = now()") < branch.index(
        "insert into public.assistant_memory_revisions")
    assert "'superseded', true" in branch
    assert "memory_pinned_user_only" in branch


def test_delete_checks_legal_hold_first_and_scrubs_copies(sql):
    branch = _branch(_fn(sql, "assistant_memory_apply"), "delete")
    hold = branch.index("ap.legal_hold")
    assert hold < branch.index("update public.")
    assert hold < branch.index("delete from public.assistant_memories")
    assert "'code', 'legal_hold'" in branch
    assert "t.memory_ids @> array[v_row.id]" in branch
    assert "array_remove(m.used_memory_ids, v_row.id)" in branch
    assert "public.assistant_redact_memory_items(b->'items', v_id)" in branch
    assert "taint_corpus = null" in branch
    assert branch.index("taint_corpus = null") < branch.index("delete from public.assistant_memories")


def test_stale_and_status_codes(sql):
    fn = _fn(sql, "assistant_memory_apply")
    for code in ("memory_not_found", "memory_stale", "memory_archived", "memory_wrong_status",
                 "memory_pinned_user_only", "revision_not_found", "legal_hold"):
        assert f"'code', '{code}'" in fn, code
    assert "v_row.updated_at is distinct from p_expected_updated_at" in fn
    assert "v_rev.base_updated_at is distinct from v_row.updated_at" in fn
    assert "to_jsonb(v_row) - 'tsv'" in fn


def test_content_free_ops_rebase_an_open_revision(sql):
    """D2: pin, unpin, archive, forget, restore and move re-base a revision
    whose base was current, so only a content change makes it stale."""
    fn = _fn(sql, "assistant_memory_apply")
    assert "v_before := v_row.updated_at;" in fn
    assert "if p_op in ('archive', 'forget', 'restore', 'pin', 'unpin', 'move') then" in fn
    assert ("update public.assistant_memory_revisions r set base_updated_at = v_row.updated_at\n"
            "     where r.memory_id = v_row.id and r.decided_at is null and r.base_updated_at = v_before;") in fn


def test_touch_writes_usage_only(sql):
    fn = _fn(sql, "assistant_memory_touch")
    assert "set last_used_at = now(), use_count = m.use_count + 1" in fn
    assert "m.user_id = p_user_id and m.id = any(p_ids) and m.status = 'active'" in fn


# --- seed and guards -------------------------------------------------------------------


def test_holidays_seed_only_while_empty(sql):
    code = _code(sql)
    m = re.search(r"update public\.assistant_settings\s+set holidays = array\[(.+?)\]::date\[\]\s+where (.+?);",
                  code, re.S)
    assert m
    dates = re.findall(r"'(\d{4}-\d{2}-\d{2})'", m.group(1))
    assert len(dates) == 10 and dates == sorted(dates)
    assert m.group(2).strip() == "id = 1 and cardinality(coalesce(holidays, '{}'::date[])) = 0"


def test_inbox_tables_are_asserted(sql):
    code = _code(sql)
    for table in ("assistant_events", "assistant_subscriptions", "assistant_inbox"):
        assert f"to_regclass('public.{table}') is null" in code


_INDEX_RE = re.compile(r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?([a-z0-9_]+)", re.I)


def test_no_0176_index_name_exists_in_another_migration(sql):
    ours = set(_INDEX_RE.findall(_code(sql)))
    assert ours == {
        "assistant_schedules_approval_uq", "assistant_schedules_refill_idx", "assistant_schedules_paused_idx",
        "assistant_schedules_conversation_idx", "assistant_schedule_runs_schedule_idx",
        "assistant_schedule_runs_open_idx", "assistant_schedule_runs_manual_idx", "assistant_memories_idem_uq",
        "assistant_memory_revisions_idem_uq", "assistant_memory_revisions_one_pending_uq",
        "assistant_tool_results_memory_ids_idx", "assistant_schedule_runs_one_pending_run_now_uq",
    }
    others: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("0*.sql")):
        if path.name == FILE:
            continue
        for name in _INDEX_RE.findall(_code(path.read_text(encoding="utf-8"))):
            others.setdefault(name.lower(), path.name)
    clashes = {n: others[n] for n in ours if n.lower() in others}
    assert not clashes, f"0176 index names already used: {clashes}"


def test_no_llm_jobs_change(sql):
    assert "llm_jobs" not in _code(sql)


def test_every_statement_parses():
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(SQL_PATH.read_text(encoding="utf-8"))

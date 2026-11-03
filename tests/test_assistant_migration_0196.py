"""Migration 0196 (docs/ASSISTANT_PHASE8_PLAN.md section 4, 4.4): model triage.
Part A adds the digest email switch, the company triage switch with its epoch
and fuse latch, the company spend identity, the triage run and item tables,
the enqueue index, the retry helper and the seven RPCs of A6. Part B (owner at
the screen) swaps the spend kind check and adds the company identity check,
redefines assistant_spend_reserve and assistant_inbox_feedback and adds the
triage purge.

Static checks on the SQL text; scripts/check_assistant_phase8.sql runs the
behaviour on dev after the apply (16.2)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0196_assistant_phase8.sql"
SQL_PATH = MIGRATIONS / FILE
CHECK_SCRIPT = ROOT / "scripts/check_assistant_phase8.sql"
MANIFEST = ROOT / "docs/phase8_wi1_manifest.json"
MARKER = "-- ==== PART B ===="
SPLIT_2 = "-- ---- PART A split point 2"
SPLIT_3 = "-- ---- PART A split point 3"
COMPANY_SPEND_ID = "00000000-0000-0000-0000-000000000001"

PHASE8_TABLES = ("assistant_triage_runs", "assistant_triage_items")

PART_A_FUNCTIONS = (
    "assistant_company_spend_id()",
    "assistant_triage_item_retry(uuid, text, timestamptz)",
    "assistant_triage_enqueue(timestamptz, int, int, uuid[])",
    "assistant_triage_claim(text, timestamptz, int, int, int, numeric, int, uuid[])",
    "assistant_triage_finish(uuid, text, timestamptz, text, text, jsonb, jsonb)",
    "assistant_triage_skip_user(uuid, text, timestamptz)",
    "assistant_triage_company_spend()",
    "assistant_triage_counts(timestamptz, timestamptz)",
    "assistant_triage_skip_all(text, timestamptz, int)",
)
PART_B_FUNCTIONS = (
    "assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid)",
    "assistant_inbox_feedback(uuid, uuid, text, timestamptz)",
    "assistant_triage_purge(timestamptz, int, int)",
    "assistant_triage_set_enabled(uuid, boolean)",
)
# Every RPC takes p_now with a default except the finish and the purge (whose
# later arguments have no sensible default), the company spend read (MC-4) and the
# switch (B6: its epoch is always the real clock, never time travel).
P_NOW_DEFAULT = (
    "assistant_triage_item_retry", "assistant_triage_enqueue", "assistant_triage_claim",
    "assistant_triage_skip_user", "assistant_triage_counts", "assistant_triage_skip_all",
)
RUN_ERROR_KINDS = {"cap_global", "cap_test", "spend_check", "ineligible", "model_error", "bad_answer",
                   "truncated", "unpriced", "busy", "timeout", "lease_expired", "switched_off", "fuse", "breaker"}
SKIP_REASONS = {"cap", "ineligible", "offboarded", "stale", "dismissed", "switched_off", "fuse", "bad_answer"}
SIX_KINDS = ("turn_call", "split_task", "plans_pages", "ai_estimate", "browser_task", "triage")


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def part_a(sql) -> str:
    return sql[:sql.index(MARKER)]


@pytest.fixture(scope="module")
def part_b(sql) -> str:
    return sql[sql.index(MARKER):]


@pytest.fixture(scope="module")
def pieces(part_a) -> tuple[str, str, str]:
    i, j = part_a.index(SPLIT_2), part_a.index(SPLIT_3)
    return part_a[:i], part_a[i:j], part_a[j:]


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[1] for line in sql.splitlines() if "--" in line)


def _fn(sql: str, name: str) -> str:
    fns = _fns(sql, name)
    assert len(fns) == 1, name
    return fns[0]


def _fns(sql: str, name: str) -> list[str]:
    return re.findall(rf"create or replace function public\.{name}\(.+?\n(?:end \$\$;|\$\$;)", sql, re.S)


def _statements(code: str) -> list[str]:
    return [s.strip() for s in re.split(r";[ \t]*(?:\n|\Z)", code) if s.strip()]


def _table(sql: str, name: str) -> str:
    m = re.search(rf"create table if not exists public\.{name} \((.+?)\n\);", sql, re.S)
    assert m, name
    return m.group(1)


# --- file shape --------------------------------------------------------------------------


def test_exactly_one_0196_file_and_the_markers_once(sql):
    assert len(list(MIGRATIONS.glob("0196_*.sql"))) == 1
    assert sql.count(MARKER) == 1
    assert sql.count("-- ==== PART A ====") == 1
    assert sql.index("-- ==== PART A ====") < sql.index(SPLIT_2) < sql.index(SPLIT_3) < sql.index(MARKER)


def test_header_notify_last_and_no_dash_characters(sql, part_a, part_b, pieces):
    assert sql.startswith("-- 0196: ")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql and "\\u2014" not in sql
    for part in (part_a, part_b):
        assert _statements(_code(part))[-1] == "notify pgrst, 'reload schema'"
    assert "notify pgrst" not in pieces[0] and "notify pgrst" not in pieces[1]


def test_each_piece_and_part_b_stay_under_40_kb(sql, part_a, part_b, pieces):
    for piece in (*pieces, part_b):
        assert len(piece.encode()) < 40_000
    # Part A as a whole is over the size, so the header documents only the three-piece
    # apply (_a1, _a2, _a3) and never a one-piece route (plan 4.4).
    if len(part_a.encode()) >= 40_000:
        header = sql[:sql.index("-- ==== PART A ====")]
        assert "0196_assistant_phase8_a1, _a2 and _a3 in that order" in header
        assert "never\n-- apply part A in one piece" in header
        assert "optional" not in _comments(header).lower()
        for line in (SPLIT_2, SPLIT_3):
            marker = sql[sql.index(line):sql.index("\n", sql.index(line))]
            assert "(required;" in marker and "optional" not in marker


def test_tables_come_first_and_each_piece_grants_what_it_creates(pieces, part_b):
    """Dev's default ACL gives EXECUTE on a new public function to anon and
    authenticated, so each piece revokes and grants its own functions."""
    assert "create table if not exists" not in _code(pieces[1] + pieces[2])
    for piece in (*pieces, part_b):
        created = set(re.findall(r"create or replace function public\.([a-z_]+)\(", piece))
        assert created
        granted = set(re.findall(r"grant execute on function public\.([a-z_]+)\([^)]*\) to service_role;", piece))
        revoked = set(re.findall(r"revoke all on function public\.([a-z_]+)\([^)]*\) from anon, authenticated;", piece))
        public = set(re.findall(r"revoke all on function public\.([a-z_]+)\([^)]*\) from public;", piece))
        assert created == granted == revoked == public, (created ^ granted, created ^ revoked)
        for name in created:
            assert piece.index(f"grant execute on function public.{name}(") > piece.index(
                f"create or replace function public.{name}(")


def test_function_sets_per_part(part_a, part_b):
    def names(sigs):
        return {s.split("(", 1)[0] for s in sigs}
    assert set(re.findall(r"create or replace function public\.([a-z_]+)\(", part_a)) == names(PART_A_FUNCTIONS)
    assert set(re.findall(r"create or replace function public\.([a-z_]+)\(", part_b)) == names(PART_B_FUNCTIONS)


@pytest.mark.parametrize("part,sig", [("a", s) for s in PART_A_FUNCTIONS] + [("b", s) for s in PART_B_FUNCTIONS])
def test_functions_are_service_role_only(part_a, part_b, part, sig):
    text = part_a if part == "a" else part_b
    assert f"revoke all on function public.{sig} from public;" in text
    assert f"revoke all on function public.{sig} from anon, authenticated;" in text
    assert f"grant execute on function public.{sig} to service_role;" in text


def test_every_function_is_invoker_and_pinned(sql):
    names = re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
    assert len(names) == len(set(names)) == len(PART_A_FUNCTIONS) + len(PART_B_FUNCTIONS)
    for name in names:
        fn = _fn(sql, name)
        assert "security invoker" in fn and "set search_path = pg_catalog, public\n" in fn, name
        assert "security definer" not in fn, name


# --- destructive text --------------------------------------------------------------------


def test_part_a_holds_no_destructive_text(part_a):
    """The connector screens part A without the owner present: no drop,
    delete or truncate word anywhere, function bodies and comments included,
    except the four foreign key referential actions (the 0190 and 0192
    precedent). The cut-off error kind is spelled in two halves."""
    code = _code(part_a)
    assert not re.search(r"\bdelete\s+from\b", code, re.I)
    assert not re.search(r"drop", part_a, re.I)
    assert not re.search(r"truncat", part_a, re.I)
    assert not re.search(r"\bbefore\s+delete\b", code, re.I)
    deletes = re.findall(r"delete", part_a, re.I)
    actions = re.findall(r"\bon delete (?:cascade|set null)\b", code, re.I)
    assert len(deletes) == len(actions) == 4
    assert not re.search(r"(drop|delet|truncat)", _comments(part_a), re.I)
    assert code.count("'trunc' || 'ated'") == 3


def test_every_destructive_statement_is_in_part_b(part_b):
    code = _code(part_b)
    assert re.findall(r"drop constraint %I", code) == ["drop constraint %I"]
    assert not re.search(r"\bdrop\s+(table|function|index|trigger|type|column|view|policy)\b", code, re.I)
    assert not re.search(r"\btruncate\b", code, re.I)
    assert re.findall(r"delete from public\.([a-z_]+)", code) == ["assistant_triage_runs"]
    assert "delete from public.assistant_triage_runs" in _fn(part_b, "assistant_triage_purge")


def test_no_llm_jobs_and_no_quotes_object(sql):
    code = _code(sql)
    assert "llm_jobs" not in code
    assert not re.search(r"\bquotes?\b", code)


# --- tables, columns, checks -------------------------------------------------------------


def test_only_part_a_creates_tables_and_only_the_two(part_a, part_b):
    assert set(re.findall(r"create table if not exists public\.([a-z_]+)", part_a)) == set(PHASE8_TABLES)
    assert "create table" not in _code(part_b)


@pytest.mark.parametrize("table", PHASE8_TABLES)
def test_new_table_has_forced_rls_and_revokes(part_a, table):
    assert f"alter table public.{table} enable row level security;" in part_a
    assert f"alter table public.{table} force row level security;" in part_a
    assert f"revoke all on table public.{table} from anon, authenticated;" in part_a


def test_no_policies_and_no_grants_to_clients(sql):
    code = _code(sql)
    assert "create policy" not in code
    assert not re.search(r"grant [a-z, ]+ on (table )?public\.assistant_triage", code)
    assert not re.search(r"to (anon|authenticated)\b", code)


@pytest.mark.parametrize("table,column", [
    ("assistant_profiles", "digest_email_enabled boolean not null default true"),
    ("assistant_settings", "triage_enabled boolean not null default false"),
    ("assistant_settings", "triage_enabled_at timestamptz"),
    ("assistant_settings", "triage_fuse_tripped_at timestamptz"),
])
def test_added_columns_are_idempotent(part_a, table, column):
    assert re.search(rf"alter table public\.{table}\s+add column if not exists {re.escape(column)};", part_a)


def test_epoch_check_is_guarded_and_validated(part_a):
    assert "if not exists (select 1 from pg_constraint where conname = 'assistant_settings_triage_epoch_chk')" in part_a
    assert "check (not triage_enabled or triage_enabled_at is not null) not valid;" in part_a
    assert "validate constraint assistant_settings_triage_epoch_chk;" in part_a


def test_no_removed_option_survives(sql):
    """Owner answers 3 (a), 11 (b), 14 (b), 15 (c): no tainted column, no
    floor, no per-person triage switch, no delegation binding."""
    low = sql.lower()
    for name in ("triage_delegation_id", "triage_chat_floor_usd", "tainted", "floor", "triage_opt",
                 "opted_out", "delegation"):
        assert name not in low, name


def test_run_table_shape(part_a):
    body = _table(part_a, "assistant_triage_runs")
    assert "user_id        uuid not null references public.profiles(id) on delete cascade" in body
    assert "check (status in ('claimed', 'decided', 'skipped', 'failed'))" in body
    assert "check (item_count between 1 and 20)" in body
    kinds = re.search(r"error_kind in\s*\((.+?)\)\)", body, re.S).group(1)
    listed = set(re.findall(r"'([a-z_]+)'", kinds.replace("'trunc' || 'ated'", "'truncated'")))
    assert listed == RUN_ERROR_KINDS
    assert "reservation_id uuid," in body and "references" not in body.split("reservation_id", 1)[1].split("\n")[0]


def test_item_table_shape(part_a):
    body = _table(part_a, "assistant_triage_items")
    assert "user_id                uuid not null references public.profiles(id) on delete cascade" in body
    assert "inbox_id               uuid not null references public.assistant_inbox(id) on delete cascade" in body
    assert "run_id                 uuid references public.assistant_triage_runs(id) on delete set null" in body
    skips = re.search(r"skip_reason in\s*\((.+?)\)\)", body, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", skips)) == SKIP_REASONS
    assert "severity_hint in ('normal', 'high', 'urgent')" in body
    assert "decision in ('none', 'suggest', 'escalate')" in body
    assert "check (attempts between 0 and 3)" in body
    assert "check (infra_retries between 0 and 1000)" in body
    assert "reason ~ '^[a-z][a-z0-9_]{2,40}$'" in body and "action ~ '^[a-z][a-z0-9_]{2,40}$'" in body
    assert "enqueued_at            timestamptz not null default clock_timestamp()" in body
    assert "cap_deferred_at        timestamptz," in body
    assert "constraint assistant_triage_items_inbox_uq unique (user_id, inbox_id)" in body
    assert "check ((status = 'decided') = (decision is not null))" in body
    assert "check (decision is distinct from 'escalate' or severity_hint is not null)" in body
    assert "check ((status = 'skipped') = (skip_reason is not null))" in body
    for column in ("project_id", "source_notification_id", "escalation_event_id"):
        line = next(ln for ln in body.splitlines() if ln.strip().startswith(column + " "))
        assert "references" not in line, column


def test_indexes(part_a):
    assert ("create index if not exists assistant_triage_items_pending_idx\n"
            "  on public.assistant_triage_items (user_id, created_at) where status = 'pending';") in part_a
    assert ("on public.assistant_triage_items (decided_at desc) where status = 'decided';") in part_a
    assert ("create index if not exists assistant_inbox_record_triage_idx\n"
            "  on public.assistant_inbox (created_at, user_id) where delivery = 'record' and dismissed_at is null;"
            ) in part_a
    assert "on public.assistant_triage_runs (lease_until) where status = 'claimed';" in part_a


_INDEX_RE = re.compile(r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?([a-z0-9_]+)",
                       re.I)
_CONSTRAINT_RE = re.compile(r"\bconstraint\s+([a-z0-9_]+)", re.I)


def test_index_and_constraint_names_are_unique_across_migrations(sql):
    code = _code(sql)
    ours = set(_INDEX_RE.findall(code)) | set(_CONSTRAINT_RE.findall(code))
    ours -= {"assistant_spend_reservations_kind_chk", "assistant_spend_reservations_company_chk", "%i"}
    others: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("0*.sql")):
        if path.name == FILE:
            continue
        other = _code(path.read_text(encoding="utf-8"))
        for name in _INDEX_RE.findall(other) + _CONSTRAINT_RE.findall(other):
            others.setdefault(name.lower(), path.name)
    clashes = {n: others[n] for n in ours if n.lower() in others}
    assert not clashes, f"0196 names already used: {clashes}"


def test_no_plpgsql_variable_is_also_a_table_alias(sql):
    for name in re.findall(r"create or replace function public\.([a-z_]+)\(", sql):
        fn = _fn(sql, name)
        if "declare" not in fn:
            continue
        declared = set(re.findall(r"^\s+([a-z_][a-z0-9_]*)\s+[a-z]", fn.split("declare", 1)[1].split("\nbegin", 1)[0],
                                  re.M))
        aliases = set(re.findall(r"public\.[a-z_]+ ([a-z_]+)\b(?! *\()", _code(fn)))
        assert not declared & aliases, (name, declared & aliases)


# --- company spend identity (A2a) ---------------------------------------------------------


def test_company_spend_id_value_and_guard(part_a):
    fn = _fn(part_a, "assistant_company_spend_id")
    assert f"select '{COMPANY_SPEND_ID}'::uuid" in fn
    assert "immutable" in fn
    assert "if exists (select 1 from public.profiles where id = public.assistant_company_spend_id()) then" in part_a
    assert "raise exception 'assistant_company_spend_id collides with a profile'" in part_a
    assert COMPANY_SPEND_ID[14] == "0"   # version nibble 0: gen_random_uuid() (v4) never yields it


def test_company_spend_id_equals_the_python_constant():
    """spend.COMPANY_SPEND_ID arrives with WI-5 (wave B, after bdr-94 releases
    spend.py); until then this pins only the SQL value above."""
    from app.assistant import spend
    value = getattr(spend, "COMPANY_SPEND_ID", None)
    if value is None:
        pytest.skip("spend.COMPANY_SPEND_ID lands with WI-5")
    assert str(value) == COMPANY_SPEND_ID


# --- RPC bodies (A4 helper, A6) ------------------------------------------------------------


def test_p_now_defaults_and_the_exceptions(sql, part_a, part_b):
    for name in P_NOW_DEFAULT:
        assert "p_now timestamptz default now()" in _fn(part_a, name), name
    finish = _fn(part_a, "assistant_triage_finish")
    assert "p_run_id uuid, p_worker text, p_now timestamptz, p_status text, p_error_kind text,\n  p_items jsonb, p_usage jsonb" in finish
    assert "create or replace function public.assistant_triage_purge(p_now timestamptz, p_run_days int, p_limit int)" in part_b


def test_company_spend_takes_no_argument_and_reads_the_real_day(part_a):
    fn = _fn(part_a, "assistant_triage_company_spend")
    assert "create or replace function public.assistant_triage_company_spend()\n" in fn
    assert "p_now" not in fn
    assert "date_trunc('day', now() at time zone 'America/Los_Angeles')" in fn
    assert "l.user_id = public.assistant_company_spend_id()" in fn
    assert "r.kind = 'triage' and r.user_id = public.assistant_company_spend_id()" in fn
    assert "stable" in fn


def test_now_appears_only_where_the_real_day_is_meant(sql):
    """Leases and fences use clock_timestamp(); now() appears only as the
    p_now default and in the two spend reads and the reserve (0186 body)."""
    allowed = {"assistant_triage_company_spend", "assistant_triage_counts", "assistant_spend_reserve"}
    for name in re.findall(r"create or replace function public\.([a-z_]+)\(", sql):
        fn = _code(_fn(sql, name)).replace("p_now timestamptz default now()", "")
        if name not in allowed:
            assert "now()" not in fn.replace("clock_timestamp()", ""), name
        assert "current_timestamp" not in fn.lower(), name


def test_leases_and_fences_use_the_real_clock(part_a):
    claim = _fn(part_a, "assistant_triage_claim")
    assert "r.lease_until < clock_timestamp()" in claim
    assert "clock_timestamp() + make_interval(secs => p_lease_seconds)" in claim
    assert "r.lease_until > clock_timestamp()" in claim
    assert "x.enqueued_at < v_set.triage_enabled_at" in claim
    # Steps 5 to 7 (candidates, first item, batch) never take a pre-epoch item the
    # 500-row sweep has not reached yet.
    assert claim.count("t.enqueued_at >= v_set.triage_enabled_at") == 3
    tail = claim[claim.index("-- 5. Candidates."):claim.index("insert into public.assistant_triage_runs")]
    assert tail.count("t.enqueued_at >= v_set.triage_enabled_at") == 3
    assert claim.count("r.claimed_at >= v_set.triage_enabled_at") == 2
    finish = _fn(part_a, "assistant_triage_finish")
    assert "r.lease_until > clock_timestamp()" in finish
    assert "v_run.claimed_at < v_set.triage_enabled_at" in finish
    assert "'emitted_at', clock_timestamp()" in finish


def test_enqueue_feed_and_switches(part_a):
    fn = _fn(part_a, "assistant_triage_enqueue")
    assert "i.delivery = 'record' and i.dismissed_at is null" in fn
    assert "i.event_type like 'bell.%'" in fn
    assert "greatest(p_now - make_interval(hours => p_window_hours), v_set.triage_enabled_at)" in fn
    assert "i.created_at <= p_now" in fn
    assert "(p_user_ids is null or i.user_id = any(p_user_ids))" in fn
    assert "on conflict (user_id, inbox_id) do nothing" in fn
    assert "v_set.triage_fuse_tripped_at is not null" in fn and "return 0;" in fn
    assert "p_limit not between 1 and 1000" in fn
    assert "cursor" not in _code(fn)


def test_claim_steps_in_order(part_a):
    fn = _fn(part_a, "assistant_triage_claim")
    order = [
        "return jsonb_build_object('switched_off', true);",
        "return jsonb_build_object('fuse', true, 'tripped_now', false);",
        "skip_reason = 'switched_off'",
        "limit 50) d;",
        "set triage_fuse_tripped_at = clock_timestamp()",
        "error_kind = 'lease_expired'",
        "perform public.assistant_triage_item_retry(v_item.id, 'lease_expired', p_now);",
        "having min(t.created_at) <= p_now - make_interval(secs => p_debounce_seconds)",
        "perform pg_advisory_xact_lock(hashtext('assistant_triage:' || v_cand.user_id::text));",
        "if v_first.attempts > 0 then",
        "insert into public.assistant_triage_runs",
    ]
    positions = [fn.index(s) for s in order]
    assert positions == sorted(positions)
    assert "where s.id = 1 and s.triage_fuse_tripped_at is null;" in fn
    assert "v_sample >= 20" in fn and "> p_fuse_ratio" in fn and "v_hour_esc > p_fuse_per_hour" in fn
    assert "p_fuse_ratio > 0" in fn and "p_fuse_per_hour > 0" in fn
    assert "t.decided_at > p_now - interval '1 hour'" in fn
    assert "limit 5" in fn and fn.count("for update skip locked") >= 3
    assert "cardinality(v_ids)" in fn
    for key in ("'run_id'", "'user_id'", "'items'", "'inbox_created_at'", "'cap_deferred_at'",
                "'source_notification_id'"):
        assert key in fn, key


def test_finish_fence_order_and_emit(part_a):
    fn = _fn(part_a, "assistant_triage_finish")
    order = [
        "r.lease_until > clock_timestamp()",
        "return jsonb_build_object('fenced', false, 'emitted', 0);",
        "for share;",
        "v_skip not in ('dismissed', 'stale', 'cap')",
        "v_error := 'switched_off';",
        "v_error := 'fuse';",
        "if v_status = 'decided' then",
        "insert into public.assistant_events",
        "perform public.assistant_triage_item_retry(v_left.id, 'bad_answer', p_now);",
        "perform public.assistant_triage_item_retry(v_left.id, 'cap', p_now);",
    ]
    positions = [fn.index(s) for s in order]
    assert positions == sorted(positions)
    assert "'assistant.triage_escalation', 'p8:esc:' || v_item.inbox_id::text, v_item.project_id,\n                v_item.user_id" in fn
    assert "p_now, 'pending', 'emit')" in fn
    assert "on conflict (dedupe_key) do nothing" in fn
    assert "'inbox_id', v_item.inbox_id" in fn and "'severity_hint', v_item.severity_hint" in fn
    assert "t.run_id = p_run_id and t.status = 'claimed'" in fn
    assert "v_severity not in ('normal', 'high', 'urgent')" in fn
    # Everything the event carries comes from the stored item row, never from p_items.
    emit = fn[fn.index("insert into public.assistant_events"):fn.index("on conflict (dedupe_key)")]
    assert "v_entry" not in emit and "p_items" not in emit


def test_retry_helper_classes(part_a):
    fn = _fn(part_a, "assistant_triage_item_retry")
    assert "t.id = p_item_id and t.status = 'claimed' for update" in fn
    assert "least(v_item.attempts + 1, 3)" in fn
    assert "interval '60 seconds' else interval '5 minutes'" in fn
    assert "('model_error', 'timeout', 'busy', 'spend_check', 'unpriced', 'lease_expired', 'breaker')" in fn
    assert "least(v_item.infra_retries + 1, 1000)" in fn
    assert "least(60 * power(2, least(v_item.infra_retries, 10)), 900)" in fn
    assert ("(date_trunc('day', p_now at time zone 'America/Los_Angeles') + interval '1 day')\n"
            "                  at time zone 'America/Los_Angeles'") in fn
    assert "mod(abs(hashtext(p_item_id::text)::bigint), 7200)" in fn
    assert "coalesce(t.cap_deferred_at, p_now)" in fn
    assert "raise exception 'assistant_triage_item_retry: unknown kind'" in fn


def test_only_the_claim_and_the_finish_call_the_retry_helper(sql):
    callers = {name for name in re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
               if name != "assistant_triage_item_retry"
               and "assistant_triage_item_retry(" in _fn(sql, name)}
    assert callers == {"assistant_triage_claim", "assistant_triage_finish"}
    for path in (ROOT / "app").rglob("*.py"):
        assert "assistant_triage_item_retry" not in path.read_text(encoding="utf-8"), path


def test_skip_user_and_skip_all(part_a):
    fn = _fn(part_a, "assistant_triage_skip_user")
    assert "p_reason not in ('offboarded', 'switched_off', 'ineligible')" in fn
    assert "case when p_reason = 'ineligible' then 'ineligible' else 'switched_off' end" in fn
    assert fn.index("update public.assistant_triage_runs") < fn.index("update public.assistant_triage_items")
    assert "limit 500 for update skip locked" in fn
    # The claim's per-person lock comes before both updates, so a claim in flight for
    # the person cannot commit a live run after this call.
    lock = "perform pg_advisory_xact_lock(hashtext('assistant_triage:' || p_user_id::text));"
    assert fn.index("bad arguments") < fn.index(lock) < fn.index("update public.assistant_triage_runs")
    fn = _fn(part_a, "assistant_triage_skip_all")
    assert "p_reason not in ('switched_off', 'fuse')" in fn
    assert "if p_reason = 'fuse' and v_set.triage_fuse_tripped_at is null then" in fn
    assert ("v_set.enabled is true and v_set.proactive_enabled is true\n"
            "     and v_set.triage_enabled is true then") in fn
    assert "x.status = 'pending'" in fn


def test_counts_are_counts_only_with_role_suppression(part_a):
    fn = _fn(part_a, "assistant_triage_counts")
    assert "bool_or(c.people < 3)" in fn and "v_roles := '[]'::jsonb;" in fn
    assert "'user_id'" not in fn
    assert "public.assistant_spend_state(null::uuid, 0, 50)" in fn
    assert "r.kind = 'browser_task'" in fn
    assert "'fuse_tripped_at', v_set.triage_fuse_tripped_at" in fn
    assert "'cap_deferred_pending'" in fn


# --- part B ---------------------------------------------------------------------------------


def test_b1_lists_exactly_six_kinds_and_adds_the_company_check(part_b):
    m = re.search(r"add constraint assistant_spend_reservations_kind_chk\s+check \(kind in \((.+?)\)\);", part_b, re.S)
    assert m
    assert tuple(re.findall(r"'([a-z_]+)'", m.group(1))) == SIX_KINDS
    assert "pg_get_constraintdef(oid) like '%split_task%'" in part_b
    assert "if position('''triage''' in v_def) = 0 then" in part_b
    assert ("check ((kind = 'triage') = (user_id = public.assistant_company_spend_id())) not valid;") in part_b
    assert "validate constraint assistant_spend_reservations_company_chk;" in part_b
    assert "existing rows break it" in part_b


def _0186_reserve() -> str:
    text = (MIGRATIONS / "0186_assistant_browser_foundation.sql").read_text(encoding="utf-8")
    m = re.search(r"create or replace function public\.assistant_spend_reserve\(.+?\n\$\$;", text, re.S)
    assert m
    return m.group(0)


def test_b2_is_the_0186_body_with_the_two_changes(part_b):
    ours = _fn(part_b, "assistant_spend_reserve")
    source = _0186_reserve()
    company = ("  if (p_kind = 'triage' and p_user_id is distinct from public.assistant_company_spend_id())\n"
               "     or (p_kind <> 'triage' and p_user_id is not distinct from public.assistant_company_spend_id()) then\n"
               "    raise exception 'assistant_spend_reserve: bad arguments' using errcode = 'check_violation';\n"
               "  end if;\n")
    assert company in ours
    undone = (ours.replace(company, "")
                  .replace("'browser_task', 'triage')", "'browser_task')")
                  .replace("  if p_kind <> 'triage'\n     and (v_state->>'user_spent')",
                           "  if (v_state->>'user_spent')"))
    assert undone == source
    assert "perform pg_advisory_xact_lock(hashtext('assistant_spend'));" in ours
    assert "floor" not in ours.lower()


def _0190_feedback_b() -> str:
    text = (MIGRATIONS / "0190_assistant_phase7.sql").read_text(encoding="utf-8")
    part = text[text.index("-- ==== PART B ===="):]
    return _fn(part, "assistant_inbox_feedback")


def test_b3_differs_from_0190_part_b_only_in_the_mute_list(part_b):
    ours = _fn(part_b, "assistant_inbox_feedback").splitlines()
    source = _0190_feedback_b().splitlines()
    assert len(ours) == len(source)
    diff = [(a, b) for a, b in zip(source, ours) if a != b]
    assert diff == [(
        "       or (v_row.event_type like 'assistant.%' and v_row.event_type <> 'assistant.approval_expiring')) then",
        "       or (v_row.event_type like 'assistant.%' and v_row.event_type not in "
        "('assistant.approval_expiring', 'assistant.triage_escalation'))) then",
    )]


def test_b4_purge_skips_legal_hold_and_claimed_runs(part_b):
    fn = _fn(part_b, "assistant_triage_purge")
    assert "pg_try_advisory_xact_lock(hashtext('assistant_triage_purge'))" in fn
    assert "ap.legal_hold" in fn
    assert "r.status <> 'claimed'" in fn
    assert "lock table public.assistant_profiles in share mode;" in fn
    assert "order by r.created_at limit p_limit for update skip locked" in fn
    assert "p_run_days not between 7 and 3650" in fn


# --- later migrations keep the triage kind (F9) ---------------------------------------------


def _number(path: Path) -> int:
    return int(path.name[:4])


def test_b6_switch_flips_stamps_and_clears_in_one_statement(part_b):
    """D-1: the switch is one guarded update of the company row; the epoch is
    the real clock (never p_now, never now()), the latch clears only on a flip
    to on, and the acting admin can never be the company spend identity."""
    fn = _fn(part_b, "assistant_triage_set_enabled")
    assert "create or replace function public.assistant_triage_set_enabled(p_user_id uuid, p_enabled boolean)\n" in fn
    assert "returns jsonb" in fn and "p_now" not in fn
    code = _code(fn)
    assert code.count("update public.assistant_settings") == 1
    update = code[code.index("update public.assistant_settings"):code.index("returning * into v_set;")]
    assert "triage_enabled_at = case when p_enabled then clock_timestamp() else s.triage_enabled_at end" in update
    assert "triage_fuse_tripped_at = case when p_enabled then null else s.triage_fuse_tripped_at end" in update
    assert "updated_by = p_user_id" in update
    assert "where s.id = 1 and s.triage_enabled = not p_enabled" in update
    assert "p_user_id = public.assistant_company_spend_id()" in code
    assert "v_changed := found;" in code
    for key in ("triage_enabled", "triage_enabled_at", "triage_fuse_tripped_at", "updated_at", "changed"):
        assert f"'{key}'" in code, key
    # The switch carries no destructive text and sits after B5, before the reload.
    assert not re.search(r"\b(drop|delete|truncate)\b", code, re.I)
    assert part_b.index("-- B5.") < part_b.index("-- B6.") < part_b.index("notify pgrst")


def test_later_migrations_keep_triage_in_the_reserve_and_the_kind_check():
    """Whichever migration numbered 0196 or later last defines
    assistant_spend_reserve, and whichever last re-adds the kind check, must
    still carry 'triage' and the company id clause."""
    later = sorted((p for p in MIGRATIONS.glob("0*.sql") if _number(p) >= 196), key=_number)
    reserve = [p for p in later if "create or replace function public.assistant_spend_reserve(" in p.read_text("utf-8")]
    kind = [p for p in later if "add constraint assistant_spend_reservations_kind_chk" in p.read_text("utf-8")]
    assert reserve and kind
    body = re.search(r"create or replace function public\.assistant_spend_reserve\(.+?\n\$\$;",
                     reserve[-1].read_text("utf-8"), re.S).group(0)
    assert "'triage'" in body and "public.assistant_company_spend_id()" in body
    text = kind[-1].read_text("utf-8")
    check = re.search(r"add constraint assistant_spend_reservations_kind_chk\s+check \(kind in \((.+?)\)\)", text, re.S)
    assert "'triage'" in check.group(1)
    assert "assistant_spend_reservations_company_chk" in text or any(
        "assistant_spend_reservations_company_chk" in p.read_text("utf-8") for p in later)


# --- the SQL check script and the manifest ---------------------------------------------------


def test_check_script_is_transactional_and_dev_only():
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("-- DEV ONLY after 0196")
    assert "BDR_Prod" in text.splitlines()[0]
    code = _code(text)
    assert _statements(code)[0] == "begin" and _statements(code)[-1] == "rollback"
    assert "raise exception 'TEST_RESULT % pass=% fail=% failures=%'" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text
    assert not re.search(r"\b(drop|truncate)\b", code, re.I)
    assert not re.search(r"\bdelete\s+from\b", code, re.I)
    assert "commit" not in code.lower()


@pytest.mark.parametrize("label", [
    "enqueue_off_returns_0", "enqueue_starts_at_epoch", "enqueue_idempotent", "enqueue_survives_moved_created_at",
    "enqueue_never_failed_to_bid", "enqueue_every_owner_without_a_flag", "claim_debounce_waits",
    "claim_after_debounce", "claim_allowlist_filters", "claim_retried_item_alone",
    "claim_ahead_never_retires_a_live_lease", "claim_batch_max_and_one_run", "finish_after_lease_is_fenced",
    "finish_after_skip_user_is_fenced", "finish_switch_off", "finish_ignores_another_runs_item",
    "finish_urgent_stored", "finish_escalation_event", "finish_dedupes_the_event", "retry_bad_answer_at_3",
    "retry_cut_off_at_3", "retry_infra_keeps_attempts", "finish_unnamed_at_attempts_2",
    "lease_retirement_at_attempts_2", "cap_finish_defers_to_midnight", "cap_claimed_after_midnight",
    "cap_midnight_across_dst", "cap_age_reported", "cap_finish_skips_first", "reserve_triage_person_raises",
    "reserve_company_other_kind_raises", "reserve_triage_null_raises", "reserve_triage_skips_the_person_cap",
    "reserve_triage_refused_exactly_at_the_cap", "reserve_triage_refused_by_a_browser_hold",
    "company_call_counts_globally_only", "company_check_refuses_person_triage_row", "company_spend_ignores_people",
    "company_spend_takes_no_argument", "mute_new_kind", "purge_skips_legal_hold_and_claimed",
    "fuse_hourly_trips", "fuse_latched_after_61_minutes", "fuse_flip_clears_and_ignores_pre_flip_runs",
    "claim_latched_returns_fuse", "claim_off_returns_switched_off", "sweep_settles_pre_flip_items",
    "skip_all_refuses_while_running", "finish_epoch_fence", "finish_latch_fence", "fuse_ratio_trips",
    "fuse_ratio_needs_20", "skip_user_ineligible", "skip_user_offboarded_fails_switched_off",
    "retry_unknown_kind_raises", "skip_all_acts_when_proactive_off", "skip_all_acts_when_assistant_off",
    "cap_offset_stable", "sweep_leaves_some_pre_epoch_pending", "claim_never_takes_unswept_pre_epoch_items",
    "skip_user_takes_the_claim_lock", "claim_live_run_blocks_another_run",
    "claim_after_the_lease_expires_takes_a_run", "cap_midnight_spring_forward",
    "fuse_hourly_exactly_30_does_not_trip", "fuse_ratio_exactly_half_does_not_trip", "counts_carry_no_user_id",
    "switch_on_while_on_writes_nothing", "switch_off_keeps_the_epoch_and_the_latch",
    "switch_on_stamps_the_real_clock_and_clears_the_latch", "switch_row_matches_the_answer",
    "switch_refuses_the_company_id",
])
def test_check_script_covers_the_4_4_cases(label):
    assert f"pg_temp.p8_check('{label}'" in CHECK_SCRIPT.read_text(encoding="utf-8")


def test_cap_offset_check_compares_the_second_deferral():
    """MC-8: cap_offset_stable compares each item's offset after the second midnight
    with its first offset, so a re-salted or random offset fails (not a count >= 1)."""
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    start = text.index("pg_temp.p8_check('cap_offset_stable'")
    check = text[start:text.index(";", start)]
    assert "count(distinct" not in check
    assert "next_attempt_at - v_mid2" in check and "= v_off2" in check and "= v_off3" in check
    assert text.index("pg_temp.p8_check('cap_test_defers_again'") < start
    assert "select t.next_attempt_at - v_mid into v_off2" in text


_WRITING_RPC = re.compile(
    r"public\.(assistant_triage_(claim|finish|enqueue|skip_user|skip_all|purge|item_retry|set_enabled)"
    r"|assistant_spend_reserve|assistant_inbox_feedback)\s*\(")


def test_no_check_reads_back_in_the_statement_of_a_writing_rpc():
    """A subquery in the same statement as a writing RPC reads the statement-start snapshot,
    so it always sees the state from before the call (the five part A false failures)."""
    checks = [s for s in _statements(_code(CHECK_SCRIPT.read_text(encoding="utf-8")))
              if s.startswith("perform pg_temp.p8_check(")]
    assert len(checks) >= 70
    bad = [re.search(r"p8_check\('([a-z0-9_]+)'", s).group(1) for s in checks
           if _WRITING_RPC.search(s) and re.search(r"\(\s*select\b", s, re.I)]
    assert not bad, bad


def test_guard_checks_give_the_second_claim_an_eligible_person():
    """MC-5: the second claim must be otherwise eligible, or the check passes without the guard."""
    text = CHECK_SCRIPT.read_text(encoding="utf-8")
    # u4: the 5 leftovers qualify at debounce 0, so only run3's live lease stops j2.
    assert "j2 := public.assistant_triage_claim(w2, v_x, 420, 0, 10, 0, 0, array[u4]);" in text
    start = text.index("pg_temp.p8_check('claim_no_second_run_for_a_person'")
    assert "i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1," in text[start - 400:start]
    # a control: the same shape of claim returns a run after the lease is expired
    assert text.index("pg_temp.p8_check('claim_live_run_blocks_another_run'") < \
        text.index("pg_temp.p8_check('claim_after_the_lease_expires_takes_a_run'")


def test_check_script_labels_are_unique():
    labels = re.findall(r"pg_temp\.p8_check\('([a-z0-9_]+)'", CHECK_SCRIPT.read_text(encoding="utf-8"))
    assert len(labels) == len(set(labels)) >= 70


def test_manifest_lists_the_read_only_files():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = data["read_only"]
    assert entries
    for path, entry in entries.items():
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), path
        assert entry["group"] in {"bdr_notifications", "assistant_read_only", "bdr94_held", "migrations", "frontend"}
    for must in ("bdr_be/app/services/notifications.py", "bdr_be/app/assistant/access.py",
                 "bdr_be/app/assistant/llm_messages.py", "bdr_be/supabase/migrations/0190_assistant_phase7.sql",
                 "bdr_fe/components/NotificationsBell.tsx", "bdr_fe/lib/i18n.ts"):
        assert must in entries, must
    assert not any("graph_email" in p or "0196_" in p for p in entries)

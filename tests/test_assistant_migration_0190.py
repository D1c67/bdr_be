"""Migration 0190 (docs/ASSISTANT_PHASE7_PLAN.md, section 3): the proactive
router. Part A adds the claim and status columns on assistant_events, the
delivery, kind, digest, bell link and dismissal columns on assistant_inbox,
source and role checks on assistant_subscriptions, the company switch, the
digest, project mute, cursor, slot and send ledgers, the classify trigger,
the bell tail index and the fourteen router RPCs (A11.1 to A11.14). Part B
(owner at the screen) widens the feedback check for mute_project, replaces
the feedback RPC and adds the reset and purge functions.

Static checks on the SQL text (plan 3.4). PHASE7_TABLES is exported for
other tests (every new assistant table must have forced RLS and revoked
privileges)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.roles import INTERNAL_ROLES

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0190_assistant_phase7.sql"
SQL_PATH = MIGRATIONS / FILE
MARKER = "-- ==== PART B ===="

PHASE7_TABLES = (
    "assistant_digests",
    "assistant_project_mutes",
    "assistant_router_cursors",
    "assistant_proactive_slots",
    "assistant_external_sends",
)

PART_A_FUNCTIONS = (
    "assistant_inbox_classify()",
    "assistant_events_claim(text, int, int, timestamptz, uuid[])",
    "assistant_event_finish(uuid, text, text, jsonb, text, int, timestamptz)",
    "assistant_inbox_deliver(uuid, uuid, text, jsonb, boolean, timestamptz, timestamptz)",
    "assistant_digest_deliver(uuid, date, text, timestamptz, text, boolean, timestamptz)",
    "assistant_inbox_mark_read(uuid, uuid[], timestamptz)",
    "assistant_inbox_feedback(uuid, uuid, text, timestamptz)",
    "assistant_inbox_snooze(uuid, uuid, timestamptz, timestamptz)",
    "assistant_subscription_set(text, uuid, text, boolean, text, text, uuid)",
    "assistant_inbox_dismiss(uuid, uuid[], text, timestamptz)",
    "assistant_inbox_feedback_report(timestamptz, int, timestamptz)",
    "assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz)",
    "assistant_external_sends_claim(text, int, int, timestamptz)",
    "assistant_external_send_settle(uuid, text, text, text, uuid, int, timestamptz)",
    "assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz)",
)

PART_B_FUNCTIONS = (
    "assistant_inbox_feedback(uuid, uuid, text, timestamptz)",
    "assistant_subscription_reset(text, uuid, text, uuid)",
    "assistant_proactive_purge(timestamptz, int, int, int, int, int)",
)

# The router RPCs that take p_now (D12). The subscription upsert and reset,
# the classify trigger and the purge (p_now without a default) are the rest.
P_NOW_FUNCTIONS = (
    "assistant_events_claim", "assistant_event_finish", "assistant_inbox_deliver", "assistant_digest_deliver",
    "assistant_inbox_mark_read", "assistant_inbox_feedback", "assistant_inbox_snooze", "assistant_inbox_dismiss",
    "assistant_inbox_feedback_report", "assistant_inbox_link_bell", "assistant_external_sends_claim",
    "assistant_external_send_settle", "assistant_inbox_fold",
)

SEND_STATUSES = {"pending", "claimed", "sent", "failed", "skipped", "uncertain"}
SEND_ERROR_KINDS = {"transient", "permanent", "uncertain", "lease_expired", "channel_off", "opted_out", "stale",
                    "offboarded", "quiet_hours"}


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def part_a(sql) -> str:
    return sql[:sql.index(MARKER)]


@pytest.fixture(scope="module")
def part_b(sql) -> str:
    return sql[sql.index(MARKER):]


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _fn(sql: str, name: str) -> str:
    m = re.search(rf"create or replace function public\.{name}\(.+?\nend \$\$;", sql, re.S)
    assert m, name
    return m.group(0)


def _fns(sql: str, name: str) -> list[str]:
    return re.findall(rf"create or replace function public\.{name}\(.+?\nend \$\$;", sql, re.S)


def _statements(code: str) -> list[str]:
    return [s.strip() for s in re.split(r";[ \t]*(?:\n|\Z)", code) if s.strip()]


def _check(code: str, name: str) -> str:
    m = re.search(rf"add constraint {name}\s+check \((.+?)\) not valid;", code, re.S)
    assert m, name
    return m.group(1)


# --- file shape --------------------------------------------------------------------------


def test_exactly_one_0190_file_and_the_markers_once(sql):
    assert len(list(MIGRATIONS.glob("0190_*.sql"))) == 1
    assert sql.count(MARKER) == 1
    assert sql.count("-- ==== PART A ====") == 1
    assert sql.index("-- ==== PART A ====") < sql.index(MARKER)


def test_header_notify_last_and_no_dash_characters(sql, part_a, part_b):
    assert sql.startswith("-- 0190: ")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    assert "\\u2014" not in sql
    for part in (part_a, part_b):
        assert _statements(_code(part))[-1] == "notify pgrst, 'reload schema'"


def test_optional_part_a_split_points_keep_each_piece_small(part_a):
    """Part A is above the usual 40 KB; the two optional split points cut it
    into pieces that each stay under 40 KB, in table-then-function order."""
    i = part_a.index("-- ---- PART A split point 2")
    j = part_a.index("-- ---- PART A split point 3")
    assert i < j
    for piece in (part_a[:i], part_a[i:j], part_a[j:]):
        assert len(piece.encode()) < 40_000
    assert "create table if not exists" not in _code(part_a[i:])
    assert "create or replace function public.assistant_events_claim(" in part_a[i:j]


def test_each_part_a_piece_grants_the_functions_it_creates(part_a):
    """Dev's default ACL gives EXECUTE on a new public function to anon and
    authenticated, so each optional piece revokes and grants its own functions:
    no RPC is exposed between pieces or after a failed later piece."""
    i = part_a.index("-- ---- PART A split point 2")
    j = part_a.index("-- ---- PART A split point 3")
    for piece in (part_a[:i], part_a[i:j], part_a[j:]):
        created = set(re.findall(r"create or replace function public\.([a-z_]+)\(", piece))
        assert created
        granted = set(re.findall(r"grant execute on function public\.([a-z_]+)\([^)]*\) to service_role;", piece))
        revoked = set(re.findall(r"revoke all on function public\.([a-z_]+)\([^)]*\) from anon, authenticated;", piece))
        assert created == granted == revoked, (created ^ granted, created ^ revoked)
        for name in created:
            assert piece.index(f"grant execute on function public.{name}(") > piece.index(
                f"create or replace function public.{name}(")


def test_part_b_stays_under_40_kb(part_b):
    assert len(part_b.encode()) < 40_000


def test_no_top_level_drop_anywhere(sql):
    code = _code(sql)
    assert not re.search(r"^\s*drop\s", code, re.M | re.I)
    assert not re.search(r"\bdrop\s+(table|function|trigger|index|policy|column|view|type)\b", code, re.I)
    for stmt in _statements(code):
        assert not re.match(r"(drop|truncate|delete)\b", stmt, re.I), stmt[:80]
    drops = [line.strip() for line in code.splitlines() if re.search(r"\bdrop\s+constraint\b", line, re.I)]
    assert drops == ["execute 'alter table public.assistant_inbox drop constraint assistant_inbox_feedback_check';"]


def test_part_a_holds_no_destructive_text(part_a):
    """The only delete text in part A is the FK referential actions (as 0180
    part A carried); comments carry no destructive words."""
    code = _code(part_a)
    assert not re.search(r"\bdelete\s+from\b", code, re.I)
    assert not re.search(r"\btruncate\b", code, re.I)
    assert not re.search(r"\bdrop\b", code, re.I)
    assert not re.search(r"\bbefore\s+delete\b", code, re.I)
    deletes = re.findall(r"\bdelete\b", code, re.I)
    actions = re.findall(r"\bon delete (?:cascade|set null)\b", code, re.I)
    assert len(deletes) == len(actions) == 5
    comments = "\n".join(line.split("--", 1)[1] for line in part_a.splitlines() if "--" in line)
    assert not re.search(r"\b(drop|delete|truncate)\w*\b", comments, re.I)


def test_every_destructive_statement_is_in_part_b(part_b):
    code = _code(part_b)
    assert "drop constraint assistant_inbox_feedback_check" in code
    reset = _fn(part_b, "assistant_subscription_reset")
    assert reset.count("delete from public.assistant_subscriptions") == 2
    purge = _fn(part_b, "assistant_proactive_purge")
    for table in PHASE7_TABLES:
        if table == "assistant_router_cursors":
            assert f"delete from public.{table}" not in purge
        else:
            assert f"delete from public.{table}" in purge, table
    assert "delete from public.assistant_inbox" in purge and "delete from public.assistant_events" in purge


def test_no_llm_jobs_and_no_quotes_object(sql):
    code = _code(sql)
    assert "llm_jobs" not in code
    assert not re.search(r"\bpublic\.quotes\b", code)
    assert not re.search(r"\bon\s+(public\.)?quotes\b", code)
    assert "quotes_tail" not in sql
    assert "quotes_late_ai_idx" not in sql


# --- tables, columns, checks -----------------------------------------------------------


def test_every_created_table_is_listed_and_only_part_a_creates_tables(sql, part_a, part_b):
    assert set(re.findall(r"create table if not exists public\.([a-z_]+)", sql)) == set(PHASE7_TABLES)
    assert "create table" not in _code(part_b)
    assert "create table if not exists public.assistant_external_sends" in part_a


@pytest.mark.parametrize("table", PHASE7_TABLES)
def test_new_table_has_forced_rls_and_revokes(part_a, table):
    assert f"alter table public.{table} enable row level security;" in part_a
    assert f"alter table public.{table} force row level security;" in part_a
    assert f"revoke all on table public.{table} from anon, authenticated;" in part_a


def test_no_policies_and_no_grants_to_clients(sql):
    code = _code(sql)
    assert "create policy" not in code
    assert not re.search(r"grant [a-z, ]+ on (table )?public\.\S+ to (anon|authenticated)", code)


@pytest.mark.parametrize("table,column", [
    ("assistant_settings", "proactive_enabled boolean not null default false"),
    ("assistant_events", "status          text not null default 'pending'"),
    ("assistant_events", "attempts        integer not null default 0"),
    ("assistant_events", "routed          jsonb not null default '{}'::jsonb"),
    ("assistant_subscriptions", "source     text not null default 'admin'"),
    ("assistant_inbox", "delivery         text not null default 'direct'"),
    ("assistant_inbox", "deliver_after    timestamptz"),
    ("assistant_inbox", "source_notification_id uuid"),
    ("assistant_inbox", "snooze_count     smallint not null default 0"),
])
def test_added_columns_are_idempotent(part_a, table, column):
    assert re.search(rf"alter table public\.{table}\s+(?:add column if not exists [^\n]+\n\s*)*"
                     rf"add column if not exists {re.escape(column)}", part_a), (table, column)


@pytest.mark.parametrize("table,name", [
    ("assistant_events", "assistant_events_status_chk"),
    ("assistant_events", "assistant_events_done_chk"),
    ("assistant_events", "assistant_events_shape_chk"),
    ("assistant_subscriptions", "assistant_subscriptions_source_chk"),
    ("assistant_subscriptions", "assistant_subscriptions_role_chk"),
    ("assistant_inbox", "assistant_inbox_delivery_chk"),
    ("assistant_inbox", "assistant_inbox_dismissed_chk"),
])
def test_new_checks_are_guarded_and_validated(part_a, table, name):
    code = _code(part_a)
    assert f"if not exists (select 1 from pg_constraint where conname = '{name}') then" in code
    assert f"alter table public.{table} add constraint {name}" in code
    assert f"alter table public.{table} validate constraint {name};" in code


def test_events_source_check_lists_exactly_the_three_emitters_and_emit(part_a):
    body = _check(_code(part_a), "assistant_events_shape_chk")
    m = re.search(r"source in \(([^)]*)\)", body)
    assert m
    assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == {"bell_tail", "audit_tail", "sweeper", "emit"}


def test_cursor_check_lists_exactly_two_tails(part_a):
    m = re.search(r"name\s+text primary key check \(name in \(([^)]*)\)\)", part_a)
    assert m
    assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == {"notifications", "audit_log"}
    seed = re.search(r"insert into public\.assistant_router_cursors \(name, cursor_at\)\s+values (.+?)\s+on conflict "
                      r"\(name\) do nothing;", part_a, re.S)
    assert seed and set(re.findall(r"'([a-z_]+)'", seed.group(1))) == {"notifications", "audit_log"}


def test_role_check_equals_internal_roles(part_a):
    body = _check(_code(part_a), "assistant_subscriptions_role_chk")
    roles = set(re.findall(r"'([a-z_]+)'", body))
    assert roles == {r.value for r in INTERNAL_ROLES}
    assert len(roles) == 6
    for refused in ("estimating_engineer", "pe", "estimator"):
        assert refused not in roles
    # The upsert RPC uses the same list.
    fn = _fn(part_a, "assistant_subscription_set")
    m = re.search(r"p_role not in \((.+?)\) then", fn, re.S)
    assert m and set(re.findall(r"'([a-z_]+)'", m.group(1))) == roles


def test_subscription_source_check(part_a):
    body = _check(_code(part_a), "assistant_subscriptions_source_chk")
    assert "source in ('admin', 'user', 'feedback_auto')" in body
    assert "(role is null or source = 'admin')" in body


def test_delivery_check_requires_deliver_after_on_router_rows(part_a):
    body = _check(_code(part_a), "assistant_inbox_delivery_chk")
    assert "delivery in ('direct', 'immediate', 'digest', 'record')" in body
    assert "(delivery not in ('immediate', 'digest') or deliver_after is not null)" in body
    assert "(source_notification_id is null or delivery = 'record')" in body
    assert "snooze_count between 0 and 3" in body


def test_dismissed_check(part_a):
    body = _check(_code(part_a), "assistant_inbox_dismissed_chk")
    assert "(dismissed_at is null) = (dismissed_reason is null)" in body
    assert set(re.findall(r"'([a-z_]+)'", body)) == {
        "user", "project_closed", "owner_offboarded", "superseded", "muted"}


def test_send_ledger_shape(part_a):
    m = re.search(r"create table if not exists public\.assistant_external_sends \((.+?)\n\);", part_a, re.S)
    assert m
    body = m.group(1)
    assert "constraint assistant_external_sends_target_uq unique (target_kind, target_id, channel)" in body
    assert "user_id         uuid not null references public.profiles(id) on delete cascade" in body
    assert "target_id       uuid not null," in body  # no FK
    state = re.search(r"constraint assistant_external_sends_state_chk check \((.+)\)\s*$", body, re.S).group(1)
    statuses = re.search(r"status in \(([^)]*)\)", state).group(1)
    assert set(re.findall(r"'([a-z_]+)'", statuses)) == SEND_STATUSES
    kinds = re.search(r"error_kind in \(([^)]*)\)", state).group(1)
    assert set(re.findall(r"'([a-z_]+)'", kinds)) == SEND_ERROR_KINDS
    assert "attempts between 0 and 5" in state
    assert "((status = 'sent') = (sent_at is not null))" in state
    assert "((target_kind = 'inbox' and channel = 'teams') or (target_kind = 'digest' and channel = 'email'))" in state
    # Never an address or exception text: no text column besides the enums and the worker id.
    cols = {line.split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith("constraint")
            and not line.strip().startswith("status in") and not line.strip().startswith("and")}
    assert not cols & {"email", "address", "recipient", "to_address", "error", "last_error", "message"}


def test_digest_ledger_and_slot_ledger_shape(part_a):
    digests = re.search(r"create table if not exists public\.assistant_digests \((.+?)\n\);", part_a, re.S).group(1)
    assert "constraint assistant_digests_slot_uq unique (user_id, digest_date, slot)" in digests
    assert "slot            text not null check (slot in ('am', 'pm'))" in digests
    assert re.search(r"created_at\s+timestamptz not null,", digests)  # no default (D12)
    slots = re.search(r"create table if not exists public\.assistant_proactive_slots \((.+?)\n\);", part_a, re.S).group(1)
    assert "check (step in ('digest_am', 'digest_pm', 'purge'))" in slots
    assert "primary key (step, slot_date)" in slots


def test_project_ids_carry_no_fk(part_a):
    mutes = re.search(r"create table if not exists public\.assistant_project_mutes \((.+?)\n\);", part_a, re.S).group(1)
    assert "project_id uuid not null," in mutes
    assert "references public.projects" not in _code(part_a)


def test_bell_tail_index_and_inbox_indexes(part_a):
    code = _code(part_a)
    assert "create index if not exists notifications_created_at_idx on public.notifications (created_at, id);" in code
    assert re.search(r"create index if not exists assistant_inbox_unread_idx\s+on public\.assistant_inbox \(user_id\)\s+"
                     r"where delivery in \('immediate', 'digest'\) and read_at is null and delivered_at is not null "
                     r"and dismissed_at is null;", code)
    assert re.search(r"create index if not exists assistant_inbox_pending_digest_idx\s+on public\.assistant_inbox "
                     r"\(user_id, deliver_after\)\s+where delivery = 'digest' and delivered_at is null and "
                     r"dismissed_at is null;", code)


def test_classify_trigger_and_backfill(part_a):
    fn = _fn(part_a, "assistant_inbox_classify")
    assert "returns trigger" in fn
    assert "when 'browser' then 'assistant.browser'" in fn
    assert "when 'calendar' then 'assistant.calendar'" in fn
    assert "when 'p7' then null" in fn
    assert "else 'assistant.mailbox_access' end;" in fn
    code = _code(part_a)
    assert "if not exists (select 1 from pg_trigger where tgname = 'assistant_inbox_classify_biu'" in code
    assert ("create trigger assistant_inbox_classify_biu before insert on public.assistant_inbox\n"
            "      for each row execute function public.assistant_inbox_classify();") in code
    assert "where event_type is null and dedupe_key not like 'p7:%';" in code


# --- functions ---------------------------------------------------------------------------


def test_function_sets_per_part(part_a, part_b):
    a = re.findall(r"create or replace function public\.([a-z_]+)\(", part_a)
    b = re.findall(r"create or replace function public\.([a-z_]+)\(", part_b)
    assert a == [s.split("(", 1)[0] for s in PART_A_FUNCTIONS]
    assert b == [s.split("(", 1)[0] for s in PART_B_FUNCTIONS]


@pytest.mark.parametrize("part,sig", [("a", s) for s in PART_A_FUNCTIONS] + [("b", s) for s in PART_B_FUNCTIONS])
def test_functions_are_service_role_only(part_a, part_b, part, sig):
    text = part_a if part == "a" else part_b
    assert f"revoke all on function public.{sig} from public;" in text
    assert f"revoke all on function public.{sig} from anon, authenticated;" in text
    assert f"grant execute on function public.{sig} to service_role;" in text
    name = sig.split("(", 1)[0]
    grants = re.findall(rf"grant execute on function public\.{name}\([^)]*\) to ([a-z_, ]+);", text)
    assert grants == ["service_role"]


def test_every_function_is_invoker_and_pinned(sql):
    names = re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
    assert len(names) == len(PART_A_FUNCTIONS) + len(PART_B_FUNCTIONS)
    for name in set(names):
        for fn in _fns(sql, name):
            assert "security invoker" in fn and "set search_path = pg_catalog, public\n" in fn, name
            assert "security definer" not in fn, name


def test_no_router_function_body_names_now_except_the_p_now_default(sql):
    for name in set(re.findall(r"create or replace function public\.([a-z_]+)\(", sql)):
        for fn in _fns(sql, name):
            stripped = fn.replace("p_now timestamptz default now()", "")
            assert "now()" not in _code(stripped), name
            assert "current_timestamp" not in stripped.lower(), name
            assert "clock_timestamp" not in stripped.lower(), name


@pytest.mark.parametrize("name", P_NOW_FUNCTIONS)
def test_router_rpcs_take_p_now_with_a_default(sql, name):
    for fn in _fns(sql, name):
        assert "p_now timestamptz default now()" in fn, name


def test_purge_takes_p_now_without_a_default(part_b):
    fn = _fn(part_b, "assistant_proactive_purge")
    assert ("p_now timestamptz, p_inbox_days int, p_event_days int, p_digest_days int, p_send_days int, "
            "p_limit int") in fn


def test_event_claim_retires_poison_then_claims_with_skip_locked(part_a):
    fn = _fn(part_a, "assistant_events_claim")
    assert "returns setof public.assistant_events" in fn
    retire = fn.index("last_error = 'lease_exhausted'")
    claim = fn.index("for update skip locked")
    assert retire < claim
    assert "e.attempts >= 5" in fn and "e.attempts < 5" in fn
    assert "(p_ids is null or e.id = any(p_ids))" in fn
    assert "(e.next_attempt_at is null or e.next_attempt_at <= p_now)" in fn
    assert "(e.status = 'pending' or (e.status = 'claimed' and e.claimed_at < p_now - v_lease))" in fn
    assert "attempts = e.attempts + 1" in fn
    assert "least(coalesce(p_limit, 50), 200)" in fn
    assert "greatest(60, coalesce(p_lease_seconds, 600))" in fn


def test_event_finish_is_fenced(part_a):
    fn = _fn(part_a, "assistant_event_finish")
    assert fn.count("e.claimed_by = p_worker and e.status = 'claimed'") == 2
    assert "p_status not in ('processed', 'skipped', 'failed', 'retry')" in fn
    assert "left(p_error, 500)" in fn


def test_inbox_deliver_has_no_cap_and_one_bell_and_ledger_row(part_a):
    fn = _fn(part_a, "assistant_inbox_deliver")
    assert "bell_cap" not in fn and "cap" not in fn.split("returns jsonb", 1)[0]
    assert "pg_advisory_xact_lock(hashtext('assistant_inbox_deliver:' || p_user_id::text))" in fn
    assert "i.delivered_at is null and i.dismissed_at is null and i.deliver_after <= p_now" in fn
    assert fn.count("insert into public.notifications") == 1
    assert "'assistant.inbox'" in fn and "project_id, type" in fn and "values (p_user_id, null," in fn
    assert "on conflict (target_kind, target_id, channel) do nothing" in fn
    assert "greatest(p_now, coalesce(p_teams_not_before, p_now))" in fn
    assert "deliver(" not in fn.replace("assistant_inbox_deliver", "")


def test_digest_deliver_claims_once_and_writes_nothing_for_an_empty_slot(part_a):
    fn = _fn(part_a, "assistant_digest_deliver")
    lock = fn.index("pg_advisory_xact_lock(hashtext('assistant_inbox_deliver:' || p_user_id::text))")
    empty = fn.index("return jsonb_build_object('claimed', false);")
    ledger = fn.index("insert into public.assistant_digests")
    assert lock < empty < ledger
    assert fn.count("i.deliver_after <= p_slot_at") == 2
    assert "on conflict (user_id, digest_date, slot) do nothing" in fn
    assert "p_now - interval '3 days'" in fn
    assert "if v_n > 0 then" in fn
    assert "'assistant.digest'" in fn
    assert "values (p_user_id, 'digest', v_id, 'email', 'pending', p_now, p_now, p_now)" in fn
    assert "n.dismissed_at is null and n.read_at is null" in fn


def test_mark_read_and_dismiss_never_touch_a_record_source_bell(part_a):
    for name in ("assistant_inbox_mark_read", "assistant_inbox_dismiss"):
        fn = _fn(part_a, name)
        assert "source_notification_id" not in fn, name
        assert "n.id = upd.notification_id" in fn, name
    assert "i.delivery in ('immediate', 'digest')" in _fn(part_a, "assistant_inbox_mark_read")


def test_feedback_part_a_refuses_mute_project_and_part_b_accepts_it(part_a, part_b):
    a = _fn(part_a, "assistant_inbox_feedback")
    b = _fn(part_b, "assistant_inbox_feedback")
    assert "p_value not in ('useful', 'not_useful', 'mute') then" in a
    assert "'invalid_feedback'" in a
    assert "mute_project" not in a
    assert "p_value not in ('useful', 'not_useful', 'mute', 'mute_project') then" in b
    assert "insert into public.assistant_project_mutes (user_id, project_id, created_at)" in b
    for fn in (a, b):
        assert "on conflict (user_id, event_type) where role is null" in fn
        assert "on conflict (user_id, event_type) where role is null do nothing;" in fn
        assert "get diagnostics v_rows = row_count;" in fn
        assert "interval '14 days'" in fn and "v_count >= 3" in fn
        assert "'not_mutable'" in fn and "'not_found'" in fn


def test_part_a_feedback_never_replaces_part_b(part_a):
    """A re-run of part A (or piece 2) after part B must keep part B's feedback RPC."""
    guard = part_a[part_a.index("do $guard$"):part_a.index("end $guard$;")]
    assert "to_regprocedure('public.assistant_inbox_feedback(uuid, uuid, text, timestamptz)')" in guard
    assert "if v_fn is not null and position('mute_project' in pg_get_functiondef(v_fn)) > 0 then\n    return;" in guard
    assert "execute $fn$\ncreate or replace function public.assistant_inbox_feedback(" in guard
    assert guard.rstrip().endswith("end $$;\n$fn$;")
    assert part_a.count("do $guard$") == 1


def test_widened_feedback_check_is_in_part_b_only(part_a, part_b):
    assert "assistant_inbox_feedback_check" not in _code(part_a)
    code = _code(part_b)
    assert "position('''mute_project''' in v_def) > 0 then return;" in code
    assert "check (feedback in ('useful', 'not_useful', 'mute', 'mute_project')) not valid" in code
    assert "validate constraint assistant_inbox_feedback_check" in code


def test_snooze_limit_and_its_own_bell(part_a):
    fn = _fn(part_a, "assistant_inbox_snooze")
    assert "v_row.snooze_count >= 3" in fn and "'snooze_limit'" in fn
    assert "deliver_after = p_next_slot" in fn and "precedence = 'snoozed'" in fn
    assert "n.id = v_row.notification_id" in fn
    # The row lets go of its old (now read) bell, so it returns unread (10.1: a router
    # row is read when read_at is set or its own bell is read).
    upd = re.search(r"update public\.assistant_inbox i\s+set (.+?)\s+where i\.id = v_row\.id;", fn, re.S).group(1)
    assert "notification_id = null" in upd and "read_at = null" in upd
    assert fn.index("update public.assistant_inbox i") < fn.index("update public.notifications n")


def test_digest_attach_never_sets_a_bell_on_the_row(part_a):
    """A snoozed row re-attached by A11.4 keeps notification_id null: the digest bell
    lives on the digest ledger, never on the row, so the row stays unread."""
    fn = _fn(part_a, "assistant_digest_deliver")
    attach = re.search(r"update public\.assistant_inbox i set (.+?)\n   where", fn, re.S).group(1)
    assert attach.strip() == "digest_id = v_id, delivered_at = p_now"


def test_subscription_set_accepts_urgent_on_role_rows_only(part_a):
    fn = _fn(part_a, "assistant_subscription_set")
    assert "(p_severity = 'urgent' and (p_role is null" in fn
    m = re.search(r"or p_event_type not in \(([^)]*)\)\)\) then", fn)
    assert m and set(re.findall(r"'([a-z_.]+)'", m.group(1))) == {
        "bid.due_not_ready", "todo.due", "assistant.approval_expiring"}
    assert "'invalid_mode'" in fn and "'bad_target'" in fn
    assert "on conflict (role, event_type) where user_id is null" in fn
    assert "on conflict (user_id, event_type) where role is null" in fn


def test_feedback_report_is_counts_only_with_a_floor(part_a):
    fn = _fn(part_a, "assistant_inbox_feedback_report")
    assert "greatest(3, coalesce(p_floor, 3))" in fn
    assert "i.delivery <> 'direct'" in fn
    assert "'suppressed', true" in fn
    assert "count(distinct r.user_id)" in fn
    assert "'user_id'" not in fn and "'email'" not in fn
    assert "stable" in fn


def test_feedback_report_complementary_suppression(part_a):
    """Roles of 4 and 1 users: the 1-user cell is below the floor, and the kind total
    (5) minus the visible 4-user cell would give it back exactly, so the role
    breakdown is withheld whenever the hidden cells sum to 1 to floor - 1 users."""
    fn = _fn(part_a, "assistant_inbox_feedback_report")
    assert ("(select coalesce(sum(c.users), 0) from cells c\n"
            "                       where c.event_type = t0.event_type and c.role is not null and c.users < v_floor) "
            "as hidden") in fn
    assert "'roles_suppressed', t.hidden between 1 and v_floor - 1," in fn
    assert "'roles', case when t.hidden between 1 and v_floor - 1 then '[]'::jsonb else coalesce((" in fn
    # The rule on the 4 and 1 example, and on cases that keep the breakdown.
    def withheld(role_users: list[int], floor: int = 3) -> bool:
        hidden = sum(u for u in role_users if u < floor)
        return 1 <= hidden <= floor - 1
    assert withheld([4, 1])
    assert withheld([4, 1, 1])
    assert not withheld([4, 3])
    assert not withheld([4, 2, 2])  # the hidden pair covers 4 users, at or above the floor


def test_link_bell_two_bell_order_and_the_documented_limit(part_a):
    fn = _fn(part_a, "assistant_inbox_link_bell")
    assert "p_dedupe_key not like 'p7:bell:%'" in fn
    assert "on conflict (user_id, dedupe_key) do nothing" in fn
    assert "v_later := p_rank = v_rank and p_bell_at > v_at + interval '120 seconds';" in fn
    assert "(p_bell_at, p_notification_id) < (v_at, v_row.source_notification_id)" in fn
    assert "limit 20" in fn and "jsonb_array_length(v_linked) < 20" in fn
    assert "created_at = case when v_later then p_bell_at else i.created_at end" in fn
    assert "v_row.delivery <> 'record'" in fn
    assert "delete" not in fn
    # Order independence is claimed for two bells only; the three-bell chain is recorded.
    head = part_a[part_a.index("-- A11.11"):part_a.index("create or replace function public.assistant_inbox_link_bell(")]
    assert "The final row is the same in any" not in head
    assert "order independent for two bells only" in head


def test_send_claim_retires_stale_claims_as_uncertain(part_a):
    fn = _fn(part_a, "assistant_external_sends_claim")
    retire = fn.index("set status = 'uncertain', error_kind = 'lease_expired', claimed_by = null")
    assert retire < fn.index("for update skip locked")
    assert "s.status = 'pending' and s.not_before <= p_now" in fn
    assert "least(coalesce(p_limit, 1), 50)" in fn
    assert "attempts = s.attempts + 1" in fn


def test_send_settle_branches(part_a):
    fn = _fn(part_a, "assistant_external_send_settle")
    assert "s.id = p_id and s.claimed_by = p_worker and s.status = 'claimed'" in fn
    assert ("p_outcome not in ('sent', 'transient', 'permanent', 'uncertain', 'skipped', 'deferred')") in fn
    assert "p_error_kind not in ('channel_off', 'opted_out', 'stale', 'offboarded')" in fn
    # An unknown error kind raises on every outcome, not only on skipped.
    m = re.search(r"or \(p_error_kind is not null and p_error_kind not in \(([^)]*)\)\)", fn)
    assert m and set(re.findall(r"'([a-z_]+)'", m.group(1))) == SEND_ERROR_KINDS
    assert "raise exception 'invalid_outcome'" in fn
    assert "when 'transient' then case when s.attempts >= 5 then 'failed' else 'pending' end" in fn
    assert "attempts = case when p_outcome = 'deferred' then greatest(0, s.attempts - 1) else s.attempts end" in fn
    assert "else 'quiet_hours' end" in fn
    assert "updated_at = p_now" in fn


def test_fold_takes_the_digest_lock_and_the_held_definition(part_a):
    fn = _fn(part_a, "assistant_inbox_fold")
    assert "pg_advisory_xact_lock(hashtext('assistant_inbox_deliver:' || p_user_id::text))" in fn
    assert "v_row.severity = 'high'" in fn
    assert "coalesce(v_row.event_type, '') <> 'assistant.approval_expiring'" in fn
    assert "v_row.deliver_after > v_row.created_at" in fn
    for result in ("'folded'", "'deliver'", "'wait'", "'none'"):
        assert f"return {result};" in fn


def test_purge_respects_legal_hold_direct_rows_and_open_sends(part_b):
    fn = _fn(part_b, "assistant_proactive_purge")
    assert fn.count("ap.legal_hold") >= 6
    assert "i.delivery in ('immediate', 'digest', 'record')" in fn
    assert "s.status in ('sent', 'failed', 'skipped', 'uncertain')" in fn
    assert "assistant_router_cursors" not in fn
    assert "limit v_limit" in fn
    assert "'declined', 'bid_outcome', 'pm_only', 'cp_only'" in fn
    # A table lock, so a hold that inserts the person's first profile row waits too.
    assert "lock table public.assistant_profiles in share mode;" in fn
    assert "for share" not in fn
    assert fn.index("lock table public.assistant_profiles") < fn.index("with doomed as")


def test_reset_needs_exactly_one_target(part_b):
    fn = _fn(part_b, "assistant_subscription_reset")
    assert "(p_role is null) = (p_user_id is null)" in fn
    assert "returns boolean" in fn


# --- names and parse ---------------------------------------------------------------------


_INDEX_RE = re.compile(r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?([a-z0-9_]+)",
                       re.I)
_CONSTRAINT_RE = re.compile(r"\bconstraint\s+([a-z0-9_]+)", re.I)


def test_index_and_constraint_names_are_unique_across_migrations(sql):
    code = _code(sql)
    ours_idx = set(_INDEX_RE.findall(code))
    ours_con = set(_CONSTRAINT_RE.findall(code)) - {"assistant_inbox_feedback_check"}
    assert "notifications_created_at_idx" in ours_idx
    others: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("0*.sql")):
        if path.name == FILE:
            continue
        other = _code(path.read_text(encoding="utf-8"))
        for name in _INDEX_RE.findall(other) + _CONSTRAINT_RE.findall(other):
            others.setdefault(name.lower(), path.name)
    clashes = {n: others[n] for n in ours_idx | ours_con if n.lower() in others}
    assert not clashes, f"0190 names already used: {clashes}"


def test_every_statement_parses():
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(SQL_PATH.read_text(encoding="utf-8"))

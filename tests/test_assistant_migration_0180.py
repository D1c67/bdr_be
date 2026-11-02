"""Migration 0180 (docs/ASSISTANT_PHASE4_PLAN.md, section 2) and the rest of
WI-1: plans notes with hybrid search, the evidence access ledger, report
files, the reason-carrying decide RPC, memory search, evidence indexes, the
two AE spend hold kinds, assistant_messages.server_data, and (part B) the
recipe, spend kind, task kind and sweep step widenings plus the ledger
triggers. Also EVIDENCE_ROLES, the phase 4 config ranges and the WI-1 stubs.

Static checks on the SQL text. PHASE4_TABLES is exported for other tests
(every new table must have forced RLS and revoked privileges)."""

from __future__ import annotations

import importlib
import inspect
import re
import sys
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, EVIDENCE_ROLES, Role

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0180_assistant_phase4.sql"
SQL_PATH = MIGRATIONS / FILE
MARKER = "-- ==== PART B (owner at the screen) ===="

PHASE4_TABLES = ("plans_reader_notes", "assistant_evidence_access", "assistant_reports")

FUNCTIONS = (
    "search_plans_notes(text, uuid[], text[], text, int)",
    "assistant_approval_reason_once()",
    "assistant_approval_decide_with_reason(uuid, uuid, text, jsonb, text, text)",
    "assistant_memory_search(uuid, text, text[], text, int)",
    "assistant_spend_state(uuid, numeric, numeric)",
    "assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid)",
)

# The two statements A8 adds to the 0174 spend state, right after the
# v_global_spent select (plan section 2, A8).
SPEND_STATE_ADDED = """  v_user_spent := v_user_spent + coalesce((select sum(r.actual_usd)
    from public.assistant_spend_reservations r
   where r.user_id = p_user_id and r.kind in ('plans_pages', 'ai_estimate')
     and r.status in ('settled', 'released') and r.actual_usd is not null
     and r.settled_at >= v_since), 0);
  v_global_spent := v_global_spent + coalesce((select sum(r.actual_usd)
    from public.assistant_spend_reservations r
   where r.kind in ('plans_pages', 'ai_estimate')
     and r.status in ('settled', 'released') and r.actual_usd is not null
     and r.settled_at >= v_since), 0);
"""


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
    m = re.search(rf"create or replace function public\.{name}\(.+?\n\$\$;", sql, re.S)
    assert m, name
    return m.group(0)


def _statements(code: str) -> list[str]:
    return [s.strip() for s in re.split(r";[ \t]*(?:\n|\Z)", code) if s.strip()]


def _sql_of(number: str) -> str:
    return next(MIGRATIONS.glob(f"{number}_*.sql")).read_text(encoding="utf-8")


# --- file shape --------------------------------------------------------------------------


def test_exactly_one_0180_file_and_the_marker_once(sql):
    assert len(list(MIGRATIONS.glob("0180_*.sql"))) == 1
    assert sql.count(MARKER) == 1
    assert sql.count("-- ==== PART A ====") == 1
    assert sql.index("-- ==== PART A ====") < sql.index(MARKER)


def test_header_notify_last_and_no_dash_characters(sql, part_a, part_b):
    assert sql.startswith("-- 0180: ")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    assert "\\u2014" not in sql
    for part in (part_a, part_b):
        assert _statements(_code(part))[-1] == "notify pgrst, 'reload schema'"


def test_parts_stay_under_40_kb(part_a, part_b):
    assert len(part_a.encode()) < 40_000
    assert len(part_b.encode()) < 40_000


def test_no_top_level_drop_anywhere(sql):
    code = _code(sql)
    assert not re.search(r"^\s*drop\s", code, re.M | re.I)
    assert not re.search(r"\bdrop\s+(table|function|trigger|index|policy|column|view|type)\b", code, re.I)
    for stmt in _statements(code):
        assert not re.match(r"(drop|truncate|delete)\b", stmt, re.I), stmt[:80]
    # The four check swaps run through execute inside do blocks.
    drops = [line.strip() for line in code.splitlines() if re.search(r"\bdrop\s+constraint\b", line, re.I)]
    assert len(drops) == 4
    assert all(line.startswith("execute format('alter table public.") for line in drops)


def test_part_a_holds_no_destructive_statement(part_a):
    """Matched as statements and phrases, so `on delete cascade` and comments
    pass (0176 part A carried a `before delete` binding the same way)."""
    code = _code(part_a)
    assert not re.search(r"\bdelete\s+from\b", code, re.I)
    assert not re.search(r"\btruncate\b", code, re.I)
    assert not re.search(r"\bdrop\b", code, re.I)
    assert not re.search(r"\bbefore\s+delete\b", code, re.I)
    for stmt in _statements(code):
        assert not re.match(r"(drop|delete|truncate)\b", stmt, re.I), stmt[:80]
    # The only delete text is the two FK referential actions.
    assert len(re.findall(r"\bdelete\b", code, re.I)) == 2
    assert code.count("on delete cascade") == 2
    # Part A comments carry no destructive words either (the connector scans
    # the whole body).
    comments = "\n".join(line.split("--", 1)[1] for line in part_a.splitlines() if "--" in line)
    assert not re.search(r"\b(drop|delete|truncate)\b", comments, re.I)


def test_part_b_holds_the_swaps_and_the_ledger_bindings(part_b):
    code = _code(part_b)
    assert "create table" not in code and "create or replace function" not in code
    assert code.count("execute format('alter table public.") == 4
    assert code.count("create or replace trigger assistant_evidence_access_") == 3


# --- tables ------------------------------------------------------------------------------


def test_every_created_table_is_listed(sql):
    created = set(re.findall(r"create table if not exists public\.([a-z_]+)", sql))
    assert created == set(PHASE4_TABLES)


@pytest.mark.parametrize("table", PHASE4_TABLES)
def test_new_table_has_forced_rls_and_revokes(part_a, table):
    assert f"alter table public.{table} enable row level security;" in part_a
    assert f"alter table public.{table} force row level security;" in part_a
    assert f"revoke all on table public.{table} from anon, authenticated;" in part_a


def test_no_policy_anywhere(sql):
    assert "create policy" not in _code(sql).lower()


def _table_body(sql: str, table: str) -> str:
    m = re.search(rf"create table if not exists public\.{table} \((.+?)\n\);", sql, re.S)
    assert m, table
    return m.group(1)


def test_notes_key_is_a_plain_unique_constraint(part_a):
    code = _code(part_a)
    m = re.search(r"add constraint plans_reader_notes_key_uq\s+unique \(([^)]*)\);", code)
    assert m
    assert [c.strip() for c in m.group(1).split(",")] == [
        "run_id", "source_scope", "category", "source_version", "source_key"]
    assert "if not exists (select 1 from pg_constraint where conname = 'plans_reader_notes_key_uq') then" in code
    # No expression key: PostgREST on_conflict could not name it (42P10).
    assert "coalesce(category" not in code
    body = _table_body(part_a, "plans_reader_notes")
    assert "category        text not null default ''" in body
    assert "references public.plans_reader_runs(id) on delete cascade" in body
    assert "project_id" not in body


def test_notes_kinds_and_search_indexes(part_a):
    body = _table_body(part_a, "plans_reader_notes")
    kinds = set(re.findall(r"'([a-z_]+)'", body[body.index("kind            text"):body.index("document_id")]))
    assert kinds == {
        "category_summary", "page_note", "scope", "spec", "schedule", "detail", "question", "brief_summary",
        "system", "assumption", "exclusion", "risk", "rfi", "missing_document", "other_trade", "sheet_summary",
        "spec_highlight", "material_note", "confidence_note"}
    assert "check (source_scope in ('category', 'main', 'brief_deliverable'))" in body
    assert "check (char_length(body) between 1 and 4000)" in body
    assert "tsvector generated always as (" in body
    code = _code(part_a)
    assert "plans_reader_notes using gin (tsv);" in code
    assert "plans_reader_notes using gin (body gin_trgm_ops);" in code
    assert "using gin (sheet_number gin_trgm_ops) where sheet_number is not null;" in code


def test_evidence_access_shape(part_a):
    body = _table_body(part_a, "assistant_evidence_access")
    assert "references" not in body                        # the ledger outlives the person
    assert "reason            text not null check (char_length(btrim(reason)) between 10 and 500)" in body
    assert "approval_id       uuid not null" in body
    assert "check (tool in ('person_evidence', 'estimator_timeline'))" in body
    assert ("check (view in ('activity', 'file_access', 'notifications', 'ai_usage', 'estimator_timeline'))"
            in body)
    assert ("create unique index if not exists assistant_evidence_access_approval_uq\n"
            "  on public.assistant_evidence_access (approval_id);") in part_a


def test_evidence_ledger_triggers_refuse_every_change(part_b):
    code = _code(part_b)
    for op, scope in (("update", "row"), ("delete", "row"), ("truncate", "statement")):
        pattern = (rf"create or replace trigger assistant_evidence_access_no_{op} before {op} on "
                   rf"public\.assistant_evidence_access\s+for each {scope} execute function "
                   rf"public\.assistant_refuse_change\(\);")
        assert re.search(pattern, code), op
    assert "allow_cascade" not in code


REPORT_PATH_CHECK = "check (storage_path = 'assistant-reports/' || user_id::text || '/' || id::text || '.xlsx')"


def _path_ok(storage_path: str, user_id: str, report_id: str) -> bool:
    """The A4 check, replayed: the concatenation in REPORT_PATH_CHECK."""
    return storage_path == "assistant-reports/" + user_id + "/" + report_id + ".xlsx"


def test_reports_shape_and_path_check(part_a):
    body = _table_body(part_a, "assistant_reports")
    assert "constraint assistant_reports_path_chk\n    " + REPORT_PATH_CHECK in body
    assert "constraint assistant_reports_purged_chk check ((status = 'purged') = (purged_at is not null))" in body
    assert "references public.profiles(id) on delete cascade" in body
    specs = set(re.findall(r"'([a-z_]+)'", body[body.index("spec            text"):body.index("params")]))
    assert specs == {"bid_invitations", "quote_readiness", "estimator_vs_bid", "project_timeline",
                     "project_file_access", "change_history", "ai_usage", "person_evidence"}
    uid, rid = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    other = "33333333-3333-3333-3333-333333333333"
    assert _path_ok(f"assistant-reports/{uid}/{rid}.xlsx", uid, rid)
    for bad in (f"assistant-reports/{other}/{rid}.xlsx", f"assistant-reports/{uid}/{other}.xlsx",
                f"{uid}/{rid}.xlsx", f"assistant-reports/{uid}/../{other}/{rid}.xlsx",
                f"assistant-reports/{uid}/{rid}.xlsx.exe", f"Assistant-reports/{uid}/{rid}.xlsx"):
        assert not _path_ok(bad, uid, rid), bad
    assert "create unique index if not exists assistant_reports_approval_uq" in part_a


def test_report_filename_pattern(part_a):
    m = re.search(r"check \(filename ~ '([^']+)'\)", part_a)
    assert m
    src = m.group(1)
    assert src.endswith("$")
    # Postgres `$` is end of string; Python's also matches before a final \n.
    pat = re.compile(src[:-1] + r"\Z")
    for ok in ("Bid Invitations (week of 10-19-2026).xlsx", "a.xlsx", "report_1.2.xlsx"):
        assert pat.search(ok), ok
    for bad in ("a/b.xlsx", "x.xls", ".." + "/x.xlsx", "x" * 121 + ".xlsx", "x" + chr(0x2014) + ".xlsx",
                "x.xlsx\n", "x.csv"):
        assert not pat.search(bad), bad


@pytest.mark.parametrize("column", [
    "public.assistant_approvals add column if not exists decision_reason text",
    "public.assistant_messages add column if not exists server_data boolean not null default false",
])
def test_added_columns_are_idempotent(part_a, column):
    assert f"alter table {column};" in _code(part_a)


def test_decision_reason_check_is_guarded_and_validated(part_a):
    code = _code(part_a)
    assert ("if not exists (select 1 from pg_constraint where conname = "
            "'assistant_approvals_decision_reason_chk') then") in code
    assert ("check (decision_reason is null or char_length(btrim(decision_reason)) between 10 and 500) "
            "not valid;") in code
    assert "alter table public.assistant_approvals validate constraint assistant_approvals_decision_reason_chk;" \
        in code


def test_reason_is_set_once(part_a):
    fn = _fn(part_a, "assistant_approval_reason_once")
    assert "old.decision_reason is not null and new.decision_reason is distinct from old.decision_reason" in fn
    assert "errcode = 'restrict_violation'" in fn
    assert re.search(r"create or replace trigger assistant_approvals_reason_once before update on "
                     r"public\.assistant_approvals\s+for each row execute function "
                     r"public\.assistant_approval_reason_once\(\);", part_a)


# --- functions ---------------------------------------------------------------------------


@pytest.mark.parametrize("sig", FUNCTIONS)
def test_functions_are_service_role_only(part_a, sig):
    assert f"revoke all on function public.{sig} from public;" in part_a
    assert f"revoke all on function public.{sig} from anon, authenticated;" in part_a
    assert f"grant execute on function public.{sig} to service_role;" in part_a
    name = sig.split("(", 1)[0]
    grants = re.findall(rf"grant execute on function public\.{name}\([^)]*\) to ([a-z_, ]+);", part_a)
    assert grants == ["service_role"]


def test_every_function_is_invoker_pinned_and_listed(sql, part_b):
    names = re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
    assert set(names) == {sig.split("(", 1)[0] for sig in FUNCTIONS}
    assert len(names) == len(FUNCTIONS)
    assert "create or replace function" not in part_b
    for name in names:
        fn = _fn(sql, name)
        assert "security invoker" in fn, name
        assert re.search(r"set search_path = pg_catalog, public(, extensions)?\n", fn), name
        assert "security definer" not in fn, name


def test_search_plans_notes_is_hybrid_and_bounded(part_a):
    fn = _fn(part_a, "search_plans_notes")
    assert "language sql" in fn and "stable" in fn
    assert "set search_path = pg_catalog, public, extensions" in fn
    assert "websearch_to_tsquery('english'::regconfig, coalesce(p_q, ''))" in fn
    assert "word_similarity(q.raw, lower(n.body))" in fn
    assert "q.raw <% lower(n.body)" in fn
    assert "where n.run_id = any(p_run_ids)" in fn
    assert "limit greatest(1, least(coalesce(p_limit, 30), 50));" in fn


def test_memory_search_reads_own_live_rows_only(part_a):
    fn = _fn(part_a, "assistant_memory_search")
    assert "set search_path = pg_catalog, public, extensions" in fn
    assert "where m.user_id = p_user_id" in fn
    assert "m.status <> 'archived'" in fn
    assert "array['active', 'pending_review']" in fn
    assert "limit greatest(1, least(coalesce(p_limit, 10), 25));" in fn


def test_decide_with_reason_is_the_0172_cas_plus_the_reason(part_a):
    fn = _fn(part_a, "assistant_approval_decide_with_reason")
    old = _fn(_sql_of("0172"), "assistant_approval_decide")
    # The reason gate runs before any write.
    assert fn.index("char_length(v_reason) < 10 or char_length(v_reason) > 500") < fn.index("update ")
    assert "'reason', 'reason_invalid'" in fn
    assert "v_reason text := btrim(coalesce(p_reason, ''));" in fn
    # One update, the approve CAS of 0172 with the reason.
    assert fn.count("update public.assistant_approvals") == 1
    assert "decision_reason = v_reason" in fn
    for pred in ("a.id = p_approval_id", "a.user_id = p_user_id", "a.status = 'pending'",
                 "a.expires_at > now()", "a.payload_sha256 = p_payload_sha256",
                 "a.precondition = p_precondition"):
        assert pred in fn and pred in old, pred
    assert "decided_session_aal = p_session_aal" in fn
    # The fall-through: the 0172 classification with a hash that cannot match.
    assert ("return public.assistant_approval_decide(p_user_id, p_approval_id, 'approve', repeat('0', 64),\n"
            "                                          p_precondition, p_session_aal);") in fn
    assert "'expired'" in old and "'superseded'" in old and "'not_pending'" in old and "'stale'" in old


def _reason_ok(fn: str, reason: str | None) -> bool:
    """The RPC's reason gate, replayed from its own bounds."""
    low, high = (int(x) for x in re.search(
        r"char_length\(v_reason\) < (\d+) or char_length\(v_reason\) > (\d+)", fn).groups())
    v = (reason or "").strip()
    return low <= len(v) <= high


def test_reason_gate_refuses_nine_characters(part_a):
    fn = _fn(part_a, "assistant_approval_decide_with_reason")
    assert not _reason_ok(fn, "123456789")
    assert not _reason_ok(fn, "   123456789   ")
    assert not _reason_ok(fn, None)
    assert _reason_ok(fn, "1234567890")
    assert _reason_ok(fn, "x" * 500)
    assert not _reason_ok(fn, "x" * 501)


# --- spend -------------------------------------------------------------------------------


def test_spend_state_is_0174_plus_exactly_the_two_terms(part_a):
    new = _fn(part_a, "assistant_spend_state")
    old = _fn(_sql_of("0174"), "assistant_spend_state")
    assert new.count(SPEND_STATE_ADDED) == 1
    assert new.replace(SPEND_STATE_ADDED, "") == old
    anchor = "or l.assistant_run_id is not null);\n"
    assert new.index(SPEND_STATE_ADDED) == new.index(anchor) + len(anchor)
    # A released AE hold with an actual counts, like a settled one.
    assert SPEND_STATE_ADDED.count("r.status in ('settled', 'released') and r.actual_usd is not null") == 2


def test_spend_reserve_guard_lists_four_kinds(part_a):
    new = _fn(part_a, "assistant_spend_reserve")
    old = _fn(_sql_of("0174"), "assistant_spend_reserve")
    four = "if p_kind not in ('turn_call', 'split_task', 'plans_pages', 'ai_estimate')"
    assert four in new
    assert new.replace(four, "if p_kind not in ('turn_call', 'split_task')") == old
    assert "p_ttl_seconds > 43200" in new


def test_ae_hold_indexes(part_a):
    code = _code(part_a)
    assert ("create unique index if not exists assistant_spend_reservations_approval_ae_uq\n"
            "  on public.assistant_spend_reservations (approval_id)\n"
            "  where approval_id is not null and kind in ('plans_pages', 'ai_estimate');") in code
    assert ("create unique index if not exists assistant_spend_reservations_task_ae_uq\n"
            "  on public.assistant_spend_reservations (task_id)\n"
            "  where task_id is not null and kind in ('plans_pages', 'ai_estimate');") in code
    assert ("create index if not exists assistant_tasks_open_kind_idx\n"
            "  on public.assistant_tasks (kind, updated_at) where status in ('pending', 'running', "
            "'waiting_external');") in code


@pytest.mark.parametrize("index", [
    "audit_log_entity_idx on public.audit_log (entity, entity_id, created_at desc)",
    "audit_log_created_idx on public.audit_log (created_at desc)",
    "notifications_user_created_idx on public.notifications (user_id, created_at desc)",
    "rfp_emails_review_by_idx on public.rfp_emails (review_by) where review_by is not null",
    "estimator_notes_author_created_idx on public.estimator_notes (author_id, created_at desc)",
    "pm_stage_events_actor_idx on public.pm_stage_events (actor_id, entered_at desc)",
])
def test_evidence_indexes(part_a, index):
    assert f"create index if not exists {index};" in _code(part_a)


# --- part B widenings --------------------------------------------------------------------


@pytest.mark.parametrize("table,match,name,values", [
    ("assistant_schedules", "labor_not_entered", "assistant_schedules_recipe_chk",
     {"due_today", "due_this_week", "missing_quotes", "labor_not_entered", "estimator_vs_bid",
      "quote_readiness"}),
    ("assistant_spend_reservations", "split_task", "assistant_spend_reservations_kind_chk",
     {"turn_call", "split_task", "plans_pages", "ai_estimate"}),
    ("assistant_tasks", "split_into_project", "assistant_tasks_kind_chk",
     {"split_into_project", "instruction", "plans_read_pages", "ai_estimating_run"}),
    ("assistant_sweeps", "orphans", "assistant_sweeps_step_chk", {"nightly", "orphans", "report_purge"}),
])
def test_part_b_widening_reads_the_live_definition(part_b, table, match, name, values):
    code = _code(part_b)
    blocks = [b for b in code.split("do $$")[1:] if f"'public.{table}'::regclass" in b]
    assert len(blocks) == 1, table
    block = blocks[0]
    assert f"pg_get_constraintdef(c.oid) like '%{match}%'" in block
    assert "c.contype = 'c'" in block
    assert f"execute format('alter table public.{table} drop constraint %I', v_name);" in block
    m = re.search(rf"add constraint {name}\s+check \([a-z_]+ in \((.+?)\)\) not valid;", block, re.S)
    assert m, name
    assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == values
    assert f"alter table public.{table} validate constraint {name};" in block
    # Idempotent: a second run finds the new values and returns before the swap.
    assert block.index("then return; end if;") < block.index("execute format(")


def test_widenings_keep_every_older_value(part_b):
    old_recipes = set(re.findall(r"'([a-z_]+)'", re.search(r"recipe\s+text not null check \(recipe in \(([^)]*)\)",
                                                           _sql_of("0170")).group(1)))
    old_tasks = set(re.findall(r"'([a-z_]+)'", re.search(r"kind\s+text not null check \(kind in "
                                                         r"\('split_into_project'[^)]*\)", _sql_of("0170")).group(0)))
    old_kinds = set(re.findall(r"'([a-z_]+)'", re.search(r"kind\s+text not null check \(kind in \(([^)]*)\)",
                                                         _sql_of("0174")).group(1)))
    old_steps = set(re.findall(r"'([a-z_]+)'", re.search(r"step\s+text not null check \(step in \(([^)]*)\)",
                                                         _sql_of("0176")).group(1)))
    code = _code(part_b)
    for old, name in ((old_recipes, "assistant_schedules_recipe_chk"), (old_tasks, "assistant_tasks_kind_chk"),
                      (old_kinds, "assistant_spend_reservations_kind_chk"),
                      (old_steps, "assistant_sweeps_step_chk")):
        m = re.search(rf"add constraint {name}\s+check \([a-z_]+ in \((.+?)\)\) not valid;", code, re.S)
        assert old and old <= set(re.findall(r"'([a-z_]+)'", m.group(1))), name


def test_llm_jobs_constraint_is_never_relisted(sql):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        checker = importlib.import_module("check_llm_jobs_constraint_relists")
    finally:
        sys.path.pop(0)
    assert not checker.relists_full_list(sql)
    assert "llm_jobs_job_type_check" not in _code(sql)
    report = checker.scan(MIGRATIONS)
    assert report.ok, report.violations


# --- roles and config --------------------------------------------------------------------


def test_evidence_roles_are_pinned_and_inside_the_bid_viewers():
    assert EVIDENCE_ROLES == frozenset({Role.EXECUTIVE, Role.IT_ADMIN})
    assert ACTUAL_BID_VIEWER_ROLES == frozenset(
        {Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN, Role.ACCOUNTANT})
    assert EVIDENCE_ROLES <= ACTUAL_BID_VIEWER_ROLES


SECRET = "phase4-test-assistant-secret-0123456789abcdef"


def _s(**over) -> Settings:
    base = dict(_env_file=None, openai_api_key="o-key", anthropic_api_key="a-key",
                assistant_enabled=True, assistant_token_secret=SECRET)
    base.update(over)
    return Settings(**base)


def test_phase4_config_defaults():
    s = _s()
    assert (s.assistant_plans_pages_per_card, s.assistant_plans_context_tokens, s.assistant_plans_hold_factor,
            s.assistant_plans_page_read_timeout_minutes) == (6, 24000, 1.5, 120)
    assert (s.assistant_ae_run_hold_ttl_seconds, s.assistant_ae_run_max_files,
            s.assistant_ae_run_max_pages) == (43200, 15, 400)
    assert (s.assistant_report_ttl_days, s.assistant_report_max_rows) == (7, 20000)
    assert (s.assistant_evidence_default_days, s.assistant_evidence_max_days) == (90, 400)
    assert s.assistant_cp_small_cell_min == 3


@pytest.mark.parametrize("over,name", [
    (dict(assistant_ae_run_hold_ttl_seconds=43201), "ASSISTANT_AE_RUN_HOLD_TTL_SECONDS"),
    (dict(assistant_ae_run_hold_ttl_seconds=29), "ASSISTANT_AE_RUN_HOLD_TTL_SECONDS"),
    (dict(assistant_plans_pages_per_card=13), "ASSISTANT_PLANS_PAGES_PER_CARD"),
    (dict(assistant_plans_pages_per_card=0), "ASSISTANT_PLANS_PAGES_PER_CARD"),
    (dict(assistant_plans_hold_factor=0.9), "ASSISTANT_PLANS_HOLD_FACTOR"),
    (dict(assistant_plans_hold_factor=3.5), "ASSISTANT_PLANS_HOLD_FACTOR"),
    (dict(assistant_ae_run_max_files=41), "ASSISTANT_AE_RUN_MAX_FILES"),
    (dict(assistant_ae_run_max_pages=1501), "ASSISTANT_AE_RUN_MAX_PAGES"),
    (dict(assistant_report_ttl_days=31), "ASSISTANT_REPORT_TTL_DAYS"),
    (dict(assistant_report_ttl_days=0), "ASSISTANT_REPORT_TTL_DAYS"),
    (dict(assistant_report_max_rows=20001), "ASSISTANT_REPORT_MAX_ROWS"),
    (dict(assistant_evidence_max_days=401), "ASSISTANT_EVIDENCE_MAX_DAYS"),
    (dict(assistant_evidence_default_days=120, assistant_evidence_max_days=100),
     "ASSISTANT_EVIDENCE_DEFAULT_DAYS"),
    (dict(assistant_cp_small_cell_min=2), "ASSISTANT_CP_SMALL_CELL_MIN"),
])
def test_phase4_config_ranges(over, name):
    with pytest.raises(ValueError, match=name):
        _s(**over)


def test_phase4_config_is_silent_while_the_assistant_is_off():
    s = _s(assistant_enabled=False, assistant_ae_run_hold_ttl_seconds=99999)
    assert s.assistant_enabled is False


# --- WI-1 stubs --------------------------------------------------------------------------

STUBS = {
    "app.services.plans_reader_notes": {
        "sync_run": (["sb", "run_id"], {}),
        "notes_behind": (["sb", "run_ids"], {}),
        "stale_files": (["sb", "project_id", "run"], {}),
    },
    "app.services.assistant_ai_estimate": {
        "page_read_estimate": (["sb", "run", "document_id", "pages", "user_id"], {}),
        "run_preview": (["sb", "project_id", "file_ids", "user_id"], {}),
        "run_state": (["sb", "run_id"], {}),
        "chat_message_state": (["sb", "message_id", "viewer_role"], {"viewer_role": "kw"}),
    },
    "app.services.evidence": {
        "pt": (["value"], {}),
        "window": (["since", "until"], {}),
        "project_timeline": (["sb", "project_id", "role", "since", "until", "sources", "limit", "cursor"], {}),
        "project_opens": (["sb", "project_id"], {}),
        "change_history": (["sb", "project_id", "field", "since", "until", "limit"], {}),
        "ai_usage": (["sb", "project_id", "since", "until", "group_by"], {}),
        "user_subject": (["sb", "user_id"], {}),
        "estimator_subjects": (["sb", "project_id"], {}),
        "estimator_timeline": (["sb", "project_id", "assignment_id"], {}),
        "person_evidence": (["sb", "user_id", "view", "since", "until", "project_id", "limit", "cursor"], {}),
    },
    "app.services.assistant_report_xlsx": {
        "build_workbook": (["title", "banner", "sheets"], {}),
    },
    "app.services.assistant_reports": {
        "build": (["sb", "user", "spec", "params", "conversation_id", "run_id", "approval_id"],
                  {"conversation_id": "kw", "run_id": "kw", "approval_id": "kw"}),
        "build_person_report": (["sb", "user", "subject", "view", "result", "conversation_id", "run_id",
                                 "approval_id"], {}),
    },
    "app.services.pm_aggregates": {
        "open_rfis": (["sb", "limit"], {}),
        "billing_summary": (["sb"], {}),
    },
    "app.services.cp_summary": {
        "week_summary": (["sb", "week_start"], {}),
    },
    "app.assistant.find": {
        "resolve": (["query", "specs", "limit"], {}),
        "catalog_line": (["specs"], {}),
    },
    "app.assistant.evidence_delivery": {
        "post_result": (["user_id", "row", "spec", "payload", "executor"], {}),
    },
    "app.assistant.plans_tasks": {
        "advance": (["user_id", "task"], {}),
    },
}


@pytest.mark.parametrize("module_name", sorted(STUBS))
def test_stub_signatures_and_not_implemented(module_name):
    module = importlib.import_module(module_name)
    for name, (params, kinds) in STUBS[module_name].items():
        fn = getattr(module, name)
        sig = inspect.signature(fn)
        # Owners may append keyword-only parameters with defaults, never
        # rename or reorder the fixed ones.
        assert list(sig.parameters)[:len(params)] == params, (module_name, name)
        extra = list(sig.parameters.values())[len(params):]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is not p.empty
                   for p in extra), (module_name, name)
        for pname, kind in kinds.items():
            assert sig.parameters[pname].kind is inspect.Parameter.KEYWORD_ONLY, (name, pname)
        args = [None] * sum(1 for p in sig.parameters.values()
                            if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD and p.default is p.empty)
        kw = {p.name: None for p in sig.parameters.values()
              if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is p.empty}
        # Only a function still carrying its WI-1 stub body must raise; once
        # its owner fills it, the signature checks above still bind.
        if "is filled by WI-" in inspect.getsource(fn):
            with pytest.raises(NotImplementedError):
                fn(*args, **kw)
    text = Path(module.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text
    if module_name.startswith("app.assistant."):
        assert "get_supabase" not in text and "supabase_client" not in text and ".table(" not in text


def test_binding_details_named_by_the_plan():
    from app.assistant import find, plans_tasks
    from app.core.roles import INTERNAL_ROLES
    from app.services import assistant_reports, cp_summary, evidence

    sig = inspect.signature(plans_tasks.advance)
    assert str(sig) == "(user_id: 'str', task: 'dict') -> 'bool'"
    assert inspect.signature(find.resolve).parameters["limit"].default == 6
    assert inspect.signature(assistant_reports.build).parameters["approval_id"].default is None
    assert cp_summary.CP_SMALL_CELL_MIN == 3
    assert evidence.VIEWS == ("activity", "file_access", "notifications", "ai_usage")
    # SPEC_ROLES covers exactly the A4 specs, with the 3.9.2 and 3.9.3 gates.
    body = _table_body(SQL_PATH.read_text(encoding="utf-8"), "assistant_reports")
    specs = set(re.findall(r"'([a-z_]+)'", body[body.index("spec            text"):body.index("params")]))
    assert set(assistant_reports.SPEC_ROLES) == specs
    assert assistant_reports.SPEC_ROLES["bid_invitations"] == INTERNAL_ROLES
    assert assistant_reports.SPEC_ROLES["quote_readiness"] == INTERNAL_ROLES
    assert assistant_reports.SPEC_ROLES["estimator_vs_bid"] == ACTUAL_BID_VIEWER_ROLES
    for spec in ("project_timeline", "project_file_access", "change_history", "ai_usage", "person_evidence"):
        assert assistant_reports.SPEC_ROLES[spec] == EVIDENCE_ROLES

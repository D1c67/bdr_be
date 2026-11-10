"""Migration 0192 (docs/ASSISTANT_BROWSER_V2_PLAN.md section 14, WI-1): the
assistant browser v2 schema, plus the WI-1 config lines and the 15 USD
per-user daily assistant budget (owner answer 2026-11-02, plan 22 question 5).

One file, six ledger pieces split by marker lines (A1 to A5 additive, B with
the owner at the screen). Static checks on the SQL text (14.4); the live
transactional check is scripts/check_assistant_browser_v2.sql.
"""

from __future__ import annotations

import base64
import re
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0192_assistant_browser_v2.sql"
SQL_PATH = MIGRATIONS / FILE
CHECK_PATH = ROOT / "scripts/check_assistant_browser_v2.sql"
PIECES = ("A1", "A2", "A3", "A4", "A5", "B")
MAX_PIECE_BYTES = 35_000

NEW_TABLES = (
    "assistant_browser_sites", "assistant_browser_site_grants", "assistant_browser_admissions",
    "assistant_browser_job_credentials", "assistant_browser_requests", "assistant_browser_holds",
    "assistant_browser_chat_outbox", "assistant_browser_user_safe_rules", "assistant_browser_profile_suggestions",
    "assistant_browser_downloads", "assistant_browser_live_sessions", "assistant_browser_live_frames",
    "assistant_browser_live_input", "assistant_browser_mail_codes_used",
)

# Every function each piece creates, with the exact signature its grants use.
FUNCTIONS: dict[str, tuple[str, ...]] = {
    "A1": (
        "assistant_browser_v2_ttl_guard()",
        "assistant_browser_v2_host(text)", "assistant_browser_v2_origin(text)", "assistant_browser_v2_is_idp(text)",
        "assistant_browser_v2_site_matches(public.assistant_browser_sites, text)",
        "assistant_browser_admission_needs(uuid)", "assistant_browser_admission_status(text[])",
    ),
    "A2": (
        "assistant_browser_site_grant(uuid, text, text, text, text, text, text, uuid, jsonb)",
        "assistant_browser_scope_extend(uuid, uuid, text)", "assistant_browser_admission_create(uuid, jsonb)",
        "assistant_browser_start_decide(uuid, uuid, text)", "assistant_browser_request_create(uuid, jsonb)",
        "assistant_browser_request_decide(uuid, uuid, text)",
        "assistant_browser_task_enqueue_v2(uuid, uuid, jsonb, numeric, numeric)",
        "assistant_browser_credential_put_v2(uuid, text, text, int, jsonb, uuid)",
        "assistant_browser_credential_revoke_v2(uuid, uuid, int)",
    ),
    "A3": (
        "assistant_browser_v2_authority(public.browser_agent_jobs)",
        "assistant_browser_user_fence_v2(uuid, uuid, uuid, bigint)",
        "assistant_browser_user_fence(uuid, uuid, uuid, bigint)", "assistant_browser_job_guard()",
        "assistant_browser_scope_allows(uuid, text)", "assistant_browser_job_policy(uuid, uuid, uuid, bigint)",
        "assistant_browser_login_begin_v2(uuid, uuid, uuid, bigint, uuid, text)",
        "assistant_browser_login_finish_v2(uuid, uuid, uuid, bigint, uuid, uuid, text, jsonb)",
        "assistant_browser_session_get_v2(uuid, uuid, uuid, bigint, text, text, text, timestamptz)",
    ),
    "A4": (
        "assistant_browser_help_open_v2(uuid, uuid, uuid, bigint, uuid, jsonb, text, uuid, uuid)",
        "assistant_browser_help_reply_v2(uuid, uuid, uuid, uuid, text, jsonb)",
        "assistant_browser_help_consume_v2(uuid, uuid, uuid, bigint, uuid, uuid, uuid)",
        "assistant_browser_help_secret_from_mail(uuid, uuid, uuid, text, jsonb)",
        "assistant_browser_hold_open(uuid, uuid, uuid, bigint, uuid, text, jsonb, jsonb, jsonb)",
        "assistant_browser_hold_expire(uuid)",
        "assistant_browser_hold_decide(uuid, uuid, text, text, timestamptz)",
        "assistant_browser_hold_release_get(uuid, uuid, uuid, bigint, uuid)",
        "assistant_browser_hold_consume(uuid, uuid, uuid, bigint, uuid)",
        "assistant_browser_turn_begin_v2(uuid, uuid, uuid, uuid, bigint, int, numeric, int, numeric, numeric)",
        "assistant_browser_job_requeue_v2(uuid, uuid, uuid, bigint, timestamptz, text)",
        "assistant_browser_job_requeue(uuid, uuid, uuid, bigint, timestamptz, text)",
    ),
    "A5": (
        "assistant_browser_sweep_v2(int)", "assistant_browser_safe_rule_add(uuid, uuid, text, text, text, text)",
        "assistant_browser_download_record(uuid, uuid, uuid, bigint, jsonb)",
        "assistant_browser_live_session_open(uuid, uuid, text, text)", "assistant_browser_outbox_drain(int)",
        "assistant_browser_outbox_ack(bigint[])", "assistant_browser_claim_user(uuid, uuid, jsonb, int)",
        "assistant_browser_terminal()", "assistant_browser_inspect(uuid, uuid, uuid, text, text)",
    ),
    "B": (
        "assistant_browser_site_revoke(uuid, uuid, int)", "assistant_browser_session_signout(uuid, uuid)",
        "assistant_browser_safe_rule_remove(uuid, uuid)", "assistant_browser_live_session_close(uuid, uuid, text, jsonb)",
        "assistant_browser_purge_v2(int)", "assistant_browser_revoke_owner(uuid)", "assistant_browser_sweep(int)",
    ),
}
# Applied functions replaced in place (same signature); everything else is new.
REPLACED_IN_PLACE = {
    "assistant_browser_user_fence", "assistant_browser_job_guard", "assistant_browser_job_requeue",
    "assistant_browser_claim_user", "assistant_browser_terminal", "assistant_browser_revoke_owner",
    "assistant_browser_sweep", "assistant_browser_inspect",
}
OLD_HELP_REASONS = ("login_failed", "login_locked", "captcha", "mfa", "sso", "logged_out", "access_denied", "not_found",
                    "ambiguous", "unexpected_page", "cap_reached", "needs_info", "provider_config", "other")
NEW_HELP_REASONS = ("needs_credential", "needs_site", "mfa_step_up", "push", "hold")


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _name(sig: str) -> str:
    return sig.split("(", 1)[0]


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pieces(sql: str) -> dict[str, str]:
    """Piece text from its marker to the next marker. A1 also carries the file
    header (it is applied with A1)."""
    starts = [sql.index(f"-- ==== PART {p} ====") for p in PIECES] + [len(sql)]
    out = {p: sql[starts[i]:starts[i + 1]] for i, p in enumerate(PIECES)}
    out["A1"] = sql[:starts[0]] + out["A1"]
    return out


def _fn(text: str, name: str) -> str:
    m = re.search(rf"create or replace function public\.{name}\(.+?\nend \$\$;", text, re.S)
    assert m, name
    return m.group(0)


def _part_a(pieces: dict[str, str]) -> str:
    return "".join(pieces[p] for p in PIECES[:-1])


# ── File, number and pieces ──────────────────────────────────────────────────


def test_one_0192_file_and_markers_in_order(sql):
    assert len(list(MIGRATIONS.glob("0192_*.sql"))) == 1
    positions = [sql.index(f"-- ==== PART {p} ====") for p in PIECES]
    assert positions == sorted(positions)
    for p in PIECES:
        assert sql.count(f"-- ==== PART {p} ====") == 1


def test_header_names_dev_ledger_pieces(sql):
    head = sql.split("-- ==== PART A1 ====", 1)[0]
    assert head.startswith("-- 0192:")
    assert "Dev project (BDR) only" in head
    for p in PIECES:
        assert f"0192_assistant_browser_v2_{p.lower()}" in head


@pytest.mark.parametrize("piece", PIECES)
def test_each_piece_is_at_most_35_kb_and_reloads_postgrest(pieces, piece):
    text = pieces[piece]
    assert len(text.encode("utf-8")) <= MAX_PIECE_BYTES, piece
    assert text.rstrip().endswith("notify pgrst, 'reload schema';"), piece


def test_no_em_or_en_dash(sql):
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    check = CHECK_PATH.read_text(encoding="utf-8")
    assert chr(0x2014) not in check and chr(0x2013) not in check


# ── Part A is additive; part B holds every destructive statement ────────────


def test_part_a_holds_no_destructive_text(pieces):
    a = _part_a(pieces)
    code = _code(a)
    assert not re.search(r"\bdelete\s+from\b", code, re.I)
    assert not re.search(r"\btruncate\b", code, re.I)
    assert not re.search(r"\bdrop\b", code, re.I)
    assert not re.search(r"\bbefore\s+delete\b", code, re.I)
    deletes = re.findall(r"\bdelete\b", code, re.I)
    actions = re.findall(r"\bon delete (?:cascade|set null)\b", code, re.I)
    assert len(deletes) == len(actions) > 0
    comments = "\n".join(line.split("--", 1)[1] for line in a.splitlines() if "--" in line)
    assert not re.search(r"\b(drop|delete|truncate)\w*\b", comments, re.I)


def test_part_a_revokes_only_its_own_functions_and_new_tables(pieces):
    """Revoke text in part A only closes functions it creates and the new
    tables to anon and authenticated; it never takes a grant from service_role."""
    a = _code(_part_a(pieces))
    created = {_name(sig) for p in PIECES[:-1] for sig in FUNCTIONS[p]}
    for m in re.finditer(r"revoke\s+all\s+on\s+function\s+public\.([a-z0-9_]+)\(", a, re.I):
        assert m.group(1) in created, m.group(1)
    assert not re.search(r"\brevoke\s[^;]*\bservice_role\b", a, re.I)
    assert "revoke all on table public.%I from anon, authenticated" in a


def test_part_b_drops_only_the_named_constraints(pieces):
    code = _code(pieces["B"])
    drops = re.findall(r"drop constraint %I", code)
    assert len(drops) == 5   # B7 (WI-12): the ledger effect check gains server_gated
    assert not re.search(r"\bdrop\s+(table|function|index|trigger|type|column)\b", code, re.I)
    assert len(re.findall(r"alter column \w+ drop not null", code)) == 2
    for lookup in ("pg_get_constraintdef(oid) like '%service%' and pg_get_constraintdef(oid) like '%procore%'",
                   "conname = 'browser_agent_jobs_user_shape'",
                   "pg_get_constraintdef(oid) like '%control_mode%'",
                   "pg_get_constraintdef(oid) like '%reason_code%'",
                   "pg_get_constraintdef(oid) like '%sensitive_read%'"):
        assert lookup in code, lookup
    assert code.count("not found exactly once") == 5


def test_part_b_keeps_the_v1_upsert_targets(pieces):
    """The live v1 routes upsert on UNIQUE (user_id, service) (0189
    credential_put) and UNIQUE (credential_id, origin) (0186 session_put);
    part B never looks them up to drop them."""
    code = _code(pieces["B"])
    assert "UNIQUE (user_id, service)" not in code
    assert "UNIQUE (credential_id, origin)" not in code
    earlier = "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("018[69]_*.sql")))
    assert "on conflict(user_id,service)" in earlier and "on conflict(credential_id,origin)" in earlier


def test_every_row_removal_is_in_part_b(pieces):
    code = _code(pieces["B"])
    for table in ("assistant_browser_sessions", "assistant_browser_user_safe_rules", "assistant_browser_live_frames",
                  "assistant_browser_live_input"):
        assert f"delete from public.{table}" in code, table


def test_help_reason_check_keeps_the_14_and_adds_5(pieces):
    code = _code(pieces["B"])
    m = re.search(r"browser_agent_help_requests_reason_v2 check \(reason_code in\s*\((.+?)\)\)", code, re.S)
    assert m
    values = re.findall(r"'([a-z_]+)'", m.group(1))
    assert values == list(OLD_HELP_REASONS + NEW_HELP_REASONS)


def test_job_shape_v2_allows_exactly_one_authority(pieces):
    code = _code(pieces["B"])
    assert "((assistant_approval_id is not null) <> (assistant_admission_id is not null))" in code
    assert "((user_credential_id is null) = (user_credential_version is null))" in code
    assert "check (control_mode in ('agent','operator') and (control_mode = 'agent' or principal_kind = 'user'))" in code


# ── Functions: security, grants, caps ───────────────────────────────────────


@pytest.mark.parametrize("piece", PIECES)
def test_each_piece_revokes_and_grants_every_function_it_creates(pieces, piece):
    text = pieces[piece]
    created = sorted(set(re.findall(r"create or replace function public\.([a-z0-9_]+)\(", text)))
    assert created == sorted({_name(s) for s in FUNCTIONS[piece]}), piece
    for sig in FUNCTIONS[piece]:
        assert f"revoke all on function public.{sig} from public;" in text, sig
        assert f"revoke all on function public.{sig} from anon, authenticated;" in text, sig
        assert (f"grant execute on function public.{sig} to service_role;" in text
                or f"'{sig}'" in text), sig


def test_every_function_is_invoker_with_pinned_search_path(sql):
    headers = re.findall(r"create or replace function public\.([a-z0-9_]+)\((.*?)\$\$", sql, re.S)
    assert len(headers) == sum(len(v) for v in FUNCTIONS.values())
    for name, head in headers:
        squashed = re.sub(r"\s+", " ", head)
        assert "security invoker" in squashed, name
        assert re.search(r"set search_path ?= ?pg_catalog, ?public", squashed), name
    assert "security definer" not in sql.lower()


def test_new_functions_are_v2_names_or_replaced_in_place(sql):
    earlier = "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("0*.sql")) if p.name < FILE)
    for name in {_name(s) for v in FUNCTIONS.values() for s in v}:
        if name in REPLACED_IN_PLACE:
            assert f"function public.{name}(" in earlier, name
        else:
            assert f"function public.{name}(" not in earlier, name


def test_no_least_2_and_no_hard_coded_8_hours_in_v2(sql):
    code = _code(sql)
    assert "least(2" not in code.replace(" ", "")
    assert "interval '8 hours'" not in code


def test_claim_selects_only_user_browser_and_counts_slots(pieces):
    fn = _fn(pieces["A5"], "assistant_browser_claim_user")
    assert "key = 'user_browser'" in fn and "user_browser_read" not in fn
    assert "exists(select 1 from public.browser_agent_jobs where lease_owner=w.id)" not in fn
    assert ">= w.max_slots" in fn and ">= typ.max_concurrency" in fn and ">= cfg.user_per_owner_concurrency" in fn
    assert "for update skip locked" in fn
    assert "'per_job_cost_cap_usd', cfg.user_per_job_cost_cap_usd" in fn
    assert "v_code := public.assistant_browser_v2_authority(j);" in fn
    assert "continue when public.assistant_browser_v2_authority(j)" not in fn
    assert "status = 'canceled', error_kind = v_code" in fn


def test_enqueue_reserves_one_step_with_one_expiry(pieces):
    fn = _fn(pieces["A2"], "assistant_browser_task_enqueue_v2")
    assert "v_step := least(cfg.user_task_reserve_step_usd, a.max_cost_usd)" in fn
    assert "v_ttl := cfg.user_task_queue_hours * 3600 + typ.max_active_seconds" in fn
    assert "select expires_at into v_exp from public.assistant_spend_reservations" in fn
    assert ">= cfg.user_per_owner_queued" in fn
    assert "p_user_cap_default numeric default 15" in fn
    assert "'user_browser'" in fn and "concurrency_cap" in fn


def test_turn_begin_v2_tops_up_against_user_and_org_caps(pieces):
    fn = _fn(pieces["A4"], "assistant_browser_turn_begin_v2")
    assert "cfg.user_per_job_cost_cap_usd" in fn
    assert "public.browser_agent_spend_today()" in fn
    assert "public.assistant_spend_state(p_user_id" in fn
    assert "set amount_usd = amount_usd + v_step" in fn
    for reason in ("task_cap", "org_daily_cap", "user_cap", "global_cap"):
        assert f'"reason":"{reason}"' in fn


def test_requeue_never_reruns_after_side_effects(pieces):
    fn = _fn(pieces["A4"], "assistant_browser_job_requeue_v2")
    assert "error_kind = 'interrupted_after_action'" in fn
    assert "set status = 'aborted' where job_id = j.id and status = 'open'" in fn
    v1 = _fn(pieces["A4"], "assistant_browser_job_requeue")
    assert "return public.assistant_browser_job_requeue_v2(" in v1


def test_terminal_trigger_writes_outbox_and_aborts(pieces):
    fn = _fn(pieces["A5"], "assistant_browser_terminal")
    assert "insert into public.assistant_browser_chat_outbox" in fn
    assert "update public.assistant_browser_holds set status='aborted'" in fn
    assert "update public.assistant_browser_requests set status='aborted'" in fn


def test_credential_put_v2_uses_registry_origins_and_refuses_idps(pieces):
    fn = _fn(pieces["A2"], "assistant_browser_credential_put_v2")
    assert "v_login := s.login_origins; v_cookie := s.cookie_domains;" in fn
    assert "public.assistant_browser_v2_is_idp(o)" in fn
    assert "attempts_day_count" not in fn
    assert "cancel_credentials" not in fn
    idp = _fn(pieces["A1"], "assistant_browser_v2_is_idp")
    for host in ("login.microsoftonline.com", "login.microsoft.com", "login.live.com", "accounts.google.com",
                 "appleid.apple.com"):
        assert host in idp
    assert "(okta|onelogin|pingone)" in idp


def test_login_v2_checks_job_credential_set_and_daily_cap(pieces):
    begin = _fn(pieces["A3"], "assistant_browser_login_begin_v2")
    assert '"code":"not_in_job"' in begin and "daily_cap" in begin and "c.attempts_day_count >= 5" in begin
    for code in ("origin_not_allowed", "identity_provider", "not_https", "sso_required", "not_configured"):
        assert f'"code":"{code}"' in begin, code
    assert "credential_not_in_job" not in begin and "origin_refused" not in begin and "credential_changed" not in begin
    finish = _fn(pieces["A3"], "assistant_browser_login_finish_v2")
    assert "v_counts := p_outcome not in ('ok','mfa','push')" in finish
    assert "'push'" in finish and "sso_required = sso_required or p_outcome = 'sso'" in finish
    assert "cfg.user_password_session_max_hours" in finish


def test_hold_expiry_parks_from_every_caller(pieces):
    helper = _fn(pieces["A4"], "assistant_browser_hold_expire")
    assert "make_interval(mins => cfg.user_park_max_minutes)" in helper
    assert "'hold_expired:' || h.id::text" in helper
    decide = _fn(pieces["A4"], "assistant_browser_hold_decide")
    release = _fn(pieces["A4"], "assistant_browser_hold_release_get")
    sweep = _fn(pieces["A5"], "assistant_browser_sweep_v2")
    for fn in (decide, release, sweep):
        assert "public.assistant_browser_hold_expire(" in fn
        assert "assistant_browser_holds set status = 'expired'" not in fn


def test_sweep_v2_takes_the_owner_lock_before_holds(pieces):
    sweep = _fn(pieces["A5"], "assistant_browser_sweep_v2")
    lock = sweep.index("pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'")
    assert lock < sweep.index("public.assistant_browser_hold_expire(")
    assert "for h in update public.assistant_browser_holds" not in sweep
    assert "and bj.status not in ('queued','running','waiting_help','applying')" in sweep


def test_v1_sweep_leaves_v2_help_rows(pieces):
    sweep = _fn(pieces["B"], "assistant_browser_sweep")
    m = re.search(r"update public\.assistant_browser_help_state h set status='canceled'(.+?);", sweep, re.S)
    assert m and "bj.agent_type='user_browser'" in m.group(1) and "not exists" in m.group(1)


def test_only_final_action_holds_carry_a_token(pieces):
    decide = _fn(pieces["A4"], "assistant_browser_hold_decide")
    assert "p_decision <> 'decline' and h.kind = 'final_action' and (length(coalesce(p_token, ''))" in decide
    release = _fn(pieces["A4"], "assistant_browser_hold_release_get")
    assert "case when h.kind = 'final_action' and h.status = 'approved'" in release


def test_credential_replace_moves_queued_and_ends_stale_active_jobs(pieces):
    fn = _fn(pieces["A2"], "assistant_browser_credential_put_v2")
    assert "and j.status = 'queued'" in fn
    assert "error_kind = 'credential_changed'" in fn
    assert "j.status in ('running','waiting_help','applying')" in fn


def test_job_policy_matches_the_protocol_shape(pieces):
    fn = _fn(pieces["A3"], "assistant_browser_job_policy")
    assert "'scope', g.scope" in fn
    assert "coalesce(public.assistant_browser_v2_origin(a.start_url), st.exact_origins[1], 'https://' || st.registrable_domains[1])" in fn
    assert "x.credential_id is not null" in fn
    session = _fn(pieces["A3"], "assistant_browser_session_get_v2")
    assert "p_origin text default null" in session and "order by updated_at desc limit 1" in session


def test_fence_v2_raises_scope_revoked(pieces):
    auth = _fn(pieces["A3"], "assistant_browser_v2_authority")
    assert "return 'scope_revoked'" in auth
    v1 = _fn(pieces["A3"], "assistant_browser_user_fence")
    assert "return public.assistant_browser_user_fence_v2(p_job,p_worker,p_boot,p_epoch);" in v1


def test_safe_rules_refuse_final_verbs(pieces):
    fn = _fn(pieces["A5"], "assistant_browser_safe_rule_add")
    assert "(submit|send|accept|declin|delet|remov|pay|approv|answer|respond|sign|award|confirm)" in fn


# ── Tables, columns, seeds ──────────────────────────────────────────────────


def test_every_new_table_has_forced_rls_and_no_public_grant(pieces):
    a1 = pieces["A1"]
    for table in NEW_TABLES:
        assert re.search(rf"create (?:unlogged )?table if not exists public\.{table} \(", a1), table
        assert f"'{table}'" in a1.split("foreach t in array array[", 1)[1].split("] loop", 1)[0], table
    assert "execute format('alter table public.%I force row level security', t);" in a1
    assert "unlogged table if not exists public.assistant_browser_live_frames" in a1
    assert "unlogged table if not exists public.assistant_browser_live_input" in a1


def test_settings_columns_and_caps(pieces):
    a1 = pieces["A1"]
    for col, default in (("user_per_job_cost_cap_usd numeric(8,2)", "5"), ("user_task_queue_hours int", "8"),
                         ("user_per_owner_concurrency smallint", "2"), ("user_per_owner_queued smallint", "5"),
                         ("user_hold_timeout_seconds int", "600"), ("user_park_max_minutes int", "15"),
                         ("user_mfa_mail_poll_seconds int", "300"), ("user_task_reserve_step_usd numeric(8,2)", "1.00"),
                         ("user_offsite_query_bytes int", "4096"), ("user_password_session_max_hours int", "24"),
                         ("user_live_session_max_days int", "14")):
        assert f"add column if not exists {col} not null default {default}" in a1, col
    assert "user_per_job_cost_cap_usd > 0 and user_per_job_cost_cap_usd <= 5" in a1
    assert "coalesce(v_hours, 0) * 3600 + coalesce(v_secs, 0) > 43200" in a1


def test_daily_assistant_budget_is_15_for_every_user(pieces):
    a1 = pieces["A1"]
    assert "alter table public.assistant_profiles alter column daily_cost_cap_usd set default 15;" in a1
    assert ("update public.assistant_profiles set daily_cost_cap_usd = 15, updated_at = now() "
            "where daily_cost_cap_usd = 5;") in a1


def test_type_row_sets_provider_model_prices_and_limits(pieces):
    from app.services.llm_pricing import PRICES

    a1 = _code(pieces["A1"])
    m = re.search(r"values\('user_browser', 'Personal browser', null, 'user_browser\.v1', 'user', false, 4,\s*"
                  r"'openai', '([^']+)', ([0-9.]+), ([0-9.]+), 150, 2000000, 1800\)", a1)
    assert m
    model, price_in, price_out = m.group(1), Decimal(m.group(2)), Decimal(m.group(3))
    assert PRICES[model].input == price_in and PRICES[model].output == price_out
    assert "on conflict (key) do nothing" in a1
    assert "label = 'Retired personal browser read', enabled = false" in a1


def test_curated_sites_seeded(pieces):
    a1 = pieces["A1"]
    for key in ("procore", "autodesk", "buildingconnected", "smartbid"):
        assert f"('{key}', " in a1, key
    assert "array['https://login.procore.com'], 'password_mfa'" in a1


def test_credentials_backfill_procore(pieces):
    a1 = pieces["A1"]
    assert "update public.assistant_credentials c set site_key = 'procore'" in a1
    assert "login_origins = array['https://login.procore.com'], cookie_domains = array['.procore.com']" in a1
    assert "create unique index if not exists assistant_browser_sessions_site_account_uq" in a1


_INDEX_RE = re.compile(r"create\s+(?:unique\s+)?index\s+(?:if\s+not\s+exists\s+)?([a-z0-9_]+)", re.I)
_CONSTRAINT_RE = re.compile(r"\bconstraint\s+([a-z0-9_]+)", re.I)


def test_index_and_constraint_names_are_new(sql):
    ours = set(_INDEX_RE.findall(_code(sql))) | set(_CONSTRAINT_RE.findall(_code(sql)))
    ours -= {"%i"}
    others: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("0*.sql")):
        if path.name == FILE:
            continue
        other = _code(path.read_text(encoding="utf-8"))
        for name in _INDEX_RE.findall(other) + _CONSTRAINT_RE.findall(other):
            others.setdefault(name.lower(), path.name)
    clashes = {n: others[n] for n in ours if n.lower() in others}
    assert not clashes, f"0192 names already used: {clashes}"


def test_no_plpgsql_variable_is_also_a_table_alias(sql):
    """A table alias named like a declared PL/pgSQL variable makes every
    qualified reference ambiguous (42702) at run time."""
    for chunk in sql.split("create or replace function public.")[1:]:
        name, body = chunk.split("(", 1)[0], chunk.split("\nend $$;", 1)[0]
        if "language plpgsql" not in body.split("$$", 1)[0]:
            continue
        dec = re.search(r"declare(.+?)\nbegin", body, re.S)
        if not dec:
            continue
        names = {part.split()[0].lower() for part in dec.group(1).split(";") if part.split()}
        aliases = set(re.findall(r"public\.\w+\s+([a-z_][a-z0-9_]*)\s+(?:where|on|join|set|using)\b", _code(body)))
        assert not names & aliases, (name, sorted(names & aliases))


def test_every_statement_parses(sql):
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(sql)


# ── SQL check script ─────────────────────────────────────────────────────────


def test_check_script_rolls_back_and_reports():
    text = CHECK_PATH.read_text(encoding="utf-8")
    assert text.startswith("-- DEV ONLY")
    code = _code(text).strip()
    assert code.startswith("begin;") and code.endswith("rollback;")
    assert "raise exception 'TEST_RESULT %" in code
    assert not re.search(r"^\s*commit\s*;", code, re.I | re.M)
    for fn in ("assistant_browser_admission_create", "assistant_browser_task_enqueue_v2", "assistant_browser_site_grant",
               "assistant_browser_site_revoke", "assistant_browser_claim_user", "assistant_browser_turn_begin_v2",
               "assistant_browser_hold_open", "assistant_browser_hold_decide", "assistant_browser_hold_consume",
               "assistant_browser_credential_put_v2", "assistant_browser_login_begin_v2",
               "assistant_browser_login_finish_v2", "assistant_browser_session_get_v2", "assistant_browser_sweep_v2",
               "assistant_browser_job_requeue", "browser_agent_hello", "assistant_browser_outbox_drain"):
        assert f"public.{fn}(" in code, fn


# ── Config (WI-1 settings and validator lines) ──────────────────────────────


def _settings(**over):
    from app.core.config import Settings

    return Settings.model_construct(**over)


def _key(byte: bytes) -> str:
    return base64.b64encode(byte * 32).decode()


def test_config_defaults_are_off_and_budget_is_15():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["assistant_browser_v2_enabled"].default is False
    assert fields["assistant_hosted_search_enabled"].default is False
    assert fields["browser_hold_signing_key"].default == ""
    assert fields["browser_hold_signing_key_id"].default == "h1"
    assert fields["assistant_daily_cap_usd"].default == 15.0


def test_v2_needs_the_signing_key_and_never_prints_it():
    secret = _key(b"s")
    s = _settings(assistant_browser_v2_enabled=True, assistant_browser_enabled=True, browser_hold_signing_key="")
    with pytest.raises(ValueError, match="BROWSER_HOLD_SIGNING_KEY must be set"):
        s._validate_browser_v2()
    s = _settings(assistant_browser_v2_enabled=True, assistant_browser_enabled=True,
                  browser_hold_signing_key=secret, user_cred_kek=secret)
    with pytest.raises(ValueError) as err:
        s._validate_browser_v2()
    assert "must differ" in str(err.value) and secret not in str(err.value)
    s = _settings(assistant_browser_v2_enabled=True, assistant_browser_enabled=True,
                  browser_hold_signing_key=secret, user_cred_kek=_key(b"k"))
    assert s._validate_browser_v2() is s


def test_hold_key_must_not_be_the_release_key_or_a_rotated_key():
    """Review finding: the release seed and rotated keys were not compared."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = b"r" * 32
    secret = base64.b64encode(seed).decode()
    public = base64.b64encode(Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    base = dict(assistant_browser_v2_enabled=True, assistant_browser_enabled=True, browser_hold_signing_key=secret,
                user_cred_kek=_key(b"k"))
    s = _settings(**base, browser_agent_release_pubkey=public)
    with pytest.raises(ValueError) as err:
        s._validate_browser_v2()
    assert "release signing key" in str(err.value) and secret not in str(err.value) and public not in str(err.value)
    for field in ("user_cred_old_keys", "browser_agent_secrets_old_keys"):
        s = _settings(**base, **{field: f"old1:{secret}"})
        with pytest.raises(ValueError, match="must differ"):
            s._validate_browser_v2()
    other_public = base64.b64encode(Ed25519PrivateKey.from_private_bytes(b"x" * 32).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    s = _settings(**base, browser_agent_release_pubkey=other_public, user_cred_old_keys=f"old1:{_key(b'o')}")
    assert s._validate_browser_v2() is s


def test_v2_needs_the_personal_browser():
    s = _settings(assistant_browser_v2_enabled=True, assistant_browser_enabled=False, browser_hold_signing_key=_key(b"s"))
    with pytest.raises(ValueError, match="needs ASSISTANT_BROWSER_ENABLED"):
        s._validate_browser_v2()


def test_hosted_search_needs_the_assistant_and_key_id_shape():
    with pytest.raises(ValueError, match="ASSISTANT_HOSTED_SEARCH_ENABLED needs ASSISTANT_ENABLED"):
        _settings(assistant_hosted_search_enabled=True, assistant_enabled=False)._validate_browser_v2()
    with pytest.raises(ValueError, match="BROWSER_HOLD_SIGNING_KEY_ID"):
        _settings(browser_hold_signing_key_id="bad id!")._validate_browser_v2()


def test_env_example_documents_the_v2_keys_without_values():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("ASSISTANT_BROWSER_V2_ENABLED=false", "BROWSER_HOLD_SIGNING_KEY=", "BROWSER_HOLD_SIGNING_KEY_ID=h1",
                "ASSISTANT_HOSTED_SEARCH_ENABLED=false", "ASSISTANT_DAILY_CAP_USD=15"):
        assert f"# {key}" in text, key
    assert re.search(r"^# BROWSER_HOLD_SIGNING_KEY= +#", text, re.M)


# ── Review fixes (wave A) ────────────────────────────────────────────────────


def test_hold_open_stores_records_with_no_help_id(pieces):
    body = _fn(pieces["A4"], "assistant_browser_hold_open")
    assert "p_summary_record, null, null)" in body and "p_screenshot_record, null, null)" in body
    assert "p_summary_record, v_help" not in body and "p_screenshot_record, v_help" not in body


def test_inspection_skips_hold_records_and_help_secrets(pieces):
    body = _fn(pieces["A5"], "assistant_browser_inspect")
    squashed = re.sub(r"\s+", " ", body)
    assert "kind<>'help_secret'" in squashed
    assert ("not exists(select 1 from public.assistant_browser_holds h where h.job_id=r.job_id "
            "and r.id in (h.summary_record_id, h.screenshot_record_id))") in squashed
    assert "role::text='it_admin'" in squashed and "assistant_browser_access" in squashed


def test_credential_put_v2_saves_only_the_matching_request(pieces):
    body = re.sub(r"\s+", " ", _fn(pieces["A2"], "assistant_browser_credential_put_v2"))
    assert ("kind = 'credential' and status = 'requested' and site_key = p_site_key "
            "and coalesce(account_label, p_account_label) = p_account_label returning * into r") in body


"""Migration 0172 (docs/ASSISTANT_PHASE1_PLAN.md, section 2): actor provenance
columns on 14 tables, the labor_reviews provenance guard, and the phase 1
approvals additions (reverts_approval_id, taint_corpus, the supersede and
decide RPCs, the widened immutability function, no delete and no truncate).

Static checks on the SQL text. PROVENANCE_TABLES is parsed from the file and
imported by other tests (every write tool's writes_tables must be in it or in
an explicit, reasoned exemption)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0172_assistant_phase1.sql"
SQL_PATH = MIGRATIONS / FILE

EXPECTED_TABLES = (
    "audit_log", "stage_events", "pm_stage_events", "estimator_notes",
    "quote_notes", "file_access_events", "email_log", "proposal_send_events",
    "labor_reviews", "project_files",
    "projects", "project_gcs", "todos", "bid_split_jobs",
)


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _parse_provenance_tables(sql: str) -> tuple[str, ...]:
    m = re.search(r"foreach t in array array\[(.+?)\]", sql, re.S)
    assert m, "the provenance foreach array"
    return tuple(re.findall(r"'([a-z_]+)'", m.group(1)))


PROVENANCE_TABLES: tuple[str, ...] = _parse_provenance_tables(SQL_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def _fn(sql: str, name: str) -> str:
    m = re.search(rf"create or replace function public\.{name}\(.+?\n\$\$;", sql, re.S)
    assert m, name
    return m.group(0)


def _statements(code: str) -> list[str]:
    return [s.strip() for s in re.split(r";[ \t]*(?:\n|\Z)", code) if s.strip()]


def test_exactly_one_0172_file():
    assert len(list(MIGRATIONS.glob("0172_*.sql"))) == 1


def test_header_notify_last_and_no_dash_characters(sql):
    assert sql.startswith("-- 0172: ")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    assert _statements(_code(sql))[-1] == "notify pgrst, 'reload schema'"


def test_provenance_tables_are_the_fourteen():
    assert PROVENANCE_TABLES == EXPECTED_TABLES
    assert len(set(PROVENANCE_TABLES)) == 14


def test_provenance_columns_constraints_and_index_per_table(sql):
    code = _code(sql)
    assert "add column if not exists actor_kind text not null default ''human''" in code
    assert "add column if not exists assistant_run_id uuid" in code
    assert "check (actor_kind in (''human'',''assistant'',''system''))" in code
    assert "check (actor_kind <> ''assistant'' or assistant_run_id is not null)" in code
    # Constraints are guarded by pg_constraint, never dropped and re-added.
    assert code.count("select 1 from pg_constraint where conname = t || ") == 2
    # Postgres truncates identifiers at 63 bytes; every derived name fits.
    for t in PROVENANCE_TABLES:
        for suffix in ("_actor_kind_chk", "_assistant_run_chk", "_assistant_run_idx"):
            assert len(t + suffix) <= 63, t + suffix


def test_no_drop_statement_anywhere(sql):
    code = _code(sql)
    assert not re.search(r"^\s*drop\s", code, re.M | re.I)
    assert not re.search(r"\bdrop\s+(table|function|trigger|index|constraint|policy|column)\b", code, re.I)


def test_no_jsonb_populate_record_in_0172(sql):
    assert "jsonb_populate_record" not in _code(sql)


def test_supersede_insert_names_its_columns(sql):
    fn = _fn(sql, "assistant_approval_supersede")
    assert re.search(
        r"insert into public\.assistant_approvals \(\s*id, user_id, user_label, conversation_id, message_id, "
        r"project_id, action_kind,\s*payload, payload_sha256, preview, precondition, status, expires_at, "
        r"reverts_approval_id\s*\)",
        fn,
    )
    assert "status = 'expired', expired_reason = 'changed_since_preview', superseded_by = v_new_id" in fn
    assert "a.status = 'executing'" in fn


@pytest.mark.parametrize("sig", [
    "assistant_approval_supersede(uuid, uuid, jsonb)",
    "assistant_approval_decide(uuid, uuid, text, text, jsonb, text)",
])
def test_rpcs_are_service_role_only(sql, sig):
    assert f"revoke all on function public.{sig} from public;" in sql
    assert f"revoke all on function public.{sig} from anon, authenticated;" in sql
    assert f"grant execute on function public.{sig} to service_role;" in sql
    name = sig.split("(", 1)[0]
    grants = re.findall(rf"grant execute on function public\.{name}\([^)]*\) to ([a-z_, ]+);", sql)
    assert grants == ["service_role"]


def test_every_function_is_invoker_and_pinned(sql):
    names = re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
    assert set(names) == {
        "labor_reviews_actor_provenance", "assistant_approval_supersede",
        "assistant_approval_immutable", "assistant_approval_decide",
    }
    for name in names:
        fn = _fn(sql, name)
        assert "security invoker" in fn and "set search_path = pg_catalog, public" in fn, name
        assert "security definer" not in fn, name


def test_immutability_function_covers_the_links(sql):
    fn = _fn(sql, "assistant_approval_immutable")
    for col in ("payload", "payload_sha256", "preview", "precondition", "user_id", "user_label",
                "action_kind", "expires_at", "project_id", "conversation_id", "message_id",
                "reverts_approval_id", "created_at"):
        assert f"new.{col} is distinct from old.{col}" in fn, col
    assert "old.superseded_by is not null and new.superseded_by is distinct from old.superseded_by" in fn
    assert "if v_new <= v_old then" in fn


def test_approvals_no_delete_and_no_truncate(sql):
    for op, level in (("delete", "row"), ("truncate", "statement")):
        assert re.search(
            rf"create or replace trigger assistant_approvals_no_{op} before {op} on public\.assistant_approvals\s+"
            rf"for each {level} execute function public\.assistant_refuse_change\(\);",
            sql,
        ), op


def test_labor_reviews_provenance_trigger(sql):
    assert re.search(
        r"create or replace trigger labor_reviews_actor_provenance\s+before insert or update on public\.labor_reviews\s+"
        r"for each row execute function public\.labor_reviews_actor_provenance\(\);",
        sql,
    )
    fn = _fn(sql, "labor_reviews_actor_provenance")
    assert "if new.actor_kind is null then" in fn
    assert "new.assistant_run_id is not distinct from old.assistant_run_id" in fn


def test_one_revert_index_and_taint_corpus(sql):
    code = _code(sql)
    assert "alter table public.assistant_approvals add column if not exists reverts_approval_id uuid;" in code
    assert re.search(
        r"create unique index if not exists assistant_approvals_one_revert_uq\s+"
        r"on public\.assistant_approvals \(reverts_approval_id\)\s+where reverts_approval_id is not null\s+"
        r"and status in \('pending', 'approved', 'executing', 'executed'\);",
        code,
    )
    assert "alter table public.assistant_tool_results add column if not exists taint_corpus jsonb;" in code


def test_decide_cas(sql):
    fn = _fn(sql, "assistant_approval_decide")
    assert "a.status = 'pending'" in fn and "a.expires_at > now()" in fn
    assert "(p_decision = 'reject'\n          or (a.payload_sha256 = p_payload_sha256 and a.precondition = p_precondition))" in fn
    for reason in ("not_found", "expired", "superseded", "not_pending", "stale"):
        assert f"'reason', '{reason}'" in fn, reason
    assert "expired_reason = 'timed_out'" in fn


_INDEX_RE = re.compile(r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?([a-z0-9_]+)", re.I)


def test_no_0172_index_name_exists_in_an_earlier_migration(sql):
    ours = set(_INDEX_RE.findall(_code(sql)))
    ours |= {f"{t}_assistant_run_idx" for t in PROVENANCE_TABLES}
    assert "proposal_send_events_sent_by_created_idx" in ours
    earlier: dict[str, str] = {}
    for path in sorted(MIGRATIONS.glob("0*.sql")):
        if path.name >= FILE[:4]:
            continue
        for name in _INDEX_RE.findall(_code(path.read_text(encoding="utf-8"))):
            earlier.setdefault(name.lower(), path.name)
    clashes = {n: earlier[n] for n in ours if n.lower() in earlier}
    assert not clashes, f"0172 index names already used earlier: {clashes}"


_POPULATE_RE = re.compile(
    r"select\s+\*\s+from\s+jsonb_populate_record(?:set)?\s*\(\s*null::(?:public\.)?([a-z_]+)", re.I
)


def test_select_star_populate_record_only_for_labor_reviews():
    """A pre-0172 snapshot restored with select * supplies an explicit NULL
    actor_kind. Only labor_reviews has the fill trigger (Section A2)."""
    hits: list[tuple[str, str]] = []
    for path in sorted(MIGRATIONS.glob("0*.sql")):
        for table in _POPULATE_RE.findall(_code(path.read_text(encoding="utf-8"))):
            hits.append((path.name, table.lower()))
    assert ("0140_external_submissions.sql", "labor_reviews") in hits
    for name, table in hits:
        if table in PROVENANCE_TABLES:
            assert table == "labor_reviews", f"add an actor_kind fill trigger for {table} ({name})"


def test_every_statement_parses():
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(SQL_PATH.read_text(encoding="utf-8"))

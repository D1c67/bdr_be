"""Migration 0191 (docs/ASSISTANT_PHASE7_PLAN.md 8.4 and 4.2a): two 0190
router RPCs replaced with the same signatures. A11.11 link-bell takes the
record row's severity from p_payload->>'severity' and its precedence rule id
from p_payload->>'precedence'; A11.14 fold keeps the
prior precedence in payload folded_from_precedence and the un-fold restores
it, so a merged row never gains a Teams message.

Static checks on the SQL text; the FakeDB twins are exercised by
test_assistant_repo_phase7.py and test_assistant_proactive_service.py, and
scripts/check_assistant_phase7_0191.sql is the rolled-back dev check."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase/migrations"
FILE = "0191_assistant_phase7_fold_fix.sql"
SQL_PATH = MIGRATIONS / FILE
SQL_0190 = (MIGRATIONS / "0190_assistant_phase7.sql").read_text(encoding="utf-8")
CHECK = ROOT / "scripts/check_assistant_phase7_0191.sql"

FUNCTIONS = (
    "assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz)",
    "assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz)",
)


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def _code(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _fn(text: str, name: str) -> str:
    m = re.search(rf"create or replace function public\.{name}\(.+?\nend \$\$;", text, re.S)
    assert m, name
    return m.group(0)


def _header(fn: str) -> str:
    return fn[:fn.index("as $$")]


def _statements(code: str) -> list[str]:
    return [s.strip() for s in re.split(r";[ \t]*(?:\n|\Z)", code) if s.strip()]


def test_one_0191_file_header_notify_last_and_no_dash_characters(sql):
    assert len(list(MIGRATIONS.glob("0191_*.sql"))) == 1
    assert sql.startswith("-- 0191: ")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    assert _statements(_code(sql))[-1] == "notify pgrst, 'reload schema'"


def test_only_create_or_replace_grants_and_notify(sql):
    code = _code(sql)
    bodies = re.findall(r"create or replace function public\.[a-z_]+\(.+?\nend \$\$;", code, re.S)
    assert len(bodies) == len(FUNCTIONS)
    rest = code
    for body in bodies:
        rest = rest.replace(body, "")
    for stmt in _statements(rest):
        assert (stmt.startswith("revoke all on function") or stmt.startswith("grant execute on function")
                or stmt == "notify pgrst, 'reload schema'"), stmt[:80]
    code = _code(sql).lower()
    for word in ("drop ", "alter table", "delete ", "truncate", "create table", "create index"):
        assert word not in code, word


def test_same_signatures_and_headers_as_0190(sql):
    names = re.findall(r"create or replace function public\.([a-z_]+)\(", sql)
    assert names == [s.split("(", 1)[0] for s in FUNCTIONS]
    for name in names:
        assert _header(_fn(sql, name)) == _header(_fn(SQL_0190, name)), name


@pytest.mark.parametrize("sig", FUNCTIONS)
def test_functions_stay_service_role_only(sql, sig):
    assert f"revoke all on function public.{sig} from public;" in sql
    assert f"revoke all on function public.{sig} from anon, authenticated;" in sql
    assert f"grant execute on function public.{sig} to service_role;" in sql
    name = sig.split("(", 1)[0]
    assert re.findall(rf"grant execute on function public\.{name}\([^)]*\) to ([a-z_, ]+);", sql) == ["service_role"]
    fn = _fn(sql, name)
    assert "security invoker" in fn and "set search_path = pg_catalog, public\n" in fn
    assert "security definer" not in fn
    assert "now()" not in _code(fn.replace("p_now timestamptz default now()", ""))


def test_link_bell_reads_severity_from_the_payload_and_keeps_the_0190_rules(sql):
    fn = _fn(sql, "assistant_inbox_link_bell")
    assert ("v_severity := case when p_payload->>'severity' in ('low', 'normal', 'high', 'urgent')\n"
            "                     then p_payload->>'severity' else 'normal' end;") in fn
    assert "v_payload := coalesce(p_payload, '{}'::jsonb) - 'severity' - 'precedence';" in fn
    assert "(user_id, dedupe_key, event_type, severity, title, why, payload" in fn
    assert "set event_type = p_event_type, severity = v_severity, title = p_title" in fn
    assert "coalesce(p_payload" not in fn.replace("v_payload := coalesce(p_payload", "")
    # Unchanged from 0190.
    old = _fn(SQL_0190, "assistant_inbox_link_bell")
    for line in ("p_dedupe_key not like 'p7:bell:%'", "on conflict (user_id, dedupe_key) do nothing",
                 "v_later := p_rank = v_rank and p_bell_at > v_at + interval '120 seconds';",
                 "(p_bell_at, p_notification_id) < (v_at, v_row.source_notification_id)",
                 "limit 20", "jsonb_array_length(v_linked) < 20", "v_row.delivery <> 'record'",
                 "created_at = case when v_later then p_bell_at else i.created_at end"):
        assert line in old and line in fn, line
    assert "delete" not in fn


def test_link_bell_reads_the_precedence_rule_id_from_the_payload(sql):
    """S6-1: the record row stores the router's precedence rule id (R13 and
    the other record rules), validated, on insert and when a new bell
    becomes the source; a linked bell leaves it; never in the payload."""
    fn = _fn(sql, "assistant_inbox_link_bell")
    assert "  v_precedence text;\n" in fn
    assert ("v_precedence := case when char_length(p_payload->>'precedence') <= 16\n"
            "                            and p_payload->>'precedence' ~ '^R[0-9]+[a-z]?$'\n"
            "                       then p_payload->>'precedence' end;") in fn
    assert "source_notification_id, delivered_at, created_at, precedence)" in fn
    assert "p_project_id, 'record', p_notification_id, p_now, p_now, v_precedence)" in fn
    assert "precedence = v_precedence, source_notification_id = p_notification_id," in fn
    # Exactly the insert and the new-source update write it; the link branch does not.
    assert fn.count("v_precedence") == 4
    link_branch = fn[fn.index("elsif jsonb_array_length(v_linked) < 20 then"):]
    assert "precedence" not in link_branch


def test_fold_parks_and_restores_the_prior_precedence(sql):
    fn = _fn(sql, "assistant_inbox_fold")
    assert ("set delivery = 'digest', precedence = 'quiet_hours', deliver_after = p_am_slot_at,\n"
            "             payload = i.payload || jsonb_build_object('folded_from_precedence', i.precedence)") in fn
    assert ("precedence = case when i.payload ? 'folded_from_precedence'\n"
            "                               then i.payload->>'folded_from_precedence' else i.precedence end,") in fn
    assert "payload = i.payload - 'folded_from_precedence'" in fn
    # The selector and the held definition are 0190's.
    old = _fn(SQL_0190, "assistant_inbox_fold")
    for line in ("pg_advisory_xact_lock(hashtext('assistant_inbox_deliver:' || p_user_id::text))",
                 "v_row.severity = 'high'", "coalesce(v_row.event_type, '') <> 'assistant.approval_expiring'",
                 "v_row.deliver_after > v_row.created_at",
                 "if v_row.delivery = 'digest' and v_row.precedence = 'quiet_hours'",
                 "if v_am or p_fold_until is null or p_now >= p_fold_until then",
                 "p_now < p_fold_until and not v_am then"):
        assert line in old and line in fn, line
    for result in ("'folded'", "'deliver'", "'wait'", "'none'"):
        assert f"return {result};" in fn


def test_the_rolled_back_check_script_exists_and_never_commits():
    text = CHECK.read_text(encoding="utf-8")
    code = _code(text)
    assert code.lstrip().startswith("begin;") and code.rstrip().endswith("rollback;")
    assert "commit" not in code.lower()
    assert "raise exception 'TEST_RESULT" in code
    assert "public.assistant_inbox_fold(" in code and "public.assistant_inbox_link_bell(" in code
    for check in ("'v_precedence'", "r.precedence = 'R13'", "r.precedence = 'R4b'", "'precedence', 'R12'",
                  "\"precedence\":\"quiet_hours\"", "repeat('1', 20)", "\"precedence\":13"):
        assert check in code, check
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_every_statement_parses(sql):
    pglast = pytest.importorskip("pglast")
    pglast.parse_sql(sql)
    pglast.parse_sql(CHECK.read_text(encoding="utf-8"))

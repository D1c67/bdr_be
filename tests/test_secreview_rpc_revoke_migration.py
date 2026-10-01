"""Security review, rpc-revoke-migration group (migration 0145).

Every function a migration leaves in the public schema must have EXECUTE
revoked from public, anon and authenticated, and must pin its search_path,
so PostgREST's /rest/v1/rpc/<name> is closed to the anon key. Functions a
later migration drops are exempt. This scans the whole migrations folder, so
a new function added without its revoke block fails here too."""

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase/migrations"

CREATE_RE = re.compile(
    r"create\s+(?:or\s+replace\s+)?function\s+(?:public\.)?([a-z_][a-z0-9_]*)\s*\(",
    re.IGNORECASE,
)
DROP_RE = re.compile(
    r"drop\s+function\s+(?:if\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)\s*\(",
    re.IGNORECASE,
)


def _files() -> list[Path]:
    return sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def _all_sql() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _files())


def _live_functions() -> set[str]:
    """Names created by some migration and not dropped by a later one."""
    live: set[str] = set()
    for p in _files():
        sql = p.read_text(encoding="utf-8")
        events = [(m.start(), "create", m.group(1).lower()) for m in CREATE_RE.finditer(sql)]
        events += [(m.start(), "drop", m.group(1).lower()) for m in DROP_RE.finditer(sql)]
        for _, kind, name in sorted(events):
            if kind == "create":
                live.add(name)
            else:
                live.discard(name)
    return live


def _revoked_from(sql: str, name: str, roles: str) -> bool:
    pat = (
        r"revoke\s+all\s+on\s+function\s+(?:public\.)?" + re.escape(name)
        + r"\s*\([^)]*\)\s+from\s+" + roles + r"\s*;"
    )
    return re.search(pat, sql, re.IGNORECASE) is not None


def test_scan_finds_the_known_functions():
    live = _live_functions()
    for name in (
        "claim_llm_jobs", "remove_project_gc_unless_sent", "search_submittals",
        "set_updated_at", "submittal_materials_build_search_text",
        "rfp_email_sightings_append_mailbox", "next_project_number",
    ):
        assert name in live
    # Dropped by later migrations, so nothing to revoke.
    assert "flag_dev_account" not in live
    assert "_bdr_stage_order" not in live


def test_every_live_function_revokes_execute_from_public_anon_authenticated():
    sql = _all_sql()
    missing = sorted(
        n for n in _live_functions()
        if not (_revoked_from(sql, n, "public") and _revoked_from(sql, n, r"anon\s*,\s*authenticated"))
    )
    assert missing == [], f"functions still EXECUTE-able by anon/authenticated: {missing}"


def test_every_live_function_pins_search_path():
    sql = _all_sql()
    unpinned = []
    for name in _live_functions():
        body_pinned = re.search(
            r"function\s+(?:public\.)?" + re.escape(name) + r"\s*\((?:(?!\$\$).)*?set\s+search_path",
            sql, re.IGNORECASE | re.DOTALL,
        )
        altered = re.search(
            r"alter\s+function\s+(?:public\.)?" + re.escape(name) + r"\s*\([^)]*\)\s+set\s+search_path",
            sql, re.IGNORECASE,
        )
        immutable_sql_helper = name.startswith("_bdr_")
        if not (body_pinned or altered or immutable_sql_helper):
            unpinned.append(name)
    assert sorted(unpinned) == []


def test_0145_house_style():
    sql = (MIGRATIONS / "0145_revoke_rpc_execute.sql").read_text(encoding="utf-8")
    assert sql.startswith("-- 0145:")
    assert "Apply after 0144." in sql
    assert sql.rstrip().endswith("notify pgrst, 'reload schema';")
    assert chr(0x2014) not in sql and chr(0x2013) not in sql
    # Exact live signatures (checked against the DEV catalog).
    for sig in (
        "claim_llm_jobs(text, integer, integer, text[])",
        "remove_project_gc_unless_sent(uuid, uuid, uuid, boolean)",
        "search_submittals(text, text)",
    ):
        assert f"grant execute on function public.{sig} to service_role;" in sql

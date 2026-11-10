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


# Browser v2 (0192, docs/ASSISTANT_BROWSER_V2_PLAN.md 14.1): every function it
# creates or replaces revokes EXECUTE explicitly, piece by piece.
FUNCTIONS_0192 = (
    "assistant_browser_v2_ttl_guard", "assistant_browser_v2_host", "assistant_browser_v2_origin",
    "assistant_browser_v2_is_idp", "assistant_browser_v2_site_matches", "assistant_browser_admission_needs",
    "assistant_browser_admission_status", "assistant_browser_site_grant", "assistant_browser_scope_extend",
    "assistant_browser_admission_create", "assistant_browser_start_decide", "assistant_browser_request_create",
    "assistant_browser_request_decide", "assistant_browser_task_enqueue_v2", "assistant_browser_credential_put_v2",
    "assistant_browser_credential_revoke_v2", "assistant_browser_v2_authority", "assistant_browser_user_fence_v2",
    "assistant_browser_user_fence", "assistant_browser_job_guard", "assistant_browser_scope_allows",
    "assistant_browser_job_policy", "assistant_browser_login_begin_v2", "assistant_browser_login_finish_v2",
    "assistant_browser_session_get_v2", "assistant_browser_help_open_v2", "assistant_browser_help_reply_v2",
    "assistant_browser_help_consume_v2", "assistant_browser_help_secret_from_mail", "assistant_browser_hold_open", "assistant_browser_hold_expire", "assistant_browser_inspect",
    "assistant_browser_hold_decide", "assistant_browser_hold_release_get", "assistant_browser_hold_consume",
    "assistant_browser_turn_begin_v2", "assistant_browser_job_requeue_v2", "assistant_browser_job_requeue",
    "assistant_browser_sweep_v2", "assistant_browser_safe_rule_add", "assistant_browser_download_record",
    "assistant_browser_live_session_open", "assistant_browser_outbox_drain", "assistant_browser_outbox_ack",
    "assistant_browser_claim_user", "assistant_browser_terminal", "assistant_browser_site_revoke",
    "assistant_browser_session_signout", "assistant_browser_safe_rule_remove", "assistant_browser_live_session_close",
    "assistant_browser_purge_v2", "assistant_browser_revoke_owner", "assistant_browser_sweep",
)


def test_0192_functions_revoke_execute_explicitly():
    sql = (MIGRATIONS / "0192_assistant_browser_v2.sql").read_text(encoding="utf-8")
    created = {m.group(1).lower() for m in CREATE_RE.finditer(sql)}
    assert created == set(FUNCTIONS_0192)
    missing = sorted(n for n in FUNCTIONS_0192
                     if not (_revoked_from(sql, n, "public") and _revoked_from(sql, n, r"anon\s*,\s*authenticated")))
    assert missing == []
    for name in FUNCTIONS_0192:
        assert name in _live_functions()

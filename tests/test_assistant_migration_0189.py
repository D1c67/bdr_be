"""Regional origin scope and immutable credential authority in migration 0189."""
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).parents[1]
SQL = (ROOT / "supabase/migrations/0189_assistant_browser_regional_procore.sql").read_text()


def function(name):
    return re.search(r"create or replace function public\." + name + r"\(.*?\$\$;",
                     SQL, re.S).group()


def accepted(url):
    pattern = re.search(r"p_url ~ '([^']+)'", function("assistant_browser_task_origin")).group(1)
    return re.fullmatch(pattern, url) is not None


@pytest.mark.parametrize("url", [
    "https://app.procore.com/123/project/home",
    "https://us02.procore.com/123/project/rfi/show/456",
    "https://us02.procore.com/webclients/host/companies/562949953430361/"
    "projects/562949955106559/tools/hubs/project-hub/views/overview",
    "https://app.procore.com/webclients/host/companies/12/projects/34/tools/rfis/56/",
    "https://us02.procore.com/webclients/host/companies/12/projects/34/tools/projecthome",
])
def test_exact_read_urls(url):
    assert accepted(url)


@pytest.mark.parametrize("url", [
    "https://us03.procore.com/123/project/home", "https://procore.com/123/project/home",
    "https://us02.procore.com.evil.invalid/123/project/home",
    "https://evil@us02.procore.com/123/project/home",
    "https://us02.procore.com:443/123/project/home",
    "http://us02.procore.com/123/project/home",
    "https://us02.procore.com/123/project/home?redirect=evil",
    "https://us02.procore.com/123/project/home#fragment",
    "https://us02.procore.com/webclients/host/companies/12/projects/34/tools/hubs/"
    "project-hub/views/admin",
    "https://us02.procore.com/webclients/host/companies/12/projects/34/tools/hubs/"
    "project-hub/views/overview/edit",
    "https://us02.procore.com/123/project/%68ome",
    "https://us02.procore.com/123/project/../home",
])
def test_nearby_or_unapproved_destinations_refused(url):
    assert not accepted(url)


def test_owner_resave_is_only_origin_expansion():
    put = function("assistant_browser_credential_put")
    assert "array['https://app.procore.com','https://login.procore.com','https://us02.procore.com']" in put
    assert "c.version<>p_expected_version" in put
    assert "v_version:=coalesce(c.version,0)+1" in put
    assert "assistant_browser_cancel_credentials" in put
    # No top-level UPDATE/DO block or profile-specific credential change.
    assert "do $$" not in SQL.lower()
    outside = re.sub(r"create or replace function .*?\$\$;", "", SQL, flags=re.S)
    assert not re.search(r"\b(update|insert|delete)\s+(into|from|public\.)", outside, re.I)


def test_admission_claim_and_lease_require_saved_origin():
    for name in ("assistant_browser_task_enqueue", "assistant_browser_claim_user",
                 "assistant_browser_user_fence"):
        body = function(name)
        assert "assistant_browser_task_origin(" in body
        assert "=any(c.origins),false)" in body
    enqueue = function("assistant_browser_task_enqueue")
    assert enqueue.index("=any(c.origins),false)") < enqueue.index("assistant_spend_reserve")


def test_cookie_session_is_bound_to_exact_job_origin():
    finish = function("assistant_browser_login_finish")
    assert "p_session->>'origin' is distinct from public.assistant_browser_task_origin(j.input->>'url')" in finish
    assert "(p_session->>'origin')=any(c.origins),false)" in finish
    get = function("assistant_browser_session_get")
    assert "p_origin is distinct from public.assistant_browser_task_origin(j.input->>'url')" in get
    assert "credential_version=j.user_credential_version and origin=p_origin" in get
    assert "create or replace function public.assistant_browser_login_begin" not in SQL


def test_private_helper_and_existing_fences_remain():
    assert "security definer" not in SQL.lower()
    assert "revoke all on function public.assistant_browser_task_origin(text) from public,anon,authenticated" in SQL
    assert "grant execute on function public.assistant_browser_task_origin(text) to service_role" in SQL
    fence = function("assistant_browser_user_fence")
    assert "lease_expires_at<=now()" in fence
    assert "c.version is distinct from j.user_credential_version" in fence
    assert fence.index("assistant_browser_owner_eligible") < fence.index("pg_advisory_xact_lock")

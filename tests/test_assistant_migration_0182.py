"""Static contract gates; real PostgreSQL races remain a separate gate."""
from pathlib import Path
import re

SQL = (Path(__file__).parents[1] / 'supabase/migrations/0182_assistant_phase5.sql').read_text()


def test_service_only_invoker_functions():
    names = re.findall(r'create or replace function public\.(\w+)\(', SQL)
    assert len(names) == 11
    assert SQL.count('security invoker set search_path = public, pg_temp') == len(names)
    for name in names:
        assert f'revoke all on function public.{name}(' in SQL
        assert f'grant execute on function public.{name}(' in SQL
    assert 'security definer' not in SQL.lower()


def test_private_tables_and_owner_state():
    for table in ('assistant_ms_connections', 'assistant_ms_oauth_states'):
        assert f'alter table public.{table} force row level security' in SQL
    assert "browser_hash text not null" in SQL
    assert 'where user_id=p_user_id and state_hash=p_state_hash for update' in SQL
    assert "s.browser_hash is distinct from p_browser_hash" in SQL
    assert "s.consumed_at is not null" in SQL


def test_callback_and_refresh_fences():
    assert 's.completion_id is distinct from p_completion_id' in SQL
    assert 'c.generation is distinct from p_generation' in SQL
    assert 'c.cache_version is distinct from p_expected_cache_version' in SQL
    assert 'c.refresh_claim_until<=now()' in SQL
    assert 'p_expected_generation is not null and c.generation<>p_expected_generation' in SQL
    assert SQL.count('generation=generation+1') == 2
    assert "interval '10 minutes'" in SQL
    assert "interval '60 seconds'" in SQL


def test_live_binding_and_secret_removal():
    assert 'p.is_active=true' in SQL
    assert 'd.revoked_at is null' in SQL
    assert 'unnest(p.rfp_mailboxes)' in SQL
    assert "flow_ciphertext=null,flow_nonce=null,flow_key_id=null" in SQL
    assert "cache_ciphertext=null,cache_nonce=null,cache_key_id=null" in SQL
    assert "check(status = 'connected' or cache_ciphertext is null)" in SQL


def test_eligibility_expiry_switches_and_lock_order():
    assert 'd.expires_at is null or d.expires_at > now()' in SQL
    assert 'public.assistant_settings where id=1 and enabled=true for share' in SQL
    assert 'public.assistant_profiles where user_id=p_user_id and enabled=false' in SQL
    for name in ('begin', 'consume', 'complete', 'refresh_claim', 'refresh_commit'):
        body = SQL.split(f'create or replace function public.assistant_ms_{name}(', 1)[1].split('end $$;', 1)[0]
        assert body.index('assistant_ms_eligible(p_user_id)') < body.index('select * into c')
    assert 'matched_address is not null and matched_address=upn' in SQL
    assert 'set consumed_at=now(),completion_id=p_completion_id,flow_ciphertext=null' in SQL


def test_profile_remap_fences_authority_and_cleanup_is_bounded():
    assert 'before update of email,rfp_mailboxes,role,is_active' in SQL
    assert "perform public.assistant_ms_revoke(old.id" in SQL
    assert 'old.email is distinct from new.email' in SQL
    assert 'order by expires_at limit 500 for update skip locked' in SQL
    assert "jsonb_build_object('ok',true,'deleted',v_count)" in SQL

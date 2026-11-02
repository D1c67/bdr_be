"""Database invariants for encrypted personal browser execution."""
from pathlib import Path
import re

ROOT = Path(__file__).parents[1]
CORE = (ROOT / 'supabase/migrations/0187_assistant_browser_runtime.sql').read_text()
CLAIM = (ROOT / 'supabase/migrations/0188_assistant_browser_claims.sql').read_text()
CHECK = (ROOT / 'scripts/check_assistant_browser_runtime.sql').read_text()


def function(source, name):
    return re.search(r'create or replace function public\.' + name + r'\(.*?\$\$;', source, re.S).group(0)


def test_private_storage_is_encrypted_and_service_only():
    for table in ('assistant_browser_records', 'assistant_browser_turns', 'assistant_browser_help_state', 'assistant_browser_notices'):
        assert f'create table if not exists public.{table}' in CORE
    assert 'force row level security' in CORE
    assert 'from public,anon,authenticated' in CORE
    assert 'security definer' not in CORE.lower()
    assert 'request_envelope jsonb,response_envelope jsonb' in CORE
    assert 'user_trace_requires_encryption' in CLAIM
    assert "'browser_agent_steps','browser_agent_turns','browser_agent_artifacts','browser_agent_help_requests'" in CLAIM


def test_live_fence_lock_order_and_expiry():
    body = function(CORE, 'assistant_browser_user_fence')
    assert body.index('assistant_browser_owner_eligible') < body.index('pg_advisory_xact_lock')
    assert body.index('pg_advisory_xact_lock') < body.index('where id=p_job for update')
    for test in ('lease_expires_at<=now()', 'lease_epoch is distinct from p_epoch', 'user_credential_version', 'mailbox_tainted_at is null'):
        assert test in body
    assert 'user_protocol_required' in function(CLAIM, 'browser_agent_fence')
    assert 'user_protocol_required' in function(CLAIM, 'browser_agent_heartbeat')


def test_turn_ledger_prevents_paid_replay_and_enforces_caps():
    body = function(CORE, 'assistant_browser_turn_begin')
    for test in ('p_turn_no<>j.turn_count+1', "then 'thinking'", 'j.tokens_in+p_estimated_tokens>typ.max_total_tokens',
                 'j.active_seconds>=typ.max_active_seconds', 'typ.model is distinct from j.input', 'for share'):
        assert test in body
    assert body.index('insert into public.assistant_browser_turns') < body.index('cost_usd=cost_usd+p_estimated_cost')
    finish = function(CORE, 'assistant_browser_turn_finish')
    assert 't.token is distinct from p_token' in finish
    assert 'cost_usd-t.estimated_cost+p_actual_cost' in finish


def test_login_mfa_generation_and_single_use():
    body = function(CORE, 'assistant_browser_login_finish')
    assert 'c.login_epoch is distinct from p_epoch' in body
    assert 'c.login_attempt_id is distinct from p_attempt_id' in body
    assert "'timeout'" in body
    assert "'login_failures',c.login_failures" in body
    reply = function(CORE, 'assistant_browser_help_reply')
    assert 'code_already_supplied' in reply
    assert reply.index("'created',false") < reply.index("h.status<>'open'")
    consume = function(CORE, 'assistant_browser_help_consume')
    assert 'envelope=null,consumed_at=now()' in consume
    assert 'secret_hash' not in consume


def test_park_retry_expiry_settlement():
    requeue = function(CORE, 'assistant_browser_job_requeue')
    assert 'p_retry_at timestamptz default null' in requeue
    assert "then 'waiting_help' else 'queued'" in requeue
    assert "now()+interval '30 seconds'" in requeue
    terminal = function(CORE, 'assistant_browser_terminal')
    assert 'actual_usd=new.cost_usd' in terminal
    assert "status='active'" in terminal
    assert "kind='help_secret'" in terminal
    assert 'assistant_browser_notice' in terminal


def test_purge_serializes_with_legal_hold_and_inspection_audits_first():
    purge = function(CORE, 'assistant_browser_records_purge')
    assert 'insert into public.assistant_profiles(user_id)' in purge
    assert 'where user_id=owner for share' in purge
    assert purge.index('if held then continue') < purge.index('delete from public.assistant_browser_records')
    inspect = function(CORE, 'assistant_browser_inspect')
    assert inspect.index('insert into public.assistant_browser_access') < inspect.index('jsonb_agg')
    assert "kind<>'help_secret'" in inspect


def test_authority_enrollment_and_readiness_are_explicit():
    enqueue = function(CLAIM, 'assistant_browser_task_enqueue')
    for name in ('role', 'model', 'conversation_id', 'credential_id', 'credential_version', 'max_cost_usd', 'url', 'objective'):
        assert '{authority,' + name + '}' in enqueue
    assert 'assistant_browser_enrollment_pool_guard' in CLAIM
    assert 'select true' in function(CLAIM, 'assistant_browser_runtime_ready')
    assert CHECK.strip().endswith('rollback;')
    assert 'legacy trace accepted user' in CHECK
    assert 'secret replay' in CHECK

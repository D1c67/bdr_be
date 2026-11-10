"""Only designated assistant claims and private-browser maintenance may run."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.main import app

original_lifespan, original_routes = app.router.lifespan_context, list(app.router.routes)
spec = importlib.util.spec_from_file_location('phase6_acceptance_app_test', Path(__file__).parents[1] / 'scripts/phase6_acceptance_app.py')
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)
# Importing this dedicated uvicorn target must not change the normal app for
# unrelated tests in the same interpreter.
app.router.lifespan_context = original_lifespan
app.router.routes[:] = original_routes


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv('PHASE6_ACCEPTANCE', 'true')
    monkeypatch.setenv('PHASE6_ACCEPTANCE_OWNER', str(uuid4()))
    monkeypatch.setenv('PHASE6_ACCEPTANCE_WORKER', str(uuid4()))
    monkeypatch.setenv('PHASE6_ACCEPTANCE_RUN', 'a' * 32)
    value = SimpleNamespace(supabase_url=acceptance.DEV_URL, is_production=False,
        assistant_enabled=True, assistant_browser_enabled=True, browser_agents_enabled=True,
        llm_queue_enabled=True, assistant_concurrency=1,
        assistant_browser_v2_enabled=True, assistant_hosted_search_enabled=True,
        llm_queue_claim_types=','.join(sorted(acceptance.CLAIM_TYPES)))
    monkeypatch.setattr(acceptance, 'get_settings', lambda: value)
    return value


def claimed(kind='assistant_turn'):
    return {'id': str(uuid4()), 'job_type': kind, 'status': 'running',
            'claimed_by': acceptance.llm_queue._WORKER_TOKEN, 'payload': {}, 'attempts': 1,
            'lease_expires_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}


@pytest.mark.parametrize('field,value', [('supabase_url', 'https://other.supabase.co'),
    ('is_production', True), ('assistant_enabled', False), ('assistant_browser_enabled', False),
    ('browser_agents_enabled', False), ('llm_queue_enabled', False), ('assistant_concurrency', 2),
    ('assistant_browser_v2_enabled', False), ('assistant_hosted_search_enabled', False),
    ('llm_queue_claim_types', 'assistant_turn,assistant_approval_execute,bid_split'),
    ('rfp_ingest_enabled', True), ('assistant_scheduler_enabled', True)])
def test_guard_refuses_broader_or_nondev_configuration(settings, field, value):
    setattr(settings, field, value)
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.guard(settings)


def test_explicit_mode_and_identity_guards(settings, monkeypatch):
    acceptance.guard(settings)
    monkeypatch.delenv('PHASE6_ACCEPTANCE')
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.guard(settings)
    monkeypatch.setenv('PHASE6_ACCEPTANCE', 'true')
    monkeypatch.setenv('PHASE6_ACCEPTANCE_WORKER', 'malformed')
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.guard(settings)
    # Before the 1.1.0 re-enrollment is approved there is no personal worker.
    monkeypatch.delenv('PHASE6_ACCEPTANCE_WORKER')
    acceptance.guard(settings)
    monkeypatch.setenv('PHASE6_ACCEPTANCE_OWNER', 'malformed')
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.guard(settings)


def test_teams_and_outlook_are_the_only_flags_the_window_turns_on(settings):
    settings.teams_alerts_enabled = True
    settings.assistant_outlook_enabled = True
    acceptance.guard(settings)
    settings.notification_emails_enabled = True
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.guard(settings)


@pytest.mark.parametrize('change', [{'job_type': 'bid_split'}, {'claimed_by': 'other'},
    {'status': 'queued'}, {'attempts': True}, {'payload': 'invalid'}, {'id': 'invalid'},
    {'lease_expires_at': '2020-01-01T00:00:00Z'}])
def test_invalid_claim_never_reaches_execute(change):
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.validate_claims([{**claimed(), **change}])


@pytest.mark.asyncio
async def test_only_narrow_claim_and_real_execution_are_used(settings, monkeypatch):
    stop = asyncio.Event()
    job, seen = claimed(), []
    monkeypatch.setattr(acceptance.llm_queue, '_claim_assistant', lambda config, running: [job])
    def execute(value):
        seen.append(value)
    monkeypatch.setattr(acceptance.llm_queue, '_execute', execute)
    for name in ('_sweep', '_claim_tick', 'worker_loop'):
        monkeypatch.setattr(acceptance.llm_queue, name, lambda *a: pytest.fail('general queue was called'))
    async def pause(*args):
        stop.set()
    monkeypatch.setattr(acceptance, 'pause', pause)
    await acceptance.assistant_loop(stop)
    assert seen == [job]


@pytest.mark.asyncio
async def test_malformed_claim_stops_claiming_without_executing(settings, monkeypatch):
    stop = asyncio.Event()
    application = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(acceptance.llm_queue, '_claim_assistant', lambda *a: [claimed('bid_split')])
    monkeypatch.setattr(acceptance.llm_queue, '_execute', lambda *a: pytest.fail('unrelated job executed'))
    await acceptance.assistant_loop(stop, application)
    assert stop.is_set() and application.state.phase6_acceptance_failed


@pytest.mark.asyncio
async def test_maintenance_calls_only_personal_sweeps_and_the_chat_outbox(monkeypatch):
    stop, calls = asyncio.Event(), []
    monkeypatch.setattr(acceptance.repo, 'browser_rpc', lambda *args: calls.append(args))
    monkeypatch.setattr(acceptance.repo, 'browser_sweep_v2', lambda *a, **k: calls.append(('v2', a, k)))
    monkeypatch.setattr(acceptance, 'drain_browser_chat', lambda: calls.append('outbox'))
    from app.services import assistant_maintenance
    monkeypatch.setattr(assistant_maintenance, 'run_browser_tasks', lambda: pytest.fail('general maintenance ran'))
    async def pause(*args):
        stop.set()
    monkeypatch.setattr(acceptance, 'pause', pause)
    await acceptance.browser_loop(stop)
    assert calls == [(None, 'assistant_browser_sweep', {'p_limit': 10}), ('v2', (None,), {'limit': 10}), 'outbox']


@pytest.mark.asyncio
async def test_one_failing_sweep_does_not_skip_the_others(monkeypatch):
    stop, calls = asyncio.Event(), []
    def boom(*args, **kwargs):
        raise RuntimeError('db down')
    monkeypatch.setattr(acceptance.repo, 'browser_rpc', boom)
    monkeypatch.setattr(acceptance.repo, 'browser_sweep_v2', boom)
    monkeypatch.setattr(acceptance, 'drain_browser_chat', lambda: calls.append('outbox'))
    async def pause(*args):
        stop.set()
    monkeypatch.setattr(acceptance, 'pause', pause)
    await acceptance.browser_loop(stop)
    assert calls == ['outbox']


def test_outbox_drain_is_bounded(monkeypatch):
    from app.services import assistant_browser_chat
    seen = []
    monkeypatch.setattr(assistant_browser_chat, 'drain_outbox', lambda **kw: seen.append(kw) or 0)
    assert acceptance.drain_browser_chat() == 0
    assert seen == [{'limit': 20}]


@pytest.mark.asyncio
async def test_lifespan_readiness_and_shutdown_only_manage_two_loops(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(acceptance, 'install_httpx_filter', lambda: calls.append('redactor'))
    monkeypatch.setattr(acceptance.llm_target, 'refresh', lambda: calls.append('target-once'))
    async def assistant(stop, application):
        calls.append('assistant')
        await stop.wait()
        calls.append('assistant-stopped')
    async def browser(stop):
        calls.append('browser')
        await stop.wait()
        calls.append('browser-stopped')
    monkeypatch.setattr(acceptance, 'assistant_loop', assistant)
    monkeypatch.setattr(acceptance, 'browser_loop', browser)
    acceptance.app.state.phase6_acceptance_ready = False
    assert acceptance.ready().status_code == 503
    async with acceptance.lifespan(acceptance.app):
        await asyncio.sleep(0)
        assert json.loads(acceptance.ready().body) == {'run': 'a' * 32, 'ready': True}
        acceptance.app.state.phase6_acceptance_failed = True
        assert acceptance.ready().status_code == 503
        acceptance.app.state.phase6_acceptance_failed = False
    assert acceptance.ready().status_code == 503
    assert set(calls) == {'redactor', 'target-once', 'assistant', 'browser', 'assistant-stopped', 'browser-stopped'}


def test_reconnect_script_requires_ready_and_serves_only_fixed_file(tmp_path, monkeypatch):
    path = tmp_path / 'constant.ps1'
    content = b'Write-Host "Public reconnect instructions"\r\n'
    path.write_bytes(content)
    monkeypatch.setattr(acceptance, 'RECONNECT_SCRIPT', path)
    acceptance.app.state.phase6_acceptance_ready = False
    assert acceptance.reconnect_script().status_code == 503
    acceptance.app.state.phase6_acceptance_ready = True
    acceptance.app.state.phase6_acceptance_failed = False
    acceptance.app.state.phase6_acceptance_run = 'a' * 32
    response = acceptance.reconnect_script()
    assert response.body == content
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-disposition'].startswith('attachment;')
    path.unlink()
    assert acceptance.reconnect_script().status_code == 503
    acceptance.app.state.phase6_acceptance_ready = False


def test_firewall_script_is_the_checked_worker_file_and_needs_ready(tmp_path, monkeypatch):
    assert acceptance.FIREWALL_SCRIPT.name == 'apply_user_pool_firewall.ps1' and acceptance.FIREWALL_SCRIPT.is_file()
    path = tmp_path / 'firewall.ps1'
    path.write_bytes(b'<# fixed firewall script #>\n')
    monkeypatch.setattr(acceptance, 'FIREWALL_SCRIPT', path)
    acceptance.app.state.phase6_acceptance_ready = False
    assert acceptance.firewall_script().status_code == 503
    acceptance.app.state.phase6_acceptance_ready = True
    acceptance.app.state.phase6_acceptance_failed = False
    acceptance.app.state.phase6_acceptance_run = 'a' * 32
    response = acceptance.firewall_script()
    assert response.body == path.read_bytes()
    assert 'apply_user_pool_firewall.ps1' in response.headers['content-disposition']
    path.unlink()
    assert acceptance.firewall_script().status_code == 503
    acceptance.app.state.phase6_acceptance_ready = False

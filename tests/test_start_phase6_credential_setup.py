"""Credential-only runner isolation without servers, sessions, or network."""
import base64
import errno
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('credential_setup', Path(__file__).parents[1] / 'scripts/start_phase6_credential_setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture(autouse=True)
def private_session_root(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'SESSION_ROOT', tmp_path / 'private-sessions')


def test_workspace_uses_private_durable_root_not_system_temp():
    first = setup.create_workspace()
    second = setup.create_workspace()
    assert first != second and first.parent == second.parent == setup.SESSION_ROOT
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in (first, second, setup.SESSION_ROOT))


def test_workspace_refuses_symlink_root(tmp_path):
    target = tmp_path / 'foreign'
    target.mkdir()
    setup.SESSION_ROOT.symlink_to(target, target_is_directory=True)
    with pytest.raises(setup.SetupError, match='symlink'):
        setup.create_workspace()
    assert list(target.iterdir()) == []


def key(ref=setup.DEV_REF, role='anon'):
    data = base64.urlsafe_b64encode(json.dumps({'ref': ref, 'role': role}).encode()).decode().rstrip('=')
    return 'header.' + data + '.signature'


@pytest.fixture
def source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'node_modules/.bin').mkdir(parents=True)
    (source / 'node_modules/.bin/next').write_text('fixture')
    (source / 'app').mkdir()
    (source / 'app/page.tsx').write_text('fresh dirty source')
    (source / '.next').mkdir()
    (source / '.next/lock').write_text('existing-server')
    (source / '.env.local').write_text(f'NEXT_PUBLIC_SUPABASE_URL={setup.DEV_URL}\nNEXT_PUBLIC_SUPABASE_ANON_KEY={key()}\nPRIVATE_LOCAL_SECRET=not-copied\n')
    return source


def test_environments_are_process_only_and_disable_execution(source):
    seen = []
    def settings(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(supabase_url=setup.DEV_URL)
    original = {'ASSISTANT_ENABLED': 'false', 'LLM_QUEUE_ENABLED': 'true'}
    saved = (source / '.env.local').read_bytes()
    before = dict(os.environ)
    backend, frontend = setup.process_environments(source, original, settings_type=settings)
    assert backend['ASSISTANT_ENABLED'] == backend['ASSISTANT_BROWSER_ENABLED'] == 'true'
    # Browser v2 (WI-20): both helpers run the v2 routes and hosted search.
    assert backend['ASSISTANT_BROWSER_V2_ENABLED'] == backend['ASSISTANT_HOSTED_SEARCH_ENABLED'] == 'true'
    assert seen[0]['assistant_browser_v2_enabled'] == seen[0]['assistant_hosted_search_enabled'] == 'true'
    assert backend['TEAMS_ALERTS_ENABLED'] == 'false'
    assert backend['LLM_QUEUE_ENABLED'] == 'false'
    assert all(backend[name] == 'false' for name in setup.OFF_FLAGS)
    assert len(backend['ASSISTANT_TOKEN_SECRET']) >= 32 and 'ASSISTANT_TOKEN_SECRET' not in frontend
    assert seen[0]['rfp_ingest_enabled'] is False and seen[0]['llm_queue_enabled'] == 'false'
    assert frontend['NEXT_PUBLIC_API_BASE_URL'] == 'http://localhost:5071'
    assert frontend['NEXT_PUBLIC_SUPABASE_URL'] == setup.DEV_URL
    assert original == {'ASSISTANT_ENABLED': 'false', 'LLM_QUEUE_ENABLED': 'true'}
    assert dict(os.environ) == before and (source / '.env.local').read_bytes() == saved


@pytest.mark.parametrize('values', [
    {'NEXT_PUBLIC_SUPABASE_URL': 'https://other.supabase.co'},
    {'NEXT_PUBLIC_SUPABASE_ANON_KEY': key('other')},
    {'NEXT_PUBLIC_SUPABASE_ANON_KEY': key(role='service_role')},
    {'NEXT_PUBLIC_SUPABASE_ANON_KEY': ''},
])
def test_frontend_wrong_project_or_private_key_refused(source, values):
    with pytest.raises(setup.SetupError):
        setup.frontend_values(source, values)


def test_config_refusal_names_the_problem_never_the_values(source):
    from pydantic import BaseModel, model_validator

    class Strict(BaseModel):
        browser_hold_signing_key: str = ''
        user_cred_kek: str = 'private-kek-value-not-printed'

        @model_validator(mode='after')
        def _v2(self):
            raise ValueError('BROWSER_HOLD_SIGNING_KEY must be set (base64 of 32 random bytes).')

    def settings(**kwargs):
        kwargs.pop('_env_file')
        return Strict(**{'user_cred_kek': 'private-kek-value-not-printed'})

    with pytest.raises(setup.SetupError) as caught:
        setup.process_environments(source, {}, settings_type=settings)
    text = str(caught.value)
    assert 'BROWSER_HOLD_SIGNING_KEY must be set' in text and 'BROWSER_V2_RELEASE_RUNBOOK' in text
    assert 'private-kek-value-not-printed' not in text


def test_backend_wrong_project_refused_before_copy(source):
    with pytest.raises(setup.SetupError):
        setup.process_environments(source, {}, settings_type=lambda **kw: SimpleNamespace(supabase_url='https://other.supabase.co'))


def test_scratch_copy_is_separate_and_never_copies_secrets_or_next_lock(source, tmp_path):
    os.utime(source / 'app/page.tsx', (1, 1))
    (source / 'tsconfig.tsbuildinfo').write_text('stale build cache')
    work = tmp_path / 'scratch'
    work.mkdir()
    target = setup.scratch_frontend(source, work)
    assert (target / 'app/page.tsx').read_text() == 'fresh dirty source'
    assert (target / 'app/page.tsx').stat().st_mtime > 1
    assert not (target / 'tsconfig.tsbuildinfo').exists()
    assert not (target / '.next').exists()
    assert (target / 'node_modules').is_symlink()
    assert (target / '.env.local').is_symlink()
    assert (target / '.env.local').resolve() == source / '.env.local'
    setup.shutil.rmtree(target)
    assert (source / '.next/lock').read_text() == 'existing-server'
    assert (source / '.env.local').exists()


def test_busy_ports_refuse_without_terminating_anything(monkeypatch):
    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def setsockopt(self, *args):
            pass
        def bind(self, address):
            raise OSError(errno.EADDRINUSE, 'busy')
    monkeypatch.setattr(setup.socket, 'socket', lambda *args: Socket())
    with pytest.raises(setup.SetupError, match='5071 is already in use.*EADDRINUSE'):
        setup.require_free_ports()


def test_recently_closed_ports_can_rebind_without_ipv6_or_reuseport(monkeypatch):
    checked = []

    class Socket:
        reuse = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def setsockopt(self, level, option, value):
            assert (level, option, value) == (setup.socket.SOL_SOCKET, setup.socket.SO_REUSEADDR, 1)
            self.reuse = True
        def bind(self, address):
            if not self.reuse:
                raise OSError(errno.EADDRINUSE, 'TIME_WAIT')
            checked.append(address)
        def listen(self, backlog):
            assert backlog == 1

    def socket(family, kind):
        assert family == setup.socket.AF_INET
        return Socket()

    monkeypatch.setattr(setup.socket, 'socket', socket)
    setup.require_free_ports()
    assert checked == [('127.0.0.1', 5071), ('127.0.0.1', 4510)]


def test_port_permission_failure_is_not_reported_as_occupied(monkeypatch):
    def socket(*args):
        raise PermissionError(errno.EPERM, 'not allowed')
    monkeypatch.setattr(setup.socket, 'socket', socket)
    with pytest.raises(setup.SetupError, match='5071 is not permitted.*EPERM'):
        setup.require_free_ports()


def test_main_uses_lifespan_off_webpack_and_only_owns_new_children(source, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, 'FRONTEND', source)
    monkeypatch.setattr(setup, 'process_environments', lambda *a: ({'LLM_QUEUE_ENABLED': 'false'}, {}))
    monkeypatch.setattr(setup, 'require_free_ports', lambda: None)
    workspace = tmp_path / 'owned'
    workspace.mkdir()
    monkeypatch.setattr(setup.tempfile, 'mkdtemp', lambda **kw: str(workspace))
    launched, stopped = [], []
    def launch(command, **kwargs):
        child = SimpleNamespace(poll=lambda: None)
        launched.append((command, kwargs, child))
        return child
    monkeypatch.setattr(setup.subprocess, 'Popen', launch)
    monkeypatch.setattr(setup, 'stop_owned', lambda child: stopped.append(child))
    monkeypatch.setattr(setup, 'wait_ready', lambda *a, **kw: None)
    monkeypatch.setattr(setup.time, 'sleep', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert setup.main() == 0
    assert launched[0][0][-2:] == ['--lifespan', 'off']
    assert launched[0][1]['env']['LLM_QUEUE_ENABLED'] == 'false'
    assert '--webpack' in launched[1][0] and '4510' in launched[1][0]
    assert all(options['start_new_session'] for _, options, _ in launched)
    assert stopped == [launched[1][2], launched[0][2]]
    assert not (workspace / 'frontend').exists()
    assert (workspace / 'backend.log').stat().st_mode & 0o777 == 0o600
    assert (source / '.env.local').exists() and (source / '.next/lock').exists()
    text = capsys.readouterr().out
    assert 'http://localhost:4510/settings/assistant' in text and 'not-copied' not in text


def test_cleanup_signals_owned_group_even_when_parent_already_exited(monkeypatch):
    calls = []
    child = SimpleNamespace(pid=12345, poll=lambda: 0, wait=lambda **kw: 0)
    monkeypatch.setattr(setup.os, 'killpg', lambda pid, sig: calls.append((pid, sig)))
    setup.stop_owned(child)
    assert calls == [(12345, setup.signal.SIGTERM), (12345, setup.signal.SIGKILL)]


WORKER = 'a1b2c3d4-0000-4000-8000-000000000001'


def test_acceptance_environment_is_explicit_narrow_and_does_not_change_original():
    original = {'LLM_QUEUE_ENABLED': 'false', 'ASSISTANT_SCHEDULER_ENABLED': 'false',
                'TEAMS_ALERTS_ENABLED': 'false', 'PHASE6_ACCEPTANCE_WORKER': 'stale'}
    env = setup.acceptance_environment(original)
    assert original['LLM_QUEUE_ENABLED'] == 'false' and original['TEAMS_ALERTS_ENABLED'] == 'false'
    assert env['LLM_QUEUE_ENABLED'] == 'true' and env['ASSISTANT_CONCURRENCY'] == '1'
    assert env['LLM_QUEUE_CLAIM_TYPES'].split(',') == ['assistant_turn', 'assistant_approval_execute', 'assistant_continue']
    assert env['PHASE6_ACCEPTANCE'] == 'true' and env['ASSISTANT_SCHEDULER_ENABLED'] == 'false'
    assert env['ASSISTANT_BROWSER_V2_ENABLED'] == env['ASSISTANT_HOSTED_SEARCH_ENABLED'] == 'true'
    assert env['TEAMS_ALERTS_ENABLED'] == 'true'  # acceptance step 3 (the owner's Teams alert)
    assert env['ASSISTANT_OUTLOOK_ENABLED'] == 'true'  # step 4 (the email code reader)
    assert set(setup.ACCEPTANCE_ON_FLAGS) <= set(setup.OFF_FLAGS)
    assert 'PHASE6_ACCEPTANCE_WORKER' not in env
    assert env['PHASE6_ACCEPTANCE_RUN'] != setup.acceptance_environment(original)['PHASE6_ACCEPTANCE_RUN']
    assert setup.acceptance_environment(original, WORKER.upper())['PHASE6_ACCEPTANCE_WORKER'] == WORKER
    with pytest.raises(setup.SetupError):
        setup.acceptance_environment(original, 'not-a-worker')


def test_reconnect_command_checks_download_hash_before_execution():
    import hashlib
    template = b'checked reconnect script'
    command = setup.reconnect_commands('https://phase-six-test.trycloudflare.com', template, WORKER)
    assert command.startswith("& {\n$ErrorActionPreference = 'Stop'")
    assert hashlib.sha256(template).hexdigest() in command
    assert command.index('Invoke-WebRequest') < command.index('Get-FileHash') < command.index('powershell -ExecutionPolicy')
    assert f"-ApiBase $api -ExpectedWorkerId '{WORKER}'" in command
    assert command.endswith('throw "Personal worker reconnect failed" }\n}')
    for url in ('http://bad.test', "https://x.trycloudflare.com';bad", 'https://x.trycloudflare.com/path'):
        with pytest.raises(setup.SetupError):
            setup.reconnect_commands(url, template, WORKER)
    for worker in ("x'; Remove-Item C:\\", '', None):
        with pytest.raises(setup.SetupError):
            setup.reconnect_commands('https://phase-six-test.trycloudflare.com', template, worker)


def test_reenroll_command_replaces_the_launcher_and_never_carries_the_code():
    command = setup.reenroll_commands('https://phase-six-test.trycloudflare.com')
    assert "$api = 'https://phase-six-test.trycloudflare.com'" in command
    assert '"$api/agent-worker/v1/installer.ps1"' in command
    assert '-Pool user -Reenroll' in command and '-ApiBase $api' in command
    assert "-PythonPath 'C:\\Program Files\\BDR Python 3.12.10\\tools\\python.exe'" in command
    assert '-Code' not in command
    assert command.endswith('throw "BDR personal re-enrollment failed" }\n}')
    for url in ('http://bad.test', 'https://x.trycloudflare.com/path'):
        with pytest.raises(setup.SetupError):
            setup.reenroll_commands(url)


def test_firewall_command_checks_the_hash_before_running_elevated():
    import hashlib
    script = setup.FIREWALL_SCRIPT.read_bytes()
    assert script.startswith(b'<#') and b'BDR user pool floor' in script
    command = setup.firewall_commands('https://phase-six-test.trycloudflare.com', script)
    assert hashlib.sha256(script).hexdigest() in command
    assert (command.index('/phase6-acceptance/firewall.ps1') < command.index('Get-FileHash')
            < command.index('powershell -ExecutionPolicy Bypass -File'))
    assert command.endswith('throw "User pool firewall script failed" }\n}')
    with pytest.raises(setup.SetupError):
        setup.firewall_commands('https://x.trycloudflare.com/path', script)


def preflight_db(problem):
    from tests.browser_agents_fake import FakeDB
    db = FakeDB()
    worker = {'id': WORKER, 'principal_kind': 'user', 'status': 'active', 'version': '1.0.5',
              'allowed_types': ['user_browser_read']}
    db.tables['browser_agent_workers'] = [] if problem == 'no_worker' else [worker]
    if problem == 'pending_worker':
        worker['status'] = 'pending'
    if problem == 'revoked_extra':
        db.tables['browser_agent_workers'].append({**worker, 'id': 'old', 'status': 'revoked'})
    if problem == 'extra_worker':
        db.tables['browser_agent_workers'].append({**worker, 'id': 'other'})
    if problem == 'org_type':
        worker['allowed_types'] = ['rfi_from_email']
    if problem == 'org_worker':
        db.tables['browser_agent_workers'].append({**worker, 'id': 'org', 'principal_kind': 'org'})
    db.tables['browser_agent_types'] = [{'key': 'user_browser_read', 'enabled': False},
                                        {'key': 'user_browser', 'enabled': problem == 'enabled_type'}]
    if problem == 'missing_type':
        db.tables['browser_agent_types'] = db.tables['browser_agent_types'][:1]
    db.tables['llm_jobs'] = []
    if problem == 'pending_job':
        db.tables['llm_jobs'] = [{'id': 'existing', 'job_type': 'assistant_turn', 'status': 'queued'}]
    db.tables['browser_agent_jobs'] = [{'id': 'done', 'agent_type': 'user_browser', 'status': 'succeeded'}]
    if problem == 'open_browser_job':
        db.tables['browser_agent_jobs'].append({'id': 'open', 'agent_type': 'user_browser', 'status': 'waiting_help'})
    return db


@pytest.mark.parametrize('problem,expected', [
    (None, WORKER), ('no_worker', None), ('pending_worker', None), ('revoked_extra', WORKER),
    ('org_worker', WORKER), ('extra_worker', 'refused'), ('org_type', 'refused'), ('enabled_type', 'refused'),
    ('missing_type', 'refused'), ('pending_job', 'refused'), ('open_browser_job', 'refused'),
])
def test_acceptance_preflight_refuses_changed_scope(monkeypatch, problem, expected):
    from app.core import config, supabase_client
    db = preflight_db(problem)
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(supabase_url=setup.DEV_URL))
    monkeypatch.setattr(supabase_client, 'get_supabase', lambda: db)
    if expected == 'refused':
        with pytest.raises(setup.SetupError):
            setup.acceptance_preflight()
    else:
        # The worker version is not checked: it upgrades through this server.
        assert setup.acceptance_preflight() == expected


@pytest.mark.parametrize('problem', [None, 'no_worker', 'pending_worker', 'extra_worker'])
def test_personal_worker_id_needs_exactly_one_approved_row(problem):
    db = preflight_db(problem)
    if problem is None:
        assert setup.personal_worker_id(db) == WORKER
    else:
        with pytest.raises(setup.SetupError):
            setup.personal_worker_id(db)


def test_acceptance_mode_starts_only_custom_lifespan_and_owns_tunnel(source, tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'FRONTEND', source)
    monkeypatch.setattr(setup, 'process_environments', lambda *a: ({}, {}))
    monkeypatch.setattr(setup, 'acceptance_preflight', lambda: WORKER)
    monkeypatch.setattr(setup, 'require_free_ports', lambda: None)
    workspace = tmp_path / 'controlled'
    workspace.mkdir()
    monkeypatch.setattr(setup.tempfile, 'mkdtemp', lambda **kw: str(workspace))
    launched, stopped, tunnel_calls = [], [], []
    def launch(command, **kwargs):
        child = SimpleNamespace(poll=lambda: None)
        launched.append((command, kwargs, child))
        return child
    monkeypatch.setattr(setup.subprocess, 'Popen', launch)
    monkeypatch.setattr(setup, 'stop_owned', lambda child, **kw: stopped.append((child, kw)))
    monkeypatch.setattr(setup, 'wait_ready', lambda *a, **kw: None)
    tunnel = SimpleNamespace(poll=lambda: None)
    def connect(client, children, env):
        tunnel_calls.append(env)
        children.append(tunnel)
    monkeypatch.setattr(setup, 'connect_acceptance_tunnel', connect)
    monkeypatch.setattr(setup.time, 'sleep', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert setup.main(controlled_read=True) == 0
    assert launched[0][0][3] == 'scripts.phase6_acceptance_app:app'
    assert launched[0][0][-4:] == ['--workers', '2', '--lifespan', 'on']
    assert tunnel_calls[0]['PHASE6_ACCEPTANCE'] == 'true'
    assert tunnel_calls[0]['PHASE6_ACCEPTANCE_WORKER'] == WORKER
    assert launched[0][1]['env']['ASSISTANT_BROWSER_V2_ENABLED'] == 'true'
    assert stopped[0] == (tunnel, {'grace': 40})
    assert len(stopped) == 3 and not (workspace / 'frontend').exists()

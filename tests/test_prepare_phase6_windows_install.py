"""Offline development preparation guards. No downloads, tunnels, or uploads."""
from __future__ import annotations
import base64
import hashlib
import importlib.util
import io
import json
import socket
from pathlib import Path
from types import SimpleNamespace
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.browser_agents_fake import FakeDB

spec = importlib.util.spec_from_file_location('prepare_phase6_windows_install', Path(__file__).parents[1] / 'scripts/prepare_phase6_windows_install.py')
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)
API = 'https://fresh-personal-test.trycloudflare.com'
CATALOG = 'https://api.nuget.org/v3/catalog0/data/2025.04.08/python.3.12.10.json'


class Process:
    def __init__(self, logs='', exit_code=None):
        self.stdout = io.StringIO(logs)
        self.code = exit_code
        self.terminated = False
    def poll(self):
        return self.code
    def terminate(self):
        self.terminated = True
        self.code = -15
    def wait(self, timeout=None):
        return self.code


class Client:
    def __init__(self, values):
        self.values, self.calls = values, []
    def get(self, url, **kwargs):
        assert kwargs.get('follow_redirects') is False
        assert set(kwargs) <= {'follow_redirects', 'timeout'}
        if 'timeout' in kwargs:
            assert kwargs['timeout'] == (5 if url.endswith('/health') else 20)
        self.calls.append(url)
        value = self.values[url]
        if callable(value):
            value = value()
        if isinstance(value, dict):
            value = json.dumps(value).encode()
        if isinstance(value, tuple):
            return SimpleNamespace(status_code=value[0], content=value[1], url=url)
        return SimpleNamespace(status_code=200, content=value, url=url)


@pytest.fixture
def env(tmp_path):
    key = Ed25519PrivateKey.generate()
    pub = prep.publisher.public_raw(key)
    bundle = tmp_path / 'bundle.zip'
    bundle.write_bytes(b'signed worker archive')
    db = FakeDB()
    row = prep.publisher.publish(db, version='1.0.1', min_version='1.0.0', bundle=bundle, key=key)
    package = b'official python fixture'
    catalog = {'id': 'python', 'version': '3.12.10', 'packageHashAlgorithm': 'SHA512',
               'packageHash': base64.b64encode(hashlib.sha512(package).digest()).decode(), 'packageSize': len(package)}
    client = Client({prep.REGISTRATION_URL: {'catalogEntry': CATALOG}, CATALOG: catalog,
                     prep.PACKAGE_URL: package, 'http://127.0.0.1:5051/health': {'status': 'ok'}})
    settings = SimpleNamespace(supabase_url=prep.DEV_URL, browser_agent_release_pubkey=base64.b64encode(pub).decode())
    envpath = tmp_path / '.env'
    envpath.write_bytes(b'ASSISTANT_BROWSER_ENABLED=false\r\nOTHER_SECRET=private\r\nBROWSER_AGENT_API_BASE_URL=old\r\n')
    envpath.chmod(0o600)
    original = envpath.read_bytes()
    process = Process()
    def installer():
        data = db.storage.store[(prep.publisher.BUCKET, prep.publisher.INSTALLER_TEMPLATE_PATH)]
        release = {'manifest': row['manifest'], 'signature': row['signature'], 'url': 'https://storage.test/release'}
        return data.replace(b'__RELEASE_JSON_B64__', base64.b64encode(json.dumps(release).encode()))
    client.values[API + '/agent-worker/v1/python-runtime.ps1'] = lambda: db.storage.store[(prep.publisher.BUCKET, prep.RUNTIME_PATH)]
    client.values[API + '/agent-worker/v1/installer.ps1'] = installer
    client.values['http://127.0.0.1:5051/agent-worker/v1/python-runtime.ps1'] = client.values[API + '/agent-worker/v1/python-runtime.ps1']
    client.values['http://127.0.0.1:5051/agent-worker/v1/installer.ps1'] = installer
    return SimpleNamespace(db=db, row=row, key=key, pub=pub, package=package, catalog=catalog,
        client=client, settings=settings, envpath=envpath, original=original,
        process=process, output=tmp_path / 'public', tunnel=lambda: (process, API))


def prepare(env):
    return prep.prepare(env.settings, env.db, env.client, output=env.output, env_path=env.envpath, tunnel_factory=env.tunnel)


@pytest.mark.parametrize('url', ['', 'https://production.supabase.co', 'http://bpidntbyvoooqvaispup.supabase.co',
    prep.DEV_URL + '.evil.test', prep.DEV_URL + ':443', prep.DEV_URL + '?x=1'])
def test_wrong_target_refused_before_any_io(env, url):
    env.settings.supabase_url = url
    with pytest.raises(prep.PreparationError, match='exact BDR development'):
        prepare(env)
    assert not env.client.calls and not env.output.exists()


def test_success_uses_public_key_only_preserves_artifacts_and_changes_only_api(env, monkeypatch):
    monkeypatch.setattr(prep.publisher, 'load_private_key', lambda *a: pytest.fail('private key opened'))
    before_tables = json.dumps(env.db.tables, default=str, sort_keys=True)
    process, api = prepare(env)
    assert process is env.process and api == API and not process.terminated
    assert env.envpath.read_bytes() == env.original.replace(b'BROWSER_AGENT_API_BASE_URL=old', f'BROWSER_AGENT_API_BASE_URL="{API}"'.encode())
    assert env.envpath.stat().st_mode & 0o777 == 0o600
    assert json.dumps(env.db.tables, default=str, sort_keys=True) == before_tables
    assert (env.output / 'python.3.12.10.nupkg').read_bytes() == env.package
    assert (env.output / 'bdr-agent-1.0.1.zip').read_bytes() == b'signed worker archive'
    runtime = (env.output / 'setup-python-runtime.ps1').read_bytes()
    assert prep.TOKEN.encode() not in runtime and hashlib.sha256(env.package).hexdigest().encode() in runtime
    assert env.db.storage.store[(prep.publisher.BUCKET, prep.RUNTIME_PATH)] == runtime
    command = prep.windows_commands(api)
    assert command.startswith("& {\n$ErrorActionPreference = 'Stop'\n")
    assert command.endswith('if ($LASTEXITCODE -ne 0) { throw "BDR personal installer failed" }\n}')
    assert '-Pool user' in command and '-ApiBase $api' in command
    assert r'C:\Program Files\BDR Python 3.12.10\tools\python.exe' in command
    assert 'private' not in command and 'OTHER_SECRET' not in command


@pytest.mark.parametrize('corrupt', ['bundle', 'signature', 'key', 'path', 'absent'])
def test_release_verification_before_http_and_upload(env, corrupt):
    rows = env.db.tables['browser_agent_worker_releases']
    if corrupt == 'bundle':
        env.db.storage.store[(prep.publisher.BUCKET, env.row['storage_path'])] = b'tampered'
    elif corrupt == 'signature':
        rows[0]['signature'] = base64.b64encode(b'x' * 64).decode()
    elif corrupt == 'key':
        env.settings.browser_agent_release_pubkey = base64.b64encode(b'x' * 32).decode()
    elif corrupt == 'path':
        rows[0]['storage_path'] = 'releases/other.zip'
    else:
        rows.clear()
    with pytest.raises(prep.PreparationError):
        prepare(env)
    assert not env.client.calls and env.envpath.read_bytes() == env.original


@pytest.mark.parametrize('field,value', [('id', 'another-package'), ('version', '3.12.9'),
    ('packageHashAlgorithm', 'SHA256'), ('packageHash', 'bad'), ('packageSize', True),
    ('packageSize', 999), ('packageHash', base64.b64encode(b'x' * 64).decode())])
def test_wrong_nuget_metadata_hash_or_size_never_uploads(env, field, value):
    env.catalog[field] = value
    with pytest.raises((prep.PreparationError, ValueError)):
        prepare(env)
    assert (prep.publisher.BUCKET, prep.RUNTIME_PATH) not in env.db.storage.store
    assert env.envpath.read_bytes() == env.original


@pytest.mark.parametrize('url', ['http://api.nuget.org/v3/catalog.json', 'https://api.nuget.org.evil.test/v3/catalog.json',
    'https://api.nuget.org:443/v3/catalog.json', 'https://evil.test/catalog.json'])
def test_untrusted_catalog_url_never_fetched(env, url):
    env.client.values[prep.REGISTRATION_URL] = {'catalogEntry': url}
    with pytest.raises(prep.PreparationError, match='NuGet catalog URL'):
        prepare(env)
    assert url not in env.client.calls


def test_redirects_refused(env):
    env.client.values[prep.REGISTRATION_URL] = (302, b'')
    with pytest.raises(prep.PreparationError, match='redirected'):
        prepare(env)


@pytest.mark.parametrize('path', ['python-runtime.ps1', 'installer.ps1'])
def test_endpoint_failure_stops_owned_tunnel_and_leaves_environment_unchanged(env, path):
    env.client.values[API + '/agent-worker/v1/' + path] = (404, b'not found')
    with pytest.raises(prep.PreparationError):
        prepare(env)
    assert env.process.terminated and env.envpath.read_bytes() == env.original


def test_stale_installer_or_wrong_selected_release_never_changes_environment(env):
    env.client.values[API + '/agent-worker/v1/installer.ps1'] = b'unrelated installer'
    with pytest.raises(prep.PreparationError, match='Select the verified'):
        prepare(env)
    assert env.process.terminated and env.envpath.read_bytes() == env.original


def test_hash_placeholder_must_be_exactly_once(env):
    for template in (b'no placeholder', (prep.TOKEN * 2).encode()):
        with pytest.raises(prep.PreparationError, match='exactly one'):
            prep.render_runtime(template, env.package)


def test_tunnel_waits_for_both_url_and_registered_connection():
    process = Process(API + '\nRegistered tunnel connection connIndex=0\n')
    seen = []
    def spawn(args, **kwargs):
        seen.append((args, kwargs))
        return process
    actual, url = prep.start_tunnel(popen=spawn, timeout=1)
    assert actual is process and url == API and not process.terminated
    assert seen[0][0] == ['cloudflared', 'tunnel', '--no-autoupdate', '--url', 'http://127.0.0.1:5051']
    assert seen[0][1]['start_new_session'] is True


@pytest.mark.parametrize('logs,exit_code', [(API + '\n', None), ('Registered tunnel connection\n', None), ('', 1)])
def test_tunnel_failure_cleans_up_only_created_process(logs, exit_code):
    process = Process(logs, exit_code=exit_code)
    with pytest.raises(prep.PreparationError):
        prep.start_tunnel(popen=lambda *a, **kw: process, timeout=0.01)
    assert process.terminated or exit_code is not None
    assert process.stdout.closed


def test_environment_duplicate_and_concurrent_change_refused(env):
    with pytest.raises(prep.PreparationError, match='duplicate'):
        prep.changed_environment('BROWSER_AGENT_API_BASE_URL=a\nBROWSER_AGENT_API_BASE_URL=b\n', API)
    env.envpath.write_text('changed externally')
    with pytest.raises(prep.PreparationError, match='changed'):
        prep.write_environment(env.envpath, env.original, 'new')
    assert env.envpath.read_text() == 'changed externally'


def test_upload_failure_cleans_owned_tunnel_and_keeps_environment(env, monkeypatch):
    monkeypatch.setattr(prep.publisher, 'upload_installer_template', lambda *a: (_ for _ in ()).throw(RuntimeError('failed upload')))
    with pytest.raises(RuntimeError):
        prepare(env)
    assert env.process.terminated and env.envpath.read_bytes() == env.original


def test_transient_endpoint_errors_retry_without_touching_other_processes(env, monkeypatch):
    real = prep.verify_endpoints
    attempts = []
    def flaky(*args):
        if args[1] != API:
            return real(*args)
        attempts.append(1)
        if len(attempts) == 1:
            raise prep.DownloadUnavailable("DNS unavailable")
        if len(attempts) == 2:
            raise prep.DownloadUnavailable("Bad gateway", 502)
        return real(*args)
    monkeypatch.setattr(prep, "verify_endpoints", flaky)
    original_verify = prep.verify_live_endpoints
    monkeypatch.setattr(prep, "verify_live_endpoints", lambda *args: original_verify(*args, sleep=lambda _: None))
    process, api = prepare(env)
    assert len(attempts) == 3 and api == API and not process.terminated


def test_transient_timeout_cleans_up_and_does_not_update_environment(env, monkeypatch):
    real_endpoints = prep.verify_endpoints
    def unavailable(*args):
        if args[1] != API:
            return real_endpoints(*args)
        raise prep.DownloadUnavailable("DNS unavailable")
    monkeypatch.setattr(prep, "verify_endpoints", unavailable)
    real = prep.verify_live_endpoints
    clock = iter([0, 1, 2, 3])
    monkeypatch.setattr(prep, "verify_live_endpoints", lambda *args: real(*args, timeout=2, clock=lambda: next(clock), sleep=lambda _: None))
    with pytest.raises(prep.PreparationError, match="did not become reachable"):
        prepare(env)
    assert env.process.terminated and env.envpath.read_bytes() == env.original


def test_runtime_404_explains_backend_reload_without_retry(env):
    env.client.values[API + '/agent-worker/v1/python-runtime.ps1'] = (404, b'')
    with pytest.raises(prep.PreparationError, match="Reload the local BDR backend"):
        prepare(env)
    assert env.client.calls.count(API + '/agent-worker/v1/python-runtime.ps1') == 1


def test_live_metadata_mismatch_does_not_retry(env):
    env.client.values[API + '/agent-worker/v1/python-runtime.ps1'] = b'wrong script'
    with pytest.raises(prep.PreparationError, match="newly verified script"):
        prepare(env)
    assert env.client.calls.count(API + '/agent-worker/v1/python-runtime.ps1') == 1
    assert env.process.terminated and env.envpath.read_bytes() == env.original


@pytest.mark.parametrize("path,status", [("python-runtime.ps1", 404), ("installer.ps1", 500)])
def test_local_endpoint_failure_is_distinguished_before_public_probe(env, path, status):
    local = 'http://127.0.0.1:5051/agent-worker/v1/' + path
    env.client.values[local] = (status, b'response-secret-body')
    with pytest.raises(prep.PreparationError, match='Local BDR install endpoint verification failed') as failure:
        prepare(env)
    assert '127.0.0.1:5051/agent-worker/v1/' + path in str(failure.value)
    assert f'HTTP {status}' in str(failure.value)
    assert 'response-secret-body' not in str(failure.value)
    assert not any(url.startswith(API) for url in env.client.calls)
    assert env.process.terminated and env.envpath.read_bytes() == env.original


@pytest.mark.parametrize("kind", ['http', 'dns', 'read_timeout', 'connect'])
def test_download_diagnostics_never_include_query_userinfo_or_body(kind):
    url = 'https://username:private-password@example.test/script.ps1?token=secret-query#secret-fragment'
    if kind == 'http':
        client = Client({url: (502, b'private-response-body')})
        expected = 'HTTP 502'
    else:
        def fail():
            if kind == 'dns':
                try:
                    raise socket.gaierror('private-dns-error')
                except socket.gaierror as cause:
                    raise prep.httpx.ConnectError('private-request-secret') from cause
            if kind == 'read_timeout':
                raise prep.httpx.ReadTimeout('private-response-body')
            raise prep.httpx.ConnectError('secret-query')
        client = Client({url: fail})
        expected = {'dns': 'DNS resolution failure', 'read_timeout': 'ReadTimeout', 'connect': 'ConnectError'}[kind]
    with pytest.raises(prep.DownloadUnavailable) as failure:
        prep.fetch(client, url)
    diagnostic = str(failure.value)
    assert diagnostic == f'example.test/script.ps1: {expected}.'
    assert 'private' not in diagnostic and 'secret' not in diagnostic and 'username' not in diagnostic


def test_progress_reports_first_then_each_fifteen_seconds_and_preserves_last_error(env, monkeypatch, capsys):
    attempts = []
    def failed(*args):
        attempts.append(1)
        raise prep.DownloadUnavailable(f'public.test/installer.ps1: HTTP {502 if len(attempts) == 1 else 503}.', 503)
    monkeypatch.setattr(prep, 'verify_endpoints', failed)
    times = iter([0, 1, 2, 16, 20])
    with pytest.raises(prep.PreparationError, match='Last check: public.test/installer.ps1: HTTP 503'):
        prep.verify_live_endpoints(env.client, API, b'', b'', env.pub, env.process, timeout=20, clock=lambda: next(times), sleep=lambda _: None)
    output = capsys.readouterr().out
    assert output.count('Waiting for public tunnel endpoints') == 2
    assert 'HTTP 502' in output and 'HTTP 503' in output

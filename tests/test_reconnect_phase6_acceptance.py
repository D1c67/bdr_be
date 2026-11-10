"""Repair the tunnel without restarting the API or requiring an upgraded worker."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import httpx

from scripts import reconnect_phase6_acceptance as repair

WORKER = 'a1b2c3d4-0000-4000-8000-000000000001'


@pytest.mark.parametrize('value', [None, {}, {'ready': False}, {'run': 'a' * 32, 'ready': 1},
    {'run': 'private-invalid', 'ready': True}, {'run': 12, 'ready': True},
    {'run': 'a' * 32, 'ready': True, 'extra': 'refused'}])
def test_invalid_existing_api_refuses_before_tunnel(monkeypatch, value):
    monkeypatch.setattr(repair.preparation, 'fetch', lambda *a, **k: json.dumps(value).encode())
    with pytest.raises(repair.setup.SetupError, match='not a ready'):
        repair.existing_run(object())


@pytest.mark.parametrize('failure', [None, 'verification', 'interrupt'])
def test_only_new_tunnel_is_owned_and_same_run_is_verified(monkeypatch, failure):
    monkeypatch.setattr(repair, 'get_settings', lambda: SimpleNamespace(supabase_url=repair.setup.DEV_URL))
    fetch = Mock(return_value=json.dumps({'run': 'a' * 32, 'ready': True}).encode())
    monkeypatch.setattr(repair.preparation, 'fetch', fetch)
    child = SimpleNamespace(poll=lambda: 1)
    stopped = Mock()
    monkeypatch.setattr(repair.preparation, 'stop_tunnel', stopped)
    preflight = Mock(side_effect=AssertionError('Worker-upgrade preflight must not block tunnel repair'))
    monkeypatch.setattr(repair.setup, 'acceptance_preflight', preflight)
    monkeypatch.setattr(repair.setup, 'personal_worker_id', lambda: WORKER)

    def connect(client, children, env):
        assert env == {'PHASE6_ACCEPTANCE_RUN': 'a' * 32, 'PHASE6_ACCEPTANCE': 'true',
                       'PHASE6_ACCEPTANCE_WORKER': WORKER}
        children.append(child)
        if failure == 'verification':
            raise repair.setup.SetupError('Remote instance mismatch')
        if failure == 'interrupt':
            raise KeyboardInterrupt

    monkeypatch.setattr(repair.setup, 'connect_acceptance_tunnel', connect)
    assert repair.main() == (0 if failure == 'interrupt' else 1)
    stopped.assert_called_once_with(child)
    preflight.assert_not_called()
    assert fetch.call_args.args[1] == 'http://127.0.0.1:5071/phase6-acceptance/ready'


def _no_single_worker(monkeypatch, rows):
    from app.core import supabase_client

    monkeypatch.setattr(repair, 'get_settings', lambda: SimpleNamespace(supabase_url=repair.setup.DEV_URL))
    monkeypatch.setattr(repair.preparation, 'fetch', Mock(return_value=json.dumps({'run': 'a' * 32, 'ready': True}).encode()))
    def none():
        raise repair.setup.SetupError('Exactly one approved personal worker is needed')
    monkeypatch.setattr(repair.setup, 'personal_worker_id', none)
    monkeypatch.setattr(supabase_client, 'get_supabase', lambda: object())
    monkeypatch.setattr(repair.setup, 'personal_workers', lambda sb: rows)
    monkeypatch.setattr(repair.preparation, 'stop_tunnel', Mock())


def test_two_approved_workers_refuse_before_any_tunnel(monkeypatch):
    _no_single_worker(monkeypatch, [{'id': WORKER, 'status': 'active'}, {'id': WORKER, 'status': 'active'}])
    connect = Mock()
    monkeypatch.setattr(repair.setup, 'connect_acceptance_tunnel', connect)
    assert repair.main() == 1
    connect.assert_not_called()


@pytest.mark.parametrize('rows', [[], [{'id': WORKER, 'status': 'pending'}]])
def test_the_reenrollment_window_still_gets_a_tunnel_and_the_reenroll_blocks(monkeypatch, rows):
    """Between revoking the old worker row and approving the new one there is
    no approved worker: the replacement tunnel still starts, verifies the
    firewall endpoint and prints the re-enroll and firewall blocks."""
    _no_single_worker(monkeypatch, rows)
    seen = {}

    def connect(client, children, env):
        seen.update(env)
        raise KeyboardInterrupt

    monkeypatch.setattr(repair.setup, 'connect_acceptance_tunnel', connect)
    assert repair.main() == 0
    assert seen == {'PHASE6_ACCEPTANCE_RUN': 'a' * 32, 'PHASE6_ACCEPTANCE': 'true'}


def test_other_project_refused_before_local_or_public_network(monkeypatch):
    monkeypatch.setattr(repair, 'get_settings', lambda: SimpleNamespace(supabase_url='https://other.supabase.co'))
    fetch = Mock()
    monkeypatch.setattr(repair.preparation, 'fetch', fetch)
    assert repair.main() == 1
    fetch.assert_not_called()


@pytest.fixture
def tunnel_check(monkeypatch):
    child = SimpleNamespace(poll=lambda: None)
    api = 'https://replacement-check.trycloudflare.com'
    monkeypatch.setattr(repair.preparation, 'start_tunnel', lambda **kw: (child, api))
    clock = [0]
    monkeypatch.setattr(repair.setup.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(repair.setup.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    template = (repair.setup.ROOT / 'scripts/phase6_reconnect_personal_worker.ps1').read_bytes()
    return child, api, clock, template


@pytest.mark.parametrize('stage', ['ready', 'script'])
@pytest.mark.parametrize('status', [530, 502])
def test_transient_tunnel_response_retries_both_verified_endpoints(tunnel_check, capsys, stage, status):
    child, api, clock, template = tunnel_check
    calls = []
    failed = False

    def get(url, **kwargs):
        nonlocal failed
        calls.append(url)
        is_ready = url.endswith('/ready')
        fail = not failed and (is_ready == (stage == 'ready'))
        failed = failed or fail
        body = json.dumps({'run': 'a' * 32, 'ready': True}).encode() if is_ready else template
        return httpx.Response(status if fail else 200, content=b'cloudflare unavailable' if fail else body,
                              request=httpx.Request('GET', url))

    children = []
    repair.setup.connect_acceptance_tunnel(SimpleNamespace(get=get), children,
                                           {'PHASE6_ACCEPTANCE_RUN': 'a' * 32, 'PHASE6_ACCEPTANCE_WORKER': WORKER})
    assert children == [child] and clock[0] == 1
    assert calls[-2:] == [api + '/phase6-acceptance/ready', api + '/phase6-acceptance/reconnect.ps1']
    out = capsys.readouterr().out
    assert 'Get-FileHash' in out and f"-ExpectedWorkerId '{WORKER}'" in out
    assert '-Reenroll' not in out  # a replacement tunnel never prints the rollout block


def first_start_get(template, firewall):
    def get(url, **kwargs):
        if url.endswith('/ready'):
            body = json.dumps({'run': 'a' * 32, 'ready': True}).encode()
        else:
            body = firewall if url.endswith('/firewall.ps1') else template
        return httpx.Response(200, content=body, request=httpx.Request('GET', url))
    return get


def test_first_start_prints_the_reenroll_and_firewall_blocks_without_a_worker(tunnel_check, capsys):
    import hashlib
    child, api, clock, template = tunnel_check
    firewall = repair.setup.FIREWALL_SCRIPT.read_bytes()
    repair.setup.connect_acceptance_tunnel(SimpleNamespace(get=first_start_get(template, firewall)), [],
                                           {'PHASE6_ACCEPTANCE_RUN': 'a' * 32, 'PHASE6_ACCEPTANCE': 'true'})
    out = capsys.readouterr().out
    assert 'bdr-personal-reconnect.ps1' not in out and 'No approved personal worker yet' in out
    assert '-Pool user -Reenroll' in out and api in out
    assert '/phase6-acceptance/firewall.ps1' in out and hashlib.sha256(firewall).hexdigest() in out
    assert out.index('-Reenroll') < out.index('firewall.ps1')


def test_a_changed_firewall_script_stops_before_any_block(tunnel_check, capsys):
    child, api, clock, template = tunnel_check
    with pytest.raises(repair.setup.SetupError, match='firewall'):
        repair.setup.connect_acceptance_tunnel(SimpleNamespace(get=first_start_get(template, b'tampered')), [],
                                               {'PHASE6_ACCEPTANCE_RUN': 'a' * 32, 'PHASE6_ACCEPTANCE': 'true'})
    out = capsys.readouterr().out
    assert 'Get-FileHash' not in out and '-Reenroll' not in out


@pytest.mark.parametrize('failure', ['persistent530', '401', 'redirect', 'wrong_run', 'wrong_script'])
def test_failed_verification_never_prints_reconnect_command(tunnel_check, capsys, failure):
    child, api, clock, template = tunnel_check

    def get(url, **kwargs):
        if failure == 'persistent530':
            clock[0] += 60
            status, body = 530, b'unavailable'
        elif failure in ('401', 'redirect'):
            status, body = (401 if failure == '401' else 302), b'refused'
        elif url.endswith('/ready'):
            status, body = 200, json.dumps({'run': ('b' if failure == 'wrong_run' else 'a') * 32, 'ready': True}).encode()
        else:
            status, body = 200, b'wrong script' if failure == 'wrong_script' else template
        return httpx.Response(status, content=body, request=httpx.Request('GET', url))

    with pytest.raises((repair.setup.SetupError, repair.preparation.PreparationError)):
        repair.setup.connect_acceptance_tunnel(SimpleNamespace(get=get), [], {'PHASE6_ACCEPTANCE_RUN': 'a' * 32,
                                                                              'PHASE6_ACCEPTANCE_WORKER': WORKER,
                                                                              'PHASE6_ACCEPTANCE': 'true'})
    out = capsys.readouterr().out
    assert 'Get-FileHash' not in out and '-Reenroll' not in out
    assert clock[0] <= 182

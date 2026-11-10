"""Regional authority remains explicit and job-bound without live I/O."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import assistant_browser_sites as sites
from app.services import assistant_browser_runtime as runtime
from app.services import assistant_browser_vault as vault
from app.services.browser_agents import protocol
from tests.test_assistant_browser_runtime import env as runtime_env, _model

env = runtime_env

REAL_JOB_CREDENTIAL = vault.job_credential

URL = ('https://us02.procore.com/webclients/host/companies/562949953430361/'
       'projects/562949955106559/tools/hubs/project-hub/views/overview')


def test_regional_project_hub_read_ceiling_is_exact():
    assert sites.validate_url('procore', URL) == URL
    for url in (URL.replace('us02', 'us01'), URL + '/edit',
                URL.replace('/overview', '/settings'), URL + '?company=other',
                URL.replace('us02.procore.com', 'us02.procore.com.evil.test')):
        with pytest.raises(ValueError):
            sites.validate_url('procore', url)


def test_existing_credential_does_not_gain_region_from_code_change():
    credential = {'origins': ['https://app.procore.com', 'https://login.procore.com']}
    assert sites.credential_allows_url(credential, 'procore', 'https://app.procore.com/123/project/home')
    assert not sites.credential_allows_url(credential, 'procore', URL)
    assert not sites.credential_allows_url({}, 'procore', URL)
    assert credential['origins'] == ['https://app.procore.com', 'https://login.procore.com']


@pytest.mark.parametrize('host', ['app.procore.com', 'us02.procore.com'])
def test_identity_origin_is_bound_to_approved_host(env, monkeypatch, host):
    env.job['input']['url'] = URL
    seen = []
    monkeypatch.setattr(runtime.repo, 'browser_rpc', lambda *a: seen.append(a) or {'ok': True})
    request = protocol.UserSessionRequest(host=host, origin=f'https://{host}')
    if host == 'app.procore.com':
        with pytest.raises(runtime.BrowserUnavailable):
            runtime.identity_session(env.lease, request)
        with pytest.raises(runtime.BrowserUnavailable):
            runtime.identity_begin(env.lease, protocol.UserLoginBeginRequest(
                host=host, origin='https://login.procore.com'))
        assert seen == []
    else:
        assert runtime.identity_session(env.lease, request) == {'status': 'no_jar'}
        runtime.identity_begin(env.lease, protocol.UserLoginBeginRequest(
            host=host, origin='https://login.procore.com'))
        assert len(seen) == 2 and seen[0][2]['p_origin'] == 'https://us02.procore.com'


@pytest.mark.parametrize('domain,allowed', [
    ('us02.procore.com', True), ('login.procore.com', True), ('.procore.com', True),
    ('procore.com', True), ('app.procore.com', False), ('us01.procore.com', False),
    ('.us02.procore.com', False), ('us02.procore.com.evil.test', False),
])
def test_cookie_scope_is_region_specific(domain, allowed):
    cookies = [{'name': 'session', 'value': 'secret', 'domain': domain, 'secure': True}]
    if allowed:
        assert runtime.checked_cookies(cookies, 'https://us02.procore.com')[0]['domain'] == domain
    else:
        with pytest.raises(runtime.BrowserUnavailable):
            runtime.checked_cookies(cookies, 'https://us02.procore.com')


def test_successful_regional_login_stores_bound_encrypted_session(env, monkeypatch):
    env.job['input']['url'] = URL
    seen = []
    monkeypatch.setattr(runtime.repo, 'browser_rpc', lambda *a: seen.append(a) or {'ok': True})
    args = {'attempt_id': str(uuid4()), 'outcome': 'ok', 'origin': 'https://us02.procore.com',
            'cookies': [{'name': 'session', 'value': 'regional_cookie_secret',
                         'domain': 'us02.procore.com', 'secure': True}]}
    runtime.identity_finish(env.lease, protocol.UserLoginResultRequest(**args))
    row = seen[-1][2]['p_session']
    assert row['origin'] == 'https://us02.procore.com' and 'regional_cookie_secret' not in str(row)
    restored = {**row, **row['envelope'], 'version': 1, 'credential_id': env.credential['id'],
                'credential_version': 1}
    assert runtime.decode_session(env.owner, restored)[0]['value'] == 'regional_cookie_secret'
    for origin in ('https://app.procore.com', 'https://login.procore.com'):
        with pytest.raises(runtime.BrowserUnavailable):
            runtime.identity_finish(env.lease, protocol.UserLoginResultRequest(**{**args, 'origin': origin}))
    assert len(seen) == 1


def test_session_rpc_cannot_return_another_region(env, monkeypatch):
    env.job['input']['url'] = URL
    monkeypatch.setattr(runtime.repo, 'browser_rpc', lambda *a: {
        'ok': True, 'session': {'origin': 'https://app.procore.com'}})
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.identity_session(env.lease, protocol.UserSessionRequest(
            host='us02.procore.com', origin='https://us02.procore.com'))


@pytest.mark.parametrize('action,allowed', [
    (('navigate', {'url': URL}), True),
    (('navigate', {'url': URL.replace('overview', 'overview/edit')}), False),
    (('navigate', {'url': URL.replace('us02.', 'app.')}), False),
    (('navigate', {'url': URL.replace('562949953430361', '562949953430362')}), False),
    (('navigate', {'url': URL.replace('562949955106559', '562949955106560')}), False),
    (('navigate', {'url': 'https://us02.procore.com/562949955106559/project/home'}), False),
    (('ensure_login', {'host': 'us02.procore.com'}), True),
    (('ensure_login', {'host': 'app.procore.com'}), False),
])
def test_regional_model_action_is_bound_to_host_company_project(env, monkeypatch, action, allowed):
    env.job['input']['url'] = URL
    _model(monkeypatch, action)
    monkeypatch.setattr(runtime.repo, 'browser_rpc', lambda owner, name, params:
        {'claimed': True, 'turn_id': str(uuid4()), 'token': 'token', 'type': env.kind}
        if name.endswith('_begin') else {'ok': True})
    result = runtime.turn(env.lease, protocol.TurnRequest(prev_turn_no=0))[1]
    assert bool(result.get('action')) is allowed
    if not allowed:
        assert result['stop'] is True


def test_regional_cookie_redaction_loads_only_job_region(env, monkeypatch):
    env.job['input']['url'] = URL
    seen = []
    monkeypatch.setattr(runtime.repo, 'browser_session', lambda *a: seen.append(a) or None)
    runtime.sanitize(env.job, {'text': 'Observation'})
    assert len(seen) == 1 and seen[0][-1] == 'https://us02.procore.com'


def test_vault_refuses_old_region_authority_before_decrypt(env, monkeypatch):
    from app.assistant import access
    monkeypatch.setattr(access, 'owner_check', lambda *a, **kw: SimpleNamespace(problem=None))
    env.credential.update(service='procore', origins=['https://app.procore.com', 'https://login.procore.com'])
    env.job['input']['url'] = URL
    # Fixture bypasses the shared vault fence for runtime isolation; restore its real function.
    with pytest.raises(vault.VaultUnavailable):
        REAL_JOB_CREDENTIAL(env.job)
    env.credential['origins'] = list(sites.site('procore').origins)
    assert REAL_JOB_CREDENTIAL(env.job) is env.credential


@pytest.mark.parametrize('change', [{'version': 2}, {'origins': ['https://app.procore.com']}, {'status': 'revoked'}])
def test_login_release_rechecks_granted_credential_scope(env, monkeypatch, change):
    env.job['input']['url'] = URL
    monkeypatch.setattr(runtime.repo, 'browser_rpc', lambda *a: {
        'granted': True, 'attempt_id': str(uuid4()), 'credential': {**env.credential, **change}})
    monkeypatch.setattr(vault, 'decrypt_credential', lambda *a: pytest.fail('must not release password'))
    with pytest.raises(runtime.BrowserUnavailable):
        runtime.identity_begin(env.lease, protocol.UserLoginBeginRequest(
            host='us02.procore.com', origin='https://login.procore.com'))
